from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from uxv_extract.batch import BatchExitCode
from uxv_extract.loop import LoopConfig, run_loop
from uxv_extract.schema import (
    Answer,
    Citation,
    FetchRequest,
    ListAnswer,
    Profile,
    ProfileMeta,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "corpus_min"


def _unknown_answer() -> Answer:
    return Answer(value=None, confidence="low", status="unknown", evidence=[], notes=None)


def _unknown_list_answer() -> ListAnswer:
    return ListAnswer(items=[], confidence="low", status="unknown", notes=None)


def _profile_with_followups(target_id: str, *, urls: list[str]) -> Profile:
    return Profile(
        target_id=target_id,
        run_id="src",
        display_name=target_id.replace("-", " ").title(),
        homepage_url=f"https://{target_id}.example",
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
                    url=f"https://{target_id}.example",
                    page_class="homepage",
                    snippet="x x x x x x x x x x x x",
                )
            ],
        ),
        products=_unknown_list_answer(),
        ndaa=_unknown_answer(),
        blue_uas=_unknown_answer(),
        readiness=_unknown_answer(),
        unresolved_questions=[],
        fetch_requests=[
            FetchRequest(
                url=u,
                reason="needed",
                expected_evidence=["products"],
                in_corpus_index=False,
            )
            for u in urls
        ],
        status="partial",
        meta=ProfileMeta(
            model="claude-sonnet-4-6",
            num_turns=4,
            total_cost_usd=0.5,
            created_at="2026-05-02T00:00:00+00:00",
            extract_version="0.1.0",
        ),
    )


def _seed_source_run(extract_root: Path, run_id: str, *, profiles: dict[str, list[str]]) -> None:
    """Drop synthetic profile.json files under extract_root/output/runs/<run_id>/."""
    src = extract_root / "output" / "runs" / run_id
    for tid, urls in profiles.items():
        d = src / tid
        d.mkdir(parents=True, exist_ok=True)
        (d / "profile.json").write_text(_profile_with_followups(tid, urls=urls).model_dump_json())


def _seed_mirror_workspace(workspace: Path, run_id: str, target_ids: list[str]) -> None:
    """Drop synthetic mirror corpora so the post-mirror batch has something to
    read. Also writes a run-level manifest with quality_status='complete' for
    every target so loop's mirror-B validation passes."""
    corpora = []
    for tid in target_ids:
        target_dir = workspace / "output" / "runs" / run_id / "targets" / tid
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(FIXTURE_ROOT, target_dir)
        m = target_dir / "manifest.json"
        manifest = json.loads(m.read_text())
        manifest["target"]["target_id"] = tid
        manifest["run_id"] = run_id
        m.write_text(json.dumps(manifest))
        # quality_report so loop's _inspect_mirror_b_results sees this as ok
        qr_path = target_dir / "quality_report.json"
        qr_path.write_text(json.dumps({
            "status": "complete", "fetched_pages": 5, "failed_pages": 0,
            "discovered_urls": 5, "skipped_urls": 0, "fetched_documents": 0,
            "total_text_chars": 1234, "browserless_calls_used": 5, "reasons": [],
        }))
        corpora.append({
            "target_id": tid, "quality_status": "complete",
            "manifest_path": str(m), "quality_report_path": str(qr_path),
        })
    run_root = workspace / "output" / "runs" / run_id
    (run_root / "manifest.json").write_text(json.dumps({"run_id": run_id, "corpora": corpora}))


def _config(
    *,
    extract_root: Path,
    workspace: Path,
    source_run_id: str = "src",
    new_run_id: str = "src-followup-1",
    target_ids: list[str] | None = None,
    include_products: bool = False,
) -> LoopConfig:
    return LoopConfig(
        source_run_id=source_run_id,
        new_run_id=new_run_id,
        workspace_root=workspace,
        extract_root=extract_root,
        target_ids=target_ids,
        include_products=include_products,
        model="claude-sonnet-4-6",
        max_turns=30,
        per_vendor_cost_usd=2.0,
        per_vendor_timeout_sec=600,
        batch_cost_usd=100.0,
        batch_timeout_sec=None,
        max_consecutive_failures=5,
        concurrency=1,
        mirror_cli="uxv-mirror",
        max_mirror_calls_per_target=15,
    )


def _ok_subprocess() -> MagicMock:
    m = MagicMock()
    m.returncode = 0
    return m


