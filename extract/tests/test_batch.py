from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from uxv_extract.batch import (
    BatchConfig,
    BatchExitCode,
    enumerate_target_ids,
    run_batch,
)
from uxv_extract.schema import (
    Answer,
    Citation,
    ListAnswer,
    ProductCatalog,
    ProductDetail,
    Profile,
    ProfileMeta,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "corpus_min"


def _unknown_answer() -> Answer:
    return Answer(value=None, confidence="low", status="unknown", evidence=[], notes=None)


def _unknown_list_answer() -> ListAnswer:
    return ListAnswer(items=[], confidence="low", status="unknown", notes=None)


def _profile(target_id: str = "test-vendor", *, cost: float = 0.5, turns: int = 5,
             status: str = "complete") -> Profile:
    return Profile(
        target_id=target_id,
        run_id="test-run",
        display_name="Test Vendor",
        homepage_url="https://test.example",
        corpus_root="/abs/path",
        products_categories=_unknown_list_answer(),
        headquarters=_unknown_answer(),
        drone_supply_chain_role=Answer(
            value="oem",
            confidence="high",
            status="answered",
            evidence=[
                Citation(
                    source_kind="mirror",
                    resource_id="resource-0001",
                    line_start=1,
                    line_end=2,
                    url="https://test.example/",
                    page_class="homepage",
                    snippet=(
                        "Test Vendor designs and manufactures heavy-lift "
                        "drones for federal customers across the United "
                        "States."
                    ),
                )
            ],
        ),
        products=_unknown_list_answer(),
        ndaa=_unknown_answer(),
        blue_uas=_unknown_answer(),
        readiness=_unknown_answer(),
        unresolved_questions=[],
        fetch_requests=[],
        status=status,
        meta=ProfileMeta(
            model="claude-sonnet-4-6",
            num_turns=turns,
            total_cost_usd=cost,
            created_at="2026-05-01T00:00:00+00:00",
            extract_version="0.1.0",
        ),
    )


def _make_workspace(tmp_path: Path, target_ids: list[str], run_id: str = "test-run") -> Path:
    """Build a fake mirroring workspace with N target dirs.

    Each copy of the shared fixture has its manifest's `target_id` rewritten
    to match the directory name, so `CorpusReader.target_id` matches the
    batch loop variable.
    """
    workspace = tmp_path / "mirroring"
    run_root = workspace / "output" / "runs" / run_id
    for tid in target_ids:
        target_dir = run_root / "targets" / tid
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(FIXTURE_ROOT, target_dir)
        manifest_path = target_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["target"]["target_id"] = tid
        manifest["run_id"] = run_id
        manifest_path.write_text(json.dumps(manifest))
    return workspace


def _config(
    *,
    workspace: Path,
    out_dir: Path,
    journal_path: Path,
    run_id: str = "test-run",
    target_ids: list[str] | None = None,
    batch_cost_usd: float = 100.0,
    batch_timeout_sec: float | None = None,
    max_consecutive_failures: int = 5,
    per_vendor_cost_usd: float = 2.0,
    per_vendor_timeout_sec: float = 600,
    concurrency: int = 1,
    include_products: bool = False,
) -> BatchConfig:
    return BatchConfig(
        workspace_root=workspace,
        run_id=run_id,
        out_dir=out_dir,
        journal_path=journal_path,
        model="claude-sonnet-4-6",
        max_turns=30,
        per_vendor_cost_usd=per_vendor_cost_usd,
        per_vendor_timeout_sec=per_vendor_timeout_sec,
        batch_cost_usd=batch_cost_usd,
        batch_timeout_sec=batch_timeout_sec,
        max_consecutive_failures=max_consecutive_failures,
        target_ids=target_ids,
        concurrency=concurrency,
        include_products=include_products,
    )


def _product_catalog(
    target_id: str = "test-vendor", *, cost: float = 0.4, products: int = 2
) -> ProductCatalog:
    placeholder_cite = Citation(
        source_kind="mirror",
        resource_id="resource-0001",
        line_start=1,
        line_end=2,
        url="https://test.example/",
        page_class="homepage",
        snippet="Test Vendor designs and manufactures heavy-lift drones.",
    )
    return ProductCatalog(
        target_id=target_id,
        run_id="test-run",
        display_name="Test Vendor",
        homepage_url="https://test.example",
        corpus_root="/abs/path",
        products=[
            ProductDetail(
                name=f"Product {i}",
                category="airframes",
                descriptor="x",
                granularity="sku",
                readiness="production",
                ndaa="unknown",
                blue_uas="unknown",
                evidence=[placeholder_cite],
            )
            for i in range(products)
        ],
        unresolved_questions=[],
        fetch_requests=[],
        status="complete",
        meta=ProfileMeta(
            model="claude-sonnet-4-6",
            num_turns=4,
            total_cost_usd=cost,
            created_at="2026-05-02T00:00:00+00:00",
            extract_version="0.1.0",
        ),
    )


def _read_journal(journal_path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in journal_path.read_text().splitlines() if line.strip()]


# -- enumerate ----------------------------------------------------------------


def test_enumerate_target_ids_lists_every_target_dir(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path, ["v1", "v2", "v3"])
    ids = enumerate_target_ids(workspace=workspace, run_id="test-run")
    assert sorted(ids) == ["v1", "v2", "v3"]


def test_enumerate_returns_empty_for_missing_run(tmp_path: Path) -> None:
    workspace = tmp_path / "mirroring"
    assert enumerate_target_ids(workspace=workspace, run_id="missing") == []


# -- Setup-error fast-fail ---------------------------------------------------


@pytest.mark.asyncio
async def test_batch_fails_setup_on_missing_workspace(tmp_path: Path) -> None:
    """A typo in --workspace-root or --run-id must NOT silently succeed —
    that would burn a long unattended job's wall clock with zero output."""
    out_dir = tmp_path / "out"
    journal = tmp_path / "j.jsonl"
    code = await run_batch(
        _config(
            workspace=tmp_path / "does-not-exist",
            out_dir=out_dir,
            journal_path=journal,
            target_ids=["v1"],
        ),
    )
    assert code == BatchExitCode.SETUP_ERROR
    events = _read_journal(journal)
    setup = next(e for e in events if e["event"] == "setup_error")
    assert setup["reason"] == "missing_targets_dir"


@pytest.mark.asyncio
async def test_batch_fails_setup_when_no_targets_available(tmp_path: Path) -> None:
    """Workspace exists but the run has no target directories — likely a
    typo'd --run-id."""
    workspace = tmp_path / "mirroring"
    (workspace / "output" / "runs" / "real-run" / "targets").mkdir(parents=True)
    out_dir = tmp_path / "out"
    journal = tmp_path / "j.jsonl"
    code = await run_batch(
        _config(
            workspace=workspace,
            run_id="real-run",
            out_dir=out_dir,
            journal_path=journal,
        ),
    )
    assert code == BatchExitCode.SETUP_ERROR
    events = _read_journal(journal)
    setup = next(e for e in events if e["event"] == "setup_error")
    assert setup["reason"] == "no_targets"


# -- happy path --------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_extracts_each_target_and_writes_profile_json(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path, ["v1", "v2"])
    out_dir = tmp_path / "extract_out"
    journal = tmp_path / "journal.jsonl"

    async def fake_extract(corpus, **_):
        return _profile(target_id=corpus.target_id, cost=0.5, turns=5)

    code = await run_batch(
        _config(workspace=workspace, out_dir=out_dir, journal_path=journal),
        extract_fn=fake_extract,
    )

    assert code == BatchExitCode.SUCCESS
    assert (out_dir / "v1" / "profile.json").exists()
    assert (out_dir / "v2" / "profile.json").exists()
    events = _read_journal(journal)
    assert any(e["event"] == "ok" and e["target_id"] == "v1" for e in events)
    assert any(e["event"] == "ok" and e["target_id"] == "v2" for e in events)
    assert events[-1]["event"] == "batch_done"
    assert events[-1]["completed"] == 2


# -- skip-existing ------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_skips_target_with_existing_profile(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path, ["v1", "v2"])
    out_dir = tmp_path / "extract_out"
    journal = tmp_path / "journal.jsonl"

    # Pre-populate v1 as already extracted:
    (out_dir / "v1").mkdir(parents=True)
    (out_dir / "v1" / "profile.json").write_text(_profile(target_id="v1").model_dump_json())

    extract_calls: list[str] = []

    async def fake_extract(corpus, **_):
        extract_calls.append(corpus.target_id)
        return _profile(target_id=corpus.target_id)

    code = await run_batch(
        _config(workspace=workspace, out_dir=out_dir, journal_path=journal),
        extract_fn=fake_extract,
    )

    assert code == BatchExitCode.SUCCESS
    assert extract_calls == ["v2"]
    events = _read_journal(journal)
    assert any(e["event"] == "skip_existing" and e["target_id"] == "v1" for e in events)


# -- batch budget cap --------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_stops_when_cost_cap_reached(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path, ["v1", "v2", "v3", "v4"])
    out_dir = tmp_path / "extract_out"
    journal = tmp_path / "journal.jsonl"

    async def fake_extract(corpus, **_):
        return _profile(target_id=corpus.target_id, cost=1.5)

    code = await run_batch(
        _config(
            workspace=workspace,
            out_dir=out_dir,
            journal_path=journal,
            batch_cost_usd=2.0,  # 2 vendors at $1.50 = $3.00 -> cap hits before 3rd
        ),
        extract_fn=fake_extract,
    )

    assert code == BatchExitCode.BUDGET_EXHAUSTED
    completed = list(out_dir.glob("*/profile.json"))
    # Two vendors complete before cumulative cost crosses $2.00:
    assert len(completed) == 2


# -- consecutive failure cap --------------------------------------------------


@pytest.mark.asyncio
async def test_batch_stops_after_consecutive_failures(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path, ["v1", "v2", "v3", "v4", "v5", "v6"])
    out_dir = tmp_path / "extract_out"
    journal = tmp_path / "journal.jsonl"

    async def always_fails(corpus, **_):
        raise RuntimeError(f"boom on {corpus.target_id}")

    code = await run_batch(
        _config(
            workspace=workspace,
            out_dir=out_dir,
            journal_path=journal,
            max_consecutive_failures=3,
        ),
        extract_fn=always_fails,
    )

    assert code == BatchExitCode.TOO_MANY_FAILURES
    events = _read_journal(journal)
    error_events = [e for e in events if e["event"] == "error"]
    assert len(error_events) == 3


@pytest.mark.asyncio
async def test_consecutive_failure_count_resets_on_success(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path, ["v1", "v2", "v3", "v4", "v5"])
    out_dir = tmp_path / "extract_out"
    journal = tmp_path / "journal.jsonl"

    pattern = iter(["fail", "fail", "ok", "fail", "fail", "ok"])

    async def maybe_fails(corpus, **_):
        outcome = next(pattern)
        if outcome == "fail":
            raise RuntimeError(f"boom on {corpus.target_id}")
        return _profile(target_id=corpus.target_id)

    code = await run_batch(
        _config(
            workspace=workspace,
            out_dir=out_dir,
            journal_path=journal,
            max_consecutive_failures=3,
        ),
        extract_fn=maybe_fails,
    )

    # Pattern: fail,fail,ok (resets),fail,fail,ok -> at most 2 consecutive
    # failures, never 3, so we should NOT trip the cap.
    assert code == BatchExitCode.SUCCESS


# -- crash isolation ---------------------------------------------------------


@pytest.mark.asyncio
async def test_one_target_failure_does_not_stop_the_batch(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path, ["v1", "v2", "v3"])
    out_dir = tmp_path / "extract_out"
    journal = tmp_path / "journal.jsonl"

    async def sometimes_fails(corpus, **_):
        if corpus.target_id == "v2":
            raise RuntimeError("v2 explodes")
        return _profile(target_id=corpus.target_id)

    code = await run_batch(
        _config(workspace=workspace, out_dir=out_dir, journal_path=journal),
        extract_fn=sometimes_fails,
    )

    assert code == BatchExitCode.SUCCESS
    assert (out_dir / "v1" / "profile.json").exists()
    assert not (out_dir / "v2" / "profile.json").exists()
    assert (out_dir / "v3" / "profile.json").exists()


# -- per-vendor timeout --------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_profile_logged_as_ok_with_partial_flag(tmp_path: Path) -> None:
    """Per the new contract, runners NEVER raise TimeoutError on cap-fire
    — they return a Profile with status='partial'. Batch logs that as
    an `ok` event with a `partial: true` flag and writes the partial
    file to disk (no `timeout` event)."""
    workspace = _make_workspace(tmp_path, ["v1", "v2"])
    out_dir = tmp_path / "extract_out"
    journal = tmp_path / "journal.jsonl"

    async def fake_extract(corpus, **_):
        return _profile(target_id=corpus.target_id, status=(
            "partial" if corpus.target_id == "v1" else "complete"
        ))

    code = await run_batch(
        _config(workspace=workspace, out_dir=out_dir, journal_path=journal),
        extract_fn=fake_extract,
    )

    assert code == BatchExitCode.SUCCESS
    events = _read_journal(journal)
    # No 'timeout' event ever produced:
    assert not any(e["event"] == "timeout" for e in events)
    # v1's ok event carries partial: true; v2's carries partial: false:
    by_tid = {(e["event"], e.get("target_id")): e for e in events}
    assert by_tid[("ok", "v1")]["partial"] is True
    assert by_tid[("ok", "v2")]["partial"] is False
    # v1 file is on disk:
    assert (out_dir / "v1" / "profile.json").exists()


@pytest.mark.asyncio
async def test_partial_does_not_count_toward_consecutive_failures(tmp_path: Path) -> None:
    """Five consecutive partials in a row do NOT trip the
    max_consecutive_failures cap (under the old timeout-as-failure
    code, five timeouts WOULD have)."""
    workspace = _make_workspace(tmp_path, [f"v{i}" for i in range(6)])
    out_dir = tmp_path / "extract_out"
    journal = tmp_path / "journal.jsonl"

    async def fake_extract(corpus, **_):
        # All vendors return partials.
        return _profile(target_id=corpus.target_id, status="partial")

    code = await run_batch(
        _config(
            workspace=workspace, out_dir=out_dir, journal_path=journal,
            max_consecutive_failures=3,
        ),
        extract_fn=fake_extract,
    )

    # Batch ran to completion — partials didn't trip the failure cap:
    assert code == BatchExitCode.SUCCESS
    events = _read_journal(journal)
    assert not any(e["event"] == "stop_failures" for e in events)
    # All six wrote partial profiles:
    assert sum(1 for e in events if e["event"] == "ok" and e.get("partial")) == 6


# -- explicit target_ids subset ------------------------------------------------


@pytest.mark.asyncio
async def test_explicit_target_ids_overrides_enumeration(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path, ["v1", "v2", "v3"])
    out_dir = tmp_path / "extract_out"
    journal = tmp_path / "journal.jsonl"

    extract_calls: list[str] = []

    async def fake_extract(corpus, **_):
        extract_calls.append(corpus.target_id)
        return _profile(target_id=corpus.target_id)

    code = await run_batch(
        _config(
            workspace=workspace,
            out_dir=out_dir,
            journal_path=journal,
            target_ids=["v2"],
        ),
        extract_fn=fake_extract,
    )

    assert code == BatchExitCode.SUCCESS
    assert extract_calls == ["v2"]


# -- per-vendor caps are passed through ---------------------------------------


# -- fan-out -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_runs_targets_concurrently_when_concurrency_gt_1(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path, ["v1", "v2", "v3", "v4"])
    out_dir = tmp_path / "out"
    journal = tmp_path / "j.jsonl"

    in_flight = 0
    max_in_flight = 0
    lock = asyncio.Lock()

    async def fake_extract(corpus, **_):
        nonlocal in_flight, max_in_flight
        async with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.05)
        async with lock:
            in_flight -= 1
        return _profile(target_id=corpus.target_id)

    code = await run_batch(
        _config(workspace=workspace, out_dir=out_dir, journal_path=journal, concurrency=3),
        extract_fn=fake_extract,
    )
    assert code == BatchExitCode.SUCCESS
    assert max_in_flight >= 2, f"expected concurrent execution; max_in_flight={max_in_flight}"


