"""Build/maintain the canonical per-vendor evidence directory.

Today, mirror writes per-run output at
`<workspace>/output/runs/<run-id>/targets/<slug>/`. That layout is great
for audit but wrong for downstream tools (extract, future SBIR /
Crunchbase fetchers, the newsletter EPIC) that need ONE canonical view
per vendor across all rounds.

`promote(slug)` walks every per-run target dir for `<slug>`, sorts by
the run's `created_at`, and writes a canonical
`<vendors_root>/<slug>/website/` that contains:

  - `manifest.json` — `MirrorCorpus`-shaped, but with `run_id="canonical"`
  - `crawl_index.json` — union of all runs' crawl_index, deduped by URL
  - `quality_report.json` — synthesised from canonical crawl_index counts
  - `promote_log.json` — append-only ledger of which runs contributed when
  - `text/`, `markdown/`, `raw/`, `json/`, `documents/` — files copied
    from the winning run for each URL, renumbered with stable canonical IDs

Resource-ID stability rules:
  - On the FIRST promote, IDs are assigned by `(depth, url)` sort order.
  - On subsequent promotes, existing canonical IDs are preserved.
    New URLs (not in the existing canonical) are appended with the next
    available numeric IDs. Existing IDs are NEVER renumbered.

Same-URL-across-rounds rule: latest run wins on content (the `MirrorResource`
and the file bytes come from the most-recent run that successfully
fetched the URL). `discovered_from` is unioned across all runs.
"""

from __future__ import annotations

import json
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from uxv_mirroring.contracts import (
    CrawlIndexEntry,
    CrawlLink,
    MirrorCorpus,
    MirrorPolicy,
    MirrorResource,
    MirrorTarget,
    QualityReport,
    utc_now_iso,
)


_FILE_DIRS = ("text", "markdown", "raw", "json", "documents")


@dataclass
class PromoteResult:
    slug: str
    action: str  # "promoted" | "noop"
    reason: str | None = None
    canonical_dir: Path | None = None
    fetched_count: int = 0
    new_url_count: int = 0
    source_run_ids: list[str] = field(default_factory=list)


@dataclass
class _RoundCorpus:
    run_id: str
    run_dir: Path
    created_at: str
    corpus: MirrorCorpus


@dataclass
class _CanonicalEntry:
    url: str
    depth: int
    in_scope: bool
    status: str
    page_class: str | None
    kind: str | None
    final_url: str | None
    skip_reason: str | None
    discovered_from: list[str]
    # Latest-fetch state (None if never fetched in any round):
    winning_resource: MirrorResource | None = None
    winning_run_dir: Path | None = None


# ---------------------------------------------------------------------------
# Public entry point


