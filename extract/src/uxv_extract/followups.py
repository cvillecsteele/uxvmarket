"""Aggregate `fetch_requests` from a run's profiles into a JSONL file
suitable for the next mirror pass.

Output rows are mirror-target-shaped:

    {
      "target_id": "harris-aerial",
      "display_name": "Harris Aerial",
      "homepage_url": "https://harrisaerial.com",
      "categories": [],
      "notes": ["source:extract:run-id=<id>"],
      "follow_ups": [
        {
          "url": "...",
          "reason": "...",
          "expected_evidence": ["products", "ndaa"],
          "source_hint": "...",
          "in_corpus_index": true
        }
      ]
    }

Mirror's current `parse_target_file` reads only the standard target fields
and ignores `follow_ups`. A future mirror upgrade can consume `follow_ups`
to seed forced re-fetches without breaking older runs.
"""

from __future__ import annotations

import json
import sys
from collections import OrderedDict
from pathlib import Path

from pydantic import Field, ValidationError

from .schema import FetchRequest, ProductCatalog, Profile, StrictModel


class Followup(StrictModel):
    target_id: str
    display_name: str
    homepage_url: str
    categories: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    follow_ups: list[FetchRequest]


def _dedupe_fetch_requests(requests: list[FetchRequest]) -> list[FetchRequest]:
    """Merge duplicate URLs, taking the union of `expected_evidence` and the
    first non-None values for the rest of the fields."""
    by_url: OrderedDict[str, FetchRequest] = OrderedDict()
    for req in requests:
        existing = by_url.get(req.url)
        if existing is None:
            by_url[req.url] = req
            continue
        merged_fields = list(
            dict.fromkeys([*existing.expected_evidence, *req.expected_evidence])
        )
        by_url[req.url] = FetchRequest(
            url=existing.url,
            reason=existing.reason,
            expected_evidence=merged_fields,
            source_hint=existing.source_hint or req.source_hint,
            in_corpus_index=existing.in_corpus_index or req.in_corpus_index,
        )
    return list(by_url.values())


def _try_load(model_cls, path: Path):
    """Returns (instance, None) on success, (None, exception_type_name) on failure."""
    try:
        return model_cls.model_validate_json(path.read_text()), None
    except (ValidationError, json.JSONDecodeError) as exc:
        return None, type(exc).__name__


def aggregate_run_followups(run_root: Path | str) -> list[Followup]:
    """Walk every `<target-id>/{profile.json, products.json}` under
    `run_root`, aggregate fetch_requests from BOTH files per target, and
    emit one `Followup` per target that has at least one request.
    Duplicate URLs across the two files are merged.

    Each target needs metadata (target_id, display_name, homepage_url),
    which is taken from whichever of the two files validates first. If
    BOTH fail validation (e.g. legacy schema), the target is skipped
    with a warning.
    """
    run_root = Path(run_root)
    if not run_root.is_dir():
        return []

    run_id = run_root.name
    out: list[Followup] = []
    for target_dir in sorted(p for p in run_root.iterdir() if p.is_dir()):
        profile_path = target_dir / "profile.json"
        products_path = target_dir / "products.json"

        profile, profile_err = (
            _try_load(Profile, profile_path)
            if profile_path.exists()
            else (None, None)
        )
        catalog, catalog_err = (
            _try_load(ProductCatalog, products_path)
            if products_path.exists()
            else (None, None)
        )

        if profile_err:
            print(
                f"warning: skipping {profile_path}: {profile_err}",
                file=sys.stderr,
            )
        if catalog_err:
            print(
                f"warning: skipping {products_path}: {catalog_err}",
                file=sys.stderr,
            )

        if profile is None and catalog is None:
            continue

        # Pull metadata from whichever side succeeded (prefer profile if both).
        meta_src = profile if profile is not None else catalog
        target_id = meta_src.target_id
        display_name = meta_src.display_name
        homepage_url = meta_src.homepage_url

        all_requests: list[FetchRequest] = []
        if profile is not None:
            all_requests.extend(profile.fetch_requests)
        if catalog is not None:
            all_requests.extend(catalog.fetch_requests)

        if not all_requests:
            continue

        out.append(
            Followup(
                target_id=target_id,
                display_name=display_name,
                homepage_url=homepage_url,
                categories=[],
                notes=[f"source:extract:run-id={run_id}"],
                follow_ups=_dedupe_fetch_requests(all_requests),
            )
        )
    return out


def write_followups_jsonl(followups: list[Followup], out_path: Path | str) -> None:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        for fu in followups:
            f.write(fu.model_dump_json() + "\n")