@pytest.mark.asyncio
async def test_loop_returns_success_when_source_has_no_followups(tmp_path: Path) -> None:
    """No fetch_requests in any profile → loop is a no-op (success exit)."""
    extract_root = tmp_path / "extract"
    workspace = tmp_path / "mirroring"
    _seed_source_run(extract_root, "src", profiles={"v1": []})

    sub = MagicMock(return_value=_ok_subprocess())
    code = await run_loop(_config(extract_root=extract_root, workspace=workspace),
                          subprocess_run=sub)
    assert code == int(BatchExitCode.SUCCESS)
    sub.assert_not_called()


@pytest.mark.asyncio
async def test_loop_aborts_when_source_dir_missing(tmp_path: Path) -> None:
    extract_root = tmp_path / "extract"  # never created
    workspace = tmp_path / "mirroring"

    sub = MagicMock(return_value=_ok_subprocess())
    code = await run_loop(
        _config(extract_root=extract_root, workspace=workspace, source_run_id="never"),
        subprocess_run=sub,
    )
    assert code == int(BatchExitCode.SETUP_ERROR)
    sub.assert_not_called()


@pytest.mark.asyncio
async def test_loop_writes_followups_jsonl_with_aggregated_urls(tmp_path: Path) -> None:
    extract_root = tmp_path / "extract"
    workspace = tmp_path / "mirroring"
    _seed_source_run(
        extract_root, "src",
        profiles={
            "v1": ["https://v1.example/products", "https://v1.example/about"],
            "v2": ["https://v2.example/spec"],
            "v3": [],  # no followups → not included
        },
    )

    captured: dict[str, Any] = {}

    def fake_subprocess(cmd, **_):
        captured["cmd"] = cmd
        captured["jsonl_path"] = Path([cmd[i + 1] for i, x in enumerate(cmd)
                                       if x == "--target-file"][0])
        return _ok_subprocess()

    async def fake_extract(corpus, **_):
        return _profile_with_followups(corpus.target_id, urls=[]).model_copy(
            update={"fetch_requests": [], "status": "complete"},
        )

    _seed_mirror_workspace(workspace, "src-followup-1", ["v1", "v2"])

    code = await run_loop(
        _config(extract_root=extract_root, workspace=workspace),
        subprocess_run=fake_subprocess,
        extract_fn=fake_extract,
    )
    assert code == int(BatchExitCode.SUCCESS)
    assert captured["jsonl_path"].exists()
    rows = [json.loads(l) for l in captured["jsonl_path"].read_text().splitlines() if l]
    by_tid = {r["target_id"]: r for r in rows}
    assert set(by_tid) == {"v1", "v2"}  # v3 had no followups
    assert sorted(fu["url"] for fu in by_tid["v1"]["follow_ups"]) == [
        "https://v1.example/about",
        "https://v1.example/products",
    ]


@pytest.mark.asyncio
async def test_loop_invokes_mirror_with_force_and_max_calls(tmp_path: Path) -> None:
    extract_root = tmp_path / "extract"
    workspace = tmp_path / "mirroring"
    _seed_source_run(extract_root, "src", profiles={"v1": ["https://v1.example/x"]})
    _seed_mirror_workspace(workspace, "src-followup-1", ["v1"])

    captured = {}

    def fake_subprocess(cmd, **_):
        captured["cmd"] = list(cmd)
        return _ok_subprocess()

    async def fake_extract(corpus, **_):
        return _profile_with_followups(corpus.target_id, urls=[]).model_copy(
            update={"fetch_requests": [], "status": "complete"},
        )

    cfg = _config(extract_root=extract_root, workspace=workspace)
    cfg.max_mirror_calls_per_target = 25
    cfg.mirror_cli = "/usr/local/bin/uxv-mirror"

    await run_loop(cfg, subprocess_run=fake_subprocess, extract_fn=fake_extract)

    cmd = captured["cmd"]
    assert cmd[0] == "/usr/local/bin/uxv-mirror"
    assert "mirror" in cmd
    assert "--force" in cmd
    assert "--max-calls-per-target" in cmd
    assert cmd[cmd.index("--max-calls-per-target") + 1] == "25"
    assert "--run-id" in cmd
    assert cmd[cmd.index("--run-id") + 1] == "src-followup-1"


