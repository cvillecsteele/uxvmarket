from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    ToolUseBlock,
    UserMessage,
)

from uxv_extract.agent import run_profile_extraction
from uxv_extract.corpus import CorpusReader

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "corpus_min"


def _unknown_answer_dict() -> dict[str, Any]:
    return {
        "value": None,
        "confidence": "low",
        "status": "unknown",
        "evidence": [],
        "notes": None,
    }


def _unknown_list_answer_dict() -> dict[str, Any]:
    return {
        "items": [],
        "confidence": "low",
        "status": "unknown",
        "notes": None,
    }


def _good_submission() -> dict[str, Any]:
    return {
        "products_categories": _unknown_list_answer_dict(),
        "headquarters": _unknown_answer_dict(),
        "drone_supply_chain_role": {
            "value": "oem",
            "confidence": "high",
            "status": "answered",
            "evidence": [
                {
                    "source_kind": "mirror",
                    "resource_id": "resource-0001",
                    "line_start": 1,
                    "line_end": 2,
                }
            ],
            "notes": None,
        },
        "products": _unknown_list_answer_dict(),
        "ndaa": _unknown_answer_dict(),
        "blue_uas": _unknown_answer_dict(),
        "readiness": _unknown_answer_dict(),
        "unresolved_questions": [],
        "fetch_requests": [],
        "status": "complete",
    }


def _result_message(num_turns: int = 3, cost: float = 0.05) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1000,
        duration_api_ms=900,
        is_error=False,
        num_turns=num_turns,
        session_id="test-session",
        stop_reason="end_turn",
        total_cost_usd=cost,
    )


def _assistant_with_submit(submission: dict[str, Any]) -> AssistantMessage:
    return AssistantMessage(
        content=[
            ToolUseBlock(
                id="tool-1",
                name="mcp__uxv_extract__submit_profile",
                input=submission,
            )
        ],
        model="claude-sonnet-4-6",
    )


def _make_query_fn(messages: list[Any]):
    async def fake_query(*, prompt, options, transport=None):
        for m in messages:
            yield m

    return fake_query


@pytest.mark.asyncio
async def test_runner_captures_submitted_profile() -> None:
    reader = CorpusReader.load(FIXTURE_ROOT)
    fake = _make_query_fn(
        [
            SystemMessage(subtype="init", data={}),
            _assistant_with_submit(_good_submission()),
            _result_message(num_turns=5, cost=0.08),
        ]
    )

    profile = await run_profile_extraction(
        reader,
        model="claude-sonnet-4-6",
        max_turns=10,
        query_fn=fake,
    )

    assert profile.target_id == "test-vendor"
    assert profile.run_id == "test-run"
    assert profile.display_name == "Test Vendor"
    assert profile.homepage_url == "https://test.example"
    assert str(reader.corpus_root) == profile.corpus_root
    assert profile.drone_supply_chain_role.value == "oem"
    assert profile.drone_supply_chain_role.confidence == "high"
    assert profile.status == "complete"
    assert profile.meta.model == "claude-sonnet-4-6"
    assert profile.meta.num_turns == 5
    assert profile.meta.total_cost_usd == 0.08


@pytest.mark.asyncio
async def test_runner_raises_when_agent_never_submits() -> None:
    reader = CorpusReader.load(FIXTURE_ROOT)
    fake = _make_query_fn(
        [
            SystemMessage(subtype="init", data={}),
            AssistantMessage(content=[], model="claude-sonnet-4-6"),
            _result_message(),
        ]
    )

    with pytest.raises(RuntimeError, match="submit_profile"):
        await run_profile_extraction(
            reader,
            model="claude-sonnet-4-6",
            max_turns=10,
            query_fn=fake,
        )


@pytest.mark.asyncio
async def test_runner_validates_submission_against_schema() -> None:
    reader = CorpusReader.load(FIXTURE_ROOT)
    bad = _good_submission()
    bad["drone_supply_chain_role"]["value"] = "totally_made_up_role"
    fake = _make_query_fn(
        [
            _assistant_with_submit(bad),
            _result_message(),
        ]
    )

    with pytest.raises(Exception):  # pydantic ValidationError subtype
        await run_profile_extraction(
            reader,
            model="claude-sonnet-4-6",
            max_turns=10,
            query_fn=fake,
        )


