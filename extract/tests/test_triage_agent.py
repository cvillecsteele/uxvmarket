from __future__ import annotations

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
from uxv_extract.schema import ProductPriorityList, ProductPrioritySubmission
from uxv_extract.triage_agent import (
    SUBMIT_TOOL_FQN,
    build_user_prompt,
    run_triage,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "corpus_min"


def _good_submission(n: int = 5) -> dict[str, Any]:
    return {
        "products": [
            {
                "name": f"Product {i}",
                "relevance_score": max(1, 10 - i),  # decreasing, floor at 1
                "rationale": f"Product {i} rationale.",
            }
            for i in range(n)
        ],
        "notes": None,
    }


def _result_message(num_turns: int = 5, cost: float = 0.15) -> ResultMessage:
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


def _assistant_with_submit(payload: dict[str, Any]) -> AssistantMessage:
    return AssistantMessage(
        content=[ToolUseBlock(id="t", name=SUBMIT_TOOL_FQN, input=payload)],
        model="claude-sonnet-4-6",
    )


def _make_query_fn(messages: list[Any]):
    async def fake(*, prompt, options, transport=None):
        for m in messages:
            yield m
    return fake


@pytest.mark.asyncio
async def test_triage_truncates_to_max_products() -> None:
    """Agent submits 25 candidates; runner truncates at max_products=15."""
    reader = CorpusReader.load(FIXTURE_ROOT)
    fake = _make_query_fn(
        [
            SystemMessage(subtype="init", data={}),
            _assistant_with_submit(_good_submission(n=25)),
            _result_message(num_turns=8),
        ]
    )
    result = await run_triage(
        reader,
        model="claude-sonnet-4-6",
        max_turns=10,
        max_products=15,
        query_fn=fake,
    )
    assert isinstance(result, ProductPriorityList)
    assert len(result.products) == 15
    assert result.agent_listed == 25
    assert result.max_products == 15
    # Ordering preserved (most relevant first):
    assert result.products[0].name == "Product 0"
    assert result.products[14].name == "Product 14"


@pytest.mark.asyncio
async def test_triage_keeps_full_list_when_max_products_zero() -> None:
    """max_products=0 disables truncation."""
    reader = CorpusReader.load(FIXTURE_ROOT)
    fake = _make_query_fn(
        [
            _assistant_with_submit(_good_submission(n=8)),
            _result_message(),
        ]
    )
    result = await run_triage(
        reader,
        model="claude-sonnet-4-6",
        max_turns=10,
        max_products=0,
        query_fn=fake,
    )
    assert len(result.products) == 8
    assert result.agent_listed == 8


@pytest.mark.asyncio
async def test_triage_accepts_empty_list_for_off_topic_vendor() -> None:
    """sf-motorsporttechnik-style: agent submits an empty product list."""
    reader = CorpusReader.load(FIXTURE_ROOT)
    fake = _make_query_fn(
        [
            _assistant_with_submit({"products": [], "notes": "no UXV products"}),
            _result_message(),
        ]
    )
    result = await run_triage(
        reader,
        model="claude-sonnet-4-6",
        max_turns=10,
        max_products=15,
        query_fn=fake,
    )
    assert result.products == []
    assert result.agent_listed == 0


@pytest.mark.asyncio
async def test_triage_raises_when_agent_never_submits() -> None:
    reader = CorpusReader.load(FIXTURE_ROOT)
    fake = _make_query_fn(
        [
            AssistantMessage(content=[], model="claude-sonnet-4-6"),
            _result_message(),
        ]
    )
    with pytest.raises(RuntimeError, match="submit_product_priority"):
        await run_triage(
            reader,
            model="claude-sonnet-4-6",
            max_turns=10,
            query_fn=fake,
        )


def test_triage_user_prompt_includes_max_products() -> None:
    reader = CorpusReader.load(FIXTURE_ROOT)
    prompt = build_user_prompt(reader, max_products=12)
    assert "max_products = 12" in prompt
    assert "test-vendor" in prompt


@pytest.mark.asyncio
async def test_triage_returns_partial_when_cap_fires_after_submission() -> None:
    """Submit-early-and-refine: agent submits, then timeout fires.
    Runner returns the submission with status='partial'."""
    import asyncio
    reader = CorpusReader.load(FIXTURE_ROOT)

    async def fake(*, prompt, options, transport=None):
        yield _assistant_with_submit(_good_submission(n=3))
        await asyncio.sleep(2)

    result = await run_triage(
        reader,
        model="claude-sonnet-4-6",
        max_turns=10,
        max_products=15,
        timeout_sec=0.1,
        query_fn=fake,
    )
    assert result.status == "partial"
    assert len(result.products) == 3


@pytest.mark.asyncio
async def test_triage_uses_latest_submission_when_agent_resubmits() -> None:
    """Agent submits twice; runner uses the latest."""
    reader = CorpusReader.load(FIXTURE_ROOT)
    first = _good_submission(n=2)
    second = _good_submission(n=5)
    fake = _make_query_fn(
        [
            _assistant_with_submit(first),
            _assistant_with_submit(second),
            _result_message(),
        ]
    )
    result = await run_triage(
        reader,
        model="claude-sonnet-4-6",
        max_turns=10,
        max_products=15,
        query_fn=fake,
    )
    assert len(result.products) == 5
