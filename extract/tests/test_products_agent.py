from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    ToolUseBlock,
)

from uxv_extract.corpus import CorpusReader
from uxv_extract.products_agent import (
    ADD_PRODUCT_TOOL_FQN,
    FINALIZE_TOOL_FQN,
    _CatalogAccumulator,
    _build_incremental_tools,
    build_user_prompt,
    hydrate_catalog_submission,
    run_product_extraction,
)
from uxv_extract.schema import (
    Citation,
    ProductCatalogSubmission,
    ProductDetail,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "corpus_min"


def _good_product_dict(name: str = "Test Drone X1") -> dict[str, Any]:
    return {
        "name": name,
        "category": "airframes",
        "descriptor": "small fixed-wing test drone",
        "granularity": "sku",
        "readiness": "production",
        "ndaa": "unknown",
        "blue_uas": "unknown",
        "evidence": [
            {
                "source_kind": "mirror",
                "resource_id": "resource-0001",
                "line_start": 1,
                "line_end": 2,
            }
        ],
        "notes": None,
    }


def _result_message(num_turns: int = 5, cost: float = 0.4) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1000,
        duration_api_ms=900,
        is_error=False,
        num_turns=num_turns,
        session_id="t",
        stop_reason="end_turn",
        total_cost_usd=cost,
    )


def _add_product_msg(payload: dict[str, Any]) -> AssistantMessage:
    return AssistantMessage(
        content=[ToolUseBlock(id="t", name=ADD_PRODUCT_TOOL_FQN, input=payload)],
        model="claude-sonnet-4-6",
    )


def _finalize_msg(
    *,
    status: str = "complete",
    unresolved_questions: list[str] | None = None,
    fetch_requests: list[dict[str, Any]] | None = None,
) -> AssistantMessage:
    payload: dict[str, Any] = {"status": status}
    if unresolved_questions is not None:
        payload["unresolved_questions"] = unresolved_questions
    if fetch_requests is not None:
        payload["fetch_requests"] = fetch_requests
    return AssistantMessage(
        content=[ToolUseBlock(id="f", name=FINALIZE_TOOL_FQN, input=payload)],
        model="claude-sonnet-4-6",
    )


def _make_query_fn(messages: list[Any]):
    """Fake `query()` that yields the given messages.

    Tests pre-populate the runner's accumulator via the `_accumulator`
    test hook on `run_product_extraction`, so this fake doesn't need
    to dispatch tools — the SDK MCP dispatch path is covered by the
    direct `_build_incremental_tools` tests above.
    """
    async def fake(*, prompt, options, transport=None):
        for m in messages:
            yield m
    return fake


# --- Direct accumulator / tool tests --------------------------------------


@pytest.mark.asyncio
async def test_add_product_appends_to_accumulator() -> None:
    reader = CorpusReader.load(FIXTURE_ROOT)
    acc = _CatalogAccumulator()
    add_tool, _ = _build_incremental_tools(reader, acc)

    result = await add_tool.handler(_good_product_dict())
    assert "is_error" not in result or result.get("is_error") is not True
    assert len(acc.products) == 1
    assert acc.products[0].name == "Test Drone X1"
    # Hydration filled snippet/url/page_class:
    assert "Test Vendor" in acc.products[0].evidence[0].snippet
    assert acc.products[0].evidence[0].url == "https://test.example/"


@pytest.mark.asyncio
async def test_add_product_rejects_invalid_without_breaking_run() -> None:
    """Validation error returns is_error; accumulator unchanged; a
    subsequent valid call still succeeds."""
    reader = CorpusReader.load(FIXTURE_ROOT)
    acc = _CatalogAccumulator()
    add_tool, _ = _build_incremental_tools(reader, acc)

    bad = _good_product_dict()
    bad["category"] = "not_a_real_category"
    result = await add_tool.handler(bad)
    assert result.get("is_error") is True
    assert len(acc.products) == 0

    # Now a valid product should still go in:
    result2 = await add_tool.handler(_good_product_dict("Real Product"))
    assert result2.get("is_error") is not True
    assert len(acc.products) == 1
    assert acc.products[0].name == "Real Product"


