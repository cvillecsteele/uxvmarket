from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urldefrag, urlparse

from uxv_mirroring.contracts import MirrorCorpus, MirrorPolicy, MirrorTarget, TargetCoverageEntry, TargetRegistry, utc_now_iso
from uxv_mirroring.materialize import write_json


def registry_path(workspace_root: Path) -> Path:
    return workspace_root / "output" / "target_registry.json"


def normalize_homepage_url(url: str) -> str:
    normalized, _fragment = urldefrag(url)
    parsed = urlparse(normalized)
    scheme = (parsed.scheme or "https").lower()
    host = (parsed.netloc or parsed.path).lower()
    path = parsed.path if parsed.netloc else ""
    if path == "/":
        path = ""
    if path.endswith("/") and path != "/":
        path = path.rstrip("/")
    return f"{scheme}://{host}{path}"


def policy_hash(policy: MirrorPolicy) -> str:
    payload = json.dumps(policy.model_dump(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_registry(workspace_root: Path) -> TargetRegistry:
    path = registry_path(workspace_root)
    if not path.exists():
        return TargetRegistry()
    return TargetRegistry.model_validate_json(path.read_text(encoding="utf-8"))


def save_registry(workspace_root: Path, registry: TargetRegistry) -> Path:
    path = registry_path(workspace_root)
    tmp_path = path.with_suffix(".json.tmp")
    write_json(tmp_path, registry.model_dump())
    os.replace(tmp_path, path)
    return path


def _entry_age_days(entry: TargetCoverageEntry) -> float | None:
    try:
        updated = datetime.fromisoformat(entry.updated_at)
    except ValueError:
        return None
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - updated).total_seconds() / 86_400


def corpus_manifest_is_valid(path: str) -> bool:
    source = Path(path)
    if not source.exists():
        return False
    try:
        MirrorCorpus.model_validate_json(source.read_text(encoding="utf-8"))
    except Exception:
        return False
    return True


def find_covered_entry(
    registry: TargetRegistry,
    *,
    target: MirrorTarget,
    policy: MirrorPolicy,
    max_age_days: int | None = None,
) -> TargetCoverageEntry | None:
    normalized = normalize_homepage_url(target.homepage_url)
    digest = policy_hash(policy)
    candidates = [
        entry
        for entry in registry.entries
        if entry.normalized_homepage_url == normalized
        and entry.profile == policy.profile
        and entry.policy_hash == digest
        and entry.quality_status == "complete"
    ]
    candidates.sort(key=lambda entry: entry.updated_at, reverse=True)
    for entry in candidates:
        if max_age_days is not None:
            age = _entry_age_days(entry)
            if age is None or age > max_age_days:
                continue
        if not corpus_manifest_is_valid(entry.corpus_manifest_path):
            continue
        return entry
    return None


def update_registry_for_corpus(workspace_root: Path, corpus: MirrorCorpus) -> TargetCoverageEntry | None:
    if corpus.quality_report.status != "complete":
        return None
    registry = load_registry(workspace_root)
    normalized = normalize_homepage_url(corpus.target.homepage_url)
    digest = policy_hash(corpus.policy)
    now = utc_now_iso()
    entry = TargetCoverageEntry(
        target_id=corpus.target.target_id,
        display_name=corpus.target.display_name,
        homepage_url=corpus.target.homepage_url,
        normalized_homepage_url=normalized,
        profile=corpus.policy.profile,
        policy_hash=digest,
        quality_status=corpus.quality_report.status,
        run_id=corpus.run_id,
        corpus_manifest_path=corpus.manifest_path,
        quality_report_path=corpus.quality_report_path,
        resource_count=len(corpus.resources),
        updated_at=now,
    )
    registry.entries = [
        existing
        for existing in registry.entries
        if not (
            existing.normalized_homepage_url == normalized
            and existing.profile == corpus.policy.profile
            and existing.policy_hash == digest
        )
    ]
    registry.entries.append(entry)
    save_registry(workspace_root, registry)
    return entry


def registry_summary(registry: TargetRegistry) -> dict[str, object]:
    by_status: dict[str, int] = {}
    by_profile: dict[str, int] = {}
    for entry in registry.entries:
        by_status[entry.quality_status] = by_status.get(entry.quality_status, 0) + 1
        by_profile[entry.profile] = by_profile.get(entry.profile, 0) + 1
    return {
        "schema_version": registry.schema_version,
        "entry_count": len(registry.entries),
        "by_status": by_status,
        "by_profile": by_profile,
        "entries": [entry.model_dump() for entry in sorted(registry.entries, key=lambda item: item.updated_at, reverse=True)],
    }