@pytest.mark.asyncio
async def test_concurrency_1_runs_serially(tmp_path: Path) -> None:
    """concurrency=1 must keep the legacy strict-sequential semantic."""
    workspace = _make_workspace(tmp_path, ["v1", "v2", "v3"])
    out_dir = tmp_path / "out"
    journal = tmp_path / "j.jsonl"

    in_flight = 0
    max_in_flight = 0

    async def fake_extract(corpus, **_):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        return _profile(target_id=corpus.target_id)

    await run_batch(
        _config(workspace=workspace, out_dir=out_dir, journal_path=journal, concurrency=1),
        extract_fn=fake_extract,
    )
    assert max_in_flight == 1


@pytest.mark.asyncio
async def test_batch_cost_cap_does_not_cancel_in_flight_concurrent_tasks(tmp_path: Path) -> None:
    """When the cap fires, in-flight tasks complete; new tasks do not start.
    Overshoot is bounded by (concurrency - 1) * per_vendor_cap."""
    workspace = _make_workspace(tmp_path, ["v1", "v2", "v3", "v4", "v5"])
    out_dir = tmp_path / "out"
    journal = tmp_path / "j.jsonl"

    started: list[str] = []
    completed: list[str] = []
    started_lock = asyncio.Lock()

    async def fake_extract(corpus, **_):
        async with started_lock:
            started.append(corpus.target_id)
        await asyncio.sleep(0.05)
        completed.append(corpus.target_id)
        # each "vendor" costs $1.50
        return _profile(target_id=corpus.target_id, cost=1.5)

    code = await run_batch(
        _config(
            workspace=workspace,
            out_dir=out_dir,
            journal_path=journal,
            concurrency=2,
            batch_cost_usd=2.0,  # 2 vendors at $1.50 -> $3 cumulative trips cap
        ),
        extract_fn=fake_extract,
    )
    assert code == BatchExitCode.BUDGET_EXHAUSTED
    # First two complete (in flight when cap hits, allowed to finish).
    assert len(completed) == 2
    # No third start.
    assert len(started) == 2


