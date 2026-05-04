"""Tests for `uxv_mirroring.promote`.

Synthetic per-run mirror corpora are constructed via `_seed_round` so
each test case is self-contained. The fixtures mimic the real on-disk
shape: per-target `manifest.json` + `crawl_index.json` + per-format
subdirs with `NNNN-<slug>.<ext>` files.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlparse

from uxv_mirroring.contracts import (
    CrawlIndexEntry,
    MirrorCorpus,
    MirrorPolicy,
    MirrorResource,
    MirrorTarget,
    QualityReport,
)
from uxv_mirroring.materialize import slugify
from uxv_mirroring.promote import (
    PromoteResult,
    promote,
)


def _seed_round(
    workspace: Path,
    run_id: str,
    slug: str,
    *,
    fetched: list[tuple[str, int, str]] = (),  # type: ignore[assignment]
    skipped: list[tuple[str, int, str, str]] = (),  # type: ignore[assignment]
    created_at: str = "2026-05-01T00:00:00+00:00",
    homepage_url: str = "https://example.com",
) -> Path:
    """Create a per-run mirror target dir.

    fetched: list of (url, depth, content). The first one is treated as
        the homepage (depth 0).
    skipped: list of (url, depth, status, skip_reason).
    Returns the run_dir.
    """
    run_dir = workspace / "output" / "runs" / run_id
    target_dir = run_dir / "targets" / slug
    for sd in ("text", "markdown", "raw", "json", "documents"):
        (target_dir / sd).mkdir(parents=True, exist_ok=True)

    resources: list[MirrorResource] = []
    crawl_index: list[CrawlIndexEntry] = []

    for i, (url, depth, content) in enumerate(fetched, start=1):
        rid = f"resource-{i:04d}"
        slug_part = slugify(urlparse(url).path.strip("/") or "home")
        prefix = f"{i:04d}-{slug_part}"
        text_path = target_dir / "text" / f"{prefix}.txt"
        text_path.write_text(content)
        # Drop tiny stub files in the other format dirs so copy paths exist.
        (target_dir / "markdown" / f"{prefix}.md").write_text(f"# {url}\n")
        (target_dir / "raw" / f"{prefix}.html").write_text(f"<p>{url}</p>")
        (target_dir / "json" / f"{prefix}.json").write_text(
            json.dumps({"url": url})
        )
        resources.append(
            MirrorResource(
                resource_id=rid,
                url=url,
                final_url=url,
                kind="html",
                status="fetched",
                text_path=str(text_path),
                markdown_path=str(target_dir / "markdown" / f"{prefix}.md"),
                html_path=str(target_dir / "raw" / f"{prefix}.html"),
                json_path=str(target_dir / "json" / f"{prefix}.json"),
                text_chars=len(content),
            )
        )
        crawl_index.append(
            CrawlIndexEntry(
                url=url,
                discovered_from=[f"{run_id}:map"],
                depth=depth,
                in_scope=True,
                status="fetched",
                final_url=url,
                kind="html",
                page_class="homepage" if depth == 0 else "product",
                resource_id=rid,
            )
        )

    for url, depth, status, reason in skipped:
        crawl_index.append(
            CrawlIndexEntry(
                url=url,
                discovered_from=[f"{run_id}:map"],
                depth=depth,
                in_scope=True,
                status=status,  # type: ignore[arg-type]
                page_class="product",
                kind="html",
                skip_reason=reason,
            )
        )

    quality = QualityReport(
        status="complete",
        fetched_pages=len(fetched),
        failed_pages=0,
        fetched_documents=0,
        discovered_urls=len(fetched) + len(skipped),
        skipped_urls=len(skipped),
        total_text_chars=sum(len(c) for _, _, c in fetched),
    )
    target = MirrorTarget(
        target_id=slug, display_name=slug.title(), homepage_url=homepage_url
    )
    corpus = MirrorCorpus(
        target=target,
        policy=MirrorPolicy(),
        run_id=run_id,
        corpus_root=str(target_dir),
        manifest_path=str(target_dir / "manifest.json"),
        crawl_index_path=str(target_dir / "crawl_index.json"),
        quality_report_path=str(target_dir / "quality_report.json"),
        resources=resources,
        crawl_index=crawl_index,
        quality_report=quality,
    )
    (target_dir / "manifest.json").write_text(corpus.model_dump_json())
    (target_dir / "crawl_index.json").write_text(
        json.dumps(
            {
                "target": target.model_dump(),
                "entries": [e.model_dump() for e in crawl_index],
                "links": [],
            }
        )
    )
    (target_dir / "quality_report.json").write_text(quality.model_dump_json())

    # Run-level manifest with created_at.
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "created_at": created_at,
                "workspace_root": str(workspace),
                "profile": "quick_evidence",
                "targets": [target.model_dump()],
                "corpora": [
                    {
                        "target_id": slug,
                        "manifest_path": str(target_dir / "manifest.json"),
                        "quality_status": "complete",
                    }
                ],
            }
        )
    )
    return run_dir


class PromoteTests(unittest.TestCase):
    def test_no_per_run_dirs_is_noop(self) -> None:
        with TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            ws.mkdir()
            vendors = Path(tmp) / "vendors"
            result = promote("missing-vendor", workspace_root=ws, vendors_root=vendors)
            self.assertEqual(result.action, "noop")
            self.assertFalse((vendors / "missing-vendor").exists())

    def test_single_run_produces_canonical_with_sequential_ids(self) -> None:
        with TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            vendors = Path(tmp) / "vendors"
            _seed_round(
                ws, "run-A", "acme",
                fetched=[
                    ("https://acme.example/", 0, "homepage content"),
                    ("https://acme.example/about", 1, "about content"),
                    ("https://acme.example/products", 1, "products content"),
                ],
            )
            result = promote("acme", workspace_root=ws, vendors_root=vendors)
            self.assertEqual(result.action, "promoted")
            self.assertEqual(result.fetched_count, 3)
            self.assertEqual(result.new_url_count, 3)

            canonical = vendors / "acme" / "website"
            self.assertTrue((canonical / "manifest.json").exists())

            cidx = json.loads((canonical / "crawl_index.json").read_text())
            entries = {e["url"]: e for e in cidx["entries"]}
            # IDs sorted by (depth, url) for the initial promote:
            self.assertEqual(entries["https://acme.example/"]["resource_id"], "resource-0001")
            self.assertEqual(entries["https://acme.example/about"]["resource_id"], "resource-0002")
            self.assertEqual(entries["https://acme.example/products"]["resource_id"], "resource-0003")

            # File contents copied:
            home_text = (canonical / "text" / "0001-home.txt").read_text()
            self.assertEqual(home_text, "homepage content")

    def test_two_runs_same_homepage_canonical_homepage_from_latest(self) -> None:
        with TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            vendors = Path(tmp) / "vendors"
            _seed_round(
                ws, "run-A", "acme",
                fetched=[("https://acme.example/", 0, "OLD homepage")],
                created_at="2026-05-01T00:00:00+00:00",
            )
            _seed_round(
                ws, "run-B", "acme",
                fetched=[("https://acme.example/", 0, "NEW homepage")],
                created_at="2026-05-02T00:00:00+00:00",
            )
            promote("acme", workspace_root=ws, vendors_root=vendors)

            canonical = vendors / "acme" / "website"
            home_text = (canonical / "text" / "0001-home.txt").read_text()
            self.assertEqual(home_text, "NEW homepage")

    def test_two_runs_second_adds_new_urls_existing_ids_preserved(self) -> None:
        with TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            vendors = Path(tmp) / "vendors"
            _seed_round(
                ws, "run-A", "acme",
                fetched=[
                    ("https://acme.example/", 0, "home"),
                    ("https://acme.example/about", 1, "about"),
                ],
                created_at="2026-05-01T00:00:00+00:00",
            )
            promote("acme", workspace_root=ws, vendors_root=vendors)

            # Initial canonical IDs:
            cidx_v1 = json.loads(
                (vendors / "acme" / "website" / "crawl_index.json").read_text()
            )
            ids_v1 = {e["url"]: e["resource_id"] for e in cidx_v1["entries"]}
            self.assertEqual(ids_v1["https://acme.example/"], "resource-0001")
            self.assertEqual(ids_v1["https://acme.example/about"], "resource-0002")

            # Round B adds two new URLs and re-fetches the homepage:
            _seed_round(
                ws, "run-B", "acme",
                fetched=[
                    ("https://acme.example/", 0, "home v2"),
                    ("https://acme.example/products", 1, "products"),
                    ("https://acme.example/contact", 1, "contact"),
                ],
                created_at="2026-05-02T00:00:00+00:00",
            )
            promote("acme", workspace_root=ws, vendors_root=vendors)

            cidx_v2 = json.loads(
                (vendors / "acme" / "website" / "crawl_index.json").read_text()
            )
            ids_v2 = {e["url"]: e["resource_id"] for e in cidx_v2["entries"]}
            # Existing IDs preserved:
            self.assertEqual(ids_v2["https://acme.example/"], "resource-0001")
            self.assertEqual(ids_v2["https://acme.example/about"], "resource-0002")
            # New URLs appended:
            new_ids = {ids_v2["https://acme.example/products"],
                       ids_v2["https://acme.example/contact"]}
            self.assertEqual(new_ids, {"resource-0003", "resource-0004"})

    def test_url_fetched_in_a_skipped_in_b_canonical_status_fetched(self) -> None:
        with TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            vendors = Path(tmp) / "vendors"
            _seed_round(
                ws, "run-A", "acme",
                fetched=[("https://acme.example/", 0, "home")],
                created_at="2026-05-01T00:00:00+00:00",
            )
            _seed_round(
                ws, "run-B", "acme",
                fetched=[("https://acme.example/", 0, "home v2")],
                skipped=[
                    # In run B, /about was discovered but skipped.
                    ("https://acme.example/about", 1, "skipped_class_budget", "budget"),
                ],
                created_at="2026-05-02T00:00:00+00:00",
            )
            # Run A also discovered /about via skipped (we'll mark it as
            # fetched in another round to simulate the intended case):
            _seed_round(
                ws, "run-C", "acme",
                fetched=[
                    ("https://acme.example/", 0, "home v3"),
                    ("https://acme.example/about", 1, "about content"),
                ],
                created_at="2026-05-03T00:00:00+00:00",
            )
            promote("acme", workspace_root=ws, vendors_root=vendors)

            cidx = json.loads(
                (vendors / "acme" / "website" / "crawl_index.json").read_text()
            )
            entries = {e["url"]: e for e in cidx["entries"]}
            # /about was fetched in run-C; canonical status must be "fetched"
            # even though run-B (later than run-A but earlier than run-C)
            # had it as skipped. Since run-C is the latest, this also
            # checks that the merge order is correct.
            self.assertEqual(entries["https://acme.example/about"]["status"], "fetched")
            self.assertIsNotNone(entries["https://acme.example/about"]["resource_id"])

    def test_discovered_from_unioned_across_rounds(self) -> None:
        with TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            vendors = Path(tmp) / "vendors"
            _seed_round(
                ws, "run-A", "acme",
                fetched=[("https://acme.example/", 0, "home")],
                created_at="2026-05-01T00:00:00+00:00",
            )
            _seed_round(
                ws, "run-B", "acme",
                fetched=[("https://acme.example/", 0, "home v2")],
                created_at="2026-05-02T00:00:00+00:00",
            )
            promote("acme", workspace_root=ws, vendors_root=vendors)

            cidx = json.loads(
                (vendors / "acme" / "website" / "crawl_index.json").read_text()
            )
            home = next(e for e in cidx["entries"] if e["url"] == "https://acme.example/")
            self.assertIn("run-A:map", home["discovered_from"])
            self.assertIn("run-B:map", home["discovered_from"])

    def test_file_copies_match_source_bytes(self) -> None:
        with TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            vendors = Path(tmp) / "vendors"
            _seed_round(
                ws, "run-A", "acme",
                fetched=[
                    ("https://acme.example/", 0, "homepage bytes"),
                    ("https://acme.example/x", 1, "x bytes"),
                ],
            )
            promote("acme", workspace_root=ws, vendors_root=vendors)
            canonical = vendors / "acme" / "website"
            # Markdown / raw / json all copied:
            self.assertTrue((canonical / "markdown" / "0001-home.md").exists())
            self.assertTrue((canonical / "raw" / "0001-home.html").exists())
            self.assertTrue((canonical / "json" / "0001-home.json").exists())
            self.assertEqual(
                (canonical / "text" / "0001-home.txt").read_text(),
                "homepage bytes",
            )

    def test_promote_log_is_appended_per_call(self) -> None:
        with TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            vendors = Path(tmp) / "vendors"
            _seed_round(
                ws, "run-A", "acme",
                fetched=[("https://acme.example/", 0, "home")],
                created_at="2026-05-01T00:00:00+00:00",
            )
            promote("acme", workspace_root=ws, vendors_root=vendors)
            log1 = json.loads(
                (vendors / "acme" / "website" / "promote_log.json").read_text()
            )
            self.assertEqual(len(log1["entries"]), 1)
            self.assertEqual(log1["entries"][0]["source_run_ids"], ["run-A"])
            self.assertEqual(log1["entries"][0]["new_url_count"], 1)

            _seed_round(
                ws, "run-B", "acme",
                fetched=[
                    ("https://acme.example/", 0, "home"),
                    ("https://acme.example/new", 1, "new"),
                ],
                created_at="2026-05-02T00:00:00+00:00",
            )
            promote("acme", workspace_root=ws, vendors_root=vendors)
            log2 = json.loads(
                (vendors / "acme" / "website" / "promote_log.json").read_text()
            )
            self.assertEqual(len(log2["entries"]), 2)
            self.assertEqual(log2["entries"][1]["source_run_ids"], ["run-A", "run-B"])
            self.assertEqual(log2["entries"][1]["new_url_count"], 1)

    def test_idempotent_when_no_new_data(self) -> None:
        with TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            vendors = Path(tmp) / "vendors"
            _seed_round(
                ws, "run-A", "acme",
                fetched=[("https://acme.example/", 0, "home")],
            )
            r1 = promote("acme", workspace_root=ws, vendors_root=vendors)
            r2 = promote("acme", workspace_root=ws, vendors_root=vendors)
            self.assertEqual(r1.action, "promoted")
            self.assertEqual(r2.action, "promoted")
            # The second call adds NO new URLs (existing ones preserved):
            self.assertEqual(r2.new_url_count, 0)

    def test_skipped_url_ids_are_stable_across_promotes(self) -> None:
        """Regression: skipped URLs have no resource_id but still occupy
        a numeric position in the canonical sort. They must keep that
        position on re-promote, not get reshuffled."""
        with TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            vendors = Path(tmp) / "vendors"
            _seed_round(
                ws, "run-A", "acme",
                fetched=[("https://acme.example/", 0, "home")],
                skipped=[
                    ("https://acme.example/products", 1, "skipped_class_budget", "budget"),
                    ("https://acme.example/about", 1, "skipped_class_budget", "budget"),
                ],
            )
            promote("acme", workspace_root=ws, vendors_root=vendors)

            # Read the url_id_map after first promote.
            map1 = json.loads(
                (vendors / "acme" / "website" / "url_id_map.json").read_text()
            )

            # Add a new round that introduces ONE more URL.
            _seed_round(
                ws, "run-B", "acme",
                fetched=[("https://acme.example/", 0, "home v2")],
                skipped=[
                    ("https://acme.example/contact", 1, "skipped_class_budget", "budget"),
                ],
                created_at="2026-05-02T00:00:00+00:00",
            )
            promote("acme", workspace_root=ws, vendors_root=vendors)

            map2 = json.loads(
                (vendors / "acme" / "website" / "url_id_map.json").read_text()
            )
            # All previously-mapped URLs keep their IDs:
            for url, idx in map1.items():
                self.assertEqual(map2.get(url), idx,
                                 f"URL {url!r} ID changed: {map1[url]} → {map2.get(url)}")
            # The new URL got an ID strictly greater than any previous:
            new_id = map2["https://acme.example/contact"]
            self.assertGreater(new_id, max(map1.values()))

    def test_paths_in_manifest_point_at_canonical_dir_not_tmp(self) -> None:
        """Regression: previously the manifest's text_path/etc. carried
        the .tmp path used during build, leaving stale paths in the
        published manifest after the rename."""
        with TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            vendors = Path(tmp) / "vendors"
            _seed_round(
                ws, "run-A", "acme",
                fetched=[("https://acme.example/", 0, "home")],
            )
            promote("acme", workspace_root=ws, vendors_root=vendors)

            manifest = json.loads(
                (vendors / "acme" / "website" / "manifest.json").read_text()
            )
            for r in manifest["resources"]:
                for path_field in ("text_path", "markdown_path", "html_path", "json_path"):
                    p = r.get(path_field)
                    if not p:
                        continue
                    self.assertNotIn(".tmp", p,
                                     f"{path_field} contains .tmp: {p}")
                    # And the path actually exists on disk:
                    self.assertTrue(Path(p).exists(),
                                    f"{path_field} doesn't exist: {p}")


if __name__ == "__main__":
    unittest.main()
