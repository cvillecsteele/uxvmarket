#!/usr/bin/env python3
"""Poll vendor news indexes; emit a delta digest of fresh items.

Reads `vendors/<slug>/newsletter_sources.json` (produced by
`detect_newsletter_sources.py`) and, for each source URL, fetches the
rendered page via Browserless `/smart-scrape`, extracts candidate
items, applies freshness + same-section filters, computes the delta
against the per-vendor URL-set state, and writes:

  - `vendors/<slug>/newsletter_state.json`  (URL-set + poll metadata)
  - `vendors/<slug>/newsletter_items.json`  (current in-window items)
  - `extract/output/newsletter_digest.json` (cohort-wide new items)

Constraint: items older than `--cutoff-days` (default 180) are
dropped. "If it's not new, it's not news." Items with no parseable
date are also dropped — conservative.

Usage:
    mirroring/.venv/bin/python scripts/poll_newsletter_sources.py
    mirroring/.venv/bin/python scripts/poll_newsletter_sources.py auterion
    mirroring/.venv/bin/python scripts/poll_newsletter_sources.py --cutoff-days 90
    mirroring/.venv/bin/python scripts/poll_newsletter_sources.py --concurrency 8
    mirroring/.venv/bin/python scripts/poll_newsletter_sources.py --dry-run
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import sys
import threading
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag
from dateutil import parser as dateparser

from uxv_mirroring.browserless import BrowserlessClient, BrowserlessHttpError
from uxv_mirroring.contracts import MirrorTarget
from uxv_mirroring.mirror import MirrorClient, policy_for_profile
from uxv_mirroring.promote import promote

ROOT = Path(__file__).resolve().parent.parent
VENDORS_ROOT = ROOT / "vendors"
MIRRORING_ROOT = ROOT / "mirroring"
DIGEST_PATH = ROOT / "extract" / "output" / "newsletter_digest.json"

# A floor on plausible publication dates — anything older is junk
# (default footer years, JS counters, decorative timestamps).
EPOCH_FLOOR = date(2010, 1, 1)


# ---------- data types ----------------------------------------------------


@dataclass
class Item:
    url: str
    title: str
    date: date
    source_url: str
    source_kind: str

    def to_dict(self, *, first_seen_at: str, is_new: bool) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "date": self.date.isoformat(),
            "source_url": self.source_url,
            "source_kind": self.source_kind,
            "first_seen_at": first_seen_at,
            "is_new": is_new,
        }


# ---------- date extraction -----------------------------------------------


_URL_DATE_RE = re.compile(r"/(20\d{2})/(\d{1,2})(?:/(\d{1,2}))?(?=/|$)")

_MONTH_NAMES = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?)"
)
_DATE_PATTERNS = [
    re.compile(rf"\b{_MONTH_NAMES}\s+\d{{1,2}},?\s+20\d{{2}}\b", re.IGNORECASE),
    re.compile(rf"\b\d{{1,2}}\s+{_MONTH_NAMES}\s+20\d{{2}}\b", re.IGNORECASE),
    re.compile(r"\b20\d{2}-\d{1,2}-\d{1,2}\b"),
    re.compile(r"\b\d{1,2}/\d{1,2}/20\d{2}\b"),
]


def find_date_in_text(text: str | None) -> date | None:
    """Locate a date in arbitrary text using strict patterns.

    Avoids dateutil's fuzzy parse, which happily matches incidental
    dates ("Updated: today") embedded in marketing copy or footers.
    """
    if not text:
        return None
    text = " ".join(text.split())
    for pat in _DATE_PATTERNS:
        m = pat.search(text)
        if m:
            d = parse_date_value(m.group(0))
            if d is not None:
                return d
    return None


def parse_date_value(value: Any) -> date | None:
    """Best-effort parse of a single string into a real date."""
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        d = value.date() if isinstance(value, datetime) else value
        return _validate(d)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = dateparser.parse(text, fuzzy=False)
    except (ValueError, OverflowError, TypeError):
        try:
            parsed = dateparser.parse(text, fuzzy=True)
        except (ValueError, OverflowError, TypeError):
            return None
    if parsed is None:
        return None
    return _validate(parsed.date() if isinstance(parsed, datetime) else parsed)


def _validate(d: date) -> date | None:
    today = date.today()
    if d > today + timedelta(days=2):  # small clock-skew tolerance
        return None
    if d < EPOCH_FLOOR:
        return None
    return d


def date_from_url(url: str) -> date | None:
    """Pull /YYYY/MM(/DD) from a URL path."""
    m = _URL_DATE_RE.search(urlparse(url).path)
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3) or 1)
    try:
        return _validate(date(y, mo, d))
    except ValueError:
        return None


def collect_jsonld_dates(soup: BeautifulSoup) -> dict[str, date]:
    """Build a {url -> publish date} map from JSON-LD blocks."""
    out: dict[str, date] = {}
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        text = script.string or script.get_text() or ""
        if not text.strip():
            continue
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            continue
        for entry in _walk_jsonld(data):
            if not isinstance(entry, dict):
                continue
            url = entry.get("url") or entry.get("@id") or _extract_main_entity_url(entry)
            published = entry.get("datePublished") or entry.get("dateCreated")
            if not url or not published:
                continue
            d = parse_date_value(published)
            if d is None:
                continue
            if isinstance(url, str):
                out[url] = d
    return out


def _walk_jsonld(node: Any) -> Iterable[Any]:
    if isinstance(node, dict):
        yield node
        graph = node.get("@graph")
        if isinstance(graph, list):
            yield from _walk_jsonld(graph)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_jsonld(item)


def _extract_main_entity_url(entry: dict) -> str | None:
    moe = entry.get("mainEntityOfPage")
    if isinstance(moe, dict):
        return moe.get("@id") or moe.get("url")
    if isinstance(moe, str):
        return moe
    return None


# ---------- item extraction -----------------------------------------------


def _normalize_url(href: str, base: str) -> str | None:
    if not href:
        return None
    href = href.strip()
    if href.startswith(("javascript:", "mailto:", "tel:", "#")):
        return None
    abs_url = urljoin(base, href)
    parsed = urlparse(abs_url)
    if parsed.scheme not in ("http", "https"):
        return None
    # Strip fragment; keep query (some vendors paginate via ?page=).
    return parsed._replace(fragment="").geturl()


def _family_prefix(source_url: str) -> str:
    """Path prefix items must live under, with trailing slash."""
    path = urlparse(source_url).path or "/"
    if not path.endswith("/"):
        path = path + "/"
    return path


_ARCHIVE_SEGMENTS = {"category", "tag", "tags", "author", "page", "p", "archive"}


def _is_archive_url(item_path: str, family: str) -> bool:
    """True for tag/category/author/page archive children of the family."""
    rest = item_path[len(family):] if item_path.startswith(family) else ""
    if not rest:
        return False
    first = rest.split("/", 1)[0]
    return first in _ARCHIVE_SEGMENTS


def _passes_url_filter(item_url: str, source_url: str) -> bool:
    src = urlparse(source_url)
    item = urlparse(item_url)
    if item.netloc != src.netloc:
        return False
    family = _family_prefix(source_url)
    if not item.path.startswith(family):
        return False
    if item.path.rstrip("/") == family.rstrip("/"):
        return False  # the index itself
    if _is_archive_url(item.path, family):
        return False
    return True


def _nearest_anchor(time_el: Tag) -> Tag | None:
    """Find the most plausible <a href> for a given <time> element."""
    # Walk up: enclosing <a>.
    cur: Tag | None = time_el
    for _ in range(6):
        if cur is None:
            break
        if cur.name == "a" and cur.get("href"):
            return cur
        cur = cur.parent if isinstance(cur.parent, Tag) else None
    # Walk up to a card-like container, then find the first <a href> inside.
    cur = time_el
    for _ in range(5):
        if cur is None:
            break
        parent = cur.parent if isinstance(cur.parent, Tag) else None
        if parent is None:
            break
        candidate = parent.find("a", href=True)
        if candidate and isinstance(candidate, Tag):
            return candidate
        cur = parent
    return None


_GENERIC_LINK_TEXT = {
    "read", "read more", "more", "view", "view more", "details",
    "continue", "continue reading", "see more", "learn more",
}


def _strip_leading_date(text: str) -> str:
    """Trim a leading date prefix like 'April 27, 2026' from a title."""
    for pat in _DATE_PATTERNS:
        m = pat.match(text)
        if m:
            text = text[m.end():].lstrip(" :-—— ")
            break
    return text


def _title_for(anchor: Tag) -> str:
    """Anchor text, falling back to nearby heading when it's generic."""
    text = " ".join((anchor.get_text() or "").split()).strip()
    text = _strip_leading_date(text)
    if text and len(text) > 2 and text.lower() not in _GENERIC_LINK_TEXT:
        return text[:240]
    # Nearest heading inside the same card.
    cur: Tag | None = anchor
    for _ in range(5):
        if cur is None:
            break
        parent = cur.parent if isinstance(cur.parent, Tag) else None
        if parent is None:
            break
        h = parent.find(["h1", "h2", "h3", "h4"])
        if h and isinstance(h, Tag):
            ht = " ".join((h.get_text() or "").split()).strip()
            if ht:
                return ht[:240]
        cur = parent
    # Last resort: anchor's title attr.
    title_attr = anchor.get("title")
    if isinstance(title_attr, str) and title_attr.strip():
        return title_attr.strip()[:240]
    return ""