@pytest.mark.asyncio
async def test_recent_window_failure_cap_works_under_concurrency(tmp_path: Path) -> None:
    """With concurrency>1, 'consecutive' is replaced by 'rolling window of
    last N completed all failed'."""
    workspace = _make_workspace(tmp_path, [f"v{i}" for i in range(10)])
    out_dir = tmp_path / "out"
    journal = tmp_path / "j.jsonl"

    # Make every extract fail; with concurrency=2 and window=3, after the
    # first 3 finish we should trip.
    async def always_fails(corpus, **_):
        await asyncio.sleep(0.01)
        raise RuntimeError("boom")

    code = await run_batch(
        _config(
            workspace=workspace,
            out_dir=out_dir,
            journal_path=journal,
            concurrency=2,
            max_consecutive_failures=3,
        ),
        extract_fn=always_fails,
    )
    assert code == BatchExitCode.TOO_MANY_FAILURES


# -- include_products --------------------------------------------------------


@pytest.mark.asyncio
async def test_include_products_runs_both_passes_per_target(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path, ["v1", "v2"])
    out_dir = tmp_path / "out"
    journal = tmp_path / "j.jsonl"

    profile_calls: list[str] = []
    products_calls: list[str] = []

    async def fake_profile(corpus, **_):
        profile_calls.append(corpus.target_id)
        return _profile(target_id=corpus.target_id, cost=0.5)

    async def fake_products(corpus, **_):
        products_calls.append(corpus.target_id)
        return _product_catalog(target_id=corpus.target_id, cost=0.4)

    code = await run_batch(
        _config(
            workspace=workspace, out_dir=out_dir, journal_path=journal,
            include_products=True,
        ),
        extract_fn=fake_profile,
        products_extract_fn=fake_products,
    )

    assert code == BatchExitCode.SUCCESS
    assert sorted(profile_calls) == ["v1", "v2"]
    assert sorted(products_calls) == ["v1", "v2"]
    assert (out_dir / "v1" / "profile.json").exists()
    assert (out_dir / "v1" / "products.json").exists()
    assert (out_dir / "v2" / "products.json").exists()
    events = _read_journal(journal)
    assert any(e["event"] == "ok" and e["target_id"] == "v1" for e in events)
    assert any(e["event"] == "products_ok" and e["target_id"] == "v1" for e in events)


