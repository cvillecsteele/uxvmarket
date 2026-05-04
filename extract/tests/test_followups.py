from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from uxv_extract.followups import (
    Followup,
    aggregate_run_followups,
    write_followups_jsonl,
)
from uxv_extract.schema import (
    Answer,
    Citation,
    FetchRequest,
    ListAnswer,
    ProductCatalog,
    ProductDetail,
    Profile,
    ProfileMeta,
)


def _catalog_with_followups(target_id: str, *, urls: list[str]) -> ProductCatalog:
    placeholder_cite = Citation(
        source_kind="mirror", resource_id="resource-0001",
        line_start=1, line_end=2, snippet="x x x x x x x x x x",
    )
    return ProductCatalog(
        target_id=target_id,
        run_id="r1",
        display_name=target_id.replace("-", " ").title(),
        homepage_url=f"https://{target_id}.example",
        corpus_root="/abs/path",
        products=[
            ProductDetail(
                name="Some product",
                category="airframes",
                descriptor="x",
                granularity="sku",
                readiness="production",
                ndaa="unknown",
                blue_uas="unknown",
                evidence=[placeholder_cite],
            )
        ],
        fetch_requests=[
            FetchRequest(
                url=u, reason="r", expected_evidence=["products"],
                in_corpus_index=False,
            ) for u in urls
        ],
        status="partial",
        meta=ProfileMeta(
            model="claude-sonnet-4-6", num_turns=4, total_cost_usd=0.5,
            created_at="2026-05-01T00:00:00+00:00", extract_version="0.1.0",
        ),
    )


def _unknown_answer() -> Answer:
    return Answer(value=None, confidence="low", status="unknown", evidence=[], notes=None)


def _unknown_list_answer() -> ListAnswer:
    return ListAnswer(items=[], confidence="low", status="unknown", notes=None)


def _profile(
    *,
    target_id: str,
    display_name: str,
    homepage_url: str,
    fetch_requests: list[FetchRequest],
    status: str = "partial",
) -> Profile:
    return Profile(
        target_id=target_id,
        run_id="r1",
        display_name=display_name,
        homepage_url=homepage_url,
        corpus_root="/abs/path",
        products_categories=_unknown_list_answer(),
        headquarters=_unknown_answer(),
        drone_supply_chain_role=Answer(
            value="oem",
            confidence="high",
            status="answered",
            evidence=[
                Citation(
                    source_kind="mirror",
                    resource_id="resource-0001",
                    line_start=1,
                    line_end=2,
                    url=homepage_url,
                    page_class="homepage",
                    snippet=(
                        "We design and manufacture heavy-lift drones in "
                        "Florida for federal customers and first responders."
                    ),
                )
            ],
        ),
        products=_unknown_list_answer(),
        ndaa=_unknown_answer(),
        blue_uas=_unknown_answer(),
        readiness=_unknown_answer(),
        unresolved_questions=[],
        fetch_requests=fetch_requests,
        status=status,
        meta=ProfileMeta(
            model="claude-sonnet-4-6",
            num_turns=4,
            total_cost_usd=0.1,
            created_at="2026-05-01T00:00:00+00:00",
            extract_version="0.1.0",
        ),
    )


def _fr(url: str, *, fields: list[str] | None = None, in_corpus_index: bool = True) -> FetchRequest:
    return FetchRequest(
        url=url,
        reason=f"need {url}",
        expected_evidence=fields or ["drone_supply_chain_role"],
        source_hint="crawl_index status=skipped_class_budget",
        in_corpus_index=in_corpus_index,
    )


def _write_profile(run_root: Path, target_id: str, profile: Profile) -> None:
    target_dir = run_root / target_id
    target_dir.mkdir(parents=True)
    (target_dir / "profile.json").write_text(profile.model_dump_json())


def test_aggregate_empty_run_yields_nothing(tmp_path: Path) -> None:
    out = aggregate_run_followups(tmp_path / "empty")
    assert out == []


