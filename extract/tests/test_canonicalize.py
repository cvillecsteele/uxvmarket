from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from uxv_extract.canonicalize import (
    _best_status,
    _merge_products_pair,
    _merge_profile_pair,
    _newer_answer_wins,
    canonicalize_vendor,
    merge_products,
    merge_profiles,
    migrate_citations_in_doc,
)


def _answer(value, status="answered", confidence="high"):
    return {
        "value": value,
        "status": status,
        "confidence": confidence,
        "evidence": [],
        "notes": None,
    }


def _list_answer(items=None, status="answered", confidence="high"):
    return {
        "items": items or [],
        "status": status,
        "confidence": confidence,
        "notes": None,
    }


def _bare_profile(**overrides):
    base = {
        "target_id": "v",
        "run_id": "test-run",
        "display_name": "V",
        "homepage_url": "https://v.example",
        "corpus_root": "/abs/path",
        "products_categories": _list_answer(status="unknown"),
        "headquarters": _answer(None, status="unknown"),
        "drone_supply_chain_role": _answer(None, status="unknown"),
        "products": _list_answer(status="unknown"),
        "ndaa": _answer(None, status="unknown"),
        "blue_uas": _answer(None, status="unknown"),
        "readiness": _answer(None, status="unknown"),
        "unresolved_questions": [],
        "fetch_requests": [],
        "status": "unknown",
        "tagline": None,
        "meta": {
            "model": "claude-sonnet-4-6",
            "num_turns": 1, "total_cost_usd": 0.0,
            "created_at": "2026-05-01T00:00:00+00:00",
            "extract_version": "0.1.0",
        },
    }
    base.update(overrides)
    return base


# --- Status + answer rules ------------------------------------------------


def test_best_status_picks_highest_rank() -> None:
    assert _best_status("partial", "complete", "failed") == "complete"
    assert _best_status("partial", "failed") == "partial"
    assert _best_status(None, "partial") == "partial"
    assert _best_status() == "unknown"


def test_newer_answer_wins_when_newer_answered_and_older_unknown() -> None:
    older = {"status": "unknown"}
    newer = {"status": "answered"}
    assert _newer_answer_wins(older, newer) is True


def test_newer_answer_does_not_win_when_newer_is_unknown() -> None:
    older = {"status": "answered"}
    newer = {"status": "unknown"}
    assert _newer_answer_wins(older, newer) is False


def test_newer_answer_wins_when_both_answered() -> None:
    """Later run gets the data — typically more evidence."""
    assert _newer_answer_wins({"status": "answered"}, {"status": "answered"}) is True


# --- Profile pair-merge --------------------------------------------------


def test_merge_promotes_unknown_to_answered_field_by_field() -> None:
    older = _bare_profile()
    newer = _bare_profile(
        drone_supply_chain_role=_answer("oem"),
        ndaa=_answer("yes"),
    )
    merged = _merge_profile_pair(older, newer)
    assert merged["drone_supply_chain_role"]["value"] == "oem"
    assert merged["drone_supply_chain_role"]["status"] == "answered"
    assert merged["ndaa"]["value"] == "yes"
    # Untouched fields stay as "unknown" from older:
    assert merged["headquarters"]["status"] == "unknown"


def test_merge_does_not_downgrade_answered_to_unknown() -> None:
    older = _bare_profile(drone_supply_chain_role=_answer("oem"))
    newer = _bare_profile(drone_supply_chain_role=_answer(None, status="unknown"))
    merged = _merge_profile_pair(older, newer)
    assert merged["drone_supply_chain_role"]["value"] == "oem"


def test_merge_takes_newer_when_both_answered() -> None:
    older = _bare_profile(drone_supply_chain_role=_answer("oem"))
    newer = _bare_profile(drone_supply_chain_role=_answer("subsystem_supplier"))
    merged = _merge_profile_pair(older, newer)
    assert merged["drone_supply_chain_role"]["value"] == "subsystem_supplier"