@pytest.mark.asyncio
async def test_include_products_skips_target_when_both_files_exist(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path, ["v1"])
    out_dir = tmp_path / "out"
    journal = tmp_path / "j.jsonl"
    (out_dir / "v1").mkdir(parents=True)
    (out_dir / "v1" / "profile.json").write_text(_profile(target_id="v1").model_dump_json())
    (out_dir / "v1" / "products.json").write_text(_product_catalog("v1").model_dump_json())

    profile_calls: list[str] = []
    products_calls: list[str] = []

    async def fake_profile(corpus, **_):
        profile_calls.append(corpus.target_id)
        return _profile(target_id=corpus.target_id)

    async def fake_products(corpus, **_):
        products_calls.append(corpus.target_id)
        return _product_catalog(target_id=corpus.target_id)

    await run_batch(
        _config(workspace=workspace, out_dir=out_dir, journal_path=journal, include_products=True),
        extract_fn=fake_profile,
        products_extract_fn=fake_products,
    )
    assert profile_calls == []
    assert products_calls == []


@pytest.mark.asyncio
async def test_include_products_runs_only_products_when_profile_already_exists(tmp_path: Path) -> None:
    """Re-running batch with --include-products on top of an earlier
    profile-only batch should fill in the missing products.json."""
    workspace = _make_workspace(tmp_path, ["v1"])
    out_dir = tmp_path / "out"
    journal = tmp_path / "j.jsonl"
    (out_dir / "v1").mkdir(parents=True)
    (out_dir / "v1" / "profile.json").write_text(_profile(target_id="v1").model_dump_json())

    profile_calls: list[str] = []
    products_calls: list[str] = []

    async def fake_profile(corpus, **_):
        profile_calls.append(corpus.target_id)
        return _profile(target_id=corpus.target_id)

    async def fake_products(corpus, **_):
        products_calls.append(corpus.target_id)
        return _product_catalog(target_id=corpus.target_id)

    await run_batch(
        _config(workspace=workspace, out_dir=out_dir, journal_path=journal, include_products=True),
        extract_fn=fake_profile,
        products_extract_fn=fake_products,
    )
    assert profile_calls == []  # profile already there
    assert products_calls == ["v1"]
    assert (out_dir / "v1" / "products.json").exists()


