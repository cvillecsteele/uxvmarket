from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from uxv_extract.cli import build_arg_parser, default_output_path, run_profile_command
from uxv_extract.schema import (
    Answer,
    Citation,
    ListAnswer,
    Profile,
    ProfileMeta,
)


def _unknown_answer() -> Answer:
    return Answer(value=None, confidence="low", status="unknown", evidence=[], notes=None)


def _unknown_list_answer() -> ListAnswer:
    return ListAnswer(items=[], confidence="low", status="unknown", notes=None)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "corpus_min"


def _good_profile(target_id: str = "test-vendor", run_id: str = "test-run") -> Profile:
    return Profile(
        target_id=target_id,
        run_id=run_id,
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
        status="complete",
        meta=ProfileMeta(
            model="claude-sonnet-4-6",
            num_turns=4,
            total_cost_usd=0.1,
            created_at="2026-05-01T00:00:00+00:00",
            extract_version="0.1.0",
        ),
    )


def test_arg_parser_required_flags() -> None:
    parser = build_arg_parser()
    args = parser.parse_args(
        [
            "profile",
            "--run-id",
            "r1",
            "--target-id",
            "t1",
            "--workspace-root",
            "/path/to/mirroring",
        ]
    )
    assert args.command == "profile"
    assert args.run_id == "r1"
    assert args.target_id == "t1"
    assert args.workspace_root == Path("/path/to/mirroring")
    assert args.model == "claude-sonnet-4-6"
    # `profile` subcommand uses the tighter local cap (atomic submit).
    assert args.max_turns == 30
    assert args.out is None


def test_arg_parser_overrides() -> None:
    parser = build_arg_parser()
    args = parser.parse_args(
        [
            "profile",
            "--run-id",
            "r1",
            "--target-id",
            "t1",
            "--workspace-root",
            "/w",
            "--model",
            "claude-haiku-4-5",
            "--max-turns",
            "50",
            "--out",
            "/tmp/foo.json",
        ]
    )
    assert args.model == "claude-haiku-4-5"
    assert args.max_turns == 50
    assert args.out == Path("/tmp/foo.json")


def test_default_output_path(tmp_path: Path) -> None:
    out = default_output_path(
        package_root=tmp_path,
        run_id="r1",
        target_id="harris-aerial",
    )
    assert out == tmp_path / "output" / "runs" / "r1" / "harris-aerial" / "profile.json"


def test_run_profile_command_writes_validated_json(tmp_path: Path) -> None:
    workspace = tmp_path / "mirroring"
    target_dir = workspace / "output" / "runs" / "test-run" / "targets" / "test-vendor"
    target_dir.parent.mkdir(parents=True)
    shutil.copytree(FIXTURE_ROOT, target_dir)

    out_path = tmp_path / "extract" / "output" / "runs" / "test-run" / "test-vendor" / "profile.json"

    expected = _good_profile()

    async def fake_extract(corpus, *, model, max_turns, max_cost_usd=None, timeout_sec=None, query_fn=None):
        return expected

    with patch("uxv_extract.cli.run_profile_extraction", fake_extract):
        rc = asyncio.run(
            run_profile_command(
                run_id="test-run",
                target_id="test-vendor",
                workspace_root=workspace,
                out=out_path,
                model="claude-sonnet-4-6",
                max_turns=10,
            )
        )

    assert rc == 0
    assert out_path.exists()
    payload = json.loads(out_path.read_text())
    assert payload["target_id"] == "test-vendor"
    assert payload["drone_supply_chain_role"]["value"] == "oem"
    # round-trips:
    Profile.model_validate(payload)


def test_run_profile_command_returns_nonzero_when_corpus_missing(tmp_path: Path) -> None:
    out_path = tmp_path / "out" / "profile.json"
    rc = asyncio.run(
        run_profile_command(
            run_id="missing-run",
            target_id="missing-vendor",
            workspace_root=tmp_path / "no-such-mirror",
            out=out_path,
            model="claude-sonnet-4-6",
            max_turns=10,
        )
    )
    assert rc != 0
    assert not out_path.exists()


def test_run_profile_command_via_vendor_slug(tmp_path: Path) -> None:
    """--vendor-slug reads <vendors_root>/<slug>/website/ and writes
    extract output under runs/<slug>-canonical/<slug>/."""
    vendors = tmp_path / "vendors"
    canonical = vendors / "test-vendor" / "website"
    canonical.parent.mkdir(parents=True)
    shutil.copytree(FIXTURE_ROOT, canonical)

    out_path = tmp_path / "extract" / "out" / "profile.json"
    expected = _good_profile()

    async def fake_extract(corpus, *, model, max_turns, max_cost_usd=None, timeout_sec=None, query_fn=None):
        # Verify the loaded corpus is the canonical one:
        assert corpus.corpus_root == canonical
        return expected

    with patch("uxv_extract.cli.run_profile_extraction", fake_extract):
        rc = asyncio.run(
            run_profile_command(
                vendor_slug="test-vendor",
                vendors_root=vendors,
                out=out_path,
                model="claude-sonnet-4-6",
                max_turns=10,
            )
        )

    assert rc == 0
    assert out_path.exists()


def test_arg_parser_accepts_vendor_slug() -> None:
    parser = build_arg_parser()
    args = parser.parse_args(
        [
            "profile",
            "--vendor-slug", "honeywell",
            "--vendors-root", "/tmp/vendors",
        ]
    )
    assert args.vendor_slug == "honeywell"
    assert args.vendors_root == Path("/tmp/vendors")
    assert args.run_id is None
    assert args.target_id is None
    assert args.workspace_root is None


def test_main_rejects_vendor_slug_with_run_id(capsys) -> None:
    """argparse exit-on-error: mutually exclusive group enforced."""
    from uxv_extract.cli import main

    with pytest.raises(SystemExit) as exc_info:
        main([
            "profile",
            "--vendor-slug", "honeywell",
            "--run-id", "r1",
            "--target-id", "t1",
            "--workspace-root", "/w",
        ])
    assert exc_info.value.code != 0
    err = capsys.readouterr().err
    assert "mutually exclusive" in err


def test_main_rejects_partial_legacy_flags(capsys) -> None:
    """All three legacy flags must be present when --vendor-slug is absent."""
    from uxv_extract.cli import main

    with pytest.raises(SystemExit) as exc_info:
        main([
            "profile",
            "--run-id", "r1",
            "--target-id", "t1",
            # missing --workspace-root
        ])
    assert exc_info.value.code != 0
    assert "must pass either" in capsys.readouterr().err
