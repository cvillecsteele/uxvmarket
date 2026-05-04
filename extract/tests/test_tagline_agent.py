from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from uxv_extract.corpus import CorpusReader
from uxv_extract.schema import (
    Answer,
    Citation,
    ListAnswer,
    Profile,
    ProfileMeta,
)
from uxv_extract.tagline_agent import (
    TAGLINE_MAX_WORDS,
    _truncate_to_words,
    build_prompt,
    generate_tagline,
    write_tagline_into_profile,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "corpus_min"


def _unknown_answer() -> Answer:
    return Answer(value=None, confidence="low", status="unknown", evidence=[])


def _unknown_list_answer() -> ListAnswer:
    return ListAnswer(items=[], confidence="low", status="unknown")


def _profile() -> Profile:
    return Profile(
        target_id="test-vendor",
        run_id="test-run",
        display_name="Test Vendor Inc.",
        homepage_url="https://test.example",
        corpus_root="/abs/path",
        products_categories=_unknown_list_answer(),
        headquarters=_unknown_answer(),
        drone_supply_chain_role=Answer(
            value="oem", confidence="high", status="answered",
            evidence=[Citation(
                source_kind="mirror", resource_id="resource-0001",
                line_start=1, line_end=2,
                url="https://test.example/", page_class="homepage",
                snippet="Test Vendor designs and manufactures heavy-lift drones.",
            )],
        ),
        products=_unknown_list_answer(),
        ndaa=_unknown_answer(),
        blue_uas=_unknown_answer(),
        readiness=_unknown_answer(),
        unresolved_questions=[], fetch_requests=[], status="complete",
        meta=ProfileMeta(
            model="claude-sonnet-4-6", num_turns=1, total_cost_usd=0.0,
            created_at="2026-05-01T00:00:00+00:00", extract_version="0.1.0",
        ),
    )


def test_build_prompt_includes_structured_fields_and_homepage() -> None:
    prompt = build_prompt(_profile(), catalog=None, homepage_text="HOMEPAGE_BODY")
    assert "Test Vendor Inc." in prompt
    assert "role: oem" in prompt
    assert "HOMEPAGE_BODY" in prompt
    # Style + length guidance present:
    assert f"no more than {TAGLINE_MAX_WORDS} words" in prompt
    assert "no marketing fluff" in prompt
    assert "Don't repeat the company name" in prompt


def test_build_prompt_includes_populated_headquarters() -> None:
    """Regression: tagline_agent referenced `hq.state_province`, the
    schema field is `state_or_province`. Build a profile with a
    populated HQ and assert no AttributeError."""
    from uxv_extract.schema import Headquarters, Citation
    p = _profile()
    populated_hq = Answer(
        value=Headquarters(
            city="Boston", state_or_province="MA", country="USA",
        ),
        confidence="high", status="answered",
        evidence=[Citation(
            source_kind="mirror", resource_id="resource-0001",
            line_start=1, line_end=2,
            url="https://test.example/", page_class="homepage",
            snippet="Boston, MA based defense supplier.",
        )],
    )
    p = p.model_copy(update={"headquarters": populated_hq})
    # The actual regression test is "no AttributeError raised".
    # Schema-level validators normalise "MA"→"Massachusetts" and
    # "USA"→"United States", so we just assert the prompt got built
    # and includes the city.
    prompt = build_prompt(p, catalog=None, homepage_text="…")
    assert "Boston" in prompt


def test_build_prompt_truncates_long_homepage() -> None:
    prompt = build_prompt(
        _profile(), catalog=None,
        homepage_text="x" * 50000,  # way over the cap
    )
    # Body cap is 8000 chars; full prompt should be well under 50k.
    assert len(prompt) < 20000


def test_truncate_to_words_caps_at_limit() -> None:
    text = " ".join(["word"] * 150)
    out = _truncate_to_words(text, 100)
    assert len(out.split()) == 100
    assert out.endswith("…")


def test_truncate_to_words_passes_short_text_through() -> None:
    assert _truncate_to_words("short text", 100) == "short text"


@pytest.mark.asyncio
async def test_generate_tagline_returns_haiku_response() -> None:
    """Mock anthropic client returns a fixed tagline; assert it's
    returned trimmed and within cap."""
    reader = CorpusReader.load(FIXTURE_ROOT)

    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text="Designs heavy-lift OEM drones for federal customers; NDAA status not disclosed.")]
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=mock_msg)

    tagline = await generate_tagline(
        profile=_profile(), catalog=None, corpus=reader,
        client=mock_client,
    )
    assert "OEM drones" in tagline
    assert len(tagline.split()) <= TAGLINE_MAX_WORDS
    # Verify Haiku was called with the right model:
    call_kwargs = mock_client.messages.create.call_args.kwargs
    assert call_kwargs["model"] == "claude-haiku-4-5"
    assert call_kwargs["max_tokens"] == 300
    assert "Test Vendor Inc." in call_kwargs["messages"][0]["content"]


@pytest.mark.asyncio
async def test_generate_tagline_truncates_overlong_response() -> None:
    """If Haiku returns >100 words despite the prompt, the runner
    truncates."""
    reader = CorpusReader.load(FIXTURE_ROOT)
    overlong = " ".join(["word"] * 150)
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text=overlong)]
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=mock_msg)

    tagline = await generate_tagline(
        profile=_profile(), catalog=None, corpus=reader,
        client=mock_client,
    )
    assert len(tagline.split()) == TAGLINE_MAX_WORDS


@pytest.mark.asyncio
async def test_generate_tagline_respects_timeout() -> None:
    """If the Haiku call hangs longer than timeout_sec, raise TimeoutError."""
    reader = CorpusReader.load(FIXTURE_ROOT)

    async def slow(*a, **kw):
        await asyncio.sleep(2)

    mock_client = MagicMock()
    mock_client.messages.create = slow

    with pytest.raises(asyncio.TimeoutError):
        await generate_tagline(
            profile=_profile(), catalog=None, corpus=reader,
            client=mock_client, timeout_sec=0.1,
        )


def test_write_tagline_persists_into_profile_json(tmp_path: Path) -> None:
    p = _profile()
    pf = tmp_path / "profile.json"
    pf.write_text(p.model_dump_json(indent=2))

    write_tagline_into_profile(pf, "Editorial summary of the vendor.")

    reloaded = json.loads(pf.read_text())
    assert reloaded["tagline"] == "Editorial summary of the vendor."
    # Round-trips through the schema:
    Profile.model_validate(reloaded)
