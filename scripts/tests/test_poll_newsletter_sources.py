"""Tests for scripts/poll_newsletter_sources.py."""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

from poll_newsletter_sources import (  # noqa: E402
    Item,
    _is_archive_url,
    _passes_url_filter,
    collect_jsonld_dates,
    date_from_url,
    merge_into_state,
    parse_date_value,
    parse_items,
)
from bs4 import BeautifulSoup  # noqa: E402


CUTOFF = date.today() - timedelta(days=180)
RECENT = (date.today() - timedelta(days=10)).isoformat()
OLD = (date.today() - timedelta(days=400)).isoformat()


# ---- parse_date_value -----------------------------------------------------


def test_parse_date_iso():
    assert parse_date_value("2026-04-15") == date(2026, 4, 15)


def test_parse_date_iso_with_time():
    assert parse_date_value("2026-04-15T08:30:00Z") == date(2026, 4, 15)


def test_parse_date_human():
    assert parse_date_value("April 15, 2026") == date(2026, 4, 15)


def test_parse_date_unparseable():
    assert parse_date_value("not a date") is None
    assert parse_date_value("") is None
    assert parse_date_value(None) is None


def test_parse_date_rejects_far_future():
    assert parse_date_value("2099-01-01") is None


def test_parse_date_rejects_pre_2010():
    assert parse_date_value("2005-06-01") is None


# ---- date_from_url --------------------------------------------------------


def test_date_from_url_full():
    assert date_from_url("https://x.example/news/2026/04/15/headline") == date(2026, 4, 15)


def test_date_from_url_year_month_only():
    assert date_from_url("https://x.example/blog/2026/03/headline") == date(2026, 3, 1)


def test_date_from_url_none():
    assert date_from_url("https://x.example/news/some-headline") is None


def test_date_from_url_rejects_invalid():
    assert date_from_url("https://x.example/news/2026/13/01/x") is None


# ---- url filters ----------------------------------------------------------


def test_passes_url_filter_same_section():
    assert _passes_url_filter("https://x.example/news/foo", "https://x.example/news") is True


def test_passes_url_filter_offsite():
    assert _passes_url_filter("https://twitter.com/x/status/123", "https://x.example/news") is False


def test_passes_url_filter_other_section():
    assert _passes_url_filter("https://x.example/products/sensor", "https://x.example/news") is False


def test_passes_url_filter_nested_path():
    assert _passes_url_filter(
        "https://x.example/en/company/news/q1-results",
        "https://x.example/en/company/news",
    ) is True


def test_passes_url_filter_excludes_index_itself():
    assert _passes_url_filter("https://x.example/news", "https://x.example/news") is False
    assert _passes_url_filter("https://x.example/news/", "https://x.example/news") is False


def test_archive_paths_filtered():
    assert _is_archive_url("/news/category/foo", "/news/") is True
    assert _is_archive_url("/news/tag/foo", "/news/") is True
    assert _is_archive_url("/news/page/2", "/news/") is True
    assert _is_archive_url("/news/q1-results", "/news/") is False


# ---- parse_items: phase 1 (time-anchored) ---------------------------------


def test_parse_items_canonical_time_inside_anchor():
    html = f"""
    <html><body><ul>
      <li><a href="/news/q1"><time datetime="{RECENT}">Apr 15</time> Q1 Results</a></li>
      <li><a href="/news/launch"><time datetime="{RECENT}">Apr 14</time> Skynode Launch</a></li>
    </ul></body></html>
    """
    items = parse_items(html, "https://x.example/news", "news", cutoff=CUTOFF)
    urls = sorted(i.url for i in items)
    assert urls == [
        "https://x.example/news/launch",
        "https://x.example/news/q1",
    ]
    assert all(i.title for i in items)
    assert all(i.date >= CUTOFF for i in items)


def test_parse_items_card_layout_time_sibling_anchor():
    html = f"""
    <html><body>
      <div class="card">
        <time datetime="{RECENT}">Apr 15</time>
        <h3>Q1 Results</h3>
        <a href="/news/q1">Read</a>
      </div>
      <div class="card">
        <time datetime="{RECENT}">Apr 14</time>
        <h3>Skynode Launch</h3>
        <a href="/news/launch">Read</a>
      </div>
    </body></html>
    """
    items = parse_items(html, "https://x.example/news", "news", cutoff=CUTOFF)
    titles = sorted(i.title for i in items)
    assert "Q1 Results" in titles
    assert "Skynode Launch" in titles


def test_parse_items_drops_old_items():
    html = f"""
    <html><body>
      <a href="/news/recent"><time datetime="{RECENT}">x</time> Recent</a>
      <a href="/news/old"><time datetime="{OLD}">x</time> Old</a>
    </body></html>
    """
    items = parse_items(html, "https://x.example/news", "news", cutoff=CUTOFF)
    urls = [i.url for i in items]
    assert urls == ["https://x.example/news/recent"]