@pytest.mark.asyncio
async def test_runner_uses_latest_submission_when_agent_resubmits() -> None:
    """Submit-early-and-refine: if the agent calls submit_profile
    multiple times (a refinement on later evidence), the runner uses
    the LATEST validated submission. This is what makes the
    submit-early pattern useful — early submission is a safety net,
    refinements are the actual answer."""
    reader = CorpusReader.load(FIXTURE_ROOT)
    second = _good_submission()
    second["drone_supply_chain_role"]["value"] = "service_provider"
    fake = _make_query_fn(
        [
            _assistant_with_submit(_good_submission()),
            _assistant_with_submit(second),
            _result_message(),
        ]
    )
    profile = await run_profile_extraction(
        reader,
        model="claude-sonnet-4-6",
        max_turns=10,
        query_fn=fake,
    )
    assert profile.drone_supply_chain_role.value == "service_provider"


@pytest.mark.asyncio
async def test_runner_accepts_first_valid_submission_after_invalid_attempt() -> None:
    """When the SDK callback returns is_error for an invalid submission, the
    agent retries. The runner must take the first VALID submission from the
    message stream, not raise on the first invalid one."""
    reader = CorpusReader.load(FIXTURE_ROOT)
    bad = _good_submission()
    bad["drone_supply_chain_role"]["evidence"][0]["snippet"] = "too short"
    fake = _make_query_fn(
        [
            _assistant_with_submit(bad),
            _assistant_with_submit(_good_submission()),
            _result_message(num_turns=6),
        ]
    )
    profile = await run_profile_extraction(
        reader,
        model="claude-sonnet-4-6",
        max_turns=10,
        query_fn=fake,
    )
    assert profile.drone_supply_chain_role.value == "oem"
    assert profile.meta.num_turns == 6


def test_build_options_passes_max_cost_to_sdk() -> None:
    from uxv_extract.agent import build_options

    reader = CorpusReader.load(FIXTURE_ROOT)
    opts = build_options(reader, model="claude-sonnet-4-6", max_turns=30, max_cost_usd=1.5)
    assert opts.max_budget_usd == 1.5


def test_build_options_omits_max_cost_when_none() -> None:
    from uxv_extract.agent import build_options

    reader = CorpusReader.load(FIXTURE_ROOT)
    opts = build_options(reader, model="claude-sonnet-4-6", max_turns=30, max_cost_usd=None)
    assert opts.max_budget_usd is None


@pytest.mark.asyncio
async def test_runner_raises_when_timeout_fires_before_any_submission() -> None:
    """Cap fires before the agent submitted anything → raise. An
    empty profile is meaningless and the failure surfaces a real
    prompt-engineering problem (agent didn't follow 'submit early')."""
    import asyncio
    reader = CorpusReader.load(FIXTURE_ROOT)

    async def slow_query(*, prompt, options, transport=None):
        await asyncio.sleep(2)
        if False:
            yield  # pragma: no cover

    with pytest.raises(RuntimeError, match="did not call"):
        await run_profile_extraction(
            reader,
            model="claude-sonnet-4-6",
            max_turns=10,
            timeout_sec=0.1,
            query_fn=slow_query,
        )


@pytest.mark.asyncio
async def test_runner_returns_partial_when_cap_fires_after_submission() -> None:
    """Submit-early-and-refine: the agent submits once, then the
    timeout fires before they can refine. Runner returns the
    submission with status='partial' (overriding whatever status the
    agent set, since the cap means we don't trust 'complete')."""
    import asyncio
    reader = CorpusReader.load(FIXTURE_ROOT)

    async def fake(*, prompt, options, transport=None):
        # Yield one valid submission, then hang until the timeout fires.
        yield _assistant_with_submit(_good_submission())
        await asyncio.sleep(2)

    profile = await run_profile_extraction(
        reader,
        model="claude-sonnet-4-6",
        max_turns=10,
        timeout_sec=0.1,
        query_fn=fake,
    )
    assert profile.status == "partial"
    assert profile.drone_supply_chain_role.value == "oem"


@pytest.mark.asyncio
async def test_runner_returns_complete_when_no_cap() -> None:
    """Sanity: happy path still produces status='complete' from the
    agent's own submission, not the partial override."""
    reader = CorpusReader.load(FIXTURE_ROOT)
    fake = _make_query_fn(
        [
            _assistant_with_submit(_good_submission()),
            _result_message(),
        ]
    )
    profile = await run_profile_extraction(
        reader,
        model="claude-sonnet-4-6",
        max_turns=10,
        query_fn=fake,
    )
    assert profile.status == "complete"