def test_aggregate_skips_targets_with_no_fetch_requests(tmp_path: Path) -> None:
    run_root = tmp_path / "run-A"
    _write_profile(
        run_root,
        "vendor-with-fetches",
        _profile(
            target_id="vendor-with-fetches",
            display_name="Vendor With",
            homepage_url="https://a.example",
            fetch_requests=[_fr("https://a.example/products")],
        ),
    )
    _write_profile(
        run_root,
        "vendor-empty",
        _profile(
            target_id="vendor-empty",
            display_name="Vendor Empty",
            homepage_url="https://b.example",
            fetch_requests=[],
            status="complete",
        ),
    )

    out = aggregate_run_followups(run_root)
    assert [f.target_id for f in out] == ["vendor-with-fetches"]


def test_aggregate_dedupes_urls_within_a_target(tmp_path: Path) -> None:
    run_root = tmp_path / "run-A"
    _write_profile(
        run_root,
        "vendor",
        _profile(
            target_id="vendor",
            display_name="Vendor",
            homepage_url="https://v.example",
            fetch_requests=[
                _fr("https://v.example/products", fields=["products"]),
                _fr("https://v.example/products", fields=["products_categories"]),
                _fr("https://v.example/about", fields=["headquarters"]),
            ],
        ),
    )
    out = aggregate_run_followups(run_root)
    assert len(out) == 1
    urls = [fu.url for fu in out[0].follow_ups]
    assert urls == ["https://v.example/products", "https://v.example/about"]


def test_aggregate_dedupe_merges_expected_evidence(tmp_path: Path) -> None:
    """When two FetchRequests share a URL but list different expected_evidence
    fields, the merged entry should keep the union."""
    run_root = tmp_path / "run-A"
    _write_profile(
        run_root,
        "vendor",
        _profile(
            target_id="vendor",
            display_name="Vendor",
            homepage_url="https://v.example",
            fetch_requests=[
                _fr("https://v.example/products", fields=["products"]),
                _fr("https://v.example/products", fields=["products_categories", "ndaa"]),
            ],
        ),
    )
    out = aggregate_run_followups(run_root)
    merged = out[0].follow_ups[0]
    assert merged.url == "https://v.example/products"
    assert set(merged.expected_evidence) == {"products", "products_categories", "ndaa"}


def test_aggregate_preserves_target_metadata(tmp_path: Path) -> None:
    run_root = tmp_path / "run-A"
    _write_profile(
        run_root,
        "harris-aerial",
        _profile(
            target_id="harris-aerial",
            display_name="Harris Aerial",
            homepage_url="https://harrisaerial.com",
            fetch_requests=[_fr("https://harrisaerial.com/payloads/")],
        ),
    )
    out = aggregate_run_followups(run_root)
    assert out[0].target_id == "harris-aerial"
    assert out[0].display_name == "Harris Aerial"
    assert out[0].homepage_url == "https://harrisaerial.com"


def test_aggregate_adds_run_id_note_from_filename(tmp_path: Path) -> None:
    run_root = tmp_path / "be-be2-possible-players-50"
    _write_profile(
        run_root,
        "v",
        _profile(
            target_id="v",
            display_name="V",
            homepage_url="https://v.example",
            fetch_requests=[_fr("https://v.example/x")],
        ),
    )
    out = aggregate_run_followups(run_root)
    assert any("be-be2-possible-players-50" in n for n in out[0].notes)


def test_write_followups_jsonl_round_trip(tmp_path: Path) -> None:
    run_root = tmp_path / "run-A"
    _write_profile(
        run_root,
        "vendor-1",
        _profile(
            target_id="vendor-1",
            display_name="Vendor 1",
            homepage_url="https://v1.example",
            fetch_requests=[_fr("https://v1.example/x"), _fr("https://v1.example/y")],
        ),
    )
    _write_profile(
        run_root,
        "vendor-2",
        _profile(
            target_id="vendor-2",
            display_name="Vendor 2",
            homepage_url="https://v2.example",
            fetch_requests=[_fr("https://v2.example/z")],
        ),
    )

    out_path = tmp_path / "followups.jsonl"
    followups = aggregate_run_followups(run_root)
    write_followups_jsonl(followups, out_path)

    lines = out_path.read_text().strip().splitlines()
    assert len(lines) == 2
    rows = [json.loads(line) for line in lines]
    assert {r["target_id"] for r in rows} == {"vendor-1", "vendor-2"}