def promote(
    slug: str,
    *,
    workspace_root: Path,
    vendors_root: Path,
    log: Any = None,
) -> PromoteResult:
    """Build/refresh `<vendors_root>/<slug>/website/` from every per-run
    mirror corpus for `slug` under `<workspace_root>/output/runs/`.
    Idempotent — re-running with no new per-run data is a no-op."""
    log = log or _default_log

    rounds = _discover_per_run_corpora(workspace_root, slug, log=log)
    if not rounds:
        return PromoteResult(
            slug=slug, action="noop",
            reason=f"no per-run corpora found for slug {slug!r}",
        )

    rounds.sort(key=lambda r: r.created_at)

    canonical_urls = _accumulate_canonical(rounds)

    canonical_dir = vendors_root / slug / "website"
    existing_id_map = _load_existing_canonical_id_map(canonical_dir)
    url_to_id = _assign_canonical_ids(canonical_urls, existing_id_map)

    new_url_count = sum(1 for u in url_to_id if u not in existing_id_map)
    fetched_count = sum(
        1 for u, e in canonical_urls.items() if e.status == "fetched"
    )

    canonical_target, canonical_policy = _pick_target_and_policy(rounds)
    canonical_links = _union_crawl_links(rounds)

    tmp_dir = canonical_dir.parent / "website.tmp"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    for sd in _FILE_DIRS:
        (tmp_dir / sd).mkdir(parents=True, exist_ok=True)

    canonical_resources: list[MirrorResource] = []
    canonical_index: list[CrawlIndexEntry] = []

    for url in sorted(url_to_id.keys(), key=lambda u: url_to_id[u]):
        idx = url_to_id[url]
        rid = f"resource-{idx:04d}"
        ce = canonical_urls[url]

        if ce.status == "fetched" and ce.winning_resource is not None:
            new_resource = _copy_artifacts(
                resource=ce.winning_resource,
                new_index=idx,
                new_resource_id=rid,
                canonical_tmp=tmp_dir,
                canonical_final=canonical_dir,
            )
            canonical_resources.append(new_resource)
            entry_resource_id: str | None = rid
        else:
            entry_resource_id = None

        canonical_index.append(
            CrawlIndexEntry(
                url=url,
                discovered_from=ce.discovered_from,
                depth=ce.depth,
                in_scope=ce.in_scope,
                status=ce.status,  # type: ignore[arg-type]
                final_url=ce.final_url,
                kind=ce.kind,  # type: ignore[arg-type]
                page_class=ce.page_class,  # type: ignore[arg-type]
                skip_reason=ce.skip_reason,
                resource_id=entry_resource_id,
            )
        )

    quality = _synthesise_quality_report(canonical_index, canonical_resources)

    canonical_corpus = MirrorCorpus(
        target=canonical_target,
        policy=canonical_policy,
        run_id="canonical",
        corpus_root=str(canonical_dir),
        manifest_path=str(canonical_dir / "manifest.json"),
        crawl_index_path=str(canonical_dir / "crawl_index.json"),
        quality_report_path=str(canonical_dir / "quality_report.json"),
        resources=canonical_resources,
        crawl_index=canonical_index,
        crawl_links=canonical_links,
        quality_report=quality,
    )

    (tmp_dir / "manifest.json").write_text(
        canonical_corpus.model_dump_json(indent=2)
    )
    (tmp_dir / "crawl_index.json").write_text(
        json.dumps(
            {
                "target": canonical_target.model_dump(),
                "entries": [e.model_dump() for e in canonical_index],
                "links": [l.model_dump() for l in canonical_links],
            },
            indent=2,
        )
    )
    (tmp_dir / "quality_report.json").write_text(
        quality.model_dump_json(indent=2)
    )
    # Write the canonical url→id sidecar so subsequent promotes
    # preserve every URL's numeric position (including skipped URLs that
    # crawl_index entries don't carry a resource_id for).
    (tmp_dir / "url_id_map.json").write_text(
        json.dumps(url_to_id, indent=2, sort_keys=True)
    )

    # Atomic-ish swap: preserve promote_log from prior canonical, then move.
    prior_log = (canonical_dir / "promote_log.json").read_text() if (
        canonical_dir / "promote_log.json"
    ).exists() else None
    if canonical_dir.exists():
        backup = canonical_dir.parent / "website.old"
        if backup.exists():
            shutil.rmtree(backup)
        canonical_dir.rename(backup)
    tmp_dir.rename(canonical_dir)
    if prior_log is not None:
        (canonical_dir / "promote_log.json").write_text(prior_log)
    # Drop the backup last so a partial failure leaves it behind for recovery.
    backup = canonical_dir.parent / "website.old"
    if backup.exists():
        shutil.rmtree(backup)

    _append_promote_log(
        canonical_dir,
        source_run_ids=[r.run_id for r in rounds],
        new_url_count=new_url_count,
        fetched_count=fetched_count,
        total_url_count=len(url_to_id),
    )

    return PromoteResult(
        slug=slug,
        action="promoted",
        canonical_dir=canonical_dir,
        fetched_count=fetched_count,
        new_url_count=new_url_count,
        source_run_ids=[r.run_id for r in rounds],
    )


# ---------------------------------------------------------------------------
# Helpers


def _default_log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _discover_per_run_corpora(
    workspace_root: Path, slug: str, *, log
) -> list[_RoundCorpus]:
    runs_root = workspace_root / "output" / "runs"
    if not runs_root.is_dir():
        return []
    out: list[_RoundCorpus] = []
    for run_dir in sorted(runs_root.iterdir()):
        if not run_dir.is_dir():
            continue
        target_dir = run_dir / "targets" / slug
        manifest_path = target_dir / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            corpus = MirrorCorpus.model_validate_json(manifest_path.read_text())
        except (ValidationError, json.JSONDecodeError) as exc:
            log(f"warning: skipping {manifest_path}: {type(exc).__name__}")
            continue
        # Pull created_at from the run-level manifest if present; fall back
        # to the run dir's mtime.
        created_at = ""
        run_manifest_path = run_dir / "manifest.json"
        if run_manifest_path.exists():
            try:
                rm = json.loads(run_manifest_path.read_text())
                created_at = rm.get("created_at") or ""
            except json.JSONDecodeError:
                pass
        if not created_at:
            created_at = datetime.fromtimestamp(
                run_dir.stat().st_mtime, tz=timezone.utc
            ).isoformat(timespec="seconds")
        out.append(
            _RoundCorpus(
                run_id=run_dir.name,
                run_dir=run_dir,
                created_at=created_at,
                corpus=corpus,
            )
        )
    return out


