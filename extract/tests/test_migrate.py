"""Tests for `uxv_extract.migrate`."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from uxv_extract.migrate import migrate_citations


def _seed_per_run_corpus(
    workspace: Path, run_id: str, slug: str, *, urls_with_files: list[tuple[str, str]]
) -> Path:
    """Mimic the per-run mirror layout (manifest.json + text/) the
    extract docs reference via corpus_root."""
    target_dir = workspace / "output" / "runs" / run_id / "targets" / slug
    text_dir = target_dir / "text"
    text_dir.mkdir(parents=True, exist_ok=True)
    crawl_index = []
    for i, (url, content) in enumerate(urls_with_files, start=1):
        rid = f"resource-{i:04d}"
        text_path = text_dir / f"{i:04d}-stub.txt"
        text_path.write_text(content)
        crawl_index.append({
            "url": url,
            "depth": 0 if i == 1 else 1,
            "in_scope": True,
            "status": "fetched",
            "resource_id": rid,
            "discovered_from": [],
        })
    manifest = {
        "target": {"target_id": slug, "display_name": slug, "homepage_url": urls_with_files[0][0]},
        "crawl_index": crawl_index,
        "resources": [],
        "policy": {},
        "run_id": run_id,
        "corpus_root": str(target_dir),
        "manifest_path": str(target_dir / "manifest.json"),
        "crawl_index_path": str(target_dir / "crawl_index.json"),
        "quality_report_path": str(target_dir / "quality_report.json"),
        "quality_report": {
            "status": "complete", "fetched_pages": len(urls_with_files),
            "failed_pages": 0, "fetched_documents": 0,
            "discovered_urls": len(urls_with_files), "skipped_urls": 0,
            "total_text_chars": sum(len(c) for _, c in urls_with_files),
        },
    }
    (target_dir / "manifest.json").write_text(json.dumps(manifest))
    return target_dir


def _seed_canonical(
    vendors_root: Path, slug: str, *, urls_with_files: list[tuple[str, str]]
) -> Path:
    """Mimic the post-promote canonical layout."""
    canonical = vendors_root / slug / "website"
    text_dir = canonical / "text"
    text_dir.mkdir(parents=True, exist_ok=True)
    url_id_map = {}
    for i, (url, content) in enumerate(urls_with_files, start=1):
        url_id_map[url] = i
        (text_dir / f"{i:04d}-stub.txt").write_text(content)
    (canonical / "url_id_map.json").write_text(json.dumps(url_id_map))
    return canonical


def _seed_extract_doc(
    extract_root: Path, run_id: str, slug: str, *,
    corpus_root: Path,
    citations: list[tuple[str, int, int, str]],  # (resource_id, ls, le, snippet)
) -> Path:
    """Drop a synthetic extract output with the given citations."""
    target = extract_root / "output" / "runs" / run_id / slug
    target.mkdir(parents=True, exist_ok=True)
    doc = {
        "target_id": slug,
        "run_id": run_id,
        "corpus_root": str(corpus_root),
        "drone_supply_chain_role": {
            "value": "oem", "confidence": "high", "status": "answered",
            "evidence": [
                {
                    "source_kind": "mirror",
                    "resource_id": rid,
                    "line_start": ls, "line_end": le,
                    "snippet": snip,
                }
                for (rid, ls, le, snip) in citations
            ],
        },
    }
    fpath = target / "profile.json"
    fpath.write_text(json.dumps(doc))
    return fpath


def test_migrate_rewrites_resource_ids_when_canonical_renumbered(tmp_path: Path) -> None:
    """Per-run resource-0001 → canonical resource-0042 because canonical
    has many more URLs that sort before this one."""
    ws = tmp_path / "ws"
    vendors = tmp_path / "vendors"
    extract_root = tmp_path / "extract"

    # Per-run corpus has 1 URL = resource-0001
    target_dir = _seed_per_run_corpus(
        ws, "run-A", "acme",
        urls_with_files=[("https://acme.example/products", "products page content\nline 2")],
    )
    # Canonical has many URLs; products is at position 42
    canonical_urls = [
        (f"https://acme.example/page-{i}", f"content {i}") for i in range(1, 42)
    ] + [("https://acme.example/products", "products page content\nline 2")]
    _seed_canonical(vendors, "acme", urls_with_files=canonical_urls)

    fpath = _seed_extract_doc(
        extract_root, "run-A", "acme", corpus_root=target_dir,
        citations=[("resource-0001", 1, 2, "products page content\nline 2")],
    )

    report = migrate_citations("acme", vendors_root=vendors, extract_root=extract_root)
    assert len(report.files) == 1
    fr = report.files[0]
    assert fr.citations_total == 1
    assert fr.citations_migrated == 1
    assert fr.citations_flagged == 0
    # File on disk got rewritten:
    new_doc = json.loads(fpath.read_text())
    new_rid = new_doc["drone_supply_chain_role"]["evidence"][0]["resource_id"]
    assert new_rid == "resource-0042"


def test_migrate_flags_snippet_drift(tmp_path: Path) -> None:
    """Round B re-fetched the URL with different content → snippet at
    the cited line range no longer matches → citation flagged, file
    NOT rewritten for that field."""
    ws = tmp_path / "ws"
    vendors = tmp_path / "vendors"
    extract_root = tmp_path / "extract"

    target_dir = _seed_per_run_corpus(
        ws, "run-A", "acme",
        urls_with_files=[("https://acme.example/", "old homepage line\nline 2")],
    )
    # Canonical has the SAME URL but different content (round B):
    _seed_canonical(
        vendors, "acme",
        urls_with_files=[("https://acme.example/", "NEW homepage line\nline 2")],
    )

    fpath = _seed_extract_doc(
        extract_root, "run-A", "acme", corpus_root=target_dir,
        citations=[("resource-0001", 1, 1, "old homepage line")],
    )

    report = migrate_citations("acme", vendors_root=vendors, extract_root=extract_root)
    fr = report.files[0]
    assert fr.citations_total == 1
    assert fr.citations_migrated == 0
    assert fr.citations_flagged == 1
    outcome = fr.outcomes[0]
    assert outcome.status == "snippet_drift"
    # File NOT rewritten:
    doc = json.loads(fpath.read_text())
    assert doc["drone_supply_chain_role"]["evidence"][0]["resource_id"] == "resource-0001"


def test_migrate_flags_url_not_in_canonical(tmp_path: Path) -> None:
    """If a URL was fetched in per-run but somehow is missing from
    canonical (canonical built without that run), flag it."""
    ws = tmp_path / "ws"
    vendors = tmp_path / "vendors"
    extract_root = tmp_path / "extract"

    target_dir = _seed_per_run_corpus(
        ws, "run-A", "acme",
        urls_with_files=[("https://acme.example/lost", "lost content")],
    )
    _seed_canonical(
        vendors, "acme",
        urls_with_files=[("https://acme.example/", "homepage only")],
    )

    _seed_extract_doc(
        extract_root, "run-A", "acme", corpus_root=target_dir,
        citations=[("resource-0001", 1, 1, "lost content")],
    )

    report = migrate_citations("acme", vendors_root=vendors, extract_root=extract_root)
    outcome = report.files[0].outcomes[0]
    assert outcome.status == "url_not_in_canonical"


def test_migrate_idempotent_on_already_canonical_ids(tmp_path: Path) -> None:
    """Re-running migrate on a doc whose IDs already match canonical
    is a no-op (status=no_change, file unchanged)."""
    ws = tmp_path / "ws"
    vendors = tmp_path / "vendors"
    extract_root = tmp_path / "extract"

    target_dir = _seed_per_run_corpus(
        ws, "run-A", "acme",
        urls_with_files=[("https://acme.example/", "home")],
    )
    _seed_canonical(
        vendors, "acme",
        urls_with_files=[("https://acme.example/", "home")],
    )

    fpath = _seed_extract_doc(
        extract_root, "run-A", "acme", corpus_root=target_dir,
        citations=[("resource-0001", 1, 1, "home")],  # already matches canonical
    )
    before = fpath.read_text()

    report = migrate_citations("acme", vendors_root=vendors, extract_root=extract_root)
    outcome = report.files[0].outcomes[0]
    assert outcome.status == "no_change"
    # File unchanged:
    assert fpath.read_text() == before


def test_migrate_dry_run_writes_no_files(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    vendors = tmp_path / "vendors"
    extract_root = tmp_path / "extract"

    target_dir = _seed_per_run_corpus(
        ws, "run-A", "acme",
        urls_with_files=[("https://acme.example/p", "content")],
    )
    canonical_urls = [
        (f"https://acme.example/skip-{i}", f"x {i}") for i in range(1, 5)
    ] + [("https://acme.example/p", "content")]
    _seed_canonical(vendors, "acme", urls_with_files=canonical_urls)

    fpath = _seed_extract_doc(
        extract_root, "run-A", "acme", corpus_root=target_dir,
        citations=[("resource-0001", 1, 1, "content")],
    )
    before = fpath.read_text()

    report = migrate_citations(
        "acme", vendors_root=vendors, extract_root=extract_root, dry_run=True,
    )
    assert report.dry_run is True
    # Original file unchanged:
    assert fpath.read_text() == before


def test_migrate_raises_when_canonical_missing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="canonical dir not found"):
        migrate_citations(
            "no-such-vendor",
            vendors_root=tmp_path / "vendors",
            extract_root=tmp_path / "extract",
        )