@pytest.mark.asyncio
async def test_finalize_records_status_and_marks_finalized() -> None:
    reader = CorpusReader.load(FIXTURE_ROOT)
    acc = _CatalogAccumulator()
    _, fin_tool = _build_incremental_tools(reader, acc)

    result = await fin_tool.handler({
        "status": "complete",
        "unresolved_questions": ["one open question"],
        "fetch_requests": [],
    })
    assert result.get("is_error") is not True
    assert acc.finalized is True
    assert acc.final_status == "complete"
    assert acc.unresolved_questions == ["one open question"]


# --- Runner-level tests ----------------------------------------------------


async def _populate(acc: _CatalogAccumulator, reader: CorpusReader,
                    *, names: list[str]) -> None:
    """Push each name through the real add_product handler so
    accumulator state matches what a live session would produce."""
    add_tool, _ = _build_incremental_tools(reader, acc)
    for name in names:
        await add_tool.handler(_good_product_dict(name))


@pytest.mark.asyncio
async def test_runner_returns_complete_catalog_on_finalize() -> None:
    reader = CorpusReader.load(FIXTURE_ROOT)
    acc = _CatalogAccumulator()
    await _populate(acc, reader, names=["X1", "X2"])
    _, fin_tool = _build_incremental_tools(reader, acc)
    await fin_tool.handler({"status": "complete"})

    fake = _make_query_fn(
        [
            SystemMessage(subtype="init", data={}),
            _result_message(num_turns=7, cost=0.5),
        ]
    )

    catalog = await run_product_extraction(
        reader,
        model="claude-sonnet-4-6",
        max_turns=10,
        query_fn=fake,
        _accumulator=acc,
    )

    assert catalog.status == "complete"
    assert len(catalog.products) == 2
    assert [p.name for p in catalog.products] == ["X1", "X2"]
    assert catalog.meta.num_turns == 7
    assert catalog.meta.total_cost_usd == 0.5


@pytest.mark.asyncio
async def test_runner_returns_partial_when_no_finalize() -> None:
    """Two products submitted, run ends without finalize: catalog has
    both products, status='partial'."""
    reader = CorpusReader.load(FIXTURE_ROOT)
    acc = _CatalogAccumulator()
    await _populate(acc, reader, names=["X1", "X2"])

    fake = _make_query_fn(
        [
            _result_message(num_turns=4),
        ]
    )

    catalog = await run_product_extraction(
        reader,
        model="claude-sonnet-4-6",
        max_turns=10,
        query_fn=fake,
        _accumulator=acc,
    )
    assert catalog.status == "partial"
    assert len(catalog.products) == 2


@pytest.mark.asyncio
async def test_runner_returns_partial_when_cap_fires_mid_session() -> None:
    """Two products submitted, SDK times out before agent finalizes.
    Runner returns partial catalog with those two products and
    status='partial' — never raises."""
    reader = CorpusReader.load(FIXTURE_ROOT)
    acc = _CatalogAccumulator()
    await _populate(acc, reader, names=["X1", "X2"])

    async def fake(*, prompt, options, transport=None):
        # Yield nothing, then time out — simulating wait_for firing.
        if False:
            yield  # make it a generator
        raise asyncio.TimeoutError()

    catalog = await run_product_extraction(
        reader,
        model="claude-sonnet-4-6",
        max_turns=10,
        timeout_sec=5,
        query_fn=fake,
        _accumulator=acc,
    )

    assert catalog.status == "partial"
    assert len(catalog.products) == 2


