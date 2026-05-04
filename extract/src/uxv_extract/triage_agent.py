"""Triage pre-pass for the products extraction.

Runs a separate agent invocation that identifies every named product on
the vendor's site and stack-ranks them by relevance to characterizing
the UxV industrial supplier base in the US and allied countries. The
runner truncates the list at `max_products` (default 15) and passes
that fixed list to the products pass for full extraction.

Why this pass exists: the products pass on rich corpora (Honeywell,
igus, Adv Nav) routinely emits 15–30 products. Combined with full
per-product structure (descriptor, evidence with line ranges, etc.) the
final tool call payload approaches Sonnet's 32 000-token output cap and
hits it on the largest vendors. Bounding the list at 15 keeps every
products pass safely under that ceiling AND focuses extraction effort
on the products that matter for the supplier directory.

The triage agent does NOT cite evidence — it only identifies and ranks.
The downstream products pass verifies each entry with Citation
line-ranges; if the triage agent named a product that isn't actually in
the corpus, the products pass surfaces it.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
from collections.abc import AsyncIterator, Awaitable
from importlib import resources
from pathlib import Path
from typing import Any, Callable

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
from .agent import (
    SDKSubprocessError,
    read_latest_session_diagnostic,
)
from .corpus import CorpusReader
from .schema import (
    Profile,
    ProfileMeta,
    ProductPriorityList,
    ProductPrioritySubmission,
)


SUBMIT_TOOL_NAME = "submit_product_priority"
MCP_SERVER_NAME = "uxv_extract_triage"
SUBMIT_TOOL_FQN = f"mcp__{MCP_SERVER_NAME}__{SUBMIT_TOOL_NAME}"

DEFAULT_MAX_PRODUCTS = 15

QueryFn = Callable[..., AsyncIterator[Any]]


def load_system_prompt() -> str:
    return resources.files("uxv_extract.prompts").joinpath("triage.md").read_text()


def _profile_context(profile_path: Path | None) -> str:
    if profile_path is None or not profile_path.exists():
        return ""
    try:
        profile = Profile.model_validate_json(profile_path.read_text())
    except (ValidationError, json.JSONDecodeError):
        return ""
    role = profile.drone_supply_chain_role.value or "(unknown)"
    cats = [c.category for c in profile.products_categories.items if c.is_primary]
    primary = cats[0] if cats else "(no primary)"
    return (
        f"\n\nVENDOR-PASS CONTEXT:\n"
        f"  role: {role}\n"
        f"  primary category: {primary}\n"
    )


def build_user_prompt(
    corpus: CorpusReader,
    *,
    max_products: int,
    profile_path: Path | None = None,
) -> str:
    fetched = corpus.fetched_resources()
    return (
        f"Vendor: {corpus.display_name}\n"
        f"target_id: {corpus.target_id}\n"
        f"homepage_url: {corpus.homepage_url}\n"
        f"corpus_root: {corpus.corpus_root}\n"
        f"\n"
        f"Mirror coverage: {len(fetched)} fetched pages, "
        f"{corpus.total_text_chars} chars.\n"
        f"\n"
        f"max_products = {max_products}. The runner will truncate your "
        f"list at this number; rank the most-supplier-relevant products "
        f"first.\n"
        f"\n"
        f"Identify every named product, rank each 1-10 against the "
        f"UxV-supplier-characterization rubric, then submit a single "
        f"`submit_product_priority` call."
        f"{_profile_context(profile_path)}"
    )


def _build_submit_tool() -> Any:
    schema = ProductPrioritySubmission.model_json_schema()

    @tool(
        SUBMIT_TOOL_NAME,
        (
            "Submit the prioritized product list. Each entry is "
            "{name, relevance_score (1-10), rationale}. Order most "
            "relevant first. The runner truncates at max_products. "
            "Submit IMMEDIATELY after your last Read — no preamble."
        ),
        schema,
    )
    async def submit_product_priority(args: dict[str, Any]) -> dict[str, Any]:
        try:
            ProductPrioritySubmission.model_validate(args)
        except ValidationError as exc:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Triage validation failed. Fix every listed "
                            f"error and call again.\n\n{exc}"
                        ),
                    }
                ],
                "is_error": True,
            }
        return {
            "content": [{"type": "text", "text": "Priority list recorded."}]
        }

    return submit_product_priority


def build_options(
    corpus: CorpusReader,
    *,
    model: str,
    max_turns: int,
    max_cost_usd: float | None = None,
    stderr_sink: Callable[[str], None] | None = None,
) -> ClaudeAgentOptions:
    submit_tool = _build_submit_tool()
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


async def run_triage(
    corpus: CorpusReader,
    *,
    model: str,
    max_turns: int = 30,
    max_cost_usd: float | None = None,
    timeout_sec: float | None = None,
    max_products: int = DEFAULT_MAX_PRODUCTS,
    profile_path: Path | None = None,
    query_fn: QueryFn | None = None,
) -> ProductPriorityList:
    """Run the triage agent and return a `ProductPriorityList`,
    truncated at `max_products`. Set `max_products=0` to disable
    truncation (the agent's full list is preserved).

    On per-vendor cap (timeout / SDK budget exhaustion / mid-session
    cancel), returns the most recent successfully-validated submission
    with `status="partial"`. Never raises TimeoutError. If the agent
    never submitted at all before the cap, raises (an empty triage
    list signals nothing useful)."""
    return await _run_inner(
        corpus,
        model=model,
        max_turns=max_turns,
        max_cost_usd=max_cost_usd,
        timeout_sec=timeout_sec,
        max_products=max_products,
        profile_path=profile_path,
        query_fn=query_fn,
    )


async def _run_inner(
    corpus: CorpusReader,
    *,
    model: str,
    max_turns: int,
    max_cost_usd: float | None,
    timeout_sec: float | None,
    max_products: int,
    profile_path: Path | None,
    query_fn: QueryFn | None,
) -> ProductPriorityList:
    stderr_lines: list[str] = []
    options = build_options(
        corpus,
        model=model,
        max_turns=max_turns,
        max_cost_usd=max_cost_usd,
        stderr_sink=stderr_lines.append,
    )
    user_prompt = build_user_prompt(
        corpus, max_products=max_products, profile_path=profile_path
    )

    q = query_fn if query_fn is not None else sdk_query

    # Latest-wins: every successful validation overwrites `submission`.
    submission: ProductPrioritySubmission | None = None
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
                            submission = ProductPrioritySubmission.model_validate(
                                block.input
                            )  # latest wins
                        except ValidationError as exc:
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
        sd = read_latest_session_diagnostic(corpus.corpus_root)
        raise SDKSubprocessError(
            exc, "\n".join(stderr_lines), session_diagnostic=sd
        ) from exc

    if submission is None:
        if submit_attempts > 0:
            raise ValueError(
                f"agent called {SUBMIT_TOOL_FQN} {submit_attempts} time(s) "
                f"but every call failed validation. Last error:\n"
                f"{last_validation_error}"
            )
        raise RuntimeError(
            f"agent did not call {SUBMIT_TOOL_FQN}; cannot triage products"
        )

    meta = ProfileMeta(
        model=model,
        num_turns=result.num_turns if result else 0,
        total_cost_usd=result.total_cost_usd if result else None,
        created_at=_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        extract_version=__version__,
    )
    return ProductPriorityList.from_submission(
        submission,
        target_id=corpus.target_id,
        run_id=corpus.run_id,
        display_name=corpus.display_name,
        homepage_url=corpus.homepage_url,
        corpus_root=str(corpus.corpus_root),
        max_products=max_products,
        meta=meta,
        status="partial" if capped else "complete",
    )