@pytest.mark.asyncio
async def test_loop_passes_max_products_into_round_b_batch(tmp_path: Path) -> None:
    """Round-B batch must receive the same triage cap as round A —
    round-B corpora are larger so the 32k output cap is more likely
    without triage."""
    extract_root = tmp_path / "extract"
    workspace = tmp_path / "mirroring"
    _seed_source_run(extract_root, "src", profiles={"v1": ["https://v1.example/x"]})
    _seed_mirror_workspace(workspace, "src-followup-1", ["v1"])

    captured_batch: dict[str, Any] = {}

    async def fake_extract(corpus, **kw):
        captured_batch["max_products_seen_in_extract"] = kw.get("max_products")
        return _profile_with_followups(corpus.target_id, urls=[]).model_copy(
            update={"fetch_requests": [], "status": "complete"},
        )

    # Spy on BatchConfig that loop builds, by monkey-patching run_batch.
    from uxv_extract import loop as loop_mod
    captured_cfg: dict[str, Any] = {}
    real_run_batch = loop_mod.run_batch

    async def spy_run_batch(cfg, **kw):
        captured_cfg["max_products"] = cfg.max_products
        captured_cfg["include_products"] = cfg.include_products
        return await real_run_batch(cfg, **kw)

    loop_mod.run_batch = spy_run_batch
    try:
        cfg = _config(extract_root=extract_root, workspace=workspace,
                      include_products=True)
        cfg.max_products = 15
        await run_loop(
            cfg,
            subprocess_run=lambda cmd, **_: _ok_subprocess(),
            extract_fn=fake_extract,
        )
    finally:
        loop_mod.run_batch = real_run_batch

    assert captured_cfg["max_products"] == 15
    assert captured_cfg["include_products"] is True


@pytest.mark.asyncio
async def test_loop_aborts_when_mirror_subprocess_fails(tmp_path: Path) -> None:
    extract_root = tmp_path / "extract"
    workspace = tmp_path / "mirroring"
    _seed_source_run(extract_root, "src", profiles={"v1": ["https://v1.example/x"]})

    fail = MagicMock()
    fail.returncode = 2

    extract_called = False
    async def fake_extract(corpus, **_):
        nonlocal extract_called
        extract_called = True
        return _profile_with_followups(corpus.target_id, urls=[])

    code = await run_loop(
        _config(extract_root=extract_root, workspace=workspace),
        subprocess_run=lambda cmd, **_: fail,
        extract_fn=fake_extract,
    )
    assert code == int(BatchExitCode.SETUP_ERROR)
    assert extract_called is False


# -- Mirror-B quality validation -------------------------------------------


def _write_run_manifest(workspace: Path, run_id: str, corpora: list[dict]) -> None:
    """Drop a synthetic run manifest mimicking what mirror writes."""
    run_root = workspace / "output" / "runs" / run_id
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "manifest.json").write_text(json.dumps({"run_id": run_id, "corpora": corpora}))


def _write_quality_report(workspace: Path, run_id: str, target_id: str, *,
                          status: str, reasons: list[str]) -> Path:
    target_dir = workspace / "output" / "runs" / run_id / "targets" / target_id
    target_dir.mkdir(parents=True, exist_ok=True)
    qr_path = target_dir / "quality_report.json"
    qr_path.write_text(json.dumps({
        "status": status, "fetched_pages": 0 if status == "failed" else 5,
        "failed_pages": 1 if status == "failed" else 0,
        "discovered_urls": 1, "skipped_urls": 0, "fetched_documents": 0,
        "total_text_chars": 0 if status == "failed" else 1234,
        "browserless_calls_used": 1, "reasons": reasons,
    }))
    return qr_path