def _extract_phase1(
    soup: BeautifulSoup, source_url: str, jsonld_dates: dict[str, date]
) -> list[tuple[str, str, date]]:
    """Time-anchored extraction: find every <time>, walk to its <a>."""
    out: list[tuple[str, str, date]] = []
    for time_el in soup.find_all("time"):
        d = parse_date_value(time_el.get("datetime")) or parse_date_value(time_el.get_text())
        anchor = _nearest_anchor(time_el)
        if anchor is None:
            continue
        href = anchor.get("href")
        if not isinstance(href, str):
            continue
        item_url = _normalize_url(href, source_url)
        if item_url is None:
            continue
        if d is None:
            d = jsonld_dates.get(item_url) or date_from_url(item_url)
        if d is None:
            continue
        title = _title_for(anchor)
        out.append((item_url, title, d))
    return out


# A URL repeated more than this many times on a listing page is almost
# certainly a category/tag badge, not a unique article.
_REPEAT_LINK_THRESHOLD = 4


def _count_qualifying_urls(soup: BeautifulSoup, source_url: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for a in soup.find_all("a", href=True):
        href = a.get("href")
        if not isinstance(href, str):
            continue
        url = _normalize_url(href, source_url)
        if url and _passes_url_filter(url, source_url):
            counts[url] = counts.get(url, 0) + 1
    return counts


def _extract_phase2(
    soup: BeautifulSoup,
    source_url: str,
    jsonld_dates: dict[str, date],
    url_counts: dict[str, int],
) -> list[tuple[str, str, date]]:
    """Anchor-anchored fallback: walk anchors under the family path."""
    out: list[tuple[str, str, date]] = []
    for a in soup.find_all("a", href=True):
        href = a.get("href")
        if not isinstance(href, str):
            continue
        item_url = _normalize_url(href, source_url)
        if item_url is None:
            continue
        if not _passes_url_filter(item_url, source_url):
            continue
        if url_counts.get(item_url, 0) >= _REPEAT_LINK_THRESHOLD:
            continue  # category/tag badge
        d = jsonld_dates.get(item_url) or date_from_url(item_url)
        if d is None:
            # Strict regex within the anchor's own text first
            # (Kratos pattern: "April 27, 2026<title>"), then parent.
            d = find_date_in_text(a.get_text(" ", strip=True))
        if d is None:
            scope = a.parent
            for _ in range(2):
                if not isinstance(scope, Tag):
                    break
                d = find_date_in_text(scope.get_text(" ", strip=True))
                if d is not None:
                    break
                scope = scope.parent if isinstance(scope.parent, Tag) else None
        if d is None:
            continue
        title = _title_for(a)
        out.append((item_url, title, d))
    return out


def parse_items(
    html: str,
    source_url: str,
    source_kind: str,
    *,
    cutoff: date,
) -> list[Item]:
    """Extract candidate items from a rendered news-index page."""
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    jsonld_dates = collect_jsonld_dates(soup)

    url_counts = _count_qualifying_urls(soup, source_url)
    raw = _extract_phase1(soup, source_url, jsonld_dates)
    # Phase 1 may have surfaced category badges via <time> proximity too —
    # filter them with the same repeat-URL signal.
    raw = [(u, t, d) for (u, t, d) in raw
           if url_counts.get(u, 0) < _REPEAT_LINK_THRESHOLD]
    if not raw:
        raw = _extract_phase2(soup, source_url, jsonld_dates, url_counts)

    # Dedupe by URL (first occurrence wins; date will be consistent enough).
    seen: dict[str, Item] = {}
    for item_url, title, d in raw:
        if item_url in seen:
            continue
        if not _passes_url_filter(item_url, source_url):
            continue
        if d < cutoff:
            continue
        seen[item_url] = Item(
            url=item_url,
            title=title,
            date=d,
            source_url=source_url,
            source_kind=source_kind,
        )
    return list(seen.values())


# ---------- per-vendor poll -----------------------------------------------


def load_state(slug: str, vendors_root: Path) -> dict[str, Any]:
    path = vendors_root / slug / "newsletter_state.json"
    if not path.exists():
        return {"slug": slug, "polls_completed": 0, "seen_urls": []}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {"slug": slug, "polls_completed": 0, "seen_urls": []}


def save_state_and_items(
    slug: str,
    vendors_root: Path,
    *,
    state: dict[str, Any],
    items_payload: dict[str, Any],
    dry_run: bool,
) -> None:
    if dry_run:
        return
    base = vendors_root / slug
    base.mkdir(parents=True, exist_ok=True)
    (base / "newsletter_state.json").write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    (base / "newsletter_items.json").write_text(json.dumps(items_payload, indent=2, sort_keys=True) + "\n")


def poll_vendor(
    slug: str,
    sidecar: dict[str, Any],
    *,
    client: BrowserlessClient,
    cutoff: date,
    now_iso: str,
    timeout_ms: int,
) -> dict[str, Any]:
    """Fetch+parse all sources for one vendor; return result envelope."""
    sources = sidecar.get("sources") or []
    fetch_errors: list[dict[str, Any]] = []
    items_by_url: dict[str, Item] = {}
    calls = 0
    for source in sources:
        url = source.get("url")
        kind = source.get("kind") or "news"
        if not url:
            continue
        try:
            calls += 1
            result = client.smart_scrape(url=url, timeout_ms=timeout_ms)
        except BrowserlessHttpError as exc:
            fetch_errors.append({"url": url, "error": str(exc)})
            continue
        except Exception as exc:  # network / timeout / payload validation
            fetch_errors.append({"url": url, "error": f"{type(exc).__name__}: {exc}"})
            continue
        if not result.ok or not result.content:
            fetch_errors.append({
                "url": url,
                "error": result.message or "no content",
            })
            continue
        try:
            for item in parse_items(result.content, url, kind, cutoff=cutoff):
                # First source wins on duplicates within a vendor.
                items_by_url.setdefault(item.url, item)
        except Exception as exc:
            fetch_errors.append({
                "url": url,
                "error": f"parse error: {type(exc).__name__}: {exc}",
            })
    return {
        "slug": slug,
        "items": list(items_by_url.values()),
        "fetch_errors": fetch_errors,
        "browserless_calls": calls,
        "polled_at": now_iso,
    }


def merge_into_state(
    state: dict[str, Any],
    items: list[Item],
    *,
    now_iso: str,
) -> tuple[list[dict[str, Any]], int]:
    """Update state in place; return (rendered items list, new count)."""
    seen: set[str] = set(state.get("seen_urls") or [])
    rendered: list[dict[str, Any]] = []
    new_count = 0
    for item in sorted(items, key=lambda x: (x.date, x.url), reverse=True):
        is_new = item.url not in seen
        if is_new:
            new_count += 1
            seen.add(item.url)
        rendered.append(item.to_dict(first_seen_at=now_iso, is_new=is_new))
    state["seen_urls"] = sorted(seen)
    state["last_polled_at"] = now_iso
    state["polls_completed"] = int(state.get("polls_completed", 0)) + 1
    return rendered, new_count


# ---------- mirror integration --------------------------------------------


def _vendor_homepage(sidecar: dict[str, Any]) -> str | None:
    return sidecar.get("homepage_url") or None


def _vendor_display_name(slug: str) -> str:
    profile = VENDORS_ROOT / slug / "profile.json"
    if profile.exists():
        try:
            data = json.loads(profile.read_text())
            for key in ("display_name", "name", "company_name"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        except (json.JSONDecodeError, OSError):
            pass
    return slug


def _already_mirrored_urls(slug: str) -> set[str]:
    """URLs that already have a fetched resource in the canonical corpus."""
    ci_path = VENDORS_ROOT / slug / "website" / "crawl_index.json"
    if not ci_path.exists():
        return set()
    try:
        ci = json.loads(ci_path.read_text())
    except (json.JSONDecodeError, OSError):
        return set()
    out: set[str] = set()
    for entry in ci.get("entries", []):
        if entry.get("status") == "fetched" and entry.get("resource_id"):
            for u in (entry.get("url"), entry.get("final_url")):
                if isinstance(u, str):
                    out.add(u)
    return out


def mirror_items_via_api(
    items_by_vendor: dict[str, dict[str, Any]],
    *,
    mirroring_root: Path,
    vendors_root: Path,
) -> dict[str, Any]:
    """For each (slug -> {homepage, urls}), fetch+persist URLs via the
    mirroring API (mirror_targets + promote). Skips URLs already in the
    canonical corpus so backfills are idempotent."""
    targets: list[MirrorTarget] = []
    promoted: list[str] = []
    for slug, info in items_by_vendor.items():
        urls = sorted({u for u in info["urls"] if u})
        if not urls:
            continue
        already = _already_mirrored_urls(slug)
        urls = [u for u in urls if u not in already]
        if not urls:
            continue
        homepage = info.get("homepage_url")
        if not homepage:
            continue
        targets.append(MirrorTarget(
            target_id=slug,
            display_name=_vendor_display_name(slug),
            homepage_url=homepage,
            seed_urls=urls,
        ))
    if not targets:
        return {"vendors_mirrored": 0, "urls_mirrored": 0, "run_id": None}

    policy = policy_for_profile("quick_evidence")
    # Only seeds may be fetched — every page-class budget zeroed,
    # so the discovery /map call (one per vendor) costs but doesn't
    # pull in extra unrelated pages.
    policy.max_pages = 1
    for cls in policy.page_class_budgets:
        policy.page_class_budgets[cls] = 0
    max_seeds = max(len(t.seed_urls) for t in targets)
    policy.max_browserless_calls_per_target = max_seeds + 5

    run_id = "newsletter-poll-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    client = MirrorClient()
    client.mirror_targets(
        targets,
        policy=policy,
        workspace_root=mirroring_root,
        run_id=run_id,
        coverage_mode="force",
    )
    for target in targets:
        try:
            promote(target.target_id, workspace_root=mirroring_root, vendors_root=vendors_root)
            promoted.append(target.target_id)
        except Exception as exc:
            print(f"promote failed for {target.target_id}: {exc}", file=sys.stderr)
    return {
        "vendors_mirrored": len(targets),
        "urls_mirrored": sum(len(t.seed_urls) for t in targets),
        "promoted": len(promoted),
        "run_id": run_id,
    }


# ---------- driver --------------------------------------------------------


def discover_vendors(vendors_root: Path, only: list[str] | None) -> list[Path]:
    if only:
        return [vendors_root / slug / "newsletter_sources.json" for slug in only]
    return sorted(vendors_root.glob("*/newsletter_sources.json"))


def run(args: argparse.Namespace) -> int:
    cutoff = date.today() - timedelta(days=args.cutoff_days)
    now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    sidecars = discover_vendors(VENDORS_ROOT, args.slugs or None)
    if not sidecars:
        print("no newsletter_sources.json sidecars found", file=sys.stderr)
        return 1

    client = BrowserlessClient.from_env()
    if not client.enabled:
        print("BROWSERLESS_API_KEY / BROWSERLESS_TOKEN not set", file=sys.stderr)
        return 2

    digest_new_items: list[dict[str, Any]] = []
    vendors_with_errors: list[dict[str, Any]] = []
    counters = {
        "vendors_polled": 0,
        "vendors_skipped": 0,
        "vendors_with_new_items": 0,
        "browserless_calls": 0,
        "fetch_errors": 0,
        "new_items_count": 0,
    }
    counters_lock = threading.Lock()
    # slug -> {"homepage_url": ..., "urls": [item urls to mirror]}
    items_to_mirror: dict[str, dict[str, Any]] = {}
    items_lock = threading.Lock()

    def worker(sidecar_path: Path) -> tuple[str, dict[str, Any]] | None:
        slug = sidecar_path.parent.name
        try:
            sidecar = json.loads(sidecar_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            return slug, {"error": f"sidecar unreadable: {exc}"}
        if sidecar.get("no_sources_found") or not sidecar.get("sources"):
            with counters_lock:
                counters["vendors_skipped"] += 1
            return None
        result = poll_vendor(
            slug,
            sidecar,
            client=client,
            cutoff=cutoff,
            now_iso=now_iso,
            timeout_ms=args.timeout_ms,
        )
        return slug, result

    futures: list[concurrent.futures.Future] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        for sidecar_path in sidecars:
            futures.append(pool.submit(worker, sidecar_path))
        for future in concurrent.futures.as_completed(futures):
            try:
                outcome = future.result()
            except Exception:  # safety net — workers shouldn't raise
                traceback.print_exc()
                continue
            if outcome is None:
                continue
            slug, result = outcome
            if "error" in result:
                vendors_with_errors.append({"slug": slug, "errors": [result["error"]]})
                continue
            state = load_state(slug, VENDORS_ROOT)
            rendered, new_count = merge_into_state(state, result["items"], now_iso=now_iso)
            items_payload = {
                "slug": slug,
                "generated_at": now_iso,
                "cutoff_date": cutoff.isoformat(),
                "items": rendered,
                "items_count": len(rendered),
                "fetch_errors": result["fetch_errors"],
            }
            save_state_and_items(
                slug, VENDORS_ROOT,
                state=state,
                items_payload=items_payload,
                dry_run=args.dry_run,
            )
            with counters_lock:
                counters["vendors_polled"] += 1
                counters["browserless_calls"] += result["browserless_calls"]
                counters["fetch_errors"] += len(result["fetch_errors"])
                if new_count:
                    counters["vendors_with_new_items"] += 1
                    counters["new_items_count"] += new_count
            new_urls_for_vendor: list[str] = []
            for r in rendered:
                if r["is_new"]:
                    digest_new_items.append({
                        "vendor_slug": slug,
                        "url": r["url"],
                        "title": r["title"],
                        "date": r["date"],
                        "source_kind": r["source_kind"],
                        "source_url": r["source_url"],
                    })
                if args.mirror_items and (r["is_new"] or args.backfill_items):
                    if args.mirror_min_date and r["date"] < args.mirror_min_date:
                        continue
                    new_urls_for_vendor.append(r["url"])
            if new_urls_for_vendor:
                with items_lock:
                    items_to_mirror[slug] = {
                        "homepage_url": (
                            json.loads(
                                (VENDORS_ROOT / slug / "newsletter_sources.json").read_text()
                            ).get("homepage_url")
                        ),
                        "urls": new_urls_for_vendor,
                    }
            if result["fetch_errors"]:
                vendors_with_errors.append({"slug": slug, "errors": result["fetch_errors"]})

    mirror_summary: dict[str, Any] = {"vendors_mirrored": 0, "urls_mirrored": 0}
    if args.mirror_items and not args.dry_run and items_to_mirror:
        mirror_summary = mirror_items_via_api(
            items_to_mirror,
            mirroring_root=MIRRORING_ROOT,
            vendors_root=VENDORS_ROOT,
        )

    digest = {
        "generated_at": now_iso,
        "cutoff_date": cutoff.isoformat(),
        "cutoff_days": args.cutoff_days,
        **counters,
        "mirror_summary": mirror_summary,
        "new_items": sorted(digest_new_items, key=lambda x: (x["date"], x["vendor_slug"]), reverse=True),
        "vendors_with_errors": vendors_with_errors,
    }
    if not args.dry_run:
        DIGEST_PATH.parent.mkdir(parents=True, exist_ok=True)
        DIGEST_PATH.write_text(json.dumps(digest, indent=2, sort_keys=True) + "\n")

    print(json.dumps({k: digest[k] for k in (
        "vendors_polled", "vendors_skipped", "vendors_with_new_items",
        "new_items_count", "browserless_calls", "fetch_errors",
    )}, indent=2))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("slugs", nargs="*", help="vendor slugs to poll (default: all)")
    p.add_argument("--cutoff-days", type=int, default=180,
                   help="drop items older than this many days (default 180)")
    p.add_argument("--concurrency", type=int, default=4,
                   help="parallel vendor polls (default 4)")
    p.add_argument("--timeout-ms", type=int, default=60_000,
                   help="Browserless smart-scrape timeout (default 60000)")
    p.add_argument("--dry-run", action="store_true",
                   help="parse + summarize but don't write state/items/digest")
    p.add_argument("--no-mirror-items", dest="mirror_items", action="store_false",
                   default=True,
                   help="skip the mirror-API fetch+promote of new items "
                        "(useful for refreshing state without article fetches)")
    p.add_argument("--backfill-items", action="store_true",
                   help="mirror every in-window item, not just is_new ones, "
                        "so prior cohort runs get their article pages "
                        "persisted into the corpus")
    p.add_argument("--mirror-min-date", default=None,
                   help="only mirror items with date >= YYYY-MM-DD "
                        "(narrow the backfill window without changing the "
                        "main 180-day cutoff)")
    return run(p.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
