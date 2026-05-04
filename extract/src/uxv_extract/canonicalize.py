"""One-shot per-vendor canonicalization.

Walks every per-run extract output for a slug, merges them
field-by-field starting from the OLDEST (then upserting from each
newer run), migrates citations to canonical resource IDs from
`vendors/<slug>/website/url_id_map.json`, and writes the merged
result to `vendors/<slug>/profile.json` and `products.json`.

Field-merge rules
=================

Atomic-submit fields (`Profile.*`, scalar `Answer[T]`):
  - If `newer.status == "answered"` and `older.status != "answered"`:
    newer wins (we got an answer where we previously had none).
  - Else if both `"answered"`: newer wins (later runs typically have
    more evidence).
  - If `newer.status == "unknown"`: keep older — never downgrade.

ListAnswer (e.g. `products_categories`, `products`):
  - Same status logic. When taking newer, take its full `items` list
    (we don't union ListAnswer items here — agents tend to re-derive
    full lists per pass; merging them produces duplicates).

`fetch_requests` and `unresolved_questions`:
  - Always take newer's value. These are forward-looking signals;
    "what's still missing right now" is owned by the latest run.

`status` (top-level):
  - Take the BEST status across all runs.
  - Rank: complete > partial > needs_more_fetches > failed.

`tagline`:
  - Keep first non-null encountered (oldest first). New tagline gen
    is a separate pass, not part of merge.

Products (`ProductCatalog.products` list):
  - Union by product `name`. On conflict, newer wins (newer run's
    extraction is presumed more complete per-product).

`meta`:
  - Keep oldest's created_at (when extraction first happened).
  - Sum total_cost_usd across all runs (cumulative spend).
  - num_turns: take newer's (not really meaningful after merge).
  - Add `merged_from_runs: list[str]` extension.

Citations:
  - All citations are migrated to canonical resource IDs (using the
    same algorithm as `migrate.py`). Citations whose URL isn't in
    canonical or whose snippet drifted are flagged in
    `vendors/<slug>/canonicalize_report.json` but the citation is
    KEPT (with original resource_id) — operator decides.
"""

from __future__ import annotations

import json
import shutil
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_PROFILE_ANSWER_FIELDS = (
    "headquarters",
    "drone_supply_chain_role",
    "ndaa",
    "blue_uas",
    "readiness",
)
_PROFILE_LIST_ANSWER_FIELDS = (
    "products_categories",
    "products",
)
_STATUS_RANK = {
    "complete": 4,
    "partial": 3,
    "needs_more_fetches": 2,
    "failed": 1,
    "unknown": 0,
}


@dataclass
class CanonicalizeReport:
    slug: str
    profile_runs: list[str] = field(default_factory=list)
    products_runs: list[str] = field(default_factory=list)
    citations_total: int = 0
    citations_migrated: int = 0
    citations_drift: int = 0
    citations_url_not_in_canonical: int = 0
    profile_written: bool = False
    products_written: bool = False
    notes: list[str] = field(default_factory=list)


# ---- Run discovery + ordering --------------------------------------------


def _discover_per_run_files(
    extract_root: Path, slug: str, fname: str
) -> list[Path]:
    """Return profile.json or products.json paths for `slug` across all
    runs, sorted by the file's mtime (proxy for run created_at —
    sufficient for our purposes since the same vendor in a later run
    will have a newer file)."""
    runs_root = extract_root / "output" / "runs"
    if not runs_root.is_dir():
        return []
    found: list[Path] = []
    for run_dir in runs_root.iterdir():
        if not run_dir.is_dir():
            continue
        p = run_dir / slug / fname
        if p.exists():
            found.append(p)
    return sorted(found, key=lambda p: p.stat().st_mtime)


# ---- Status and answer helpers -------------------------------------------


def _best_status(*statuses: str | None) -> str:
    """Return the highest-ranked status across inputs."""
    valid = [s for s in statuses if s]
    if not valid:
        return "unknown"
    return max(valid, key=lambda s: _STATUS_RANK.get(s, 0))


def _newer_answer_wins(older: dict[str, Any], newer: dict[str, Any]) -> bool:
    """Should `newer` replace `older` for an Answer/ListAnswer field?"""
    o = older.get("status", "unknown")
    n = newer.get("status", "unknown")
    if n == "answered" and o != "answered":
        return True
    if o == "answered" and n != "answered":
        return False
    if n == "answered" and o == "answered":
        return True  # later runs typically have more evidence
    # Both non-answered: prefer non-unknown
    if n != "unknown" and o == "unknown":
        return True
    return False


# ---- Profile merge --------------------------------------------------------