@pytest.mark.asyncio
async def test_runner_no_timeout_when_timeout_sec_is_none() -> None:
    """Smoke: passing timeout_sec=None disables the wall-clock cap."""
    reader = CorpusReader.load(FIXTURE_ROOT)
    fake = _make_query_fn(
        [
            _assistant_with_submit(_good_submission()),
            _result_message(),
        ]
    )
    profile = await run_profile_extraction(
        reader,
        model="claude-sonnet-4-6",
        max_turns=10,
        timeout_sec=None,
        query_fn=fake,
    )
    assert profile.drone_supply_chain_role.value == "oem"


@pytest.mark.asyncio
async def test_runner_rejects_out_of_range_line_citation() -> None:
    reader = CorpusReader.load(FIXTURE_ROOT)
    sub = _good_submission()
    # text/0001-home.txt only has 2 lines; ask for line 50.
    sub["drone_supply_chain_role"]["evidence"][0]["line_end"] = 50
    fake = _make_query_fn(
        [
            _assistant_with_submit(sub),
            _result_message(),
        ]
    )
    with pytest.raises(ValueError, match="past end of file"):
        await run_profile_extraction(
            reader,
            model="claude-sonnet-4-6",
            max_turns=10,
            query_fn=fake,
        )


@pytest.mark.asyncio
async def test_runner_rejects_unknown_resource_id_in_citation() -> None:
    reader = CorpusReader.load(FIXTURE_ROOT)
    sub = _good_submission()
    sub["drone_supply_chain_role"]["evidence"][0]["resource_id"] = "resource-9999"
    fake = _make_query_fn(
        [
            _assistant_with_submit(sub),
            _result_message(),
        ]
    )
    with pytest.raises(ValueError, match="not a fetched resource"):
        await run_profile_extraction(
            reader,
            model="claude-sonnet-4-6",
            max_turns=10,
            query_fn=fake,
        )


@pytest.mark.asyncio
async def test_runner_hydrates_snippet_url_and_page_class_from_corpus() -> None:
    reader = CorpusReader.load(FIXTURE_ROOT)
    sub = _good_submission()
    # Strip the optional snippet/url/page_class fields — they should be
    # filled in from the corpus regardless of what the agent provided.
    sub["drone_supply_chain_role"]["evidence"][0].pop("snippet", None)
    sub["drone_supply_chain_role"]["evidence"][0].pop("url", None)
    sub["drone_supply_chain_role"]["evidence"][0].pop("page_class", None)
    fake = _make_query_fn(
        [
            _assistant_with_submit(sub),
            _result_message(),
        ]
    )
    profile = await run_profile_extraction(
        reader,
        model="claude-sonnet-4-6",
        max_turns=10,
        query_fn=fake,
    )
    cit = profile.drone_supply_chain_role.evidence[0]
    # Lines 1-2 of fixture text/0001-home.txt:
    #   Test Vendor
    #   Home page text content for the test fixture.
    assert "Test Vendor" in cit.snippet
    assert "fixture" in cit.snippet
    assert cit.url == "https://test.example/"
    assert cit.page_class == "homepage"


def test_hydrate_submission_rejects_oversized_extraction(tmp_path: Path) -> None:
    """If the cited line range produces more than 60 words, hydration
    rejects so the agent can pick a tighter range."""
    import shutil
    from uxv_extract.agent import CitationHydrationError, hydrate_submission
    from uxv_extract.schema import Answer, Citation, ProfileSubmission, ListAnswer

    target = tmp_path / "corpus"
    shutil.copytree(FIXTURE_ROOT, target)
    # Stuff line 1 with 80 words.
    fat_line = " ".join(["lorem"] * 80) + "\n" + "second line\n"
    (target / "text" / "0001-home.txt").write_text(fat_line)

    reader = CorpusReader.load(target)
    sub = ProfileSubmission(
        products_categories=ListAnswer(items=[], confidence="low", status="unknown"),
        headquarters=Answer(value=None, confidence="low", status="unknown"),
        drone_supply_chain_role=Answer(
            value="oem",
            confidence="high",
            status="answered",
            evidence=[
                Citation(
                    source_kind="mirror",
                    resource_id="resource-0001",
                    line_start=1,
                    line_end=1,
                )
            ],
        ),
        products=ListAnswer(items=[], confidence="low", status="unknown"),
        ndaa=Answer(value=None, confidence="low", status="unknown"),
        blue_uas=Answer(value=None, confidence="low", status="unknown"),
        readiness=Answer(value=None, confidence="low", status="unknown"),
        unresolved_questions=[],
        fetch_requests=[],
        status="complete",
    )
    with pytest.raises(CitationHydrationError, match="cap is 60"):
        hydrate_submission(sub, reader)