def test_parse_items_drops_offsite_links():
    html = f"""
    <html><body>
      <a href="/news/inside"><time datetime="{RECENT}">x</time> Inside</a>
      <a href="https://twitter.com/x/123"><time datetime="{RECENT}">x</time> Tweet</a>
    </body></html>
    """
    items = parse_items(html, "https://x.example/news", "news", cutoff=CUTOFF)
    assert [i.url for i in items] == ["https://x.example/news/inside"]


def test_parse_items_drops_other_section_links():
    html = f"""
    <html><body>
      <a href="/news/inside"><time datetime="{RECENT}">x</time> Inside</a>
      <a href="/products/sensor"><time datetime="{RECENT}">x</time> Sensor</a>
    </body></html>
    """
    items = parse_items(html, "https://x.example/news", "news", cutoff=CUTOFF)
    assert [i.url for i in items] == ["https://x.example/news/inside"]


def test_parse_items_skips_undated_phase1():
    html = """
    <html><body>
      <a href="/news/no-date">No date here</a>
    </body></html>
    """
    items = parse_items(html, "https://x.example/news", "news", cutoff=CUTOFF)
    assert items == []


def test_parse_items_dedupes_by_url():
    html = f"""
    <html><body>
      <a href="/news/q1"><time datetime="{RECENT}">x</time> Q1</a>
      <a href="/news/q1"><time datetime="{RECENT}">x</time> Q1 dup</a>
    </body></html>
    """
    items = parse_items(html, "https://x.example/news", "news", cutoff=CUTOFF)
    assert len(items) == 1


# ---- parse_items: phase 2 (anchor-anchored fallback) ----------------------


def test_parse_items_phase2_url_dated_path():
    """No <time> elements; date inferred from /YYYY/MM/DD URL."""
    html = """
    <html><body>
      <a href="/news/2026/04/15/q1">Q1</a>
      <a href="/news/2026/04/14/launch">Launch</a>
    </body></html>
    """
    items = parse_items(html, "https://x.example/news", "news", cutoff=CUTOFF)
    urls = sorted(i.url for i in items)
    assert urls == [
        "https://x.example/news/2026/04/14/launch",
        "https://x.example/news/2026/04/15/q1",
    ]


def test_parse_items_phase2_jsonld_dates():
    html = f"""
    <html><head>
      <script type="application/ld+json">
      {{
        "@context": "https://schema.org",
        "@type": "Article",
        "url": "https://x.example/news/q1",
        "datePublished": "{RECENT}"
      }}
      </script>
    </head><body>
      <a href="/news/q1">Q1 Results</a>
    </body></html>
    """
    items = parse_items(html, "https://x.example/news", "news", cutoff=CUTOFF)
    assert [i.url for i in items] == ["https://x.example/news/q1"]
    assert items[0].title == "Q1 Results"


def test_collect_jsonld_dates_handles_graph():
    soup = BeautifulSoup(f"""
    <script type="application/ld+json">
    {{"@graph":[
      {{"@type":"Article","url":"https://x.example/news/q1","datePublished":"{RECENT}"}}
    ]}}
    </script>
    """, "lxml")
    out = collect_jsonld_dates(soup)
    assert out == {"https://x.example/news/q1": date.fromisoformat(RECENT)}


# ---- merge_into_state -----------------------------------------------------


def test_merge_into_state_flags_new_only():
    items = [
        Item(url="https://x.example/news/old",
             title="Old", date=date.today() - timedelta(days=20),
             source_url="https://x.example/news", source_kind="news"),
        Item(url="https://x.example/news/new",
             title="New", date=date.today() - timedelta(days=5),
             source_url="https://x.example/news", source_kind="news"),
    ]
    state = {"slug": "v", "polls_completed": 1,
             "seen_urls": ["https://x.example/news/old"]}
    rendered, new_count = merge_into_state(state, items, now_iso="2026-05-04T00:00:00+00:00")
    assert new_count == 1
    flags = {r["url"]: r["is_new"] for r in rendered}
    assert flags["https://x.example/news/old"] is False
    assert flags["https://x.example/news/new"] is True
    assert "https://x.example/news/new" in state["seen_urls"]
    assert state["polls_completed"] == 2


def test_merge_into_state_sorts_newest_first():
    items = [
        Item(url="https://x.example/news/a",
             title="A", date=date(2026, 1, 1),
             source_url="https://x.example/news", source_kind="news"),
        Item(url="https://x.example/news/b",
             title="B", date=date(2026, 4, 1),
             source_url="https://x.example/news", source_kind="news"),
    ]
    rendered, _ = merge_into_state({"seen_urls": []}, items, now_iso="t")
    assert rendered[0]["url"].endswith("/b")
    assert rendered[1]["url"].endswith("/a")