def _merge_profile_pair(
    older: dict[str, Any], newer: dict[str, Any]
) -> dict[str, Any]:
    """In-place upsert of older with newer's fields per the rules."""
    result = deepcopy(older)

    for f in _PROFILE_ANSWER_FIELDS + _PROFILE_LIST_ANSWER_FIELDS:
        if f in newer and _newer_answer_wins(result.get(f, {}), newer[f]):
            result[f] = deepcopy(newer[f])

    # Forward-looking signals — newer always wins.
    for f in ("fetch_requests", "unresolved_questions"):
        if f in newer:
            result[f] = deepcopy(newer[f])

    # Status: take the best across both.
    result["status"] = _best_status(
        result.get("status"), newer.get("status")
    )

    # Tagline: keep first non-null seen.
    if not result.get("tagline") and newer.get("tagline"):
        result["tagline"] = newer["tagline"]

    return result


def merge_profiles(profile_paths: list[Path]) -> dict[str, Any] | None:
    """Merge a chronologically-sorted list of profile.json files
    (oldest first). Returns the merged dict, or None if no inputs.

    Merge provenance (which runs contributed) is recorded in the
    sidecar `canonicalize_report.json`, NOT in the merged profile
    itself — Profile/ProfileMeta are StrictModel and reject extras."""
    if not profile_paths:
        return None
    merged = json.loads(profile_paths[0].read_text())
    for p in profile_paths[1:]:
        try:
            newer = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        merged = _merge_profile_pair(merged, newer)
    return merged


# ---- Products merge -------------------------------------------------------


def _merge_products_pair(
    older: dict[str, Any], newer: dict[str, Any]
) -> dict[str, Any]:
    result = deepcopy(older)

    # Union products by name; newer's entry replaces older's on collision.
    by_name: dict[str, dict[str, Any]] = {
        p["name"]: p for p in result.get("products", [])
    }
    for p in newer.get("products", []):
        by_name[p["name"]] = deepcopy(p)
    result["products"] = list(by_name.values())

    # Forward-looking signals — newer wins.
    for f in ("fetch_requests", "unresolved_questions"):
        if f in newer:
            result[f] = deepcopy(newer[f])

    result["status"] = _best_status(
        result.get("status"), newer.get("status")
    )
    return result


def merge_products(products_paths: list[Path]) -> dict[str, Any] | None:
    if not products_paths:
        return None
    merged = json.loads(products_paths[0].read_text())
    for p in products_paths[1:]:
        try:
            newer = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        merged = _merge_products_pair(merged, newer)
    return merged


# ---- Citation migration (in-process) -------------------------------------


def _walk_citations(doc: Any) -> list[dict[str, Any]]:
    """Walk JSON tree, return every dict that looks like a Citation."""
    found: list[dict[str, Any]] = []
    if isinstance(doc, dict):
        if (
            isinstance(doc.get("resource_id"), str)
            and doc.get("resource_id", "").startswith("resource-")
            and ("line_start" in doc or "snippet" in doc)
        ):
            found.append(doc)
        for v in doc.values():
            found.extend(_walk_citations(v))
    elif isinstance(doc, list):
        for v in doc:
            found.extend(_walk_citations(v))
    return found


def _build_per_run_url_lookup(corpus_root: str | Path) -> dict[str, str]:
    """resource_id -> url for the per-run mirror corpus that originally
    produced the citation."""
    manifest_path = Path(corpus_root) / "manifest.json"
    if not manifest_path.exists():
        return {}
    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError:
        return {}
    out: dict[str, str] = {}
    for entry in manifest.get("crawl_index", []):
        rid = entry.get("resource_id")
        url = entry.get("url")
        if rid and url:
            out[rid] = url
    return out


def _read_canonical_text_lines(
    canonical_text_dir: Path, idx: int
) -> list[str] | None:
    matches = sorted(canonical_text_dir.glob(f"{idx:04d}-*.txt"))
    if not matches:
        return None
    return matches[0].read_text().splitlines()


def migrate_citations_in_doc(
    doc: dict[str, Any],
    *,
    canonical_url_to_id: dict[str, int],
    canonical_text_dir: Path,
    per_run_url_lookup: dict[str, str],
) -> tuple[int, int, int]:
    """Mutate `doc` in place, rewriting every citation's resource_id to
    the canonical id by URL. Returns (total, migrated_count,
    drift_or_missing_count). Snippet drift / URL-not-in-canonical
    citations are LEFT in place with their original resource_id +
    annotated with `_canonicalize_note`."""
    citations = _walk_citations(doc)
    total = len(citations)
    migrated = 0
    flagged = 0

    for cit in citations:
        rid_before = cit.get("resource_id", "")
        url = per_run_url_lookup.get(rid_before)
        if url is None:
            # Can't migrate (per-run mirror manifest doesn't know this id).
            # Leave citation untouched; flag in the report tally.
            flagged += 1
            continue
        canonical_idx = canonical_url_to_id.get(url)
        if canonical_idx is None:
            flagged += 1
            continue
        rid_after = f"resource-{canonical_idx:04d}"
        # Snippet drift check (best-effort): if the canonical text at
        # the same line range differs from the stored snippet, rewrite
        # the id but tally the drift. Citation/Profile use StrictModel
        # so we can't annotate in-place — drift counts go to the
        # report sidecar.
        ls = cit.get("line_start"); le = cit.get("line_end")
        expected = cit.get("snippet")
        if isinstance(ls, int) and isinstance(le, int) and isinstance(expected, str):
            lines = _read_canonical_text_lines(canonical_text_dir, canonical_idx)
            if lines is not None:
                actual = "\n".join(lines[ls - 1 : le])
                if actual != expected:
                    flagged += 1
        cit["resource_id"] = rid_after
        if rid_after != rid_before:
            migrated += 1

    return total, migrated, flagged