def test_single_line_overcap_message_tells_agent_not_to_resubmit(tmp_path: Path) -> None:
    """When the cited line is itself >60 words (paragraph-on-one-line),
    the error must NOT say "pick a tighter range" — that's impossible
    on a single-line citation. Instead, instruct picking a different
    line or splitting the evidence. This test pins that guidance."""
    import shutil
    from uxv_extract.agent import CitationHydrationError, hydrate_submission
    from uxv_extract.schema import Answer, Citation, ProfileSubmission, ListAnswer

    target = tmp_path / "corpus"
    shutil.copytree(FIXTURE_ROOT, target)
    fat_line = " ".join(["lorem"] * 80) + "\n" + "second line\n"
    (target / "text" / "0001-home.txt").write_text(fat_line)
    reader = CorpusReader.load(target)

    sub = ProfileSubmission(
        products_categories=ListAnswer(items=[], confidence="low", status="unknown"),
        headquarters=Answer(value=None, confidence="low", status="unknown"),
        drone_supply_chain_role=Answer(
            value="oem", confidence="high", status="answered",
            evidence=[Citation(
                source_kind="mirror",
                resource_id="resource-0001",
                line_start=1, line_end=1,  # single line
            )],
        ),
        products=ListAnswer(items=[], confidence="low", status="unknown"),
        ndaa=Answer(value=None, confidence="low", status="unknown"),
        blue_uas=Answer(value=None, confidence="low", status="unknown"),
        readiness=Answer(value=None, confidence="low", status="unknown"),
        unresolved_questions=[], fetch_requests=[], status="complete",
    )
    with pytest.raises(CitationHydrationError) as exc_info:
        hydrate_submission(sub, reader)
    msg = str(exc_info.value)
    assert "single line" in msg
    assert "DIFFERENT line" in msg
    assert "DO NOT submit the same line again" in msg
    # Crucially, must NOT use the misleading "pick a tighter range"
    # wording — that's the bug: the agent kept obeying it on a
    # single line and thrashing.
    assert "pick a tighter range" not in msg.lower()


def test_validate_fetch_requests_rejects_in_idx_true_when_not_in_crawl(tmp_path: Path) -> None:
    from uxv_extract.agent import (
        CitationHydrationError, validate_fetch_requests
    )
    from uxv_extract.schema import FetchRequest

    reader = CorpusReader.load(FIXTURE_ROOT)
    bad = FetchRequest(
        url="https://test.example/totally-not-discovered",
        reason="agent imagined it",
        expected_evidence=["products"],
        in_corpus_index=True,
    )
    with pytest.raises(CitationHydrationError, match="NOT in crawl_index"):
        validate_fetch_requests([bad], reader)


def test_validate_fetch_requests_rejects_in_idx_false_when_url_is_in_crawl(tmp_path: Path) -> None:
    """The fixture's crawl_index has https://test.example/products as
    skipped_class_budget. Claiming in_corpus_index=false for it must be
    rejected — agent should set true."""
    from uxv_extract.agent import (
        CitationHydrationError, validate_fetch_requests
    )
    from uxv_extract.schema import FetchRequest

    reader = CorpusReader.load(FIXTURE_ROOT)
    bad = FetchRequest(
        url="https://test.example/products",
        reason="claims it's a guess",
        expected_evidence=["products"],
        in_corpus_index=False,
    )
    with pytest.raises(CitationHydrationError, match="IS in crawl_index"):
        validate_fetch_requests([bad], reader)


def test_validate_fetch_requests_rejects_already_fetched_url(tmp_path: Path) -> None:
    """The fixture's homepage is fetched. Re-requesting it is wasteful."""
    from uxv_extract.agent import (
        CitationHydrationError, validate_fetch_requests
    )
    from uxv_extract.schema import FetchRequest

    reader = CorpusReader.load(FIXTURE_ROOT)
    bad = FetchRequest(
        url="https://test.example/",
        reason="agent forgot it already read this",
        expected_evidence=["products"],
        in_corpus_index=True,
    )
    with pytest.raises(CitationHydrationError, match="already.*fetched"):
        validate_fetch_requests([bad], reader)


def test_session_diagnostic_returns_none_when_dir_missing(tmp_path: Path) -> None:
    from uxv_extract.agent import read_latest_session_diagnostic
    assert read_latest_session_diagnostic(tmp_path / "no-such-cwd") is None


