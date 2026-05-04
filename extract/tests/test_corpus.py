from __future__ import annotations

from pathlib import Path

import pytest

from uxv_extract.corpus import CorpusReader

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "corpus_min"


def test_load_reads_target_metadata() -> None:
    reader = CorpusReader.load(FIXTURE_ROOT)

    assert reader.target_id == "test-vendor"
    assert reader.display_name == "Test Vendor"
    assert reader.run_id == "test-run"
    assert reader.homepage_url == "https://test.example"
    assert reader.corpus_root == FIXTURE_ROOT


def test_fetched_resources_resolve_per_format_files() -> None:
    reader = CorpusReader.load(FIXTURE_ROOT)
    fetched = reader.fetched_resources()

    assert [r.resource_id for r in fetched] == ["resource-0001", "resource-0002"]

    home = fetched[0]
    assert home.url == "https://test.example/"
    assert home.page_class == "homepage"
    assert home.text_path is not None and home.text_path.name == "0001-home.txt"
    assert home.markdown_path is not None and home.markdown_path.name == "0001-home.md"
    assert home.raw_html_path is not None and home.raw_html_path.name == "0001-home.html"
    assert home.json_path is not None and home.json_path.name == "0001-home.json"

    about = fetched[1]
    assert about.url == "https://test.example/about"
    assert about.page_class == "company"


def test_fetched_resource_returns_none_for_missing_format_file(tmp_path: Path) -> None:
    """Real corpora have resources where some formats are missing (e.g. kml has
    json+text but no markdown/raw). Reader must not assume all four exist."""
    import shutil

    target = tmp_path / "corpus_partial"
    shutil.copytree(FIXTURE_ROOT, target)
    (target / "markdown" / "0001-home.md").unlink()
    (target / "raw" / "0001-home.html").unlink()

    reader = CorpusReader.load(target)
    home = reader.fetched_resources()[0]
    assert home.text_path is not None
    assert home.markdown_path is None
    assert home.raw_html_path is None
    assert home.json_path is not None


def test_skipped_resources_includes_skipped_and_failed() -> None:
    reader = CorpusReader.load(FIXTURE_ROOT)
    skipped = reader.skipped_resources()

    urls = {s.url for s in skipped}
    assert urls == {
        "https://test.example/products",
        "https://test.example/news",
    }
    by_url = {s.url: s for s in skipped}
    assert by_url["https://test.example/products"].status == "skipped_class_budget"
    assert by_url["https://test.example/products"].page_class == "product"
    assert by_url["https://test.example/news"].status == "failed"


def test_quality_report_fields() -> None:
    reader = CorpusReader.load(FIXTURE_ROOT)
    assert reader.quality_status == "partial"
    assert reader.total_text_chars == 1234


def test_load_missing_corpus_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        CorpusReader.load(tmp_path / "does-not-exist")


def test_load_resolves_via_workspace(tmp_path: Path) -> None:
    """Convenience constructor for the standard mirroring layout."""
    import shutil

    workspace = tmp_path / "mirroring"
    target_dir = workspace / "output" / "runs" / "test-run" / "targets" / "test-vendor"
    target_dir.parent.mkdir(parents=True)
    shutil.copytree(FIXTURE_ROOT, target_dir)

    reader = CorpusReader.from_workspace(
        workspace_root=workspace,
        run_id="test-run",
        target_id="test-vendor",
    )
    assert reader.target_id == "test-vendor"
    assert reader.corpus_root == target_dir


def test_from_vendor_canonical_resolves_website_subdir(tmp_path: Path) -> None:
    """Canonical layout: <vendors_root>/<slug>/website/manifest.json."""
    import shutil

    vendors = tmp_path / "vendors"
    canonical = vendors / "test-vendor" / "website"
    canonical.parent.mkdir(parents=True)
    shutil.copytree(FIXTURE_ROOT, canonical)

    reader = CorpusReader.from_vendor_canonical(
        vendors_root=vendors, slug="test-vendor",
    )
    assert reader.target_id == "test-vendor"
    assert reader.corpus_root == canonical