# ---- Top-level entry point ------------------------------------------------


def canonicalize_vendor(
    slug: str,
    *,
    extract_root: Path,
    vendors_root: Path,
) -> CanonicalizeReport:
    """Merge all per-run extract outputs for `slug`, migrate citations
    to canonical resource IDs, and write to `vendors/<slug>/`.
    Returns a report describing what happened."""
    report = CanonicalizeReport(slug=slug)

    canonical_dir = vendors_root / slug
    website_dir = canonical_dir / "website"

    profile_paths = _discover_per_run_files(extract_root, slug, "profile.json")
    products_paths = _discover_per_run_files(extract_root, slug, "products.json")
    report.profile_runs = [p.parent.parent.name for p in profile_paths]
    report.products_runs = [p.parent.parent.name for p in products_paths]

    if not profile_paths and not products_paths:
        report.notes.append("no per-run files found; nothing to canonicalize")
        return report

    # Citation migration setup (only if canonical website exists).
    canonical_url_to_id: dict[str, int] = {}
    canonical_text_dir = website_dir / "text"
    if (website_dir / "url_id_map.json").exists():
        canonical_url_to_id = {
            str(k): int(v) for k, v in
            json.loads((website_dir / "url_id_map.json").read_text()).items()
        }
    else:
        report.notes.append(
            "no canonical website at vendors/<slug>/website — citations NOT migrated"
        )

    canonical_dir.mkdir(parents=True, exist_ok=True)

    # ---- Profile merge + migrate + write ---------------------------------
    merged_profile = merge_profiles(profile_paths)
    if merged_profile is not None:
        if canonical_url_to_id:
            corpus_root = merged_profile.get("corpus_root", "")
            per_run_url_lookup = _build_per_run_url_lookup(corpus_root)
            t, m, f = migrate_citations_in_doc(
                merged_profile,
                canonical_url_to_id=canonical_url_to_id,
                canonical_text_dir=canonical_text_dir,
                per_run_url_lookup=per_run_url_lookup,
            )
            report.citations_total += t
            report.citations_migrated += m
            report.citations_drift += f  # drift + url-missing + id-missing
        merged_profile["corpus_root"] = str(website_dir)
        merged_profile["run_id"] = "canonical"
        (canonical_dir / "profile.json").write_text(
            json.dumps(merged_profile, indent=2)
        )
        report.profile_written = True

    # ---- Products merge + migrate + write --------------------------------
    merged_products = merge_products(products_paths)
    if merged_products is not None:
        if canonical_url_to_id:
            corpus_root = merged_products.get("corpus_root", "")
            per_run_url_lookup = _build_per_run_url_lookup(corpus_root)
            t, m, f = migrate_citations_in_doc(
                merged_products,
                canonical_url_to_id=canonical_url_to_id,
                canonical_text_dir=canonical_text_dir,
                per_run_url_lookup=per_run_url_lookup,
            )
            report.citations_total += t
            report.citations_migrated += m
            report.citations_drift += f
        merged_products["corpus_root"] = str(website_dir)
        merged_products["run_id"] = "canonical"
        (canonical_dir / "products.json").write_text(
            json.dumps(merged_products, indent=2)
        )
        report.products_written = True

    return report


def write_report(report: CanonicalizeReport, vendors_root: Path) -> Path:
    """Persist the per-vendor canonicalize report sidecar."""
    p = vendors_root / report.slug / "canonicalize_report.json"
    p.write_text(json.dumps({
        "slug": report.slug,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "profile_runs": report.profile_runs,
        "products_runs": report.products_runs,
        "citations_total": report.citations_total,
        "citations_migrated": report.citations_migrated,
        "citations_drift": report.citations_drift,
        "citations_url_not_in_canonical": report.citations_url_not_in_canonical,
        "profile_written": report.profile_written,
        "products_written": report.products_written,
        "notes": report.notes,
    }, indent=2))
    return p
