"""One-time migration of existing extract outputs to canonical resource IDs.

Existing `extract/output/runs/<run-id>/<slug>/{profile,products,products-priority}.json`
files cite resources by per-run `resource-NNNN`. Now that
`<vendors_root>/<slug>/website/` is the canonical corpus with its own
stable numeric IDs, those citations need to be rewritten.

For each citation:
  1. Look up the URL via the per-run mirror corpus (the original
     `corpus_root` is recorded in the extract output).
  2. Look up the URL in canonical's `url_id_map.json` to get the new
     resource_id.
  3. Re-extract the snippet at `line_start..line_end` from the canonical
     text file. If it matches the original snippet, the migration is
     clean. If not, the citation gets flagged in the migration report
     (typically because a later round re-fetched the page and the line
     content drifted — operator must decide to drop or re-extract).

Citations are detected by shape (any JSON dict carrying both
`resource_id` and `line_start`/`line_end`) so the walker survives
schema additions over time without code changes.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_TARGET_FILES = ("profile.json", "products.json", "products-priority.json")


@dataclass
class _CitationLocation:
    """Pointer into the in-memory JSON tree where a citation lives."""
    parent: Any  # the dict containing the citation
    key: str  # key in the parent (always "evidence" today, but kept generic)
    index: int | None  # index within a list, or None if not list
    field_path: str  # human-readable path for the report


@dataclass
class CitationMigrationOutcome:
    field_path: str
    resource_id_before: str
    resource_id_after: str | None
    url: str | None
    status: str  # "migrated" | "snippet_drift" | "url_not_in_canonical" | "no_per_run_lookup" | "no_change"
    note: str | None = None


@dataclass
class FileMigrationReport:
    file: str
    citations_total: int = 0
    citations_migrated: int = 0
    citations_flagged: int = 0
    outcomes: list[CitationMigrationOutcome] = field(default_factory=list)


@dataclass
class MigrationReport:
    slug: str
    canonical_dir: str
    files: list[FileMigrationReport] = field(default_factory=list)
    dry_run: bool = False


def migrate_citations(
    slug: str,
    *,
    vendors_root: Path,
    extract_root: Path,
    dry_run: bool = False,
) -> MigrationReport:
    """Walk every extract output for `slug` under `extract_root` and
    rewrite citations to point at canonical resource IDs.

    `extract_root` is the package root (e.g. `extract/`); per-vendor
    extract outputs live at
    `<extract_root>/output/runs/<run-id>/<slug>/`.
    """
    canonical_dir = vendors_root / slug / "website"
    if not canonical_dir.is_dir():
        raise FileNotFoundError(
            f"canonical dir not found: {canonical_dir} "
            f"(run `uxv-mirror promote {slug}` first)"
        )

    canonical_url_to_id = _load_canonical_url_map(canonical_dir)
    canonical_text_dir = canonical_dir / "text"

    runs_root = extract_root / "output" / "runs"
    report = MigrationReport(
        slug=slug, canonical_dir=str(canonical_dir), dry_run=dry_run
    )
    if not runs_root.is_dir():
        return report

    for run_dir in sorted(runs_root.iterdir()):
        if not run_dir.is_dir():
            continue
        target_dir = run_dir / slug
        if not target_dir.is_dir():
            continue
        for fname in _TARGET_FILES:
            fpath = target_dir / fname
            if not fpath.exists():
                continue
            file_report = _migrate_one_file(
                fpath=fpath,
                canonical_url_to_id=canonical_url_to_id,
                canonical_text_dir=canonical_text_dir,
                dry_run=dry_run,
            )
            report.files.append(file_report)

    if not dry_run:
        report_path = canonical_dir.parent / f"migration_report.json"
        report_path.write_text(_report_to_json(report))

    return report


# ---------------------------------------------------------------------------


def _load_canonical_url_map(canonical_dir: Path) -> dict[str, int]:
    map_path = canonical_dir / "url_id_map.json"
    if not map_path.exists():
        raise FileNotFoundError(
            f"{map_path} not found — re-run `uxv-mirror promote` after "
            f"upgrading promote.py to write url_id_map.json"
        )
    return {
        str(k): int(v) for k, v in json.loads(map_path.read_text()).items()
    }


def _migrate_one_file(
    *,
    fpath: Path,
    canonical_url_to_id: dict[str, int],
    canonical_text_dir: Path,
    dry_run: bool,
) -> FileMigrationReport:
    report = FileMigrationReport(file=str(fpath))
    doc = json.loads(fpath.read_text())

    # Locate the per-run corpus this extract was produced against.
    per_run_url_lookup = _build_per_run_url_lookup(doc)

    citations = _find_citations(doc)
    for cit, loc in citations:
        report.citations_total += 1
        outcome = _migrate_one_citation(
            cit,
            field_path=loc.field_path,
            per_run_url_lookup=per_run_url_lookup,
            canonical_url_to_id=canonical_url_to_id,
            canonical_text_dir=canonical_text_dir,
        )
        report.outcomes.append(outcome)
        if outcome.status == "migrated":
            report.citations_migrated += 1
        elif outcome.status not in ("no_change",):
            report.citations_flagged += 1

    if not dry_run and report.citations_migrated > 0:
        # Backup before overwrite — promote-style atomicity.
        backup = fpath.with_suffix(fpath.suffix + ".pre-migrate")
        if not backup.exists():
            shutil.copy2(fpath, backup)
        fpath.write_text(json.dumps(doc, indent=2))

    return report


def _build_per_run_url_lookup(doc: dict[str, Any]) -> dict[str, str] | None:
    """Read the per-run mirror manifest referenced by `doc.corpus_root`
    and build resource_id → url. Returns None if we can't locate it
    (citations from such docs are flagged as "no_per_run_lookup")."""
    corpus_root = doc.get("corpus_root")
    if not corpus_root:
        return None
    manifest_path = Path(corpus_root) / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError:
        return None
    out: dict[str, str] = {}
    for entry in manifest.get("crawl_index", []):
        rid = entry.get("resource_id")
        url = entry.get("url")
        if rid and url:
            out[rid] = url
    return out


def _find_citations(
    doc: Any, _path: str = "$"
) -> list[tuple[dict[str, Any], _CitationLocation]]:
    """Walk the JSON tree and find every dict that looks like a
    Citation (has `resource_id` and either `line_start` or `snippet`)."""
    found: list[tuple[dict[str, Any], _CitationLocation]] = []
    if isinstance(doc, dict):
        if (
            "resource_id" in doc
            and isinstance(doc.get("resource_id"), str)
            and doc.get("resource_id", "").startswith("resource-")
            and ("line_start" in doc or "snippet" in doc)
        ):
            found.append((doc, _CitationLocation(parent=None, key="", index=None, field_path=_path)))
        for k, v in doc.items():
            found.extend(_find_citations(v, _path=f"{_path}.{k}"))
    elif isinstance(doc, list):
        for i, v in enumerate(doc):
            found.extend(_find_citations(v, _path=f"{_path}[{i}]"))
    return found


def _migrate_one_citation(
    cit: dict[str, Any],
    *,
    field_path: str,
    per_run_url_lookup: dict[str, str] | None,
    canonical_url_to_id: dict[str, int],
    canonical_text_dir: Path,
) -> CitationMigrationOutcome:
    rid_before = cit.get("resource_id", "")

    if per_run_url_lookup is None:
        return CitationMigrationOutcome(
            field_path=field_path, resource_id_before=rid_before,
            resource_id_after=None, url=None,
            status="no_per_run_lookup",
            note="extract document has no `corpus_root` or its manifest is unreadable",
        )

    url = per_run_url_lookup.get(rid_before)
    if url is None:
        return CitationMigrationOutcome(
            field_path=field_path, resource_id_before=rid_before,
            resource_id_after=None, url=None,
            status="no_per_run_lookup",
            note=f"resource_id {rid_before!r} not in per-run crawl_index",
        )

    canonical_idx = canonical_url_to_id.get(url)
    if canonical_idx is None:
        return CitationMigrationOutcome(
            field_path=field_path, resource_id_before=rid_before,
            resource_id_after=None, url=url,
            status="url_not_in_canonical",
            note="URL not present in canonical url_id_map (canonical may be stale; re-promote)",
        )

    rid_after = f"resource-{canonical_idx:04d}"

    # Verify snippet still matches at the same line range in canonical.
    # Snippet drift can occur even when rid_after == rid_before (same
    # URL, same canonical position) because a later round may have
    # re-fetched the page with different content.
    line_start = cit.get("line_start")
    line_end = cit.get("line_end")
    expected_snippet = cit.get("snippet")
    if (
        isinstance(line_start, int)
        and isinstance(line_end, int)
        and isinstance(expected_snippet, str)
    ):
        canonical_text = _read_canonical_text(canonical_text_dir, canonical_idx)
        if canonical_text is not None:
            actual = "\n".join(canonical_text[line_start - 1 : line_end])
            if actual != expected_snippet:
                return CitationMigrationOutcome(
                    field_path=field_path, resource_id_before=rid_before,
                    resource_id_after=rid_after, url=url,
                    status="snippet_drift",
                    note=(
                        f"canonical text at L{line_start}-{line_end} differs "
                        f"from stored snippet (canonical first 80 chars: "
                        f"{actual[:80]!r}); citation NOT rewritten"
                    ),
                )

    if rid_after == rid_before:
        return CitationMigrationOutcome(
            field_path=field_path, resource_id_before=rid_before,
            resource_id_after=rid_after, url=url, status="no_change",
        )

    # Clean migration: rewrite the resource_id in place.
    cit["resource_id"] = rid_after
    return CitationMigrationOutcome(
        field_path=field_path, resource_id_before=rid_before,
        resource_id_after=rid_after, url=url, status="migrated",
    )


def _read_canonical_text(
    canonical_text_dir: Path, canonical_idx: int
) -> list[str] | None:
    matches = sorted(canonical_text_dir.glob(f"{canonical_idx:04d}-*.txt"))
    if not matches:
        return None
    return matches[0].read_text().splitlines()


def _report_to_json(report: MigrationReport) -> str:
    return json.dumps(
        {
            "slug": report.slug,
            "canonical_dir": report.canonical_dir,
            "dry_run": report.dry_run,
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "files": [
                {
                    "file": fr.file,
                    "citations_total": fr.citations_total,
                    "citations_migrated": fr.citations_migrated,
                    "citations_flagged": fr.citations_flagged,
                    "outcomes": [
                        {
                            "field_path": o.field_path,
                            "resource_id_before": o.resource_id_before,
                            "resource_id_after": o.resource_id_after,
                            "url": o.url,
                            "status": o.status,
                            "note": o.note,
                        }
                        for o in fr.outcomes
                    ],
                }
                for fr in report.files
            ],
        },
        indent=2,
    )