def _accumulate_canonical(
    rounds: list[_RoundCorpus],
) -> dict[str, _CanonicalEntry]:
    canonical: dict[str, _CanonicalEntry] = {}
    for round_corpus in rounds:
        resources_by_id: dict[str, MirrorResource] = {
            r.resource_id: r for r in round_corpus.corpus.resources
        }
        for entry in round_corpus.corpus.crawl_index:
            url = entry.url
            ce = canonical.get(url)
            resource = (
                resources_by_id.get(entry.resource_id)
                if entry.resource_id
                else None
            )
            if ce is None:
                ce = _CanonicalEntry(
                    url=url,
                    depth=int(entry.depth),
                    in_scope=bool(entry.in_scope),
                    status=str(entry.status),
                    page_class=entry.page_class,
                    kind=entry.kind,
                    final_url=entry.final_url,
                    skip_reason=entry.skip_reason,
                    discovered_from=list(entry.discovered_from),
                )
                if entry.status == "fetched" and resource is not None:
                    ce.winning_resource = resource
                    ce.winning_run_dir = round_corpus.run_dir
                canonical[url] = ce
                continue
            # Merge with existing.
            if entry.status == "fetched" and resource is not None:
                ce.status = "fetched"
                ce.winning_resource = resource
                ce.winning_run_dir = round_corpus.run_dir
                ce.final_url = entry.final_url or ce.final_url
                ce.page_class = entry.page_class or ce.page_class
                ce.kind = entry.kind or ce.kind
            elif ce.status != "fetched":
                # Latest non-fetched status wins only if no round has fetched.
                ce.status = str(entry.status)
                ce.skip_reason = entry.skip_reason
            for src in entry.discovered_from:
                if src not in ce.discovered_from:
                    ce.discovered_from.append(src)
            ce.depth = min(ce.depth, int(entry.depth))
            if entry.in_scope:
                ce.in_scope = True
    return canonical


def _load_existing_canonical_id_map(canonical_dir: Path) -> dict[str, int]:
    """Return url → numeric canonical id from an existing canonical dir.

    Reads `<canonical_dir>/url_id_map.json`, an explicit sidecar that
    tracks EVERY URL's canonical position (including skipped ones).
    Falls back to scanning crawl_index.json's resource_id values if the
    sidecar isn't present (covers canonicals built by an older promote).
    """
    map_path = canonical_dir / "url_id_map.json"
    if map_path.exists():
        try:
            data = json.loads(map_path.read_text())
        except json.JSONDecodeError:
            data = {}
        if isinstance(data, dict):
            return {str(k): int(v) for k, v in data.items() if isinstance(v, int)}

    # Fallback: scrape crawl_index entries that DO have resource_ids
    # (won't include skipped URLs but is best we can do for legacy).
    crawl_index_path = canonical_dir / "crawl_index.json"
    if not crawl_index_path.exists():
        return {}
    try:
        data = json.loads(crawl_index_path.read_text())
    except json.JSONDecodeError:
        return {}
    out: dict[str, int] = {}
    for entry in data.get("entries", []):
        url = entry.get("url")
        rid = entry.get("resource_id")
        if not url or not rid:
            continue
        try:
            num = int(rid.split("-", 1)[1])
        except (IndexError, ValueError):
            continue
        out[url] = num
    return out


def _assign_canonical_ids(
    canonical_urls: dict[str, _CanonicalEntry],
    existing_id_map: dict[str, int],
) -> dict[str, int]:
    """Assign canonical numeric IDs. Existing URL→ID mappings are
    preserved; new URLs get the next available number, sorted by
    `(depth, url)` for deterministic ordering."""
    url_to_id: dict[str, int] = {}
    used: set[int] = set()
    # First, preserve existing IDs for URLs still present in canonical.
    for url, idx in existing_id_map.items():
        if url in canonical_urls:
            url_to_id[url] = idx
            used.add(idx)
    # Then assign new URLs, sorted by (depth, url).
    new_urls = sorted(
        (u for u in canonical_urls if u not in url_to_id),
        key=lambda u: (canonical_urls[u].depth, u),
    )
    next_idx = (max(used) + 1) if used else 1
    for url in new_urls:
        while next_idx in used:
            next_idx += 1
        url_to_id[url] = next_idx
        used.add(next_idx)
        next_idx += 1
    return url_to_id


