"""Pure-node tests for the Phase 2 agents (pre-orchestrator refactor).

Each of the four agents now exposes a Layer A pure function that the
LangGraph orchestrator (Phase 2 Slice 6) will wrap as a node. These tests
prove the Layer A / Layer B split is genuine:

  - The node functions are callable WITHOUT a sqlite connection.
  - They do NOT invoke `run_step` (no step rows written, no events).
  - They return a typed `*NodeOutput` carrying the agent's structured
    output plus the telemetry the wrapper (or the orchestrator) will
    record onto its step.

The existing four agent test suites (`test_researcher.py`, `test_extractor.py`,
`test_scorer.py`, `test_summarizer.py`) verify the Layer B wrappers
end-to-end through the database; they remain unchanged. This file is the
new isolation contract on Layer A.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_habitat.agents import (
    extractor as extractor_mod,
    researcher as researcher_mod,
    scorer as scorer_mod,
    summarizer as summarizer_mod,
)
from agent_habitat.agents.extractor import (
    ExtractorNodeOutput,
    ExtractorParseError,
    extractor_node,
)
from agent_habitat.agents.models import (
    PROFILE_FIELD_NAMES,
    CompanyProfile,
    DimensionScore,
    ExtractionGap,
    ProfileField,
    RawSignals,
    ScoredCompany,
    Signal,
    SourceSpan,
)
from agent_habitat.agents.researcher import (
    ResearcherNodeOutput,
    researcher_node,
)
from agent_habitat.agents.scorer import (
    ScorerError,
    ScorerNodeOutput,
    scorer_node,
)
from agent_habitat.agents.summarizer import (
    MAX_PROMPT_CHARS,
    SummarizerError,
    SummarizerNodeOutput,
    summarize_text,
)
from agent_habitat.llm import Citation, LLMResult
from agent_habitat.scoring import load_rubric


# ---------------------------------------------------------------------------
# Shared fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def log_root(tmp_path: Path) -> Path:
    return tmp_path / "logs"


def _signal(text: str, url: str = "https://example.com/s") -> Signal:
    return Signal(
        text=text,
        source_url=url,
        source_title=None,
        retrieved_at=datetime(2026, 5, 14, tzinfo=UTC),
    )


def _llm_result_text(
    content: str,
    *,
    cost_usd: float = 0.0123,
    jsonl_ref: str = "data/logs/2026-05-14/wf.jsonl:1",
    input_tokens: int = 250,
    output_tokens: int = 50,
    truncated: bool = False,
) -> LLMResult:
    return LLMResult(
        content=content,
        model="claude-sonnet-4-6",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        jsonl_ref=jsonl_ref,
        stop_reason="end_turn" if not truncated else "max_tokens",
        web_searches=0,
        citations=[],
    )


def _llm_result_research(
    *,
    citations: list[Citation] | None = None,
    cost_usd: float = 0.0321,
    web_searches: int = 2,
    jsonl_ref: str = "data/logs/2026-05-14/wf-res.jsonl:1",
) -> LLMResult:
    return LLMResult(
        content="Narrative.",
        model="claude-haiku-4-5-20251001",
        input_tokens=1500,
        output_tokens=120,
        cost_usd=cost_usd,
        jsonl_ref=jsonl_ref,
        stop_reason="end_turn",
        web_searches=web_searches,
        citations=citations or [],
    )


# ---------------------------------------------------------------------------
# researcher_node
# ---------------------------------------------------------------------------


class TestResearcherNodePure:
    def test_returns_typed_output_with_telemetry(self, log_root: Path) -> None:
        cits = [
            Citation(cited_text="Acme raised $50M.", source_url="https://x", source_title="X"),
            Citation(cited_text="Hired CTO Jane.", source_url="https://y", source_title="Y"),
        ]
        with patch.object(
            researcher_mod, "complete", return_value=_llm_result_research(citations=cits)
        ):
            out = researcher_node(
                company_name="Acme",
                workflow_id="wf-pure-1",
                log_root=log_root,
            )

        assert isinstance(out, ResearcherNodeOutput)
        assert out.raw_signals.company_name == "Acme"
        assert out.raw_signals.signal_count == 2
        assert out.cost_usd == pytest.approx(0.0321)
        assert out.output_ref == "data/logs/2026-05-14/wf-res.jsonl:1"
        assert out.structured_data == {
            "signal_count": 2,
            "source_count": 2,
            "web_searches": 2,
        }

    def test_no_citations_yields_empty_signals(self, log_root: Path) -> None:
        with patch.object(
            researcher_mod, "complete", return_value=_llm_result_research(citations=[])
        ):
            out = researcher_node(
                company_name="Obscure",
                workflow_id="wf-pure-2",
                log_root=log_root,
            )
        assert out.raw_signals.signal_count == 0
        assert out.structured_data["signal_count"] == 0

    def test_infrastructure_exception_propagates(self, log_root: Path) -> None:
        """Layer A raises on LLM failure; the wrapper (Layer B) converts to FAILED."""

        class Boom(RuntimeError):
            pass

        with patch.object(researcher_mod, "complete", side_effect=Boom("api down")):
            with pytest.raises(Boom):
                researcher_node(
                    company_name="Acme",
                    workflow_id="wf-pure-3",
                    log_root=log_root,
                )

    def test_forwards_max_searches_to_tool(self, log_root: Path) -> None:
        with patch.object(
            researcher_mod, "complete", return_value=_llm_result_research()
        ) as mock_complete:
            researcher_node(
                company_name="Acme",
                workflow_id="wf-pure-4",
                max_searches=7,
                log_root=log_root,
            )
        tools = mock_complete.call_args.kwargs["tools"]
        assert tools[0]["max_uses"] == 7


# ---------------------------------------------------------------------------
# extractor_node
# ---------------------------------------------------------------------------


_HAPPY_BODY = json.dumps(
    {
        "size": {
            "values": ["~500 employees"],
            "source_spans": [{"signal_index": 0, "quote": "approximately 500 employees"}],
        },
        "industry": {
            "values": ["Fintech"],
            "source_spans": [{"signal_index": 1, "quote": "fintech startup"}],
        },
        "tech_stack": {"gap": {"reason": "field_not_in_signals"}},
        "recent_news": {"gap": {"reason": "field_not_in_signals"}},
        "decision_makers": {"gap": {"reason": "field_not_in_signals"}},
    }
)
_HAPPY_INPUT = RawSignals(
    company_name="Acme Corp",
    signals=[
        _signal("Acme Corp has approximately 500 employees as of Q2 2026."),
        _signal("Acme Corp is a fintech startup based in San Francisco."),
    ],
)


class TestExtractorNodePure:
    def test_returns_typed_output_with_grounded_profile(self, log_root: Path) -> None:
        with patch.object(extractor_mod, "complete", return_value=_llm_result_text(_HAPPY_BODY)):
            out = extractor_node(
                raw_signals=_HAPPY_INPUT,
                workflow_id="wf-pure-ex-1",
                log_root=log_root,
            )

        assert isinstance(out, ExtractorNodeOutput)
        assert out.profile.company_name == "Acme Corp"
        assert not out.profile.size.is_gap
        assert not out.profile.industry.is_gap
        assert out.cost_usd == pytest.approx(0.0123)
        assert out.output_ref == "data/logs/2026-05-14/wf.jsonl:1"
        # Projection includes one has_<field> per profile field plus gap_count.
        for name in PROFILE_FIELD_NAMES:
            assert f"has_{name}" in out.structured_data
        assert out.structured_data["gap_count"] == 3

    def test_empty_input_short_circuits_no_llm_call(self, log_root: Path) -> None:
        """Empty RawSignals: no LLM call, cost $0, all-gaps profile, output_ref=None."""
        empty = RawSignals(company_name="Vacant Co", signals=[])
        with patch.object(extractor_mod, "complete") as mock_complete:
            out = extractor_node(
                raw_signals=empty,
                workflow_id="wf-pure-ex-empty",
                log_root=log_root,
            )

        assert mock_complete.call_count == 0
        assert out.cost_usd == 0.0
        assert out.output_ref is None
        assert all(out.profile.field(n).is_gap for n in PROFILE_FIELD_NAMES)
        assert out.structured_data["gap_count"] == len(PROFILE_FIELD_NAMES)

    def test_malformed_response_raises_parse_error(self, log_root: Path) -> None:
        with patch.object(
            extractor_mod, "complete", return_value=_llm_result_text("not json at all")
        ):
            with pytest.raises(ExtractorParseError):
                extractor_node(
                    raw_signals=_HAPPY_INPUT,
                    workflow_id="wf-pure-ex-bad",
                    log_root=log_root,
                )


# ---------------------------------------------------------------------------
# scorer_node
# ---------------------------------------------------------------------------


_SCORER_LLM_BODY = json.dumps(
    {
        "dimensions": [
            {
                "field": "industry",
                "score": 4.0,
                "grounded_quote": "fintech startup",
                "reasoning": "Matches the fintech rubric band.",
            },
            {
                "field": "size",
                "score": 3.0,
                "grounded_quote": "approximately 500 employees",
                "reasoning": "Mid-band on the size rubric.",
            },
        ]
    }
)


def _profile_with_two_fields() -> CompanyProfile:
    fields: dict[str, ProfileField] = {}
    for name in PROFILE_FIELD_NAMES:
        if name == "size":
            fields[name] = ProfileField(
                values=["~500 employees"],
                source_spans=[SourceSpan(signal_index=0, quote="approximately 500 employees")],
            )
        elif name == "industry":
            fields[name] = ProfileField(
                values=["Fintech"],
                source_spans=[SourceSpan(signal_index=1, quote="fintech startup")],
            )
        else:
            fields[name] = ProfileField(gap=ExtractionGap(reason="field_not_in_signals"))
    return CompanyProfile(company_name="Acme Corp", **fields)


def _all_gaps_profile() -> CompanyProfile:
    fields = {
        n: ProfileField(gap=ExtractionGap(reason="field_not_in_signals"))
        for n in PROFILE_FIELD_NAMES
    }
    return CompanyProfile(company_name="Vacant Co", **fields)


class TestScorerNodePure:
    def test_returns_typed_output_with_scored_company(self, log_root: Path) -> None:
        rubric = load_rubric()  # bundled template — placeholder weights summing to 1
        profile = _profile_with_two_fields()

        # Build an LLM response covering exactly the rubric dimensions whose
        # field is non-gap in the profile.
        scorable = [d.field for d in rubric.dimensions if not profile.field(d.field).is_gap]
        body = json.dumps(
            {
                "dimensions": [
                    {
                        "field": f,
                        "score": 4.0,
                        "grounded_quote": profile.field(f).source_spans[0].quote,
                        "reasoning": "rubric-band-match",
                    }
                    for f in scorable
                ]
            }
        )

        with patch.object(scorer_mod, "complete", return_value=_llm_result_text(body)):
            out = scorer_node(
                profile=profile,
                rubric=rubric,
                workflow_id="wf-pure-sc-1",
                log_root=log_root,
            )

        assert isinstance(out, ScorerNodeOutput)
        assert isinstance(out.scored_company, ScoredCompany)
        assert out.cost_usd == pytest.approx(0.0123)
        assert out.output_ref == "data/logs/2026-05-14/wf.jsonl:1"
        # Projection carries the auditor-facing keys.
        for key in (
            "score",
            "coverage",
            "floor",
            "min_coverage",
            "passed_floor",
            "passed_coverage",
            "tier",
            "gated_by",
        ):
            assert key in out.structured_data

    def test_all_gaps_short_circuits_no_llm_call(self, log_root: Path) -> None:
        rubric = load_rubric()
        with patch.object(scorer_mod, "complete") as mock_complete:
            out = scorer_node(
                profile=_all_gaps_profile(),
                rubric=rubric,
                workflow_id="wf-pure-sc-empty",
                log_root=log_root,
            )

        assert mock_complete.call_count == 0
        assert out.cost_usd == 0.0
        assert out.output_ref is None
        # Every dimension excluded → score is None, coverage 0.
        assert out.scored_company.score is None
        assert out.scored_company.coverage == 0.0
        # Every dimension surfaces as an excluded DimensionScore.
        for d in out.scored_company.dimensions:
            assert isinstance(d, DimensionScore)
            assert d.is_excluded

    def test_schema_mismatch_raises_scorer_error(self, log_root: Path) -> None:
        rubric = load_rubric()
        profile = _profile_with_two_fields()
        with patch.object(scorer_mod, "complete", return_value=_llm_result_text("{not: valid")):
            with pytest.raises(ScorerError):
                scorer_node(
                    profile=profile,
                    rubric=rubric,
                    workflow_id="wf-pure-sc-bad",
                    log_root=log_root,
                )


# ---------------------------------------------------------------------------
# summarize_text
# ---------------------------------------------------------------------------


class TestSummarizeTextPure:
    def test_under_limit_returns_summary_with_no_truncation(self, log_root: Path) -> None:
        readable = "A short readable page."
        with patch.object(
            summarizer_mod,
            "complete",
            return_value=_llm_result_text("Concise summary.", cost_usd=0.00123),
        ):
            out = summarize_text(readable, workflow_id="wf-pure-sum-1", log_root=log_root)

        assert isinstance(out, SummarizerNodeOutput)
        assert out.summary == "Concise summary."
        assert out.cost_usd == pytest.approx(0.00123)
        assert out.output_ref == "data/logs/2026-05-14/wf.jsonl:1"
        assert out.truncation.truncated is False
        assert out.truncation.original_chars == len(readable)
        assert out.truncation.used_chars == len(readable)
        assert out.truncation.dropped_chars == 0
        # When not truncated: only the LLM-result keys go into structured_data.
        assert "input_truncated" not in out.structured_data
        assert out.structured_data["input_tokens"] == 250
        assert out.structured_data["output_tokens"] == 50
        assert out.structured_data["truncated"] is False

    def test_over_limit_marks_truncation_and_records_extras(self, log_root: Path) -> None:
        readable = "x" * (MAX_PROMPT_CHARS + 1234)
        with patch.object(
            summarizer_mod,
            "complete",
            return_value=_llm_result_text("Summary of truncated input."),
        ) as mock_complete:
            out = summarize_text(readable, workflow_id="wf-pure-sum-2", log_root=log_root)

        # The complete() call saw only the truncated prefix.
        sent = mock_complete.call_args.kwargs["messages"][0]["content"]
        assert len(sent) == MAX_PROMPT_CHARS
        assert out.truncation.truncated is True
        assert out.truncation.original_chars == MAX_PROMPT_CHARS + 1234
        assert out.truncation.used_chars == MAX_PROMPT_CHARS
        assert out.truncation.dropped_chars == 1234
        assert out.structured_data["input_truncated"] is True
        assert out.structured_data["original_chars"] == MAX_PROMPT_CHARS + 1234
        assert out.structured_data["used_chars"] == MAX_PROMPT_CHARS
        assert out.structured_data["dropped_chars"] == 1234

    def test_llm_exception_wrapped_as_summarizer_error(self, log_root: Path) -> None:
        class Boom(RuntimeError):
            pass

        with patch.object(summarizer_mod, "complete", side_effect=Boom("api down")):
            with pytest.raises(SummarizerError) as exc_info:
                summarize_text("any readable", workflow_id="wf-pure-sum-3", log_root=log_root)
        assert exc_info.value.step_name == "summarize"
        assert "Boom" in exc_info.value.message
        assert "api down" in exc_info.value.message