@pytest.mark.asyncio
async def test_runner_partial_when_cap_fires_with_finalize_already_done() -> None:
    """Even if the agent finalized with status='complete' before the
    cap, if a cap STILL fires (SDK kills mid-stream after finalize for
    some reason), runner forces status='partial'. This is conservative:
    we only believe 'complete' if the run reached natural end."""
    reader = CorpusReader.load(FIXTURE_ROOT)
    acc = _CatalogAccumulator()
    await _populate(acc, reader, names=["X1"])
    _, fin_tool = _build_incremental_tools(reader, acc)
    await fin_tool.handler({"status": "complete"})
    assert acc.finalized is True

    async def fake(*, prompt, options, transport=None):
        if False:
            yield
        raise asyncio.TimeoutError()

    catalog = await run_product_extraction(
        reader,
        model="claude-sonnet-4-6",
        max_turns=10,
        timeout_sec=5,
        query_fn=fake,
        _accumulator=acc,
    )
    assert catalog.status == "partial"
    assert len(catalog.products) == 1


@pytest.mark.asyncio
async def test_runner_raises_only_when_nothing_submitted_and_no_cap() -> None:
    """If the agent never called add_product or finalize AND the run
    completed normally (no cap), raise. This is a prompt-engineering
    bug surface, not normal cap behavior."""
    reader = CorpusReader.load(FIXTURE_ROOT)
    fake = _make_query_fn(
        [
            AssistantMessage(content=[], model="claude-sonnet-4-6"),
            _result_message(),
        ]
    )
    with pytest.raises(RuntimeError, match="add_product"):
        await run_product_extraction(
            reader,
            model="claude-sonnet-4-6",
            max_turns=10,
            query_fn=fake,
        )


# --- Prompt tests (unchanged from prior incremental behavior) -------------


def test_user_prompt_includes_priority_names_when_provided() -> None:
    reader = CorpusReader.load(FIXTURE_ROOT)
    prompt = build_user_prompt(
        reader,
        priority_names=["Carrier H6 Hybrid", "C-NAVIGATOR", "C-SRoC"],
    )
    assert "PRIORITIZED PRODUCTS" in prompt
    assert "Carrier H6 Hybrid" in prompt
    assert "C-NAVIGATOR" in prompt
    assert "C-SRoC" in prompt
    assert "Do not add products outside this list" in prompt


def test_user_prompt_falls_back_to_enumerate_when_no_priority() -> None:
    reader = CorpusReader.load(FIXTURE_ROOT)
    prompt = build_user_prompt(reader, priority_names=None)
    assert "PRIORITIZED PRODUCTS" not in prompt
    assert "Enumerate every named product" in prompt
    assert "add_product" in prompt


def test_user_prompt_includes_target_metadata() -> None:
    reader = CorpusReader.load(FIXTURE_ROOT)
    prompt = build_user_prompt(reader)
    assert "test-vendor" in prompt
    assert "Test Vendor" in prompt
    assert "https://test.example" in prompt


def test_user_prompt_omits_vendor_context_when_no_profile(tmp_path: Path) -> None:
    reader = CorpusReader.load(FIXTURE_ROOT)
    prompt = build_user_prompt(reader, profile_path=tmp_path / "missing.json")
    assert "VENDOR-PASS CONTEXT" not in prompt