def test_session_diagnostic_extracts_stop_reason_and_output_tokens(tmp_path: Path, monkeypatch) -> None:
    """Drop a synthetic session jsonl mimicking the SDK's transcript and
    verify we pull stop_reason / usage.output_tokens / last_text."""
    from uxv_extract.agent import read_latest_session_diagnostic, _session_project_dir_for_cwd

    cwd = tmp_path / "fake-corpus-cwd"
    cwd.mkdir()
    fake_home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(fake_home))
    sdir = _session_project_dir_for_cwd(cwd)
    sdir.mkdir(parents=True)
    session = sdir / "abc-123.jsonl"
    session.write_text(
        '{"type":"system","subtype":"init","session_id":"abc-123"}\n'
        '{"type":"assistant","message":{"role":"assistant","stop_reason":"end_turn",'
        '"usage":{"output_tokens":150},"content":[{"type":"text","text":"first"}]}}\n'
        '{"type":"user","message":{"role":"user","content":[]}}\n'
        '{"type":"assistant","message":{"role":"assistant","stop_reason":"max_tokens",'
        '"usage":{"output_tokens":32000},"content":[{"type":"text","text":"compiling now"}]}}\n'
    )
    sd = read_latest_session_diagnostic(cwd)
    assert sd is not None
    assert sd["stop_reason"] == "max_tokens"
    assert sd["output_tokens"] == 32000
    assert sd["num_turns"] == 2
    assert sd["last_text"] == "compiling now"
    assert sd["session_path"].endswith("abc-123.jsonl")


def test_session_diagnostic_picks_most_recent_when_multiple_sessions(tmp_path: Path, monkeypatch) -> None:
    import time
    from uxv_extract.agent import read_latest_session_diagnostic, _session_project_dir_for_cwd

    cwd = tmp_path / "cwd"
    cwd.mkdir()
    fake_home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(fake_home))
    sdir = _session_project_dir_for_cwd(cwd)
    sdir.mkdir(parents=True)

    older = sdir / "older.jsonl"
    older.write_text(
        '{"type":"assistant","message":{"role":"assistant","stop_reason":"end_turn",'
        '"usage":{"output_tokens":100},"content":[{"type":"text","text":"old"}]}}\n'
    )
    time.sleep(0.05)
    newer = sdir / "newer.jsonl"
    newer.write_text(
        '{"type":"assistant","message":{"role":"assistant","stop_reason":"max_tokens",'
        '"usage":{"output_tokens":32000},"content":[{"type":"text","text":"new"}]}}\n'
    )
    sd = read_latest_session_diagnostic(cwd)
    assert sd["stop_reason"] == "max_tokens"
    assert sd["last_text"] == "new"


def test_sdk_subprocess_error_message_includes_session_diagnostic() -> None:
    from uxv_extract.agent import SDKSubprocessError

    exc = SDKSubprocessError(
        Exception("Command failed with exit code 1"),
        "",
        session_diagnostic={
            "session_path": "/x/abc.jsonl",
            "stop_reason": "max_tokens",
            "output_tokens": 32000,
            "num_turns": 27,
            "last_text": "Let me compile and submit the catalog.",
        },
    )
    msg = str(exc)
    assert "stop_reason: max_tokens" in msg
    assert "output_tokens: 32000" in msg
    assert "Let me compile" in msg


def test_validate_fetch_requests_accepts_correct_claims(tmp_path: Path) -> None:
    from uxv_extract.agent import validate_fetch_requests
    from uxv_extract.schema import FetchRequest

    reader = CorpusReader.load(FIXTURE_ROOT)
    # /products is in crawl_index as skipped_class_budget — claim true
    in_idx = FetchRequest(
        url="https://test.example/products",
        reason="from crawl_index",
        expected_evidence=["products"],
        in_corpus_index=True,
    )
    # genuinely novel URL not in crawl_index — claim false
    novel = FetchRequest(
        url="https://test.example/totally-new-path",
        reason="guessed",
        expected_evidence=["products"],
        in_corpus_index=False,
    )
    validate_fetch_requests([in_idx, novel], reader)  # no raise


def test_build_user_prompt_includes_target_metadata() -> None:
    """Sanity: the prompt actually mentions the corpus the agent should read."""
    from uxv_extract.agent import build_user_prompt

    reader = CorpusReader.load(FIXTURE_ROOT)
    prompt = build_user_prompt(reader)
    assert "test-vendor" in prompt
    assert "Test Vendor" in prompt
    assert "https://test.example" in prompt