@pytest.mark.asyncio
async def test_products_pass_failure_does_not_count_toward_consecutive_failures(tmp_path: Path) -> None:
    """Products is enrichment; only profile failures should trip the
    failure circuit-breaker."""
    workspace = _make_workspace(tmp_path, [f"v{i}" for i in range(8)])
    out_dir = tmp_path / "out"
    journal = tmp_path / "j.jsonl"

    async def fake_profile(corpus, **_):
        return _profile(target_id=corpus.target_id)

    async def fake_products_always_fails(corpus, **_):
        raise RuntimeError("products explodes")

    code = await run_batch(
        _config(
            workspace=workspace, out_dir=out_dir, journal_path=journal,
            include_products=True,
            max_consecutive_failures=3,
        ),
        extract_fn=fake_profile,
        products_extract_fn=fake_products_always_fails,
    )
    # Profile succeeds for every vendor; products fails for every vendor;
    # batch should NOT trip the failure cap.
    assert code == BatchExitCode.SUCCESS
    events = _read_journal(journal)
    products_errors = [e for e in events if e["event"] == "products_error"]
    assert len(products_errors) == 8


@pytest.mark.asyncio
async def test_products_cost_contributes_to_aggregate_cap(tmp_path: Path) -> None:
    """Products cost rolls into total_cost_usd and counts against batch cap."""
    workspace = _make_workspace(tmp_path, ["v1", "v2", "v3", "v4"])
    out_dir = tmp_path / "out"
    journal = tmp_path / "j.jsonl"

    async def fake_profile(corpus, **_):
        return _profile(target_id=corpus.target_id, cost=1.0)  # $1 each

    async def fake_products(corpus, **_):
        return _product_catalog(target_id=corpus.target_id, cost=1.0)  # $1 each

    code = await run_batch(
        _config(
            workspace=workspace, out_dir=out_dir, journal_path=journal,
            include_products=True,
            batch_cost_usd=4.0,  # 2 vendors at $1 profile + $1 products = $4
        ),
        extract_fn=fake_profile,
        products_extract_fn=fake_products,
    )
    assert code == BatchExitCode.BUDGET_EXHAUSTED