@pytest.mark.asyncio
async def test_loop_aborts_when_all_mirror_targets_failed(tmp_path: Path) -> None:
    """Mirror exits 0 but all targets have quality_status=failed → abort
    with SETUP_ERROR and surface the per-target reasons. Common cause:
    missing BROWSERLESS_API_KEY."""
    extract_root = tmp_path / "extract"
    workspace = tmp_path / "mirroring"
    _seed_source_run(extract_root, "src", profiles={
        "v1": ["https://v1.example/x"],
        "v2": ["https://v2.example/y"],
    })

    def fake_subprocess(cmd, **_):
        # Simulate mirror writing a run manifest where both targets failed.
        run_id = cmd[cmd.index("--run-id") + 1]
        for tid in ["v1", "v2"]:
            qr = _write_quality_report(workspace, run_id, tid,
                                       status="failed",
                                       reasons=["Set BROWSERLESS_API_KEY or BROWSERLESS_TOKEN"])
        _write_run_manifest(workspace, run_id, corpora=[
            {"target_id": "v1", "quality_status": "failed",
             "quality_report_path": str(workspace / "output" / "runs" / run_id /
                                        "targets" / "v1" / "quality_report.json")},
            {"target_id": "v2", "quality_status": "failed",
             "quality_report_path": str(workspace / "output" / "runs" / run_id /
                                        "targets" / "v2" / "quality_report.json")},
        ])
        return _ok_subprocess()

    extract_called = False
    async def fake_extract(corpus, **_):
        nonlocal extract_called
        extract_called = True
        return _profile_with_followups(corpus.target_id, urls=[])

    code = await run_loop(
        _config(extract_root=extract_root, workspace=workspace),
        subprocess_run=fake_subprocess,
        extract_fn=fake_extract,
    )
    assert code == int(BatchExitCode.SETUP_ERROR)
    assert extract_called is False  # never proceeded to re-extract


@pytest.mark.asyncio
async def test_loop_proceeds_with_successful_subset_when_some_targets_failed(tmp_path: Path) -> None:
    """Some succeeded, some failed → re-extract runs on the successful subset."""
    extract_root = tmp_path / "extract"
    workspace = tmp_path / "mirroring"
    _seed_source_run(extract_root, "src", profiles={
        "v1": ["https://v1.example/x"],
        "v2": ["https://v2.example/y"],
    })

    def fake_subprocess(cmd, **_):
        run_id = cmd[cmd.index("--run-id") + 1]
        for tid, status in [("v1", "complete"), ("v2", "failed")]:
            _write_quality_report(workspace, run_id, tid, status=status,
                                  reasons=[] if status == "complete" else ["network error"])
        _write_run_manifest(workspace, run_id, corpora=[
            {"target_id": "v1", "quality_status": "complete",
             "quality_report_path": str(workspace / "output" / "runs" / run_id /
                                        "targets" / "v1" / "quality_report.json")},
            {"target_id": "v2", "quality_status": "failed",
             "quality_report_path": str(workspace / "output" / "runs" / run_id /
                                        "targets" / "v2" / "quality_report.json")},
        ])
        return _ok_subprocess()

    # Seed a real corpus dir for v1 so re-extract has something to read
    _seed_mirror_workspace(workspace, "src-followup-1", ["v1"])

    extract_called: list[str] = []
    async def fake_extract(corpus, **_):
        extract_called.append(corpus.target_id)
        return _profile_with_followups(corpus.target_id, urls=[]).model_copy(
            update={"fetch_requests": [], "status": "complete"},
        )

    code = await run_loop(
        _config(extract_root=extract_root, workspace=workspace),
        subprocess_run=fake_subprocess,
        extract_fn=fake_extract,
    )
    assert code == int(BatchExitCode.SUCCESS)
    assert extract_called == ["v1"]  # only v1 (v2 was failed)


@pytest.mark.asyncio
async def test_loop_target_id_filter_restricts_followups_and_extract(tmp_path: Path) -> None:
    extract_root = tmp_path / "extract"
    workspace = tmp_path / "mirroring"
    _seed_source_run(
        extract_root, "src",
        profiles={
            "v1": ["https://v1.example/x"],
            "v2": ["https://v2.example/y"],
            "v3": ["https://v3.example/z"],
        },
    )
    _seed_mirror_workspace(workspace, "src-followup-1", ["v1", "v2", "v3"])

    captured = {}
    def fake_subprocess(cmd, **_):
        path = Path([cmd[i + 1] for i, x in enumerate(cmd) if x == "--target-file"][0])
        captured["jsonl"] = path
        return _ok_subprocess()

    extract_called: list[str] = []
    async def fake_extract(corpus, **_):
        extract_called.append(corpus.target_id)
        return _profile_with_followups(corpus.target_id, urls=[]).model_copy(
            update={"fetch_requests": [], "status": "complete"},
        )

    cfg = _config(extract_root=extract_root, workspace=workspace, target_ids=["v2"])
    code = await run_loop(cfg, subprocess_run=fake_subprocess, extract_fn=fake_extract)

    assert code == int(BatchExitCode.SUCCESS)
    rows = [json.loads(l) for l in captured["jsonl"].read_text().splitlines() if l]
    assert {r["target_id"] for r in rows} == {"v2"}
    assert extract_called == ["v2"]
