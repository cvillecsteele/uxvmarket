#!/usr/bin/env python3
"""Auto-detect per-vendor newsletter sources from canonical mirror data.

For each vendor with a canonical website corpus at
`vendors/<slug>/website/`, walk the `crawl_index.json` URL list and
identify which paths look like news indexes (newsroom, news, press,
blog, stories, articles, insights, …). Writes:

  - `vendors/<slug>/newsletter_sources.json` (per vendor)
  - `extract/output/newsletter_sources_summary.json` (cohort-wide)

This is a one-shot heuristic pass; output is reviewable + editable
per vendor. The newsletter fetcher (separate phase) reads these
sidecars to know what to poll.

Usage:
    python3 scripts/detect_newsletter_sources.py            # all vendors
    python3 scripts/detect_newsletter_sources.py auterion   # one vendor
    python3 scripts/detect_newsletter_sources.py --dry-run  # don't write
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VENDORS_ROOT = PROJECT_ROOT / "vendors"
SUMMARY_PATH = PROJECT_ROOT / "extract" / "output" / "newsletter_sources_summary.json"

# ---- Reject filters --------------------------------------------------------

# Paths that start with /<2-letter>/ where <2-letter> is a known locale.
_LOCALE_PREFIX = re.compile(
    r"^/(es|ja|de|fr|cn|zh|ko|it|pt|ru|nl|pl|tw|hk|jp|en-(us|gb|au|ca))/",
    re.IGNORECASE,
)

# LinkedIn / SPA URN-style fragments: /feed/update/urn:li:...
_URN_FRAG = re.compile(r"/(feed|activity)/.*urn[:\-]")


def is_rejected_path(path: str) -> bool:
    """True if the path should never be classified as a newsletter source."""
    p = path.lower()
    if _LOCALE_PREFIX.match(p):
        return True
    if p.endswith(".pdf"):
        return True
    if _URN_FRAG.search(p):
        return True
    if p.startswith("/resources/") or p == "/resources":
        # Cohort-25 evidence: /resources is dominated by datasheets, not news.
        return True
    return False


# ---- Family classifier -----------------------------------------------------

# (rank, family_name, regex_for_root, regex_for_any)
# - regex_for_root matches the BARE INDEX path; any path that ENDS at
#   the family marker counts (e.g. /news, /en/company/news, /newsroom/)
# - regex_for_any matches any path that contains the family marker as a
#   distinct path segment (e.g. /news/foo, /en/company/news/article-1)
#
# Patterns use `(?:^|/)X(?:/|$)` to match X as a path segment ANYWHERE
# in the URL. This is necessary for sites that nest news under deeper
# paths like septentrio's /en/company/news/<article>.
_FAMILIES: list[tuple[int, str, re.Pattern, re.Pattern]] = [
    (1, "newsroom",
     re.compile(r"(?:^|/)newsroom/?$"),
     re.compile(r"(?:^|/)newsroom(?:/|$)")),
    (2, "news",
     re.compile(r"(?:^|/)news/?$"),
     re.compile(r"(?:^|/)news(?:/|$)")),
    (3, "press",
     re.compile(r"(?:^|/)(?:press(?:-?releases?|-room)?)/?$"),
     re.compile(r"(?:^|/)press(?:-?releases?|-room)?(?:/|$)")),
    # Curtiss-Wright bespoke layout
    (4, "media-center",
     re.compile(r"(?:^|/)media-center/?$"),
     re.compile(r"(?:^|/)media-center(?:/|$)")),
    (5, "blog",
     re.compile(r"(?:^|/)(?:blog|posts?)/?$"),
     re.compile(r"(?:^|/)(?:blog|posts?)(?:/|$)")),
    (6, "stories",
     re.compile(r"(?:^|/)stor(?:y|ies)/?$"),
     re.compile(r"(?:^|/)stor(?:y|ies)(?:/|$)")),
    (7, "articles",
     re.compile(r"(?:^|/)articles?/?$"),
     re.compile(r"(?:^|/)articles?(?:/|$)")),
    (8, "insights",
     re.compile(r"(?:^|/)insights?/?$"),
     re.compile(r"(?:^|/)insights?(?:/|$)")),
    (9, "news-events",
     re.compile(r"(?:^|/)news-(?:and-)?events?/?$"),
     re.compile(r"(?:^|/)news-(?:and-)?events?(?:/|$)")),
    (10, "announce",
     re.compile(r"(?:^|/)announce(?:ments?)?/?$"),
     re.compile(r"(?:^|/)announce(?:ments?)?(?:/|$)")),
    (11, "updates",
     re.compile(r"(?:^|/)updates?/?$"),
     re.compile(r"(?:^|/)updates?(?:/|$)")),
]


def normalize_path(path: str) -> str:
    """Strip prefixes, extensions, and pagination suffixes that obscure
    the underlying family marker. Returns the lowercased, canonicalised
    path used for classification AND bucketing.

    Examples:
        /News_bc_1.html → /news
        /Category/Press-Releases/foo → /press-releases/foo
        /blog.php → /blog
        /news/page/2 → /news
    """
    p = path.lower()
    # /category/<rest> → /<rest>  (WordPress-style category routes)
    if p.startswith("/category/"):
        p = "/" + p[len("/category/"):]
    # Strip common file extensions on the leaf BEFORE pagination
    # suffixes — `/news_bc_1.html` needs `.html` peeled first so the
    # `_bc_1$` regex below can fire.
    for ext in (".html", ".htm", ".php", ".aspx", ".jsp"):
        if p.endswith(ext):
            p = p[: -len(ext)]
            break
    # Strip trailing pagination segments: /page/<n>, /p/<n>
    p = re.sub(r"/(page|p)/\d+/?$", "", p)
    # Strip pagination suffixes baked into the leaf: _bc_<n>, _page_<n>
    p = re.sub(r"_bc_\d+$|_page_\d+$", "", p)
    # Trailing slash normalised away (we re-add /|$ in regexes)
    if len(p) > 1 and p.endswith("/"):
        p = p[:-1]
    return p


def classify_path(path: str) -> str | None:
    """Return the family name a path belongs to, or None if rejected /
    not a newsletter family. First-match-wins per priority order above."""
    if is_rejected_path(path):
        return None
    p = normalize_path(path)
    for _, family, _root_re, any_re in _FAMILIES:
        # Use search() so `(?:^|/)X` patterns match X as a path segment
        # anywhere in the URL (e.g. /en/company/news/foo).
        if any_re.search(p):
            return family
    return None


def family_rank(family: str) -> int:
    """Priority rank (1 = highest)."""
    for rank, name, _, _ in _FAMILIES:
        if name == family:
            return rank
    return 99


def is_bare_index(path: str, family: str) -> bool:
    """True if `path` is a bare-index URL for its family (e.g. /news,
    /newsroom, /en/company/news — not /news/foo)."""
    p = normalize_path(path)
    for _, name, root_re, _ in _FAMILIES:
        if name == family:
            return bool(root_re.search(p))
    return False


# ---- Index URL inference ---------------------------------------------------


def _segment_common_prefix(paths: list[str]) -> str:
    """Longest common SEGMENT prefix across a list of paths.

    e.g. ['/en/company/news/foo', '/en/company/news/bar']
         → '/en/company/news'
         (NOT '/en/company/news/' as a char-prefix would give)
    """
    if not paths:
        return ""
    segments_list = [p.strip("/").split("/") for p in paths if p.strip("/")]
    if not segments_list:
        return ""
    common: list[str] = []
    for parts in zip(*segments_list):
        if len(set(parts)) == 1:
            common.append(parts[0])
        else:
            break
    return "/" + "/".join(common) if common else ""


def _common_root_path(family: str, paths: list[str]) -> str:
    """Given paths in the same family, return the bare-index path.

    Strategy:
      1. If any path is itself a bare-index for this family, use the
         shortest such path.
      2. Otherwise, find the longest common SEGMENT prefix of the
         paths. That should be the index (e.g. ['/en/co/news/a',
         '/en/co/news/b'] → '/en/co/news').
      3. Fallback: use the shortest path stripped of its last segment.
      4. Last resort: '/<family>'.
    """
    if not paths:
        return f"/{family}"
    # 1. Bare-index already in the bucket? Pick the shortest.
    bares = [p for p in paths if is_bare_index(p, family)]
    if bares:
        return min(bares, key=lambda p: (len(p.strip("/").split("/")), len(p)))
    # 2. Segment-common prefix
    prefix = _segment_common_prefix(paths)
    if prefix and is_bare_index(prefix, family):
        return prefix
    # 3. Strip last segment of the shortest path
    shortest = min(paths, key=lambda p: (p.count("/"), len(p)))
    parts = shortest.strip("/").split("/")
    if len(parts) > 1:
        candidate = "/" + "/".join(parts[:-1])
        if is_bare_index(candidate, family):
            return candidate
    # 4. Last resort
    return f"/{family}"


def infer_index_url(
    homepage_url: str, family: str, paths: list[str]
) -> tuple[str, bool]:
    """Return (index_url, bare_index_seen)."""
    bare = next((p for p in paths if is_bare_index(p, family)), None)
    if bare is not None:
        return _join_homepage(homepage_url, bare), True
    inferred = _common_root_path(family, paths)
    return _join_homepage(homepage_url, inferred), False


def _join_homepage(homepage_url: str, path: str) -> str:
    parsed = urlparse(homepage_url)
    scheme = parsed.scheme or "https"
    host = parsed.netloc or homepage_url.replace("https://", "").replace("http://", "").rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    return f"{scheme}://{host}{path}"


# ---- Per-vendor detection --------------------------------------------------


def _load_crawl_entries(slug: str, vendors_root: Path) -> tuple[list[dict], str]:
    """Return (entries, homepage_url). Entries empty if no crawl_index."""
    cidx = vendors_root / slug / "website" / "crawl_index.json"
    if not cidx.exists():
        return [], ""
    doc = json.loads(cidx.read_text())
    if isinstance(doc, dict):
        entries = doc.get("entries", [])
        homepage = doc.get("target", {}).get("homepage_url", "")
    else:
        entries = doc or []
        homepage = ""
    if not homepage:
        # Fallback: try profile.json
        pf = vendors_root / slug / "profile.json"
        if pf.exists():
            try:
                homepage = json.loads(pf.read_text()).get("homepage_url", "")
            except Exception:
                pass
    return entries, homepage


def detect_for_vendor(slug: str, vendors_root: Path) -> dict:
    entries, homepage = _load_crawl_entries(slug, vendors_root)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if not entries:
        return {
            "slug": slug,
            "homepage_url": homepage,
            "generated_at": now,
            "auto_detected": True,
            "sources": [],
            "no_sources_found": True,
            "notes": "no crawl_index entries; canonical website corpus is empty or missing",
        }

    # Bucket every discovered URL by family (regardless of fetch status).
    # A `skipped_class_budget` /news/foo entry still tells us the news
    # section exists — that's exactly what newsletter-source detection
    # cares about. Bucket holds (normalized_path, original_url) so we
    # can do bare-index detection on normalized paths but report
    # real URLs for sample_items.
    buckets: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for e in entries:
        url = e.get("final_url") or e.get("url") or ""
        if not url:
            continue
        path = urlparse(url).path
        family = classify_path(path)
        if family:
            buckets[family].append((normalize_path(path), url))

    if not buckets:
        return {
            "slug": slug,
            "homepage_url": homepage,
            "generated_at": now,
            "auto_detected": True,
            "sources": [],
            "no_sources_found": True,
            "notes": "no news-family URLs in crawl_index; live homepage scrape may surface them",
        }

    # Rank families: a family with at least 3 items "qualifies" for
    # primary; among qualifiers, priority wins. If no family qualifies
    # (every bucket is 1-2 items, e.g. only nav links surfaced), fall
    # back to priority-wins across all. Reason: vendors like
    # beta-technologies have /newsroom (1 nav-link item) AND /stories
    # (129 actual articles) — pure priority-wins picks /newsroom, but
    # /stories is the real news source.
    PRIMARY_MIN_ITEMS = 3
    qualifying = [f for f, items in buckets.items() if len(items) >= PRIMARY_MIN_ITEMS]
    if qualifying:
        primary = min(qualifying, key=family_rank)
        rest = sorted(
            [f for f in buckets.keys() if f != primary],
            key=family_rank,
        )
        ranked = [primary] + rest
    else:
        ranked = sorted(buckets.keys(), key=family_rank)

    sources = []
    for rank_idx, family in enumerate(ranked, start=1):
        items = buckets[family]
        normalized_paths = [n for n, _ in items]
        index_url, bare_seen = infer_index_url(homepage, family, normalized_paths)
        # Sample items: up to 3 ORIGINAL urls of items that are not
        # the bare index.
        samples = [
            url for n, url in items if not is_bare_index(n, family)
        ][:3]
        sources.append({
            "rank": rank_idx,
            "kind": family,
            "url": index_url,
            "bare_index_seen": bare_seen,
            "item_count_seen": len(items),
            "sample_items": samples,
        })

    return {
        "slug": slug,
        "homepage_url": homepage,
        "generated_at": now,
        "auto_detected": True,
        "sources": sources,
        "no_sources_found": False,
        "notes": None,
    }


# ---- Cohort orchestration --------------------------------------------------


def _list_canonical_slugs(vendors_root: Path) -> list[str]:
    """Slugs with a canonical website corpus (crawl_index.json present
    OR profile.json present)."""
    out = []
    for d in sorted(vendors_root.iterdir()):
        if not d.is_dir():
            continue
        if (d / "website" / "crawl_index.json").exists() or (d / "profile.json").exists():
            out.append(d.name)
    return out


def write_summary(reports: list[dict], path: Path) -> None:
    kind_distribution: Counter = Counter()
    inferred_only_vendors = []
    no_sources = []
    has_sources_count = 0
    for r in reports:
        if r["no_sources_found"]:
            no_sources.append(r["slug"])
            continue
        has_sources_count += 1
        for s in r["sources"]:
            if s["rank"] == 1:
                kind_distribution[s["kind"]] += 1
            if not s["bare_index_seen"]:
                inferred_only_vendors.append({
                    "slug": r["slug"],
                    "kind": s["kind"],
                    "inferred_url": s["url"],
                })
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total_vendors": len(reports),
        "with_sources": has_sources_count,
        "without_sources": len(no_sources),
        "vendors_without_sources": no_sources,
        "primary_kind_distribution": dict(kind_distribution.most_common()),
        "vendors_with_inferred_index_only": inferred_only_vendors,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "slugs", nargs="*",
        help="Vendor slug(s) to process. Default: every canonical vendor.",
    )
    parser.add_argument(
        "--vendors-root", type=Path, default=VENDORS_ROOT,
        help=f"Default: {VENDORS_ROOT}",
    )
    parser.add_argument(
        "--summary-path", type=Path, default=SUMMARY_PATH,
        help=f"Default: {SUMMARY_PATH}",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print results; do not write any files.",
    )
    args = parser.parse_args(argv)

    slugs = args.slugs or _list_canonical_slugs(args.vendors_root)
    if not slugs:
        print("no vendors found", file=sys.stderr)
        return 1

    reports = []
    for slug in slugs:
        report = detect_for_vendor(slug, args.vendors_root)
        reports.append(report)
        if args.dry_run:
            print(json.dumps(report, indent=2))
            continue
        out = args.vendors_root / slug / "newsletter_sources.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2))

    if args.dry_run:
        return 0

    write_summary(reports, args.summary_path)

    # Console summary.
    n = len(reports)
    with_sources = sum(1 for r in reports if not r["no_sources_found"])
    primary_kinds = Counter(
        r["sources"][0]["kind"] for r in reports if r["sources"]
    )
    print(f"processed {n} vendor(s): {with_sources} with sources, {n - with_sources} without")
    print("primary kind distribution:")
    for kind, count in primary_kinds.most_common():
        print(f"  {kind:<14s} {count}")
    print(f"\nwrote per-vendor sidecars and summary: {args.summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