@pytest.mark.asyncio
async def test_products_pass_skipped_when_profile_pass_fails(tmp_path: Path) -> None:
    """If the profile pass fails for a target, the products pass is not run."""
    workspace = _make_workspace(tmp_path, ["v1"])
    out_dir = tmp_path / "out"
    journal = tmp_path / "j.jsonl"

    products_calls: list[str] = []

    async def fake_profile_fails(corpus, **_):
        raise RuntimeError("profile fails")

    async def fake_products(corpus, **_):
        products_calls.append(corpus.target_id)
        return _product_catalog(target_id=corpus.target_id)

    await run_batch(
        _config(workspace=workspace, out_dir=out_dir, journal_path=journal, include_products=True),
        extract_fn=fake_profile_fails,
        products_extract_fn=fake_products,
    )
    assert products_calls == []


# -- SDK subprocess error classification ------------------------------------


@pytest.mark.asyncio
async def test_batch_halts_immediately_on_balance_exhausted(tmp_path: Path) -> None:
    """Balance-exhausted error must stop the batch with a distinct exit
    code so the user doesn't burn more credit attempts."""
    from uxv_extract.agent import SDKSubprocessError

    workspace = _make_workspace(tmp_path, [f"v{i}" for i in range(8)])
    out_dir = tmp_path / "out"
    journal = tmp_path / "j.jsonl"

    call_count = 0

    async def fake_extract(corpus, **_):
        nonlocal call_count
        call_count += 1
        raise SDKSubprocessError(
            Exception("Command failed with exit code 1"),
            "anthropic API: Your credit balance is too low to access "
            "the API. Please go to Plans & Billing to upgrade or top up.",
        )

    code = await run_batch(
        _config(workspace=workspace, out_dir=out_dir, journal_path=journal),
        extract_fn=fake_extract,
    )
    assert code == BatchExitCode.BALANCE_EXHAUSTED
    # Should have stopped after the FIRST failure, not chewed through 8.
    assert call_count == 1
    events = _read_journal(journal)
    assert any(e["event"] == "stop_balance_exhausted" for e in events)
    err = next(e for e in events if e["event"] == "error")
    assert "credit balance" in err["error"].lower()


@pytest.mark.asyncio
async def test_batch_halts_immediately_on_auth_error(tmp_path: Path) -> None:
    from uxv_extract.agent import SDKSubprocessError

    workspace = _make_workspace(tmp_path, ["v1", "v2", "v3"])
    out_dir = tmp_path / "out"
    journal = tmp_path / "j.jsonl"

    call_count = 0

    async def fake_extract(corpus, **_):
        nonlocal call_count
        call_count += 1
        raise SDKSubprocessError(
            Exception("Command failed with exit code 1"),
            "401 Unauthorized: invalid x-api-key",
        )

    code = await run_batch(
        _config(workspace=workspace, out_dir=out_dir, journal_path=journal),
        extract_fn=fake_extract,
    )
    assert code == BatchExitCode.AUTH_ERROR
    assert call_count == 1


@pytest.mark.asyncio
async def test_batch_treats_unknown_subprocess_error_as_normal_failure(tmp_path: Path) -> None:
    """A non-fatal subprocess error counts toward consecutive_failures
    but doesn't halt the whole batch on its own."""
    from uxv_extract.agent import SDKSubprocessError

    workspace = _make_workspace(tmp_path, ["v1", "v2", "v3"])
    out_dir = tmp_path / "out"
    journal = tmp_path / "j.jsonl"

    pattern = iter(["fail", "ok", "ok"])

    async def fake_extract(corpus, **_):
        outcome = next(pattern)
        if outcome == "fail":
            raise SDKSubprocessError(
                Exception("Command failed"),
                "Some unexpected stderr that doesn't match balance/auth patterns",
            )
        return _profile(target_id=corpus.target_id)

    code = await run_batch(
        _config(workspace=workspace, out_dir=out_dir, journal_path=journal),
        extract_fn=fake_extract,
    )
    assert code == BatchExitCode.SUCCESS
    # 1 failure, 2 successes — failure cap not tripped.


@pytest.mark.asyncio
async def test_balance_error_in_products_pass_halts_batch(tmp_path: Path) -> None:
    """Balance error is fatal whether it happens in profile or products."""
    from uxv_extract.agent import SDKSubprocessError

    workspace = _make_workspace(tmp_path, ["v1", "v2", "v3"])
    out_dir = tmp_path / "out"
    journal = tmp_path / "j.jsonl"

    products_calls = 0

    async def fake_profile(corpus, **_):
        return _profile(target_id=corpus.target_id)

    async def fake_products(corpus, **_):
        nonlocal products_calls
        products_calls += 1
        raise SDKSubprocessError(
            Exception("Command failed"),
            "anthropic API: Your credit balance is too low",
        )

    code = await run_batch(
        _config(
            workspace=workspace, out_dir=out_dir, journal_path=journal,
            include_products=True,
        ),
        extract_fn=fake_profile,
        products_extract_fn=fake_products,
    )
    assert code == BatchExitCode.BALANCE_EXHAUSTED
    # Halted on first products-pass balance error; should not have run
    # products on all 3.
    assert products_calls == 1