def _copy_artifacts(
    *,
    resource: MirrorResource,
    new_index: int,
    new_resource_id: str,
    canonical_tmp: Path,
    canonical_final: Path,
) -> MirrorResource:
    """Copy a winning run's artifact files into the canonical .tmp dir
    with renumbered prefixes. Returns a new MirrorResource whose paths
    point at the FINAL canonical location (where the .tmp dir will be
    swapped to), not the .tmp path itself — so manifest paths remain
    valid after the post-copy rename."""
    new_paths: dict[str, str | None] = {
        "html_path": None,
        "json_path": None,
        "markdown_path": None,
        "text_path": None,
        "document_path": None,
    }
    for kind_attr, subdir, _ext in (
        ("text_path", "text", ".txt"),
        ("markdown_path", "markdown", ".md"),
        ("html_path", "raw", ".html"),
        ("json_path", "json", ".json"),
        ("document_path", "documents", None),
    ):
        src_str = getattr(resource, kind_attr)
        if not src_str:
            continue
        src = Path(src_str)
        if not src.exists():
            continue
        # Slug suffix: drop the original NNNN- prefix, keep the rest.
        original_name = src.name
        if "-" in original_name:
            _, suffix = original_name.split("-", 1)
        else:
            suffix = original_name
        new_name = f"{new_index:04d}-{suffix}"
        # Write to .tmp — but record the path as it WILL be after the
        # tmp→final rename.
        shutil.copy2(src, canonical_tmp / subdir / new_name)
        new_paths[kind_attr] = str(canonical_final / subdir / new_name)
    return MirrorResource(
        resource_id=new_resource_id,
        url=resource.url,
        final_url=resource.final_url,
        kind=resource.kind,
        status=resource.status,
        content_type=resource.content_type,
        response_status_code=resource.response_status_code,
        html_path=new_paths["html_path"],
        json_path=new_paths["json_path"],
        markdown_path=new_paths["markdown_path"],
        text_path=new_paths["text_path"],
        document_path=new_paths["document_path"],
        sha256=resource.sha256,
        text_chars=resource.text_chars,
        browserless_strategy=resource.browserless_strategy,
        browserless_attempted=list(resource.browserless_attempted),
        attempts=list(resource.attempts),
        error_message=resource.error_message,
    )


def _pick_target_and_policy(
    rounds: list[_RoundCorpus],
) -> tuple[MirrorTarget, MirrorPolicy]:
    """Take the most-recent round's target metadata and policy as the
    canonical record. They tend to be stable, so even an older round's
    values would usually do — but using the latest is the least
    surprising default."""
    latest = rounds[-1].corpus
    return latest.target, latest.policy


def _union_crawl_links(rounds: list[_RoundCorpus]) -> list[CrawlLink]:
    seen: set[tuple[str, str, bool]] = set()
    out: list[CrawlLink] = []
    for r in rounds:
        for link in r.corpus.crawl_links:
            key = (link.source_url, link.target_url, link.target_in_scope)
            if key in seen:
                continue
            seen.add(key)
            out.append(link)
    return out


def _synthesise_quality_report(
    crawl_index: list[CrawlIndexEntry], resources: list[MirrorResource]
) -> QualityReport:
    fetched_html = sum(
        1 for r in resources if r.kind == "html" and r.status == "fetched"
    )
    fetched_documents = sum(
        1 for r in resources if r.kind == "document" and r.status == "fetched"
    )
    failed_pages = sum(
        1 for e in crawl_index if e.status == "failed"
    )
    skipped_urls = sum(
        1 for e in crawl_index if e.status not in ("fetched", "queued")
    ) - failed_pages
    skipped_urls = max(skipped_urls, 0)
    total_text_chars = sum(r.text_chars or 0 for r in resources)
    if fetched_html == 0:
        status = "failed"
        reasons = ["no usable HTML pages were fetched"]
    elif failed_pages or skipped_urls > 0:
        status = "partial"
        reasons: list[str] = []
        if failed_pages:
            reasons.append(f"{failed_pages} HTML page(s) failed")
    else:
        status = "complete"
        reasons = []
    return QualityReport(
        status=status,  # type: ignore[arg-type]
        fetched_pages=fetched_html,
        failed_pages=failed_pages,
        fetched_documents=fetched_documents,
        discovered_urls=len(crawl_index),
        skipped_urls=skipped_urls,
        total_text_chars=total_text_chars,
        reasons=reasons,
    )


def _append_promote_log(
    canonical_dir: Path,
    *,
    source_run_ids: list[str],
    new_url_count: int,
    fetched_count: int,
    total_url_count: int,
) -> None:
    log_path = canonical_dir / "promote_log.json"
    if log_path.exists():
        try:
            log = json.loads(log_path.read_text())
        except json.JSONDecodeError:
            log = {"entries": []}
    else:
        log = {"entries": []}
    log.setdefault("entries", []).append(
        {
            "ts": utc_now_iso(),
            "source_run_ids": source_run_ids,
            "new_url_count": new_url_count,
            "fetched_count": fetched_count,
            "total_url_count": total_url_count,
        }
    )
    log_path.write_text(json.dumps(log, indent=2))
