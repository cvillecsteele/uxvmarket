"""Tests for scripts/detect_newsletter_sources.py."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Make the script importable as a module
SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

from detect_newsletter_sources import (  # noqa: E402
    classify_path,
    detect_for_vendor,
    infer_index_url,
    is_bare_index,
    is_rejected_path,
    normalize_path,
)


# ---- reject filters --------------------------------------------------------


def test_locale_prefix_rejected():
    assert is_rejected_path("/es/news") is True
    assert is_rejected_path("/ja/blog/foo") is True
    assert is_rejected_path("/de/press-releases") is True


def test_canonical_locale_kept():
    # /news (no locale) should NOT be rejected
    assert is_rejected_path("/news") is False
    assert is_rejected_path("/blog/posts") is False


def test_pdf_rejected():
    assert is_rejected_path("/media/uploads/foo.pdf") is True
    assert is_rejected_path("/news.pdf") is True


def test_urn_fragment_rejected():
    assert is_rejected_path("/feed/update/urn:li:activity:1234") is True


def test_resources_rejected():
    # All /resources/* are datasheet noise per cohort-25 evidence
    assert is_rejected_path("/resources") is True
    assert is_rejected_path("/resources/datasheet-foo") is True


# ---- path normalization ----------------------------------------------------


def test_normalize_strips_category_prefix():
    """WordPress-style /category/<rest> routes (desert-rotor uses this)."""
    assert normalize_path("/category/news") == "/news"
    assert normalize_path("/category/press-releases") == "/press-releases"
    assert normalize_path("/category/blog/foo") == "/blog/foo"


def test_normalize_strips_pagination_segments():
    assert normalize_path("/news/page/2") == "/news"
    assert normalize_path("/blog/page/3/") == "/blog"
    assert normalize_path("/news/p/5") == "/news"


def test_normalize_strips_underscore_pagination_suffix():
    """Old CMS pattern (actuonix uses /News_bc_1.html)."""
    assert normalize_path("/news_bc_1.html") == "/news"
    assert normalize_path("/news_page_3") == "/news"


def test_normalize_strips_extensions():
    assert normalize_path("/news.html") == "/news"
    assert normalize_path("/blog.htm") == "/blog"
    assert normalize_path("/press.php") == "/press"
    assert normalize_path("/news.aspx") == "/news"


def test_normalize_lowercases():
    assert normalize_path("/News") == "/news"
    assert normalize_path("/Category/News_BC_1.HTML") == "/news"


def test_classify_recognizes_normalized_news_variants():
    assert classify_path("/News_bc_1.html") == "news"
    assert classify_path("/category/news") == "news"
    assert classify_path("/category/press-releases") == "press"
    assert classify_path("/news.php") == "news"


# ---- family classifier -----------------------------------------------------


def test_news_family_priority_over_blog():
    """When both /news and /blog exist for a vendor, news wins as primary."""
    # Direct: classifier per-path
    assert classify_path("/news") == "news"
    assert classify_path("/news/foo") == "news"
    assert classify_path("/blog") == "blog"
    assert classify_path("/blog/post") == "blog"


def test_newsroom_classified():
    assert classify_path("/newsroom") == "newsroom"
    assert classify_path("/newsroom/some-item") == "newsroom"


def test_press_variants_all_classified_as_press():
    assert classify_path("/press") == "press"
    assert classify_path("/press-releases") == "press"
    assert classify_path("/press-releases/abc") == "press"
    assert classify_path("/press-room") == "press"
    assert classify_path("/category/press-releases") == "press"
    assert classify_path("/category/press-releases/foo") == "press"


def test_media_center_classified():
    """Curtiss-Wright bespoke layout."""
    assert classify_path("/media-center") == "media-center"
    assert classify_path("/media-center/articles") == "media-center"
    assert classify_path("/media-center/events/some-show") == "media-center"


def test_news_events_distinct_from_news():
    """`/news-and-events` is its own family (lower priority than bare `/news`)."""
    assert classify_path("/news-and-events") == "news-events"
    assert classify_path("/news-events") == "news-events"


def test_unmatched_paths_return_none():
    assert classify_path("/about") is None
    assert classify_path("/products/sensor-x") is None
    assert classify_path("/contact") is None


# ---- bare-index detection --------------------------------------------------


def test_bare_index_detection():
    assert is_bare_index("/news", "news") is True
    assert is_bare_index("/news/", "news") is True
    assert is_bare_index("/news/foo", "news") is False
    assert is_bare_index("/newsroom", "newsroom") is True
    assert is_bare_index("/newsroom/foo", "newsroom") is False


# ---- infer_index_url -------------------------------------------------------


def test_infer_index_url_uses_bare_when_present():
    url, bare_seen = infer_index_url(
        "https://x.example", "news", ["/news", "/news/foo", "/news/bar"]
    )
    assert url == "https://x.example/news"
    assert bare_seen is True


def test_infer_index_url_falls_back_when_no_bare():
    url, bare_seen = infer_index_url(
        "https://x.example", "news", ["/news/foo", "/news/bar"]
    )
    assert url == "https://x.example/news"
    assert bare_seen is False


# ---- detect_for_vendor end-to-end -----------------------------------------


def _seed_vendor(tmp_path: Path, slug: str, *, homepage: str, fetched_paths: list[str]) -> Path:
    vendor_dir = tmp_path / slug
    website = vendor_dir / "website"
    website.mkdir(parents=True)
    entries = [
        {"url": f"{homepage}{p}", "final_url": f"{homepage}{p}", "status": "fetched"}
        for p in fetched_paths
    ]
    (website / "crawl_index.json").write_text(json.dumps({
        "target": {"homepage_url": homepage},
        "entries": entries,
        "links": [],
    }))
    return vendor_dir


def test_detect_picks_news_over_blog_when_both_present(tmp_path: Path):
    _seed_vendor(tmp_path, "v1", homepage="https://v1.example",
                 fetched_paths=["/news", "/news/a", "/news/b", "/blog", "/blog/x"])
    r = detect_for_vendor("v1", tmp_path)
    assert r["no_sources_found"] is False
    assert len(r["sources"]) == 2
    assert r["sources"][0]["kind"] == "news"
    assert r["sources"][0]["rank"] == 1
    assert r["sources"][0]["bare_index_seen"] is True
    assert r["sources"][1]["kind"] == "blog"
    assert r["sources"][1]["rank"] == 2


def test_detect_handles_no_crawl_index(tmp_path: Path):
    (tmp_path / "v1").mkdir()  # no website/ subdir
    r = detect_for_vendor("v1", tmp_path)
    assert r["no_sources_found"] is True
    assert r["sources"] == []


def test_detect_skips_filtered_paths(tmp_path: Path):
    """Multilingual + PDF + URN-fragment paths must not surface as sources."""
    _seed_vendor(tmp_path, "v1", homepage="https://v1.example",
                 fetched_paths=[
                     "/es/news",  # locale
                     "/news.pdf",  # pdf
                     "/feed/update/urn:li:activity:1234",  # urn
                     "/resources/datasheet",  # resources
                     "/about",  # not a family
                 ])
    r = detect_for_vendor("v1", tmp_path)
    assert r["no_sources_found"] is True


def test_detect_inferred_index_when_only_items_present(tmp_path: Path):
    _seed_vendor(tmp_path, "v1", homepage="https://v1.example",
                 fetched_paths=["/news/foo", "/news/bar", "/news/baz"])
    r = detect_for_vendor("v1", tmp_path)
    assert r["sources"][0]["kind"] == "news"
    assert r["sources"][0]["url"] == "https://v1.example/news"
    assert r["sources"][0]["bare_index_seen"] is False
    assert r["sources"][0]["item_count_seen"] == 3


def test_detect_includes_skipped_and_failed_entries(tmp_path: Path):
    """Discovered-but-not-fetched URLs are still strong evidence that
    a news section exists — they should feed into source detection."""
    vendor_dir = tmp_path / "v1"
    website = vendor_dir / "website"
    website.mkdir(parents=True)
    (website / "crawl_index.json").write_text(json.dumps({
        "target": {"homepage_url": "https://v1.example"},
        "entries": [
            {"url": "https://v1.example/news",       "status": "fetched"},
            {"url": "https://v1.example/blog",       "status": "skipped_class_budget"},
            {"url": "https://v1.example/press",      "status": "failed"},
        ],
        "links": [],
    }))
    r = detect_for_vendor("v1", tmp_path)
    kinds = sorted(s["kind"] for s in r["sources"])
    assert kinds == ["blog", "news", "press"]


def test_primary_pick_respects_min_items_threshold(tmp_path: Path):
    """When the priority-1 family has < 3 items but a lower-priority
    family has many (beta-technologies pattern: newsroom:1 + stories:129),
    the high-volume family wins as primary. Newsroom drops to secondary."""
    paths = (
        ["/newsroom"]                       # rank 1, 1 item
        + ["/news"]                          # rank 2, 1 item
        + [f"/stories/article-{i}" for i in range(20)]  # rank 6, 20 items + bare
        + ["/stories"]
    )
    _seed_vendor(tmp_path, "v1", homepage="https://v1.example",
                 fetched_paths=paths)
    r = detect_for_vendor("v1", tmp_path)
    primary = r["sources"][0]
    assert primary["kind"] == "stories"  # qualifies (>=3 items); newsroom doesn't


def test_primary_falls_back_to_priority_when_no_family_qualifies(tmp_path: Path):
    """If every family has <3 items, priority still wins (no demotion)."""
    _seed_vendor(tmp_path, "v1", homepage="https://v1.example",
                 fetched_paths=["/news", "/blog", "/press"])
    r = detect_for_vendor("v1", tmp_path)
    assert r["sources"][0]["kind"] == "news"  # priority 2 > blog (5) > press (3)


def test_detect_handles_nested_news_path(tmp_path: Path):
    """Septentrio pattern: news is at /en/company/news, not /news."""
    paths = (
        ["/en/company/news"]
        + [f"/en/company/news/article-{i}" for i in range(10)]
    )
    _seed_vendor(tmp_path, "v1", homepage="https://v1.example",
                 fetched_paths=paths)
    r = detect_for_vendor("v1", tmp_path)
    assert r["sources"][0]["kind"] == "news"
    assert r["sources"][0]["url"] == "https://v1.example/en/company/news"
    assert r["sources"][0]["bare_index_seen"] is True


def test_detect_curtiss_wright_picks_media_center_not_resources(tmp_path: Path):
    """Regression for the Curtiss-Wright pattern: lots of /resources/
    datasheets + /media-center/articles. Should pick media-center; resources
    must be filtered out (would dominate by volume otherwise)."""
    paths = (
        ["/resources"] +
        [f"/resources/datasheet-{i}" for i in range(20)] +
        ["/media-center/articles", "/media-center/articles/article-1",
         "/media-center/articles/article-2", "/media-center/events"]
    )
    _seed_vendor(tmp_path, "cw", homepage="https://cw.example", fetched_paths=paths)
    r = detect_for_vendor("cw", tmp_path)
    kinds = [s["kind"] for s in r["sources"]]
    assert "resources" not in kinds  # filtered, not classified
    assert "media-center" in kinds
    assert r["sources"][0]["kind"] == "media-center"