def test_user_prompt_includes_vendor_context_when_profile_present(tmp_path: Path) -> None:
    reader = CorpusReader.load(FIXTURE_ROOT)
    profile_payload = {
        "target_id": "test-vendor",
        "run_id": "test-run",
        "display_name": "Test Vendor",
        "homepage_url": "https://test.example",
        "corpus_root": "/x",
        "products_categories": {
            "items": [
                {
                    "category": "airframes",
                    "is_primary": True,
                    "confidence": "high",
                    "evidence": [
                        {
                            "source_kind": "mirror",
                            "resource_id": "resource-0001",
                            "line_start": 1,
                            "line_end": 2,
                            "snippet": "x x x x x x x x x x",
                        }
                    ],
                    "notes": None,
                }
            ],
            "confidence": "high",
            "status": "answered",
            "notes": None,
        },
        "headquarters": {
            "value": None, "confidence": "low", "status": "unknown",
            "evidence": [], "notes": None,
        },
        "drone_supply_chain_role": {
            "value": "oem", "confidence": "high", "status": "answered",
            "evidence": [
                {
                    "source_kind": "mirror",
                    "resource_id": "resource-0001",
                    "line_start": 1,
                    "line_end": 2,
                    "snippet": "x x x x x x x x x x",
                }
            ],
            "notes": None,
        },
        "products": {
            "items": [], "confidence": "low", "status": "unknown", "notes": None,
        },
        "ndaa": {
            "value": None, "confidence": "low", "status": "unknown",
            "evidence": [], "notes": None,
        },
        "blue_uas": {
            "value": None, "confidence": "low", "status": "unknown",
            "evidence": [], "notes": None,
        },
        "readiness": {
            "value": None, "confidence": "low", "status": "unknown",
            "evidence": [], "notes": None,
        },
        "unresolved_questions": [],
        "fetch_requests": [],
        "status": "complete",
        "meta": {
            "model": "claude-sonnet-4-6",
            "num_turns": 1,
            "total_cost_usd": 0.0,
            "created_at": "2026-05-01T00:00:00+00:00",
            "extract_version": "0.1.0",
        },
    }
    p = tmp_path / "profile.json"
    p.write_text(json.dumps(profile_payload))
    prompt = build_user_prompt(reader, profile_path=p)
    assert "VENDOR-PASS CONTEXT" in prompt
    assert "role: oem" in prompt
    assert "primary category: airframes" in prompt


def test_hydrate_rejects_oversized_extraction(tmp_path: Path) -> None:
    """Oversized line range → CitationHydrationError, same as vendor pass."""
    from uxv_extract.agent import CitationHydrationError

    target = tmp_path / "corpus"
    shutil.copytree(FIXTURE_ROOT, target)
    fat_line = " ".join(["lorem"] * 80) + "\nsecond line\n"
    (target / "text" / "0001-home.txt").write_text(fat_line)
    reader = CorpusReader.load(target)

    sub = ProductCatalogSubmission(
        products=[
            ProductDetail(
                name="X",
                category="airframes",
                descriptor="test",
                granularity="sku",
                readiness="unknown",
                ndaa="unknown",
                blue_uas="unknown",
                evidence=[
                    Citation(
                        source_kind="mirror",
                        resource_id="resource-0001",
                        line_start=1,
                        line_end=1,
                    )
                ],
            )
        ],
        unresolved_questions=[],
        fetch_requests=[],
        status="complete",
    )
    with pytest.raises(CitationHydrationError, match="cap is 60"):
        hydrate_catalog_submission(sub, reader)


def test_single_line_overcap_message_tells_agent_not_to_resubmit(tmp_path: Path) -> None:
    """Single-line citation >60 words — error must instruct picking a
    DIFFERENT line, not say 'tighter range' (impossible at one line)."""
    from uxv_extract.agent import CitationHydrationError

    target = tmp_path / "corpus"
    shutil.copytree(FIXTURE_ROOT, target)
    fat = " ".join(["lorem"] * 80) + "\n"
    (target / "text" / "0001-home.txt").write_text(fat)
    reader = CorpusReader.load(target)

    sub = ProductCatalogSubmission(
        products=[
            ProductDetail(
                name="X", category="airframes", descriptor="t",
                granularity="sku", readiness="unknown",
                ndaa="unknown", blue_uas="unknown",
                evidence=[Citation(
                    source_kind="mirror",
                    resource_id="resource-0001",
                    line_start=1, line_end=1,
                )],
            )
        ],
        unresolved_questions=[], fetch_requests=[], status="complete",
    )
    with pytest.raises(CitationHydrationError) as exc_info:
        hydrate_catalog_submission(sub, reader)
    msg = str(exc_info.value)
    assert "single line" in msg
    assert "DIFFERENT line" in msg
    assert "DO NOT submit the same line again" in msg
    assert "pick a tighter range" not in msg.lower()