def test_merge_takes_newer_fetch_requests_and_unresolved_questions() -> None:
    older = _bare_profile(fetch_requests=[{"a": 1}], unresolved_questions=["old q"])
    newer = _bare_profile(fetch_requests=[{"b": 2}], unresolved_questions=["new q"])
    merged = _merge_profile_pair(older, newer)
    assert merged["fetch_requests"] == [{"b": 2}]
    assert merged["unresolved_questions"] == ["new q"]


def test_merge_status_is_best_across_runs() -> None:
    older = _bare_profile(status="partial")
    newer = _bare_profile(status="complete")
    assert _merge_profile_pair(older, newer)["status"] == "complete"
    older = _bare_profile(status="complete")
    newer = _bare_profile(status="partial")
    assert _merge_profile_pair(older, newer)["status"] == "complete"


def test_merge_keeps_first_non_null_tagline() -> None:
    older = _bare_profile(tagline=None)
    middle = _bare_profile(tagline="middle tagline")
    newer = _bare_profile(tagline="newer tagline")
    after_middle = _merge_profile_pair(older, middle)
    final = _merge_profile_pair(after_middle, newer)
    assert final["tagline"] == "middle tagline"


# --- Multi-file merge_profiles --------------------------------------------


def test_merge_profiles_orders_by_input_order(tmp_path: Path) -> None:
    p1 = tmp_path / "r1" / "v" / "profile.json"
    p2 = tmp_path / "r2" / "v" / "profile.json"
    p1.parent.mkdir(parents=True); p2.parent.mkdir(parents=True)
    p1.write_text(json.dumps(_bare_profile(drone_supply_chain_role=_answer("oem"))))
    p2.write_text(json.dumps(_bare_profile(ndaa=_answer("yes"))))
    merged = merge_profiles([p1, p2])
    # Both fields present:
    assert merged["drone_supply_chain_role"]["value"] == "oem"
    assert merged["ndaa"]["value"] == "yes"
    # No merge metadata in the validated structures (StrictModel forbids
    # extras). Provenance lives in canonicalize_report.json sidecar.
    assert "merged_from_runs" not in merged.get("meta", {})


# --- Products merge --------------------------------------------------------


def _bare_products(**overrides):
    base = {
        "target_id": "v",
        "run_id": "test-run",
        "display_name": "V",
        "homepage_url": "https://v.example",
        "corpus_root": "/abs/path",
        "products": [],
        "unresolved_questions": [],
        "fetch_requests": [],
        "status": "complete",
        "meta": {
            "model": "x", "num_turns": 1, "total_cost_usd": 0.0,
            "created_at": "2026-05-01T00:00:00+00:00",
            "extract_version": "0.1.0",
        },
    }
    base.update(overrides)
    return base


def _product(name, **kw):
    base = {
        "name": name, "category": "airframes", "descriptor": "x",
        "granularity": "sku", "readiness": "production",
        "ndaa": "unknown", "blue_uas": "unknown",
        "evidence": [], "notes": None,
    }
    base.update(kw)
    return base


def test_merge_products_unions_by_name() -> None:
    older = _bare_products(products=[_product("A"), _product("B")])
    newer = _bare_products(products=[_product("B"), _product("C")])
    merged = _merge_products_pair(older, newer)
    names = sorted(p["name"] for p in merged["products"])
    assert names == ["A", "B", "C"]


def test_merge_products_replaces_on_name_conflict() -> None:
    older = _bare_products(products=[_product("A", descriptor="OLD")])
    newer = _bare_products(products=[_product("A", descriptor="NEW")])
    merged = _merge_products_pair(older, newer)
    assert len(merged["products"]) == 1
    assert merged["products"][0]["descriptor"] == "NEW"


# --- Citation migration ----------------------------------------------------