# -- Error classifier helpers ------------------------------------------------


def test_is_fatal_balance_error_matches_real_message() -> None:
    from uxv_extract.agent import is_fatal_balance_error

    assert is_fatal_balance_error(
        "anthropic API: Your credit balance is too low to access the API. "
        "Please go to Plans & Billing to upgrade or top up."
    )
    assert is_fatal_balance_error("Insufficient credits for this request.")
    assert is_fatal_balance_error("error: insufficient_credits")
    # Negative cases:
    assert not is_fatal_balance_error("connection reset by peer")
    assert not is_fatal_balance_error("rate limit exceeded; retry after 60s")
    assert not is_fatal_balance_error("")
    assert not is_fatal_balance_error(None)  # type: ignore[arg-type]


def test_is_fatal_auth_error_matches_common_signatures() -> None:
    from uxv_extract.agent import is_fatal_auth_error

    assert is_fatal_auth_error("401 Unauthorized: invalid x-api-key")
    assert is_fatal_auth_error("authentication_error: Invalid API key")
    assert is_fatal_auth_error("403 Forbidden")
    assert not is_fatal_auth_error("connection refused")
    assert not is_fatal_auth_error("")


@pytest.mark.asyncio
async def test_batch_passes_per_vendor_caps_to_extractor(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path, ["v1"])
    out_dir = tmp_path / "extract_out"
    journal = tmp_path / "journal.jsonl"

    captured: dict[str, Any] = {}

    async def fake_extract(corpus, **kwargs):
        captured.update(kwargs)
        return _profile(target_id=corpus.target_id)

    await run_batch(
        _config(
            workspace=workspace,
            out_dir=out_dir,
            journal_path=journal,
            per_vendor_cost_usd=1.25,
            per_vendor_timeout_sec=42,
        ),
        extract_fn=fake_extract,
    )

    assert captured["max_cost_usd"] == 1.25
    # timeout_sec is now derived from `vendor_remaining()`, so it's a
    # float close to (but not exactly) the budget at vendor start. The
    # full-budget-vs-shared semantics are tested below.
    assert 41.0 <= captured["timeout_sec"] <= 42.0


@pytest.mark.asyncio
async def test_per_vendor_timeout_is_shared_across_passes(tmp_path: Path) -> None:
    """`per_vendor_timeout_sec` is the TOTAL across all passes for one
    vendor. If profile takes 0.2s, products should get ~budget - 0.2s
    as its timeout — NOT a fresh full budget."""
    import asyncio
    workspace = _make_workspace(tmp_path, ["v1"])
    out_dir = tmp_path / "extract_out"
    journal = tmp_path / "journal.jsonl"

    captured_timeouts: dict[str, float] = {}

    async def fake_profile(corpus, **kwargs):
        captured_timeouts["profile"] = kwargs["timeout_sec"]
        await asyncio.sleep(0.2)
        return _profile(target_id=corpus.target_id)

    async def fake_products(corpus, **kwargs):
        captured_timeouts["products"] = kwargs["timeout_sec"]
        return _product_catalog(target_id=corpus.target_id)

    cfg = _config(
        workspace=workspace, out_dir=out_dir, journal_path=journal,
        per_vendor_timeout_sec=2.0,
        include_products=True,
    )
    # Disable triage so this test only spans profile + products.
    cfg.max_products = 0
    await run_batch(
        cfg,
        extract_fn=fake_profile,
        products_extract_fn=fake_products,
    )

    # Profile gets the full ~2.0s budget at vendor start.
    assert 1.9 <= captured_timeouts["profile"] <= 2.0
    # After ~0.2s spent on profile, products gets ~1.8s left
    # (NOT a fresh 2.0s, which would mean per-pass budget — the bug
    # we're guarding against).
    assert 1.5 <= captured_timeouts["products"] <= 1.85


@pytest.mark.asyncio
async def test_products_pass_uses_max_turns_floor(tmp_path: Path) -> None:
    """Products is incremental — needs more turns than profile/triage.
    Even when config.max_turns is low (e.g. 30 for profile/triage),
    the products pass receives at least PRODUCTS_MAX_TURNS_FLOOR (100)."""
    from uxv_extract.batch import PRODUCTS_MAX_TURNS_FLOOR

    workspace = _make_workspace(tmp_path, ["v1"])
    out_dir = tmp_path / "extract_out"
    journal = tmp_path / "journal.jsonl"

    captured: dict[str, int] = {}

    async def fake_profile(corpus, **kwargs):
        captured["profile"] = kwargs["max_turns"]
        return _profile(target_id=corpus.target_id)

    async def fake_products(corpus, **kwargs):
        captured["products"] = kwargs["max_turns"]
        return _product_catalog(target_id=corpus.target_id)

    cfg = _config(
        workspace=workspace, out_dir=out_dir, journal_path=journal,
        include_products=True,
    )
    cfg.max_turns = 30  # tight cap for profile/triage
    cfg.max_products = 0  # skip triage for this test
    await run_batch(cfg, extract_fn=fake_profile, products_extract_fn=fake_products)

    # Profile gets the tight cap as-is:
    assert captured["profile"] == 30
    # Products gets at least the incremental floor:
    assert captured["products"] >= PRODUCTS_MAX_TURNS_FLOOR


