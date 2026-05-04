"""Single-agent extraction loop.

The agent runs in CWD = the corpus directory, with built-in `Read`, `Glob`,
`Grep` tools plus a custom `submit_profile` MCP tool that the agent must call
exactly once with the final structured answer.

The runner does NOT rely on the MCP tool callback to capture state. Instead
it scans the message stream for `ToolUseBlock`s named
`mcp__uxv_extract__submit_profile` and validates the input dict against
`ProfileSubmission`. This keeps the runner testable without standing up the
MCP runtime.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
from collections.abc import AsyncIterator, Awaitable, Callable
from importlib import resources
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    ToolUseBlock,
    create_sdk_mcp_server,
    query as sdk_query,
    tool,
)
from pydantic import ValidationError

from . import __version__
from .corpus import CorpusReader, Resource
from .schema import (
    Answer,
    Citation,
    ListAnswer,
    Profile,
    ProfileMeta,
    ProfileSubmission,
)


# Hard cap on snippet word count after extraction. A line-based citation
# that pulls more than this is almost always too coarse — the agent should
# pick a tighter range.
SNIPPET_MAX_WORDS = 60


class CitationHydrationError(ValueError):
    """Raised when a citation's line range cannot be resolved against the
    corpus (missing resource, out-of-range lines, oversized snippet, etc.)."""


SUBMIT_TOOL_NAME = "submit_profile"
MCP_SERVER_NAME = "uxv_extract"
SUBMIT_TOOL_FQN = f"mcp__{MCP_SERVER_NAME}__{SUBMIT_TOOL_NAME}"


QueryFn = Callable[..., AsyncIterator[Any]]


# ---------------------------------------------------------------------------
# Subprocess error capture and classification.
#
# The Claude Agent SDK shells out to the `claude` CLI, which can fail with an
# opaque "Command failed with exit code 1" exception that the SDK's own
# ProcessError.stderr does NOT contain the real subprocess stderr. We attach
# our own `stderr` callback to ClaudeAgentOptions to capture every line, then
# raise SDKSubprocessError with the real stderr attached.
# ---------------------------------------------------------------------------


class SDKSubprocessError(RuntimeError):
    """Raised when the Claude CLI subprocess fails. Carries the real
    stderr lines captured via the `options.stderr` callback AND, where
    available, the last assistant message's stop_reason / output_tokens
    pulled from the SDK's session transcript so callers can distinguish
    e.g. `max_tokens` (output cap hit) from balance / auth / network.

    Why: SDK's ProcessError hardcodes `stderr="Check stderr output for
    details"` and many failure modes (`stop_reason: max_tokens` with no
    tool call, end_turn-without-submit, etc.) write nothing to stderr.
    The real diagnostic is in `~/.claude/projects/<cwd>/<session>.jsonl`.
    """

    def __init__(
        self,
        original: BaseException,
        stderr_text: str,
        *,
        session_diagnostic: dict | None = None,
    ) -> None:
        self.original = original
        self.stderr_text = stderr_text
        self.session_diagnostic = session_diagnostic or {}
        msg = f"{type(original).__name__}: {original}"
        sd = self.session_diagnostic
        if sd:
            msg += "\n--- session diagnostic ---"
            if sd.get("stop_reason"):
                msg += f"\n  stop_reason: {sd['stop_reason']}"
            if sd.get("output_tokens") is not None:
                msg += f"\n  output_tokens: {sd['output_tokens']}"
            if sd.get("num_turns") is not None:
                msg += f"\n  num_assistant_messages: {sd['num_turns']}"
            if sd.get("last_text"):
                msg += f"\n  last_text: {sd['last_text']!r}"
            if sd.get("session_path"):
                msg += f"\n  session_path: {sd['session_path']}"
        if stderr_text:
            tail = stderr_text[-2000:]
            msg += f"\n--- captured stderr (last {len(tail)} chars) ---\n{tail}"
        super().__init__(msg)


def _session_project_dir_for_cwd(cwd: Path) -> Path:
    """The Claude Code SDK writes session transcripts to
    `~/.claude/projects/<sanitized-cwd>/<session-id>.jsonl` where the
    sanitized cwd is the absolute path with `/` replaced by `-`.
    """
    sanitized = str(Path(cwd).resolve()).replace("/", "-")
    return Path.home() / ".claude" / "projects" / sanitized


def read_latest_session_diagnostic(cwd: Path) -> dict | None:
    """Find the most-recently-modified session jsonl for `cwd` and pull
    diagnostics from the last assistant message. Returns None if no
    session file exists or can't be parsed.

    The returned dict (when non-None) carries:
      - session_path: str
      - stop_reason: str | None
      - output_tokens: int | None
      - num_turns: int (count of assistant messages in the session)
      - last_text: str | None  (last 200 chars of last assistant text)
    """
    import json as _json
    sdir = _session_project_dir_for_cwd(cwd)
    if not sdir.is_dir():
        return None
    sessions = sorted(
        sdir.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not sessions:
        return None
    latest = sessions[0]
    last_assistant = None
    assistant_count = 0
    try:
        with latest.open() as f:
            for line in f:
                try:
                    d = _json.loads(line)
                except _json.JSONDecodeError:
                    continue
                if d.get("type") == "assistant":
                    assistant_count += 1
                    last_assistant = d
    except OSError:
        return None
    if not last_assistant:
        return {
            "session_path": str(latest),
            "stop_reason": None,
            "output_tokens": None,
            "num_turns": 0,
            "last_text": None,
        }
    msg = last_assistant.get("message", {})
    if not isinstance(msg, dict):
        msg = {}
    usage = msg.get("usage") or {}
    last_text = None
    for c in msg.get("content", []) or []:
        if isinstance(c, dict) and c.get("type") == "text":
            last_text = (c.get("text") or "")[:200]
            break
    return {
        "session_path": str(latest),
        "stop_reason": msg.get("stop_reason"),
        "output_tokens": usage.get("output_tokens"),
        "num_turns": assistant_count,
        "last_text": last_text,
    }


# Phrases observed in stderr that indicate the API balance / billing has
# failed. Not exhaustive; add as we encounter new variants.
_BALANCE_INDICATORS = (
    "credit balance is too low",
    "credit balance too low",
    "insufficient credits",
    "insufficient_credits",
    "insufficient_funds",
    "your credit balance is below",
    "billing",
)


def is_fatal_balance_error(stderr_text: str) -> bool:
    """Return True if stderr indicates the Anthropic API balance is
    exhausted — retrying is futile until the balance is topped up."""
    if not stderr_text:
        return False
    s = stderr_text.lower()
    return any(ind in s for ind in _BALANCE_INDICATORS)


_AUTH_INDICATORS = (
    "invalid x-api-key",
    "invalid api key",
    "authentication_error",
    "unauthorized",
    "401 unauthorized",
    "403 forbidden",
)


def is_fatal_auth_error(stderr_text: str) -> bool:
    """Return True if stderr indicates an authentication problem
    (missing/invalid ANTHROPIC_API_KEY) — retrying without fixing config
    is futile."""
    if not stderr_text:
        return False
    s = stderr_text.lower()
    return any(ind in s for ind in _AUTH_INDICATORS)


def validate_fetch_requests(
    fetch_requests: list, corpus: CorpusReader
) -> None:
    """Cross-check every FetchRequest's `in_corpus_index` claim against the
    actual `crawl_index` content. Raises `CitationHydrationError` on:

      - `in_corpus_index=true` but URL not in crawl_index (false claim)
      - `in_corpus_index=false` but URL IS in crawl_index (should be true)
      - URL is already in `fetched` status (no point re-requesting)

    Force the agent to grep crawl_index before emitting requests so the
    round-B mirror gets accurate provenance, not unprincipled guesses.
    """
    crawl_urls: dict[str, str] = {}  # url -> status
    for entry in corpus.crawl_index:
        url = entry.get("url")
        if url:
            crawl_urls[url] = entry.get("status", "")

    for fr in fetch_requests:
        url = fr.url
        status = crawl_urls.get(url)

        if status == "fetched":
            raise CitationHydrationError(
                f"fetch_request {url!r} is already in crawl_index with "
                f"status='fetched' — that page has been read; no need to "
                f"request it again"
            )

        if fr.in_corpus_index and status is None:
            raise CitationHydrationError(
                f"fetch_request {url!r} has in_corpus_index=true but the "
                f"URL is NOT in crawl_index. Either set in_corpus_index="
                f"false (it is a fresh guess), or pick a URL that IS in "
                f"crawl_index"
            )

        if not fr.in_corpus_index and status is not None:
            raise CitationHydrationError(
                f"fetch_request {url!r} has in_corpus_index=false but the "
                f"URL IS in crawl_index (status={status!r}). Set "
                f"in_corpus_index=true and update source_hint accordingly"
            )


def hydrate_submission(
    submission: ProfileSubmission, corpus: CorpusReader
) -> None:
    """Fill `snippet`, `url`, and `page_class` on every Citation by reading
    the cited line range from the mirrored corpus. Mutates in place.
    Also validates fetch_requests' in_corpus_index claims.

    Raises `CitationHydrationError` on any unresolvable citation: missing
    resource, missing text file, line range out of bounds, oversized
    snippet, or fetch_request provenance mismatch.
    """
    fields: list[Answer | ListAnswer] = list(submission._answers().values())
    citations: list[tuple[str, Citation]] = []
    for field_name, answer in zip(submission._answers().keys(), fields):
        if isinstance(answer, Answer):
            for c in answer.evidence:
                citations.append((field_name, c))
        else:  # ListAnswer
            for item in answer.items:
                if hasattr(item, "evidence"):
                    for c in item.evidence:
                        citations.append((field_name, c))

    for field_name, c in citations:
        resource = corpus.resource_by_id(c.resource_id)
        if resource is None:
            raise CitationHydrationError(
                f"{field_name}: citation resource_id={c.resource_id!r} "
                f"is not a fetched resource in this corpus"
            )
        if resource.text_path is None or not resource.text_path.exists():
            raise CitationHydrationError(
                f"{field_name}: citation resource_id={c.resource_id!r} "
                f"has no text/ artifact (kind={resource.page_class!r})"
            )
        text_lines = resource.text_path.read_text().splitlines()
        if c.line_end > len(text_lines):
            raise CitationHydrationError(
                f"{field_name}: citation {c.resource_id} line_end={c.line_end} "
                f"is past end of file (file has {len(text_lines)} lines)"
            )
        snippet = "\n".join(text_lines[c.line_start - 1 : c.line_end])
        word_count = len(snippet.split())
        if word_count == 0:
            raise CitationHydrationError(
                f"{field_name}: citation {c.resource_id} "
                f"L{c.line_start}-{c.line_end} extracts no words"
            )
        if word_count > SNIPPET_MAX_WORDS:
            if c.line_start == c.line_end:
                # The cited line is itself a single block of >cap words —
                # usually a whole paragraph that the text extractor
                # serialised onto one line. The agent CANNOT make this
                # tighter via line range. They must pick a DIFFERENT
                # line that conveys the same claim concisely, or split
                # this evidence into multiple shorter citations
                # pointing at different lines.
                raise CitationHydrationError(
                    f"{field_name}: citation {c.resource_id} "
                    f"L{c.line_start} is a single line of {word_count} "
                    f"words (cap is {SNIPPET_MAX_WORDS}). A single-line "
                    f"range CANNOT be made tighter. Either pick a "
                    f"DIFFERENT line that backs the same claim more "
                    f"concisely (use Read/Grep to scan adjacent lines), "
                    f"or use multiple smaller citations on different "
                    f"lines — DO NOT submit the same line again."
                )
            raise CitationHydrationError(
                f"{field_name}: citation {c.resource_id} "
                f"L{c.line_start}-{c.line_end} produces {word_count} words "
                f"(cap is {SNIPPET_MAX_WORDS}); pick a tighter range "
                f"(narrow line_start/line_end, or split into multiple "
                f"smaller citations)"
            )
        c.snippet = snippet
        c.url = resource.final_url or resource.url
        c.page_class = resource.page_class

    validate_fetch_requests(submission.fetch_requests, corpus)


def load_system_prompt() -> str:
    return resources.files("uxv_extract.prompts").joinpath("system.md").read_text()


def build_user_prompt(corpus: CorpusReader) -> str:
    fetched = corpus.fetched_resources()
    skipped = corpus.skipped_resources()
    page_class_counts: dict[str, int] = {}
    for r in fetched:
        page_class_counts[r.page_class] = page_class_counts.get(r.page_class, 0) + 1
    pc_summary = ", ".join(
        f"{c}={n}" for c, n in sorted(page_class_counts.items())
    ) or "(none)"

    return (
        f"Vendor: {corpus.display_name}\n"
        f"target_id: {corpus.target_id}\n"
        f"homepage_url: {corpus.homepage_url}\n"
        f"run_id: {corpus.run_id}\n"
        f"corpus_root: {corpus.corpus_root}\n"
        f"\n"
        f"Mirror coverage: status={corpus.quality_status}, "
        f"total_text_chars={corpus.total_text_chars}, "
        f"fetched_pages={len(fetched)}, "
        f"skipped_or_failed={len(skipped)}.\n"
        f"Fetched page_classes: {pc_summary}.\n"
        f"\n"
        f"Read crawl_index.json to see exactly which URLs were fetched vs "
        f"skipped, then read the relevant text/ files. Submit a single "
        f"`submit_profile` call when done."
    )


def _build_submit_tool(corpus: CorpusReader) -> Any:
    schema = ProfileSubmission.model_json_schema()

    @tool(
        SUBMIT_TOOL_NAME,
        (
            "Submit the final structured supplier profile. Call this once you "
            "have your final answer. Each citation must include line_start "
            "and line_end (1-indexed inclusive); the runner extracts the "
            "snippet from the cited resource's text/ file. If validation or "
            "hydration fails, the call returns an error and you should fix "
            "the listed problems and call again."
        ),
        schema,
    )
    async def submit_profile(args: dict[str, Any]) -> dict[str, Any]:
        try:
            sub = ProfileSubmission.model_validate(args)
            hydrate_submission(sub, corpus)
        except (ValidationError, CitationHydrationError) as exc:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Profile validation failed. Fix every listed "
                            "error and call submit_profile again.\n\n"
                            f"{exc}"
                        ),
                    }
                ],
                "is_error": True,
            }
        return {
            "content": [
                {"type": "text", "text": "Profile recorded."}
            ]
        }

    return submit_profile


def build_options(
    corpus: CorpusReader,
    *,
    model: str,
    max_turns: int,
    max_cost_usd: float | None = None,
    stderr_sink: Callable[[str], None] | None = None,
) -> ClaudeAgentOptions:
    submit_tool = _build_submit_tool(corpus)
    server = create_sdk_mcp_server(
        name=MCP_SERVER_NAME,
        version=__version__,
        tools=[submit_tool],
    )
    return ClaudeAgentOptions(
        system_prompt=load_system_prompt(),
        tools=["Read", "Glob", "Grep"],
        allowed_tools=["Read", "Glob", "Grep", SUBMIT_TOOL_FQN],
        mcp_servers={MCP_SERVER_NAME: server},
        permission_mode="bypassPermissions",
        cwd=str(corpus.corpus_root),
        model=model,
        max_turns=max_turns,
        max_budget_usd=max_cost_usd,
        setting_sources=[],
        stderr=stderr_sink,
    )


async def run_profile_extraction(
    corpus: CorpusReader,
    *,
    model: str,
    max_turns: int = 30,
    max_cost_usd: float | None = None,
    timeout_sec: float | None = None,
    query_fn: QueryFn | None = None,
) -> Profile:
    """Run the agent against `corpus` and return the validated `Profile`.

    Guardrails:
      - `max_turns`: caps round trips between agent and tools.
      - `max_cost_usd`: hard-stops the conversation at that USD spend
        (enforced by the SDK via ClaudeAgentOptions.max_budget_usd).
      - `timeout_sec`: wall-clock ceiling on the entire extraction.

    On per-vendor cap (timeout / SDK budget exhaustion / mid-session
    cancel), returns the most recent successfully-validated submission
    with `status="partial"`. Never raises TimeoutError. If the agent
    never submitted at all before the cap, raises RuntimeError —
    that's a prompt-engineering bug, not normal cap behavior.

    `query_fn` is injectable for tests; in production we use the SDK's
    `query()` directly.
    """
    return await _run_inner(
        corpus,
        model=model,
        max_turns=max_turns,
        max_cost_usd=max_cost_usd,
        timeout_sec=timeout_sec,
        query_fn=query_fn,
    )


async def _run_inner(
    corpus: CorpusReader,
    *,
    model: str,
    max_turns: int,
    max_cost_usd: float | None,
    timeout_sec: float | None,
    query_fn: QueryFn | None,
) -> Profile:
    stderr_lines: list[str] = []
    options = build_options(
        corpus,
        model=model,
        max_turns=max_turns,
        max_cost_usd=max_cost_usd,
        stderr_sink=stderr_lines.append,
    )
    user_prompt = build_user_prompt(corpus)

    q = query_fn if query_fn is not None else sdk_query

    # Latest-wins: every successful validation overwrites `submission`.
    # The agent is instructed to submit early and refine — so the LAST
    # validated submission is the one with the most evidence accrued.
    submission: ProfileSubmission | None = None
    last_validation_error: str | None = None
    submit_attempts = 0
    result: ResultMessage | None = None
    capped = False

    async def _drive() -> None:
        nonlocal submission, last_validation_error, submit_attempts, result
        async for message in q(prompt=user_prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if (
                        isinstance(block, ToolUseBlock)
                        and block.name == SUBMIT_TOOL_FQN
                    ):
                        submit_attempts += 1
                        try:
                            candidate = ProfileSubmission.model_validate(
                                block.input
                            )
                            hydrate_submission(candidate, corpus)
                            submission = candidate  # latest wins
                        except (ValidationError, CitationHydrationError) as exc:
                            last_validation_error = str(exc)
            elif isinstance(message, ResultMessage):
                result = message
            elif isinstance(message, SystemMessage):
                pass

    try:
        if timeout_sec is None:
            await _drive()
        else:
            await asyncio.wait_for(_drive(), timeout=timeout_sec)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        capped = True
    except SDKSubprocessError:
        raise
    except BaseException as exc:
        # Wrap any subprocess failure with the captured stderr AND the
        # session-transcript diagnostic so callers can classify
        # (balance / auth / max_tokens / unknown) and respond. We do NOT
        # catch ValidationError or CitationHydrationError here — those
        # arise inside our own validation block above and never propagate
        # out of the iteration.
        sd = read_latest_session_diagnostic(corpus.corpus_root)
        raise SDKSubprocessError(
            exc, "\n".join(stderr_lines), session_diagnostic=sd
        ) from exc

    if submission is None:
        # Cap fired before any submission, or run completed with no
        # submission. Either way an empty profile is meaningless —
        # raise so the caller (batch) knows this vendor produced
        # nothing. (Cap-fired-before-submission IS a prompt-engineering
        # signal: the agent isn't following "submit early" guidance.)
        if submit_attempts > 0:
            raise ValueError(
                f"agent called {SUBMIT_TOOL_FQN} {submit_attempts} time(s) "
                f"but every call failed validation. Last error:\n"
                f"{last_validation_error}"
            )
        raise RuntimeError(
            f"agent did not call {SUBMIT_TOOL_FQN}; cannot extract profile"
        )

    if capped:
        # Override whatever the agent's status was; the run was capped,
        # so anything they submitted is by definition partial.
        submission = submission.model_copy(update={"status": "partial"})

    meta = ProfileMeta(
        model=model,
        num_turns=result.num_turns if result else 0,
        total_cost_usd=result.total_cost_usd if result else None,
        created_at=_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        extract_version=__version__,
    )
    return Profile.from_submission(
        submission,
        target_id=corpus.target_id,
        run_id=corpus.run_id,
        display_name=corpus.display_name,
        homepage_url=corpus.homepage_url,
        corpus_root=str(corpus.corpus_root),
        meta=meta,
    )