def test_migrate_citations_rewrites_resource_id_via_url_lookup(tmp_path: Path) -> None:
    canonical_text_dir = tmp_path / "text"
    canonical_text_dir.mkdir()
    (canonical_text_dir / "0042-x.txt").write_text("line one\nline two\n")

    doc = {
        "evidence": [
            {
                "source_kind": "mirror",
                "resource_id": "resource-0001",
                "line_start": 1, "line_end": 1,
                "snippet": "line one",
            }
        ]
    }
    canonical_url_to_id = {"https://x.example/p": 42}
    per_run_url_lookup = {"resource-0001": "https://x.example/p"}

    t, m, f = migrate_citations_in_doc(
        doc,
        canonical_url_to_id=canonical_url_to_id,
        canonical_text_dir=canonical_text_dir,
        per_run_url_lookup=per_run_url_lookup,
    )
    assert (t, m, f) == (1, 1, 0)
    assert doc["evidence"][0]["resource_id"] == "resource-0042"
    # No annotation in the citation — Citation is StrictModel, drift
    # signals live in the (total, migrated, flagged) return tuple.
    assert "_canonicalize_note" not in doc["evidence"][0]


def test_migrate_flags_snippet_drift_but_still_rewrites_id(tmp_path: Path) -> None:
    canonical_text_dir = tmp_path / "text"
    canonical_text_dir.mkdir()
    # Canonical text is DIFFERENT from the citation's stored snippet.
    (canonical_text_dir / "0042-x.txt").write_text("DIFFERENT line one\n")

    doc = {
        "evidence": [
            {
                "source_kind": "mirror",
                "resource_id": "resource-0001",
                "line_start": 1, "line_end": 1,
                "snippet": "original line one",
            }
        ]
    }
    t, m, flagged = migrate_citations_in_doc(
        doc,
        canonical_url_to_id={"https://x.example/p": 42},
        canonical_text_dir=canonical_text_dir,
        per_run_url_lookup={"resource-0001": "https://x.example/p"},
    )
    assert flagged == 1  # drift counted in the return tuple
    cit = doc["evidence"][0]
    assert cit["resource_id"] == "resource-0042"
    assert "_canonicalize_note" not in cit


# --- Top-level canonicalize_vendor end-to-end -----------------------------


def test_canonicalize_vendor_merges_two_runs_and_writes_canonical(tmp_path: Path) -> None:
    extract_root = tmp_path / "extract"
    vendors_root = tmp_path / "vendors"

    # Set up two per-run profile.json files for slug "v":
    run1 = extract_root / "output" / "runs" / "run-A" / "v"
    run2 = extract_root / "output" / "runs" / "run-B" / "v"
    run1.mkdir(parents=True); run2.mkdir(parents=True)
    p_old = _bare_profile(drone_supply_chain_role=_answer("oem"))
    p_new = _bare_profile(ndaa=_answer("yes"), tagline="new tagline")
    (run1 / "profile.json").write_text(json.dumps(p_old))
    import time; time.sleep(0.05)
    (run2 / "profile.json").write_text(json.dumps(p_new))

    pd_old = _bare_products(products=[_product("A")])
    pd_new = _bare_products(products=[_product("B")])
    (run1 / "products.json").write_text(json.dumps(pd_old))
    time.sleep(0.05)
    (run2 / "products.json").write_text(json.dumps(pd_new))

    # No canonical website — citation migration is skipped (still works).
    report = canonicalize_vendor(
        "v", extract_root=extract_root, vendors_root=vendors_root,
    )
    assert report.profile_written is True
    assert report.products_written is True
    assert report.profile_runs == ["run-A", "run-B"]
    assert "no canonical website" in " ".join(report.notes)

    merged_pf = json.loads((vendors_root / "v" / "profile.json").read_text())
    assert merged_pf["drone_supply_chain_role"]["value"] == "oem"  # from older
    assert merged_pf["ndaa"]["value"] == "yes"  # from newer
    assert merged_pf["tagline"] == "new tagline"
    assert merged_pf["corpus_root"] == str(vendors_root / "v" / "website")
    assert merged_pf["run_id"] == "canonical"

    merged_pd = json.loads((vendors_root / "v" / "products.json").read_text())
    names = sorted(p["name"] for p in merged_pd["products"])
    assert names == ["A", "B"]


def test_canonicalize_vendor_no_inputs_returns_empty_report(tmp_path: Path) -> None:
    report = canonicalize_vendor(
        "missing", extract_root=tmp_path / "extract",
        vendors_root=tmp_path / "vendors",
    )
    assert report.profile_written is False
    assert report.products_written is False
