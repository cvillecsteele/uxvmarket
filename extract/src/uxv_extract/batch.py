"""Batch extraction over a mirroring run, with multi-dimensional guardrails
and bounded fan-out.

Per-vendor caps (delegated to `agent.run_profile_extraction`):
  - max_turns
  - max_cost_usd  (SDK-enforced via ClaudeAgentOptions.max_budget_usd)
  - timeout_sec   (asyncio.wait_for)

Aggregate batch caps (enforced here):
  - batch_cost_usd          : stop when cumulative spend ≥ this
  - batch_timeout_sec       : stop when wall clock ≥ this
  - max_consecutive_failures: stop when the last N completed targets all
                              failed (rolling window). Under fan-out the
                              old "strict consecutive" semantic is
                              ill-defined, so we track outcomes in
                              completion order with a fixed-size deque.

Fan-out:
  - `concurrency`: up to N extractions in flight at once
                   (asyncio.Semaphore + asyncio.create_task)
  - When a cap is tripped, no NEW tasks acquire the semaphore, but any
    in-flight extractions are allowed to complete (Anthropic API calls
    cannot be cleanly canceled mid-flight without paying anyway).
    Worst-case overshoot of `batch_cost_usd` is bounded by
    (concurrency - 1) * per_vendor_cost_usd.

The batch is resumable by file existence: any target whose `profile.json`
already exists in `out_dir/<target_id>/profile.json` is skipped.

Every event is written to a JSONL journal so the user can inspect what
happened, which targets to retry, and where the budget was spent.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum
from pathlib import Path
from typing import Any, Awaitable, Callable, IO

from .agent import (
    SDKSubprocessError,
    is_fatal_auth_error,
    is_fatal_balance_error,
    run_profile_extraction,
)
from .corpus import CorpusReader
from .products_agent import run_product_extraction
from .schema import Profile, ProductCatalog, ProductPriorityList
from .tagline_agent import (
    DEFAULT_TAGLINE_MODEL,
    generate_tagline,
    write_tagline_into_profile,
)
from .triage_agent import run_triage


# Products is incremental (one turn per `add_product`), so its turn-cap
# floor is much higher than profile/triage's. `config.max_turns` is the
# user's profile/triage cap; the products pass takes
# `max(config.max_turns, PRODUCTS_MAX_TURNS_FLOOR)` so a low user
# override still leaves room for incremental submission.
PRODUCTS_MAX_TURNS_FLOOR = 100


class BatchExitCode(IntEnum):
    SUCCESS = 0
    SETUP_ERROR = 1
    BUDGET_EXHAUSTED = 10
    WALLCLOCK_EXHAUSTED = 11
    TOO_MANY_FAILURES = 12
    BALANCE_EXHAUSTED = 13  # API credits ran out — retry is futile
    AUTH_ERROR = 14         # ANTHROPIC_API_KEY missing/invalid


def _classify_subprocess_error(
    exc: SDKSubprocessError,
) -> tuple[BatchExitCode, str] | None:
    """Map an SDK subprocess error to a fatal batch exit code, or None
    if the error is a transient/unknown failure (count toward
    consecutive_failures and continue)."""
    if is_fatal_balance_error(exc.stderr_text):
        return (BatchExitCode.BALANCE_EXHAUSTED, "stop_balance_exhausted")
    if is_fatal_auth_error(exc.stderr_text):
        return (BatchExitCode.AUTH_ERROR, "stop_auth_error")
    return None


@dataclass
class BatchConfig:
    workspace_root: Path
    run_id: str
    out_dir: Path
    journal_path: Path
    model: str
    max_turns: int
    per_vendor_cost_usd: float
    per_vendor_timeout_sec: float
    batch_cost_usd: float
    batch_timeout_sec: float | None
    max_consecutive_failures: int
    target_ids: list[str] | None = None
    concurrency: int = 1
    include_products: bool = False
    """When True, run the products pass after the profile pass succeeds for
    each target. Both passes share the per-vendor caps. Products failures
    are isolated — they don't count toward `max_consecutive_failures` (the
    profile is the load-bearing pass; products is enrichment). Both costs
    contribute to the aggregate `batch_cost_usd`."""
    max_products: int = 0
    """When > 0 AND `include_products` is True: run a triage pre-pass that
    stack-ranks products by relevance to UxV-supplier characterization,
    truncate at this cap, then extract ProductDetail only for the top N.
    Default 0 = legacy behavior (products pass identifies products itself,
    risks 32k output cap on rich corpora)."""
    include_tagline: bool = True
    """When True, run the cheap Haiku tagline pass at the end of each
    vendor's pipeline (after profile + optional products). Tagline
    failures are isolated and do NOT count as vendor failures."""
    tagline_model: str = "claude-haiku-4-5"
    tagline_timeout_sec: float = 60.0


@dataclass
class _BatchState:
    started_at: float = 0.0
    total_cost_usd: float = 0.0
    completed: int = 0
    skipped: int = 0
    failed: int = 0
    recent_outcomes: deque = field(default_factory=deque)
    stopped: bool = False
    stop_code: BatchExitCode | None = None


ExtractFn = Callable[..., Awaitable[Profile]]
ProductsExtractFn = Callable[..., Awaitable[ProductCatalog]]
TriageExtractFn = Callable[..., Awaitable[ProductPriorityList]]


def enumerate_target_ids(*, workspace: Path, run_id: str) -> list[str]:
    """Discover target_ids by listing `<workspace>/output/runs/<run_id>/targets/`."""
    targets_dir = workspace / "output" / "runs" / run_id / "targets"
    if not targets_dir.is_dir():
        return []
    return sorted(p.name for p in targets_dir.iterdir() if p.is_dir())


def _journal_event(fp: IO[str], event: str, **fields: Any) -> None:
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "event": event,
        **fields,
    }
    fp.write(json.dumps(payload) + "\n")
    fp.flush()


def _check_caps(
    config: BatchConfig, state: _BatchState
) -> tuple[BatchExitCode, str, dict[str, Any]] | None:
    """Return (exit_code, journal_event_name, fields) if a cap is hit, else None."""
    if state.total_cost_usd >= config.batch_cost_usd:
        return (
            BatchExitCode.BUDGET_EXHAUSTED,
            "stop_budget",
            {
                "total_cost_usd": state.total_cost_usd,
                "batch_cost_usd": config.batch_cost_usd,
            },
        )
    if (
        config.batch_timeout_sec is not None
        and (time.monotonic() - state.started_at) >= config.batch_timeout_sec
    ):
        return (
            BatchExitCode.WALLCLOCK_EXHAUSTED,
            "stop_wallclock",
            {
                "elapsed_sec": time.monotonic() - state.started_at,
                "batch_timeout_sec": config.batch_timeout_sec,
            },
        )
    if (
        len(state.recent_outcomes) >= config.max_consecutive_failures
        and all(o == "fail" for o in state.recent_outcomes)
    ):
        return (
            BatchExitCode.TOO_MANY_FAILURES,
            "stop_failures",
            {
                "window": list(state.recent_outcomes),
                "max_consecutive_failures": config.max_consecutive_failures,
            },
        )
    return None


async def _generate_and_persist_tagline(
    *,
    corpus,
    profile_path: Path,
    products_path: Path,
    target_id: str,
    journal,
    state: "_BatchState",
    state_lock: asyncio.Lock,
    tagline_model: str,
    tagline_timeout_sec: float,
) -> None:
    """Read the on-disk profile + (optional) products catalog, generate
    a tagline via Haiku, and write it back into profile.json. Logs
    success or specific failure modes; lets caller swallow the
    exception."""
    profile = Profile.model_validate_json(profile_path.read_text())
    if profile.tagline is not None:
        # Already has a tagline (e.g. resumed run). Skip — no need to
        # spend Haiku money rewriting an existing tagline.
        async with state_lock:
            _journal_event(
                journal, "tagline_skip_existing", target_id=target_id,
            )
        return
    catalog: ProductCatalog | None = None
    if products_path.exists():
        try:
            catalog = ProductCatalog.model_validate_json(
                products_path.read_text()
            )
        except Exception:
            catalog = None  # malformed; proceed without products context

    tagline = await generate_tagline(
        profile=profile,
        catalog=catalog,
        corpus=corpus,
        model=tagline_model,
        timeout_sec=tagline_timeout_sec,
    )
    write_tagline_into_profile(profile_path, tagline)
    async with state_lock:
        _journal_event(
            journal, "tagline_ok",
            target_id=target_id,
            words=len(tagline.split()),
        )


async def run_batch(
    config: BatchConfig,
    *,
    extract_fn: ExtractFn | None = None,
    products_extract_fn: ProductsExtractFn | None = None,
    triage_fn: TriageExtractFn | None = None,
) -> BatchExitCode:
    extractor = extract_fn or run_profile_extraction
    products_extractor = products_extract_fn or run_product_extraction
    triage_extractor = triage_fn or run_triage

    # Catch typo'd --workspace-root or --run-id BEFORE any other work.
    # Otherwise an empty enumeration would silently succeed with exit
    # code 0 — terrible for unattended long jobs.
    targets_dir = (
        config.workspace_root / "output" / "runs" / config.run_id / "targets"
    )
    if not targets_dir.is_dir():
        config.journal_path.parent.mkdir(parents=True, exist_ok=True)
        with config.journal_path.open("a") as journal:
            _journal_event(
                journal,
                "setup_error",
                reason="missing_targets_dir",
                workspace_root=str(config.workspace_root),
                run_id=config.run_id,
                expected_path=str(targets_dir),
            )
        return BatchExitCode.SETUP_ERROR

    target_ids = config.target_ids
    if target_ids is None:
        target_ids = enumerate_target_ids(
            workspace=config.workspace_root,
            run_id=config.run_id,
        )

    if not target_ids:
        config.journal_path.parent.mkdir(parents=True, exist_ok=True)
        with config.journal_path.open("a") as journal:
            _journal_event(
                journal,
                "setup_error",
                reason="no_targets",
                workspace_root=str(config.workspace_root),
                run_id=config.run_id,
                targets_dir=str(targets_dir),
            )
        return BatchExitCode.SETUP_ERROR

    config.out_dir.mkdir(parents=True, exist_ok=True)
    config.journal_path.parent.mkdir(parents=True, exist_ok=True)

    state = _BatchState(
        started_at=time.monotonic(),
        recent_outcomes=deque(maxlen=config.max_consecutive_failures),
    )
    state_lock = asyncio.Lock()
    sem = asyncio.Semaphore(max(1, config.concurrency))

    with config.journal_path.open("a") as journal:
        _journal_event(
            journal,
            "batch_start",
            target_count=len(target_ids),
            run_id=config.run_id,
            concurrency=config.concurrency,
            batch_cost_cap_usd=config.batch_cost_usd,
            batch_timeout_sec=config.batch_timeout_sec,
            max_consecutive_failures=config.max_consecutive_failures,
            per_vendor_cost_usd=config.per_vendor_cost_usd,
            per_vendor_timeout_sec=config.per_vendor_timeout_sec,
            include_products=config.include_products,
        )

        async def process_one(target_id: str) -> None:
            # Pre-flight: short-circuit if a cap already tripped.
            async with state_lock:
                if state.stopped:
                    return

            profile_path = config.out_dir / target_id / "profile.json"
            products_path = config.out_dir / target_id / "products.json"
            profile_done = profile_path.exists()
            products_done = (
                products_path.exists() if config.include_products else True
            )

            if profile_done and products_done:
                async with state_lock:
                    state.skipped += 1
                    _journal_event(journal, "skip_existing", target_id=target_id)
                return

            async with sem:
                # Re-check after acquiring; cap may have tripped while we waited.
                async with state_lock:
                    if state.stopped:
                        return

                # Per-vendor wall-clock budget: `per_vendor_timeout_sec`
                # is the TOTAL across all passes (profile + triage +
                # products), not per-pass. Each pass gets `remaining =
                # budget - elapsed_so_far`. When budget exhausts mid-pass,
                # that pass returns a partial via the
                # partial-on-cap path. When exhausted between passes,
                # the next pass starts with ~0s, caps immediately, and
                # writes an empty partial. Either way: no work is
                # discarded.
                vendor_started = time.monotonic()

                def vendor_remaining() -> float | None:
                    if config.per_vendor_timeout_sec is None:
                        return None
                    elapsed = time.monotonic() - vendor_started
                    return max(0.0, config.per_vendor_timeout_sec - elapsed)

                try:
                    corpus = CorpusReader.from_workspace(
                        workspace_root=config.workspace_root,
                        run_id=config.run_id,
                        target_id=target_id,
                    )
                except FileNotFoundError as exc:
                    async with state_lock:
                        state.failed += 1
                        state.recent_outcomes.append("fail")
                        _journal_event(
                            journal,
                            "error",
                            target_id=target_id,
                            error=f"corpus_not_found: {exc}",
                        )
                        cap = _check_caps(config, state)
                        if cap is not None:
                            _journal_event(journal, cap[1], **cap[2])
                            state.stopped = True
                            state.stop_code = cap[0]
                    return

                # ---- Profile pass ----------------------------------------
                if not profile_done:
                    try:
                        profile = await extractor(
                            corpus,
                            model=config.model,
                            max_turns=config.max_turns,
                            max_cost_usd=config.per_vendor_cost_usd,
                            timeout_sec=vendor_remaining(),
                        )
                    except SDKSubprocessError as exc:
                        async with state_lock:
                            state.failed += 1
                            state.recent_outcomes.append("fail")
                            fatal = _classify_subprocess_error(exc)
                            _journal_event(
                                journal,
                                "error",
                                target_id=target_id,
                                error=str(exc)[:2000],
                                fatal=fatal[1] if fatal else None,
                                stderr_lines=len(exc.stderr_text.splitlines()),
                            )
                            if fatal is not None:
                                code, event_name = fatal
                                _journal_event(journal, event_name, target_id=target_id)
                                state.stopped = True
                                state.stop_code = code
                                return
                            cap = _check_caps(config, state)
                            if cap is not None:
                                _journal_event(journal, cap[1], **cap[2])
                                state.stopped = True
                                state.stop_code = cap[0]
                        return
                    except Exception as exc:  # crash isolation for non-SDK errors
                        async with state_lock:
                            state.failed += 1
                            state.recent_outcomes.append("fail")
                            _journal_event(
                                journal,
                                "error",
                                target_id=target_id,
                                error=repr(exc)[:500],
                            )
                            cap = _check_caps(config, state)
                            if cap is not None:
                                _journal_event(journal, cap[1], **cap[2])
                                state.stopped = True
                                state.stop_code = cap[0]
                        return

                    profile_path.parent.mkdir(parents=True, exist_ok=True)
                    profile_path.write_text(profile.model_dump_json(indent=2))
                    profile_cost = profile.meta.total_cost_usd or 0.0
                    profile_partial = profile.status == "partial"
                    async with state_lock:
                        state.completed += 1
                        # Partials count as "ok" for the consecutive-failures
                        # window (the runner did its job; the cap was an
                        # external constraint, not a failure).
                        state.recent_outcomes.append("ok")
                        state.total_cost_usd += profile_cost
                        _journal_event(
                            journal,
                            "ok",
                            target_id=target_id,
                            cost=profile_cost,
                            turns=profile.meta.num_turns,
                            cumulative_cost=state.total_cost_usd,
                            partial=profile_partial,
                        )
                        cap = _check_caps(config, state)
                        if cap is not None:
                            _journal_event(journal, cap[1], **cap[2])
                            state.stopped = True
                            state.stop_code = cap[0]
                            return

                # ---- Products pass (optional) ----------------------------
                if not config.include_products or products_done:
                    # Profile is done; products is intentionally skipped
                    # for this vendor. Run the tagline pass before
                    # returning so vendors that don't need products
                    # still get a tagline.
                    if config.include_tagline and profile_path.exists():
                        try:
                            await _generate_and_persist_tagline(
                                corpus=corpus,
                                profile_path=profile_path,
                                products_path=products_path,
                                target_id=target_id,
                                journal=journal,
                                state=state,
                                state_lock=state_lock,
                                tagline_model=config.tagline_model,
                                tagline_timeout_sec=config.tagline_timeout_sec,
                            )
                        except Exception as exc:
                            async with state_lock:
                                _journal_event(
                                    journal, "tagline_error",
                                    target_id=target_id,
                                    error=repr(exc)[:500],
                                )
                    return

                # Re-check stopped between passes.
                async with state_lock:
                    if state.stopped:
                        return

                # Optional triage pre-pass to stack-rank and cap products
                # before the full extraction. Avoids hitting Sonnet's 32k
                # output cap on vendors with 30+ products.
                priority_names: list[str] | None = None
                if config.max_products > 0:
                    triage_path = config.out_dir / target_id / "products-priority.json"
                    if not triage_path.exists():
                        try:
                            triage_result = await triage_extractor(
                                corpus,
                                model=config.model,
                                max_turns=config.max_turns,
                                max_cost_usd=config.per_vendor_cost_usd,
                                timeout_sec=vendor_remaining(),
                                max_products=config.max_products,
                                profile_path=profile_path if profile_path.exists() else None,
                            )
                        except SDKSubprocessError as exc:
                            async with state_lock:
                                fatal = _classify_subprocess_error(exc)
                                _journal_event(
                                    journal, "triage_error",
                                    target_id=target_id, error=str(exc)[:2000],
                                    fatal=fatal[1] if fatal else None,
                                )
                                if fatal is not None:
                                    code, event_name = fatal
                                    _journal_event(journal, event_name, target_id=target_id)
                                    state.stopped = True
                                    state.stop_code = code
                            return
                        except Exception as exc:
                            async with state_lock:
                                _journal_event(
                                    journal, "triage_error",
                                    target_id=target_id, error=repr(exc)[:500],
                                )
                            return
                        triage_path.parent.mkdir(parents=True, exist_ok=True)
                        triage_path.write_text(triage_result.model_dump_json(indent=2))
                        triage_cost = triage_result.meta.total_cost_usd or 0.0
                        triage_partial = triage_result.status == "partial"
                        async with state_lock:
                            state.total_cost_usd += triage_cost
                            _journal_event(
                                journal, "triage_ok",
                                target_id=target_id,
                                cost=triage_cost,
                                turns=triage_result.meta.num_turns,
                                priority_count=len(triage_result.products),
                                agent_listed=triage_result.agent_listed,
                                cumulative_cost=state.total_cost_usd,
                                partial=triage_partial,
                            )
                            cap = _check_caps(config, state)
                            if cap is not None:
                                _journal_event(journal, cap[1], **cap[2])
                                state.stopped = True
                                state.stop_code = cap[0]
                                return
                    else:
                        # Already-triaged: load priority list from
                        # sidecar. If it fails to validate (legacy file
                        # missing fields added later), log + skip
                        # priority constraint — the products pass will
                        # enumerate from scratch instead of erroring.
                        try:
                            triage_result = ProductPriorityList.model_validate_json(
                                triage_path.read_text()
                            )
                        except Exception as exc:
                            async with state_lock:
                                _journal_event(
                                    journal, "triage_load_failed",
                                    target_id=target_id,
                                    triage_path=str(triage_path),
                                    error=str(exc)[:500],
                                )
                            triage_result = None
                    priority_names = (
                        [p.name for p in triage_result.products]
                        if triage_result is not None else None
                    )

                try:
                    catalog = await products_extractor(
                        corpus,
                        model=config.model,
                        max_turns=max(config.max_turns, PRODUCTS_MAX_TURNS_FLOOR),
                        max_cost_usd=config.per_vendor_cost_usd,
                        timeout_sec=vendor_remaining(),
                        profile_path=profile_path if profile_path.exists() else None,
                        priority_names=priority_names,
                    )
                except SDKSubprocessError as exc:
                    async with state_lock:
                        fatal = _classify_subprocess_error(exc)
                        _journal_event(
                            journal,
                            "products_error",
                            target_id=target_id,
                            error=str(exc)[:2000],
                            fatal=fatal[1] if fatal else None,
                        )
                        # Even though products failures don't trip the
                        # consecutive-failure cap, a balance/auth error
                        # is fatal for the WHOLE batch — no point doing
                        # more work either pass.
                        if fatal is not None:
                            code, event_name = fatal
                            _journal_event(journal, event_name, target_id=target_id)
                            state.stopped = True
                            state.stop_code = code
                    return
                except Exception as exc:
                    async with state_lock:
                        _journal_event(
                            journal,
                            "products_error",
                            target_id=target_id,
                            error=repr(exc)[:500],
                        )
                    return

                products_path.parent.mkdir(parents=True, exist_ok=True)
                products_path.write_text(catalog.model_dump_json(indent=2))
                products_cost = catalog.meta.total_cost_usd or 0.0
                products_partial = catalog.status == "partial"
                async with state_lock:
                    state.total_cost_usd += products_cost
                    _journal_event(
                        journal,
                        "products_ok",
                        target_id=target_id,
                        cost=products_cost,
                        turns=catalog.meta.num_turns,
                        products=len(catalog.products),
                        fetch_requests=len(catalog.fetch_requests),
                        cumulative_cost=state.total_cost_usd,
                        partial=products_partial,
                    )
                    cap = _check_caps(config, state)
                    if cap is not None:
                        _journal_event(journal, cap[1], **cap[2])
                        state.stopped = True
                        state.stop_code = cap[0]
                        return

                # ---- Tagline pass (cheap Haiku enrichment) -----------
                # Runs IF profile exists. Failures are isolated — a
                # tagline error is NOT a vendor failure. Profile
                # remains usable without one. Uses anthropic SDK
                # directly (one-shot, no agent loop).
                if config.include_tagline and profile_path.exists():
                    try:
                        await _generate_and_persist_tagline(
                            corpus=corpus,
                            profile_path=profile_path,
                            products_path=products_path,
                            target_id=target_id,
                            journal=journal,
                            state=state,
                            state_lock=state_lock,
                            tagline_model=config.tagline_model,
                            tagline_timeout_sec=config.tagline_timeout_sec,
                        )
                    except Exception as exc:
                        async with state_lock:
                            _journal_event(
                                journal, "tagline_error",
                                target_id=target_id,
                                error=repr(exc)[:500],
                            )

        # `return_exceptions=True` so an unexpected exception in one
        # task (e.g. a legacy on-disk file failing schema validation)
        # does NOT cascade and kill the journal for sibling tasks
        # still in flight. Each task already has internal try/excepts
        # for expected failures; this is the defensive net for
        # everything else.
        tasks = [
            asyncio.create_task(process_one(tid)) for tid in target_ids
        ]
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for tid, r in zip(target_ids, results):
                if isinstance(r, BaseException):
                    _journal_event(
                        journal, "error",
                        target_id=tid,
                        error=f"unhandled_in_task: {type(r).__name__}: {r}"[:1000],
                    )

        exit_code = state.stop_code or BatchExitCode.SUCCESS
        _journal_event(
            journal,
            "batch_done",
            exit_code=int(exit_code),
            completed=state.completed,
            skipped=state.skipped,
            failed=state.failed,
            total_cost_usd=state.total_cost_usd,
            wallclock_sec=time.monotonic() - state.started_at,
        )

    return exit_code