@pytest.mark.asyncio
async def test_per_vendor_timeout_is_disabled_when_none(tmp_path: Path) -> None:
    """When per_vendor_timeout_sec is None, every pass gets timeout=None."""
    workspace = _make_workspace(tmp_path, ["v1"])
    out_dir = tmp_path / "extract_out"
    journal = tmp_path / "journal.jsonl"

    captured: list[Any] = []

    async def fake_extract(corpus, **kwargs):
        captured.append(kwargs["timeout_sec"])
        return _profile(target_id=corpus.target_id)

    cfg = _config(workspace=workspace, out_dir=out_dir, journal_path=journal)
    cfg.per_vendor_timeout_sec = None
    await run_batch(cfg, extract_fn=fake_extract)
    assert captured == [None]


@pytest.mark.asyncio
async def test_batch_runs_tagline_pass_after_profile(tmp_path: Path) -> None:
    """`include_tagline=True` (default) runs Haiku tagline gen after
    profile pass succeeds; tagline gets persisted into profile.json
    and a `tagline_ok` event is journaled."""
    from unittest.mock import AsyncMock, MagicMock, patch

    workspace = _make_workspace(tmp_path, ["v1"])
    out_dir = tmp_path / "extract_out"
    journal = tmp_path / "journal.jsonl"

    async def fake_extract(corpus, **_):
        return _profile(target_id=corpus.target_id)

    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text="Heavy-lift OEM drone vendor; NDAA status undisclosed.")]
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=mock_msg)

    with patch("uxv_extract.tagline_agent.anthropic.AsyncAnthropic", return_value=mock_client):
        await run_batch(
            _config(workspace=workspace, out_dir=out_dir, journal_path=journal),
            extract_fn=fake_extract,
        )

    events = _read_journal(journal)
    assert any(e["event"] == "tagline_ok" and e["target_id"] == "v1" for e in events)
    written = json.loads((out_dir / "v1" / "profile.json").read_text())
    assert written["tagline"] == "Heavy-lift OEM drone vendor; NDAA status undisclosed."


@pytest.mark.asyncio
async def test_tagline_failure_does_not_break_vendor(tmp_path: Path) -> None:
    """Tagline pass failure is isolated — profile still on disk,
    `tagline_error` journaled, batch exits SUCCESS."""
    from unittest.mock import AsyncMock, MagicMock, patch

    workspace = _make_workspace(tmp_path, ["v1"])
    out_dir = tmp_path / "extract_out"
    journal = tmp_path / "journal.jsonl"

    async def fake_extract(corpus, **_):
        return _profile(target_id=corpus.target_id)

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=RuntimeError("simulated network failure"))

    with patch("uxv_extract.tagline_agent.anthropic.AsyncAnthropic", return_value=mock_client):
        code = await run_batch(
            _config(workspace=workspace, out_dir=out_dir, journal_path=journal),
            extract_fn=fake_extract,
        )

    assert code == BatchExitCode.SUCCESS
    events = _read_journal(journal)
    assert any(e["event"] == "tagline_error" and e["target_id"] == "v1" for e in events)
    # Profile still wrote successfully:
    assert (out_dir / "v1" / "profile.json").exists()


@pytest.mark.asyncio
async def test_tagline_skipped_when_already_present(tmp_path: Path) -> None:
    """Idempotent: if profile already has a tagline, skip Haiku
    (no API call, log skip event)."""
    from unittest.mock import AsyncMock, MagicMock, patch

    workspace = _make_workspace(tmp_path, ["v1"])
    out_dir = tmp_path / "extract_out"
    journal = tmp_path / "journal.jsonl"

    async def fake_extract(corpus, **_):
        # Profile arrives with a tagline already set (e.g. resumed run).
        return _profile(target_id=corpus.target_id).model_copy(
            update={"tagline": "pre-existing tagline"}
        )

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock()  # would crash if called

    with patch("uxv_extract.tagline_agent.anthropic.AsyncAnthropic", return_value=mock_client):
        await run_batch(
            _config(workspace=workspace, out_dir=out_dir, journal_path=journal),
            extract_fn=fake_extract,
        )

    mock_client.messages.create.assert_not_called()
    events = _read_journal(journal)
    assert any(e["event"] == "tagline_skip_existing" and e["target_id"] == "v1" for e in events)
