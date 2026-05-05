#!/usr/bin/env python3
"""Concatenate mirrored newsletter items into a single bundle file.

Reads `vendors/<slug>/newsletter_items.json`, filters to items
within `--since-days` of today, looks each item URL up in that
vendor's canonical manifest at `vendors/<slug>/website/manifest.json`,
and emits the markdown body for each with `==== <url> ====`
separators.

Usage:
    mirroring/.venv/bin/python scripts/bundle_newsletter_items.py
    mirroring/.venv/bin/python scripts/bundle_newsletter_items.py --since-days 14
    mirroring/.venv/bin/python scripts/bundle_newsletter_items.py -o /tmp/bundle.md
"""
from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDORS_ROOT = ROOT / "vendors"


def _normalize_url(url: str) -> str:
    """Trailing-slash agnostic URL key."""
    return url.rstrip("/")


def load_url_to_path(slug: str) -> dict[str, Path]:
    """Map URL -> markdown/text/html path for a vendor's mirror corpus.

    Keys are normalized (trailing slashes stripped) so callers can
    look up either form.
    """
    manifest = VENDORS_ROOT / slug / "website" / "manifest.json"
    if not manifest.exists():
        return {}
    data = json.loads(manifest.read_text())
    out: dict[str, Path] = {}
    for r in data.get("resources", []):
        if r.get("status") != "fetched":
            continue
        chosen = r.get("markdown_path") or r.get("text_path") or r.get("html_path")
        if not chosen:
            continue
        path = Path(chosen)
        if not path.is_absolute():
            path = (VENDORS_ROOT / slug / "website" / path).resolve()
        for u in (r.get("url"), r.get("final_url")):
            if isinstance(u, str):
                out[_normalize_url(u)] = path
    return out


def gather_items(since_days: int) -> list[dict]:
    cutoff = date.today() - timedelta(days=since_days)
    items: list[dict] = []
    for path in sorted(VENDORS_ROOT.glob("*/newsletter_items.json")):
        slug = path.parent.name
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        for it in data.get("items", []):
            try:
                d = date.fromisoformat(it["date"])
            except (KeyError, ValueError):
                continue
            if d < cutoff:
                continue
            items.append({**it, "slug": slug, "date_obj": d})
    items.sort(key=lambda x: (x["date_obj"], x["slug"]), reverse=True)
    return items


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--since-days", type=int, default=7,
                   help="include items dated within the last N days (default 7)")
    p.add_argument("-o", "--output", default="/tmp/uxv-newsletter-bundle.md",
                   help="output path (default /tmp/uxv-newsletter-bundle.md)")
    args = p.parse_args()

    items = gather_items(args.since_days)
    cutoff = date.today() - timedelta(days=args.since_days)

    out_path = Path(args.output)
    written = 0
    missing: list[str] = []
    with out_path.open("w") as f:
        f.write(f"# UxV newsletter bundle — items dated {cutoff.isoformat()} or later\n\n")
        f.write(f"Generated {date.today().isoformat()}. Cutoff: today − {args.since_days}d.\n\n")
        # Cache URL maps per vendor.
        url_map_cache: dict[str, dict[str, Path]] = {}
        for it in items:
            slug = it["slug"]
            if slug not in url_map_cache:
                url_map_cache[slug] = load_url_to_path(slug)
            path = url_map_cache[slug].get(_normalize_url(it["url"]))
            f.write(f"\n==== {it['url']} ====\n")
            f.write(f"vendor: {slug}\n")
            f.write(f"date:   {it['date']}\n")
            f.write(f"title:  {it.get('title', '')}\n\n")
            if path is None or not path.exists():
                f.write(f"[NOT MIRRORED — no entry for this URL in {slug}/website/manifest.json]\n")
                missing.append(f"{slug} {it['url']}")
                continue
            body = path.read_text(errors="replace").strip()
            f.write(body)
            f.write("\n")
            written += 1

    print(f"wrote {out_path}")
    print(f"  items in window: {len(items)}")
    print(f"  bodies included: {written}")
    print(f"  missing from corpus: {len(missing)}")
    for m in missing:
        print(f"    - {m}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
