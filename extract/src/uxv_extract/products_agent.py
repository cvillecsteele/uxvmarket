"""Products-pass extraction loop.

Mirrors `agent.py` (vendor pass) but:
  - uses incremental submission: agent calls `add_product` once per
    product as it researches them, then `finalize_product_catalog` to
    declare completion. Server-side accumulator holds the validated
    list. This means a per-vendor cap (timeout / max_cost_usd) firing
    mid-session still produces a `products.json` with whatever was
    submitted before the cap, marked `status="partial"` — never an
    empty file, never a "rerun with bigger budget" mop-up loop.
  - validates each `ProductDetail` independently (instead of a whole
    `ProductCatalogSubmission` at the end)
  - reads `profile.json` if it exists in the extract output dir and
    prepends a one-line vendor-context summary to the user prompt

Citation hydration, retry-on-validation-failure, per-vendor caps, and
runner-side message-stream capture all reuse `agent.py` helpers.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Callable
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
from .agent import (
    CitationHydrationError,
    SDKSubprocessError,
    SNIPPET_MAX_WORDS,
    read_latest_session_diagnostic,
    validate_fetch_requests,
)
from .corpus import CorpusReader
from .schema import (
    Citation,
    FetchRequest,
    ProductCatalog,
    ProductCatalogSubmission,
    ProductDetail,
    Profile,
    ProfileMeta,
    ProfileStatus,
)


ADD_PRODUCT_TOOL_NAME = "add_product"
FINALIZE_TOOL_NAME = "finalize_product_catalog"
MCP_SERVER_NAME = "uxv_extract_products"
ADD_PRODUCT_TOOL_FQN = f"mcp__{MCP_SERVER_NAME}__{ADD_PRODUCT_TOOL_NAME}"
FINALIZE_TOOL_FQN = f"mcp__{MCP_SERVER_NAME}__{FINALIZE_TOOL_NAME}"


QueryFn = Callable[..., AsyncIterator[Any]]


@dataclass
class _CatalogAccumulator:
    """Server-side state that lives across all `add_product` /
    `finalize_product_catalog` invocations within one agent session.

    On normal completion, the runner reads `final_status` set by
    `finalize_product_catalog`. On cap-fire (timeout / SDK kill), the
    runner forces `status="partial"` regardless.
    """
    products: list[ProductDetail] = field(default_factory=list)
    unresolved_questions: list[str] = field(default_factory=list)
    fetch_requests: list[FetchRequest] = field(default_factory=list)
    finalized: bool = False
    final_status: ProfileStatus | None = None


def load_system_prompt() -> str:
    return resources.files("uxv_extract.prompts").joinpath("products.md").read_text()


def _profile_context(profile_path: Path | None) -> str:
    """If `profile.json` exists, summarise the vendor pass as a few prompt
    lines. Returns "" if the profile is absent or unreadable."""
    if profile_path is None or not profile_path.exists():
        return ""
    try:
        profile = Profile.model_validate_json(profile_path.read_text())
    except (ValidationError, json.JSONDecodeError):
        return ""

    role = profile.drone_supply_chain_role.value or "(unknown)"
    cats = [c.category for c in profile.products_categories.items if c.is_primary]
    primary = cats[0] if cats else "(no primary)"
    secondary = [
        c.category for c in profile.products_categories.items if not c.is_primary
    ]
    secondary_str = (
        f" (also: {', '.join(secondary)})" if secondary else ""
    )
    products_seen = [p.name for p in profile.products.items[:10]]
    products_str = (
        f"Vendor pass already named these products: {', '.join(products_seen)}"
        if products_seen
        else "Vendor pass did not enumerate any products."
    )
    return (
        f"\n\nVENDOR-PASS CONTEXT (from existing profile.json):\n"
        f"  role: {role}\n"
        f"  primary category: {primary}{secondary_str}\n"
        f"  {products_str}\n"
        f"\n"
        f"Treat this as a hint, not ground truth. Re-derive products from "
        f"the corpus; you may find more (or fewer) SKUs than the vendor "
        f"pass listed.\n"
    )


def build_user_prompt(
    corpus: CorpusReader,
    *,
    profile_path: Path | None = None,
    priority_names: list[str] | None = None,
) -> str:
    fetched = corpus.fetched_resources()
    skipped = corpus.skipped_resources()
    page_class_counts: dict[str, int] = {}
    for r in fetched:
        page_class_counts[r.page_class] = page_class_counts.get(r.page_class, 0) + 1
    pc_summary = ", ".join(
        f"{c}={n}" for c, n in sorted(page_class_counts.items())
    ) or "(none)"

    if priority_names:
        instruction = (
            f"Extract ProductDetail for the {len(priority_names)} "
            f"product(s) listed below — and ONLY those. The triage pass "
            f"has already ranked these as the most supplier-relevant.\n"
            f"\nPRIORITIZED PRODUCTS:\n"
            + "".join(f"  - {n}\n" for n in priority_names)
            + f"\nDo not add products outside this list. If a listed "
            f"product cannot be located in the corpus, omit it (do not "
            f"call `add_product`) and explain in `unresolved_questions` "
            f"when you call `finalize_product_catalog`. Call `add_product` "
            f"once per located product as you finish researching it.\n"
        )
    else:
        instruction = (
            f"Enumerate every named product, classifying each on category, "
            f"granularity, readiness, ndaa, and blue_uas with cited "
            f"evidence. Call `add_product` once per product as you finish "
            f"researching it; call `finalize_product_catalog` when done.\n"
        )

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
        f"{instruction}"
        f"{_profile_context(profile_path)}"
    )


def _hydrate_one_citation(c: Citation, corpus: CorpusReader, label: str) -> None:
    resource = corpus.resource_by_id(c.resource_id)
    if resource is None:
        raise CitationHydrationError(
            f"{label}: citation resource_id={c.resource_id!r} is not a "
            f"fetched resource in this corpus"
        )
    if resource.text_path is None or not resource.text_path.exists():
        raise CitationHydrationError(
            f"{label}: citation resource_id={c.resource_id!r} has no "
            f"text/ artifact (kind={resource.page_class!r})"
        )
    text_lines = resource.text_path.read_text().splitlines()
    if c.line_end > len(text_lines):
        raise CitationHydrationError(
            f"{label}: citation {c.resource_id} line_end={c.line_end} "
            f"is past end of file (file has {len(text_lines)} lines)"
        )
    snippet = "\n".join(text_lines[c.line_start - 1 : c.line_end])
    word_count = len(snippet.split())
    if word_count == 0:
        raise CitationHydrationError(
            f"{label}: citation {c.resource_id} L{c.line_start}-{c.line_end} "
            f"extracts no words"
        )
    if word_count > SNIPPET_MAX_WORDS:
        if c.line_start == c.line_end:
            raise CitationHydrationError(
                f"{label}: citation {c.resource_id} L{c.line_start} is a "
                f"single line of {word_count} words (cap is "
                f"{SNIPPET_MAX_WORDS}). A single-line range CANNOT be "
                f"made tighter. Either pick a DIFFERENT line that backs "
                f"the same claim more concisely (use Read/Grep to scan "
                f"adjacent lines), or use multiple smaller citations on "
                f"different lines — DO NOT submit the same line again."
            )
        raise CitationHydrationError(
            f"{label}: citation {c.resource_id} L{c.line_start}-{c.line_end} "
            f"produces {word_count} words (cap is {SNIPPET_MAX_WORDS}); "
            f"pick a tighter range (narrow line_start/line_end, or "
            f"split into multiple smaller citations)"
        )
    c.snippet = snippet
    c.url = resource.final_url or resource.url
    c.page_class = resource.page_class


def _hydrate_one_product(
    product: ProductDetail, corpus: CorpusReader, *, index: int
) -> None:
    label = f"products[{index}].{product.name!r}"
    for c in product.evidence:
        _hydrate_one_citation(c, corpus, label)


def hydrate_catalog_submission(
    submission: ProductCatalogSubmission, corpus: CorpusReader
) -> None:
    """Walk every Citation across every ProductDetail in the submission and
    fill snippet / url / page_class from the corpus. Also validate every
    fetch_request's in_corpus_index claim against the real crawl_index.
    Raises on any unresolvable citation or false provenance claim.

    Kept for any callers that still want whole-catalog hydration; the
    incremental tool path uses `_hydrate_one_product` per product."""
    for i, product in enumerate(submission.products):
        _hydrate_one_product(product, corpus, index=i)
    validate_fetch_requests(submission.fetch_requests, corpus)


def _build_incremental_tools(
    corpus: CorpusReader, accumulator: _CatalogAccumulator
) -> tuple[Any, Any]:
    """Return (add_product_tool, finalize_tool). Both close over
    `accumulator` so server-side state survives across many agent
    invocations within one session."""
    product_schema = ProductDetail.model_json_schema()

    @tool(
        ADD_PRODUCT_TOOL_NAME,
        (
            "Append one product to the catalog. Must carry name, "
            "category, descriptor, granularity, readiness, ndaa, "
            "blue_uas, and at least one Citation in evidence. Citations "
            "must include resource_id and line_start/line_end (1-indexed "
            "inclusive); the runner extracts the snippet from text/. If "
            "validation or hydration fails, the call returns an error "
            "and you should fix the listed problems and call again "
            "(only the failing product is rejected; previously-added "
            "products remain in the catalog)."
        ),
        product_schema,
    )
    async def add_product(args: dict[str, Any]) -> dict[str, Any]:
        try:
            candidate = ProductDetail.model_validate(args)
            _hydrate_one_product(
                candidate, corpus, index=len(accumulator.products)
            )
        except (ValidationError, CitationHydrationError) as exc:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Product rejected. Fix every listed error "
                            f"and call add_product again.\n\n{exc}"
                        ),
                    }
                ],
                "is_error": True,
            }
        accumulator.products.append(candidate)
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"Product {len(accumulator.products)} recorded "
                        f"({candidate.name!r})."
                    ),
                }
            ]
        }

    finalize_schema = {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["complete", "partial", "needs_more_fetches", "failed"],
                "description": (
                    "complete = every product on fetched product pages "
                    "is recorded; partial = enumerated what could be "
                    "found but suspect more SKUs on skipped pages; "
                    "needs_more_fetches = corpus has no product pages "
                    "and you need more fetches before enumeration is "
                    "possible; failed = corpus malformed."
                ),
            },
            "unresolved_questions": {
                "type": "array",
                "items": {"type": "string"},
                "default": [],
            },
            "fetch_requests": {
                "type": "array",
                "items": FetchRequest.model_json_schema(),
                "default": [],
            },
        },
        "required": ["status"],
    }

    @tool(
        FINALIZE_TOOL_NAME,
        (
            "Mark the product catalog as finished. Pass status, "
            "unresolved_questions, and any fetch_requests for product "
            "detail pages you'd like fetched in the next mirror pass. "
            "Call this exactly once after all add_product calls."
        ),
        finalize_schema,
    )
    async def finalize_product_catalog(args: dict[str, Any]) -> dict[str, Any]:
        try:
            requests_raw = args.get("fetch_requests", []) or []
            fetch_requests = [
                FetchRequest.model_validate(fr) for fr in requests_raw
            ]
            validate_fetch_requests(fetch_requests, corpus)
        except (ValidationError, CitationHydrationError) as exc:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Finalize rejected (fetch_requests "
                            f"validation):\n\n{exc}"
                        ),
                    }
                ],
                "is_error": True,
            }
        status = args.get("status")
        if status not in ("complete", "partial", "needs_more_fetches", "failed"):
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Finalize rejected: status={status!r} is not "
                            "one of complete/partial/needs_more_fetches/"
                            "failed."
                        ),
                    }
                ],
                "is_error": True,
            }
        accumulator.unresolved_questions = list(
            args.get("unresolved_questions", []) or []
        )
        accumulator.fetch_requests = fetch_requests
        accumulator.final_status = status
        accumulator.finalized = True
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"Catalog finalized with {len(accumulator.products)} "
                        f"product(s), status={status!r}."
                    ),
                }
            ]
        }

    return add_product, finalize_product_catalog


def build_options(
    corpus: CorpusReader,
    *,
    accumulator: _CatalogAccumulator,
    model: str,
    max_turns: int,
    max_cost_usd: float | None = None,
    stderr_sink: Callable[[str], None] | None = None,
) -> ClaudeAgentOptions:
    add_tool, finalize_tool = _build_incremental_tools(corpus, accumulator)
    server = create_sdk_mcp_server(
        name=MCP_SERVER_NAME,
        version=__version__,
        tools=[add_tool, finalize_tool],
    )
    return ClaudeAgentOptions(
        system_prompt=load_system_prompt(),
        tools=["Read", "Glob", "Grep"],
        allowed_tools=[
            "Read", "Glob", "Grep",
            ADD_PRODUCT_TOOL_FQN, FINALIZE_TOOL_FQN,
        ],
        mcp_servers={MCP_SERVER_NAME: server},
        permission_mode="bypassPermissions",
        cwd=str(corpus.corpus_root),
        model=model,
        max_turns=max_turns,
        max_budget_usd=max_cost_usd,
        setting_sources=[],
        stderr=stderr_sink,
    )


async def run_product_extraction(
    corpus: CorpusReader,
    *,
    model: str,
    max_turns: int = 30,
    max_cost_usd: float | None = None,
    timeout_sec: float | None = None,
    profile_path: Path | None = None,
    priority_names: list[str] | None = None,
    query_fn: QueryFn | None = None,
    _accumulator: _CatalogAccumulator | None = None,
) -> ProductCatalog:
    """Run the products-pass agent and return the validated `ProductCatalog`.

    On per-vendor cap (timeout / SDK budget exhaustion / mid-session
    cancel), returns a ProductCatalog with whatever the agent submitted
    via `add_product` before the cap, with `status="partial"`. Never
    raises TimeoutError — caps are normal events that produce partial
    output, not failures.

    `profile_path` (when provided and existing) is parsed and prepended
    to the user prompt as vendor context.

    `priority_names` (when provided) constrains the agent to extract
    ProductDetail only for those product names — the triage pass produces
    this list. Without it the agent enumerates products itself.

    `_accumulator` is a test hook: pass a pre-populated accumulator
    instead of letting the runner create a fresh one. Tests use this
    to simulate "agent already submitted N products" without needing
    to dispatch through the SDK MCP layer.
    """
    return await _run_inner(
        corpus,
        model=model,
        max_turns=max_turns,
        max_cost_usd=max_cost_usd,
        timeout_sec=timeout_sec,
        profile_path=profile_path,
        priority_names=priority_names,
        query_fn=query_fn,
        accumulator=_accumulator,
    )


async def _run_inner(
    corpus: CorpusReader,
    *,
    model: str,
    max_turns: int,
    max_cost_usd: float | None,
    timeout_sec: float | None,
    profile_path: Path | None,
    priority_names: list[str] | None,
    query_fn: QueryFn | None,
    accumulator: _CatalogAccumulator | None = None,
) -> ProductCatalog:
    stderr_lines: list[str] = []
    if accumulator is None:
        accumulator = _CatalogAccumulator()
    options = build_options(
        corpus,
        accumulator=accumulator,
        model=model,
        max_turns=max_turns,
        max_cost_usd=max_cost_usd,
        stderr_sink=stderr_lines.append,
    )
    user_prompt = build_user_prompt(
        corpus, profile_path=profile_path, priority_names=priority_names
    )

    q = query_fn if query_fn is not None else sdk_query

    result: ResultMessage | None = None
    capped = False

    async def _drive() -> None:
        nonlocal result
        async for message in q(prompt=user_prompt, options=options):
            if isinstance(message, ResultMessage):
                result = message
            elif isinstance(message, (AssistantMessage, SystemMessage)):
                # Tool calls are handled in-process by the MCP server
                # (see _build_incremental_tools). We just consume the
                # message stream so the SDK keeps driving.
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

    # Decide the final status:
    #  - cap fired → "partial" (override whatever the agent finalized)
    #  - agent finalized → use their status
    #  - neither → "partial" (run ended without finalize, but we have
    #    whatever they submitted)
    if capped:
        status: ProfileStatus = "partial"
    elif accumulator.finalized and accumulator.final_status is not None:
        status = accumulator.final_status
    else:
        status = "partial"

    # Only raise if we have absolutely nothing to write and the agent
    # never even started. A cap that fires before any add_product is
    # still legitimate to record as an empty partial — it carries the
    # signal "we tried, agent produced nothing."
    if (
        not capped
        and not accumulator.finalized
        and not accumulator.products
    ):
        raise RuntimeError(
            f"agent did not call {ADD_PRODUCT_TOOL_FQN} or "
            f"{FINALIZE_TOOL_FQN}; cannot extract catalog"
        )

    meta = ProfileMeta(
        model=model,
        num_turns=result.num_turns if result else 0,
        total_cost_usd=result.total_cost_usd if result else None,
        created_at=_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        extract_version=__version__,
    )
    submission = ProductCatalogSubmission(
        products=accumulator.products,
        unresolved_questions=accumulator.unresolved_questions,
        fetch_requests=accumulator.fetch_requests,
        status=status,
    )
    return ProductCatalog.from_submission(
        submission,
        target_id=corpus.target_id,
        run_id=corpus.run_id,
        display_name=corpus.display_name,
        homepage_url=corpus.homepage_url,
        corpus_root=str(corpus.corpus_root),
        meta=meta,
    )