def test_written_rows_are_valid_mirror_target_payloads(tmp_path: Path) -> None:
    """The mirror's parse_target_file requires display_name and homepage_url
    and ignores unknown fields. Our follow_ups must be ignorable."""
    run_root = tmp_path / "run-A"
    _write_profile(
        run_root,
        "v",
        _profile(
            target_id="v",
            display_name="V",
            homepage_url="https://v.example",
            fetch_requests=[_fr("https://v.example/x")],
        ),
    )
    out_path = tmp_path / "out.jsonl"
    write_followups_jsonl(aggregate_run_followups(run_root), out_path)

    payload = json.loads(out_path.read_text().strip())
    assert payload["display_name"] and payload["homepage_url"]
    assert isinstance(payload["follow_ups"], list)
    assert payload["follow_ups"][0]["url"] == "https://v.example/x"
    # The shape MirrorTarget reads — required fields present:
    assert "target_id" in payload


def test_aggregate_picks_up_products_json_fetch_requests(tmp_path: Path) -> None:
    """The products pass writes its own fetch_requests to products.json.
    The aggregator must merge those with profile.json's, not silently
    ignore them."""
    run_root = tmp_path / "run-A"
    target_dir = run_root / "vendor"
    target_dir.mkdir(parents=True)
    # profile with one fetch_request:
    (target_dir / "profile.json").write_text(
        _profile(
            target_id="vendor",
            display_name="Vendor",
            homepage_url="https://vendor.example",
            fetch_requests=[_fr("https://vendor.example/from-profile")],
        ).model_dump_json()
    )
    # products with two more fetch_requests:
    (target_dir / "products.json").write_text(
        _catalog_with_followups(
            "vendor",
            urls=["https://vendor.example/from-products-1",
                  "https://vendor.example/from-products-2"],
        ).model_dump_json()
    )

    out = aggregate_run_followups(run_root)
    assert len(out) == 1
    urls = sorted(fu.url for fu in out[0].follow_ups)
    assert urls == [
        "https://vendor.example/from-products-1",
        "https://vendor.example/from-products-2",
        "https://vendor.example/from-profile",
    ]


def test_aggregate_uses_products_json_when_profile_invalid(tmp_path: Path) -> None:
    """If profile.json is unreadable but products.json validates and has
    fetch_requests, still emit a Followup."""
    run_root = tmp_path / "run-A"
    target_dir = run_root / "vendor"
    target_dir.mkdir(parents=True)
    (target_dir / "profile.json").write_text("{ this is not valid json")
    (target_dir / "products.json").write_text(
        _catalog_with_followups(
            "vendor",
            urls=["https://vendor.example/x"],
        ).model_dump_json()
    )

    out = aggregate_run_followups(run_root)
    assert len(out) == 1
    assert out[0].target_id == "vendor"
    assert [fu.url for fu in out[0].follow_ups] == ["https://vendor.example/x"]


def test_summary_counts(tmp_path: Path) -> None:
    """Aggregator returns enough info to print a summary."""
    run_root = tmp_path / "run-A"
    for i in range(3):
        _write_profile(
            run_root,
            f"v{i}",
            _profile(
                target_id=f"v{i}",
                display_name=f"V{i}",
                homepage_url=f"https://v{i}.example",
                fetch_requests=[_fr(f"https://v{i}.example/p{j}") for j in range(i + 1)],
            ),
        )
    out = aggregate_run_followups(run_root)
    assert len(out) == 3
    total_urls = sum(len(f.follow_ups) for f in out)
    assert total_urls == 1 + 2 + 3
