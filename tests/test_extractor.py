"""Tests for the Phase 2 Slice 3 Extractor agent + CompanyProfile model.

Deterministic units (no API, no network — the LLM call is mocked) plus
one guarded live smoke that runs at most once when ANTHROPIC_API_KEY is set.

Coverage:
  TestModels                       — Pydantic models: ProfileField exclusivity,
                                     SourceSpan/ExtractionGap validate, extra="forbid",
                                     CompanyProfile gap_count + round-trip.
  TestParseAndGround               — Pure parse + substring-grounding helpers:
                                     happy parse, fenced-JSON tolerance, malformed JSON
                                     raises, schema mismatch raises, substring downgrade
                                     of over-reach (THE SHORT-SPAN CASE), out-of-range
                                     signal_index downgrade.
  TestRunHappy                     — run_extractor with mocked LLM persists
                                     workflow + step + events; cost rolled up;
                                     output_ref set; projection mirrored;
                                     source-span refs traceable into RawSignals.
  TestEmptyOutcome                 — empty RawSignals → all-gaps + COMPLETED,
                                     no LLM call made, cost = 0.
  TestSparseSignals                — sparse signals → some fields gappy, others
                                     extracted; mixed projection.
  TestShortSpanForcesGap           — short cited spans force gaps when the
                                     model over-reaches (inherited ADR-003
                                     addendum forward-dependency).
  TestInfrastructureFailure        — LLM raises → workflow FAILED, step FAILED;
                                     malformed-JSON response → FAILED; bad-schema
                                     response → FAILED.
  TestCLI                          — run-extractor happy + failure exits non-zero.
  test_live_extractor_round_trip   — one real Researcher + Extractor end-to-end.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from agent_habitat.agents import extractor as extractor_mod
from agent_habitat.agents import researcher as researcher_mod
from agent_habitat.agents.extractor import (
    AGENT_NAME,
    WORKFLOW_TYPE,
    ExtractorParseError,
    ExtractorResult,
    _ground_profile,
    _normalise_for_substring,
    _parse_profile,
    _projection,
    run_extractor,
)
from agent_habitat.agents.models import (
    PROFILE_FIELD_NAMES,
    CompanyProfile,
    ExtractionGap,
    ProfileField,
    RawSignals,
    Signal,
    SourceSpan,
)
from agent_habitat.cli import main
from agent_habitat.llm import Citation, LLMResult
from agent_habitat.state import (
    StepStatus,
    WorkflowStatus,
    init_db,
    load_events,
    load_steps,
    load_workflow,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "agent_habitat.db"


@pytest.fixture
def conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    c = init_db(db_path)
    try:
        yield c
    finally:
        c.close()


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


def _raw_signals(*texts: str, company: str = "Acme Corp") -> RawSignals:
    return RawSignals(
        company_name=company,
        signals=[_signal(t, f"https://example.com/s{i}") for i, t in enumerate(texts)],
    )


def _llm_result(content: str, *, cost_usd: float = 0.0123) -> LLMResult:
    return LLMResult(
        content=content,
        model="claude-sonnet-4-6",
        input_tokens=2000,
        output_tokens=300,
        cost_usd=cost_usd,
        jsonl_ref="data/logs/2026-05-14/wf-ext.jsonl:1",
        stop_reason="end_turn",
        web_searches=0,
        citations=[],
    )


# A "good" response body for a 2-signal Acme input: both fields the signals
# can support are extracted with verbatim grounding; remaining fields are
# explicit gaps (not silent nulls).
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
_HAPPY_INPUT = _raw_signals(
    "Acme Corp has approximately 500 employees as of Q2 2026.",
    "Acme Corp is a fintech startup based in San Francisco.",
)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class TestModels:
    def test_source_span_validates(self) -> None:
        s = SourceSpan(signal_index=2, quote="some text")
        assert s.signal_index == 2
        assert s.quote == "some text"

    def test_source_span_negative_index_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SourceSpan(signal_index=-1, quote="x")

    def test_source_span_empty_quote_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SourceSpan(signal_index=0, quote="")

    def test_source_span_frozen(self) -> None:
        s = SourceSpan(signal_index=0, quote="x")
        with pytest.raises(ValidationError):
            s.quote = "y"  # type: ignore[misc]

    def test_source_span_extra_forbid(self) -> None:
        with pytest.raises(ValidationError):
            SourceSpan.model_validate({"signal_index": 0, "quote": "x", "unexpected": "field"})

    def test_extraction_gap_validates(self) -> None:
        g = ExtractionGap(reason="field_not_in_signals")
        assert g.reason == "field_not_in_signals"

    def test_extraction_gap_empty_reason_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ExtractionGap(reason="")

    def test_extraction_gap_extra_forbid(self) -> None:
        with pytest.raises(ValidationError):
            ExtractionGap.model_validate({"reason": "x", "extra": 1})

    def test_profile_field_extracted_ok(self) -> None:
        pf = ProfileField(
            values=["~500 employees"],
            source_spans=[SourceSpan(signal_index=0, quote="500 employees")],
        )
        assert pf.is_gap is False
        assert pf.gap is None

    def test_profile_field_gap_ok(self) -> None:
        pf = ProfileField(gap=ExtractionGap(reason="field_not_in_signals"))
        assert pf.is_gap is True

    def test_profile_field_as_gap_helper(self) -> None:
        pf = ProfileField.as_gap("no_signals")
        assert pf.is_gap is True
        assert pf.gap is not None
        assert pf.gap.reason == "no_signals"

    def test_profile_field_must_be_one_of_value_or_gap(self) -> None:
        # Neither values nor gap.
        with pytest.raises(ValidationError):
            ProfileField()
        # Both values and gap.
        with pytest.raises(ValidationError):
            ProfileField(
                values=["x"],
                source_spans=[SourceSpan(signal_index=0, quote="x")],
                gap=ExtractionGap(reason="r"),
            )

    def test_profile_field_values_require_source_span(self) -> None:
        # values without source_spans is invalid.
        with pytest.raises(ValidationError):
            ProfileField(values=["x"], source_spans=[])

    def test_profile_field_gap_must_be_pure(self) -> None:
        # gap with stray values/source_spans is invalid.
        with pytest.raises(ValidationError):
            ProfileField(
                values=["x"],
                gap=ExtractionGap(reason="r"),
            )

    def test_profile_field_extra_forbid(self) -> None:
        with pytest.raises(ValidationError):
            ProfileField.model_validate(
                {
                    "gap": {"reason": "x"},
                    "extra": "field",
                }
            )

    def test_company_profile_gap_count_and_extracted_count(self) -> None:
        good = ProfileField(
            values=["x"],
            source_spans=[SourceSpan(signal_index=0, quote="x")],
        )
        gap = ProfileField.as_gap("field_not_in_signals")
        cp = CompanyProfile(
            company_name="Acme",
            size=good,
            industry=gap,
            tech_stack=gap,
            recent_news=good,
            decision_makers=gap,
        )
        assert cp.gap_count == 3
        assert cp.extracted_count == 2
        assert len(PROFILE_FIELD_NAMES) == 5

    def test_company_profile_extra_forbid(self) -> None:
        gap = ProfileField.as_gap("r")
        with pytest.raises(ValidationError):
            CompanyProfile.model_validate(
                {
                    "company_name": "Acme",
                    "size": gap.model_dump(),
                    "industry": gap.model_dump(),
                    "tech_stack": gap.model_dump(),
                    "recent_news": gap.model_dump(),
                    "decision_makers": gap.model_dump(),
                    "rogue_field": "nope",
                }
            )

    def test_company_profile_round_trip(self) -> None:
        gap = ProfileField.as_gap("r")
        good = ProfileField(
            values=["x"],
            source_spans=[SourceSpan(signal_index=0, quote="x")],
        )
        cp = CompanyProfile(
            company_name="Acme",
            size=good,
            industry=gap,
            tech_stack=gap,
            recent_news=gap,
            decision_makers=gap,
        )
        dumped = cp.model_dump_json()
        loaded = CompanyProfile.model_validate_json(dumped)
        assert loaded == cp

    def test_company_profile_field_lookup_by_name(self) -> None:
        gap = ProfileField.as_gap("r")
        cp = CompanyProfile(
            company_name="Acme",
            size=gap,
            industry=gap,
            tech_stack=gap,
            recent_news=gap,
            decision_makers=gap,
        )
        assert cp.field("size") is cp.size
        with pytest.raises(KeyError):
            cp.field("not_a_real_field")


# ---------------------------------------------------------------------------
# Pure parse + grounding
# ---------------------------------------------------------------------------


class TestParseAndGround:
    def test_parse_happy(self) -> None:
        cp = _parse_profile(_HAPPY_BODY, "Acme Corp")
        assert cp.company_name == "Acme Corp"
        assert cp.size.values == ["~500 employees"]
        assert cp.industry.values == ["Fintech"]
        assert cp.tech_stack.is_gap

    def test_parse_strips_markdown_fence(self) -> None:
        wrapped = "```json\n" + _HAPPY_BODY + "\n```"
        cp = _parse_profile(wrapped, "Acme Corp")
        assert cp.size.values == ["~500 employees"]

    def test_parse_strips_unlabelled_fence(self) -> None:
        wrapped = "```\n" + _HAPPY_BODY + "\n```"
        cp = _parse_profile(wrapped, "Acme Corp")
        assert cp.industry.values == ["Fintech"]

    def test_parse_malformed_json_raises(self) -> None:
        with pytest.raises(ExtractorParseError):
            _parse_profile("{not valid json", "Acme")

    def test_parse_non_object_root_raises(self) -> None:
        with pytest.raises(ExtractorParseError):
            _parse_profile("[1, 2, 3]", "Acme")

    def test_parse_schema_mismatch_raises(self) -> None:
        # Missing fields.
        with pytest.raises(ExtractorParseError):
            _parse_profile('{"size": {"gap": {"reason": "r"}}}', "Acme")

    def test_parse_extra_field_rejected_by_forbid(self) -> None:
        body = json.loads(_HAPPY_BODY)
        body["extra_field"] = "nope"
        with pytest.raises(ExtractorParseError):
            _parse_profile(json.dumps(body), "Acme")

    def test_parse_company_name_disagreement_rejected(self) -> None:
        body = json.loads(_HAPPY_BODY)
        body["company_name"] = "Different Co"
        with pytest.raises(ExtractorParseError):
            _parse_profile(json.dumps(body), "Acme Corp")

    def test_ground_passes_when_quote_substrings_match(self) -> None:
        parsed = _parse_profile(_HAPPY_BODY, "Acme Corp")
        grounded = _ground_profile(parsed, _HAPPY_INPUT)
        # Both extracted fields survive grounding.
        assert grounded.size.values == ["~500 employees"]
        assert grounded.industry.values == ["Fintech"]
        # All gap fields remain gaps.
        assert grounded.tech_stack.is_gap

    def test_ground_normalisation_is_case_and_whitespace_insensitive(self) -> None:
        body = json.dumps(
            {
                "size": {
                    "values": ["large"],
                    # Wonky whitespace + case in the quote — should still ground.
                    "source_spans": [
                        {"signal_index": 0, "quote": "APPROXIMATELY   500\nEMPLOYEES"}
                    ],
                },
                "industry": {"gap": {"reason": "field_not_in_signals"}},
                "tech_stack": {"gap": {"reason": "field_not_in_signals"}},
                "recent_news": {"gap": {"reason": "field_not_in_signals"}},
                "decision_makers": {"gap": {"reason": "field_not_in_signals"}},
            }
        )
        parsed = _parse_profile(body, "Acme Corp")
        grounded = _ground_profile(parsed, _HAPPY_INPUT)
        assert grounded.size.is_gap is False

    def test_ground_downgrades_overreach_to_gap(self) -> None:
        """A model that over-reaches beyond what the cited span supports must be
        downgraded to a gap. THE SHORT-SPAN inherited forward-dependency lives
        here: when a span is too narrow to support extraction the substring
        check catches the over-reach and forces a gap."""
        body = json.dumps(
            {
                "size": {
                    "values": ["enormous, world-leading"],
                    # The signal does not contain this text — over-reach.
                    "source_spans": [{"signal_index": 0, "quote": "enormous, world-leading firm"}],
                },
                "industry": {"gap": {"reason": "field_not_in_signals"}},
                "tech_stack": {"gap": {"reason": "field_not_in_signals"}},
                "recent_news": {"gap": {"reason": "field_not_in_signals"}},
                "decision_makers": {"gap": {"reason": "field_not_in_signals"}},
            }
        )
        parsed = _parse_profile(body, "Acme Corp")
        grounded = _ground_profile(parsed, _HAPPY_INPUT)
        # Downgraded — not let through.
        assert grounded.size.is_gap is True
        assert grounded.size.gap is not None
        assert grounded.size.gap.reason == "span_not_grounded"

    def test_ground_downgrades_out_of_range_signal_index(self) -> None:
        body = json.dumps(
            {
                "size": {
                    "values": ["x"],
                    "source_spans": [{"signal_index": 99, "quote": "x"}],
                },
                "industry": {"gap": {"reason": "r"}},
                "tech_stack": {"gap": {"reason": "r"}},
                "recent_news": {"gap": {"reason": "r"}},
                "decision_makers": {"gap": {"reason": "r"}},
            }
        )
        parsed = _parse_profile(body, "Acme Corp")
        grounded = _ground_profile(parsed, _HAPPY_INPUT)
        assert grounded.size.is_gap is True
        assert grounded.size.gap is not None
        assert grounded.size.gap.reason == "span_not_grounded"

    def test_normalise(self) -> None:
        assert _normalise_for_substring("  Foo   BAR\n\tbaz  ") == "foo bar baz"

    def test_projection_shape(self) -> None:
        good = ProfileField(
            values=["x"],
            source_spans=[SourceSpan(signal_index=0, quote="x")],
        )
        gap = ProfileField.as_gap("r")
        cp = CompanyProfile(
            company_name="Acme",
            size=good,
            industry=gap,
            tech_stack=good,
            recent_news=gap,
            decision_makers=gap,
        )
        proj = _projection(cp)
        assert proj["has_size"] is True
        assert proj["has_industry"] is False
        assert proj["has_tech_stack"] is True
        assert proj["has_recent_news"] is False
        assert proj["has_decision_makers"] is False
        assert proj["gap_count"] == 3


# ---------------------------------------------------------------------------
# Happy path — run_extractor end-to-end with mocked LLM
# ---------------------------------------------------------------------------


class TestRunHappy:
    def _run(
        self,
        conn: sqlite3.Connection,
        log_root: Path,
        *,
        raw_signals: RawSignals = _HAPPY_INPUT,
        body: str = _HAPPY_BODY,
        cost_usd: float = 0.0123,
    ) -> ExtractorResult:
        with patch.object(
            extractor_mod,
            "complete",
            return_value=_llm_result(body, cost_usd=cost_usd),
        ):
            return run_extractor(conn, raw_signals=raw_signals, log_root=log_root)

    def test_returns_completed_with_profile(self, conn: sqlite3.Connection, log_root: Path) -> None:
        result = self._run(conn, log_root)
        assert result.status is WorkflowStatus.COMPLETED
        assert result.company_name == "Acme Corp"
        assert result.profile.size.values == ["~500 employees"]
        assert result.profile.industry.values == ["Fintech"]
        assert result.profile.gap_count == 3
        assert result.cost_usd == pytest.approx(0.0123)

    def test_persists_workflow_step_events(self, conn: sqlite3.Connection, log_root: Path) -> None:
        result = self._run(conn, log_root)
        wf = load_workflow(conn, result.workflow_id)
        assert wf is not None
        assert wf.status is WorkflowStatus.COMPLETED
        assert wf.finished_at is not None
        assert wf.workflow_type == WORKFLOW_TYPE
        assert wf.metadata.get("company_name") == "Acme Corp"
        assert wf.metadata.get("input_signal_count") == 2
        assert wf.cost_total_usd == pytest.approx(0.0123)

        steps = load_steps(conn, result.workflow_id)
        assert [s.agent_name for s in steps] == [AGENT_NAME]
        step = steps[0]
        assert step.status is StepStatus.COMPLETED
        assert step.finished_at is not None
        assert step.cost_usd == pytest.approx(0.0123)
        assert step.output_ref == "data/logs/2026-05-14/wf-ext.jsonl:1"

        events = load_events(conn, result.workflow_id)
        types = [(e.structured_data or {}).get("event_type") for e in events]
        assert types.count("workflow.started") == 1
        assert types.count("workflow.completed") == 1
        assert types.count("step.started") == 1
        assert types.count("step.completed") == 1
        assert "step.failed" not in types
        assert "workflow.failed" not in types

    def test_step_completed_projection(self, conn: sqlite3.Connection, log_root: Path) -> None:
        result = self._run(conn, log_root)
        events = load_events(conn, result.workflow_id)
        completed = next(
            e for e in events if (e.structured_data or {}).get("event_type") == "step.completed"
        )
        sd = completed.structured_data or {}
        assert sd.get("step_name") == AGENT_NAME
        assert sd.get("has_size") is True
        assert sd.get("has_industry") is True
        assert sd.get("has_tech_stack") is False
        assert sd.get("has_recent_news") is False
        assert sd.get("has_decision_makers") is False
        assert sd.get("gap_count") == 3
        assert sd.get("cost_usd") == pytest.approx(0.0123)
        assert sd.get("output_ref") == "data/logs/2026-05-14/wf-ext.jsonl:1"

    def test_uses_sonnet_tier(self, conn: sqlite3.Connection, log_root: Path) -> None:
        with patch.object(
            extractor_mod, "complete", return_value=_llm_result(_HAPPY_BODY)
        ) as mock_complete:
            run_extractor(conn, raw_signals=_HAPPY_INPUT, log_root=log_root)
        kwargs = mock_complete.call_args.kwargs
        assert kwargs["model_tier"].value == "claude-sonnet-4-6"
        # No tools= — the Extractor does not invoke web_search.
        assert "tools" not in kwargs or kwargs.get("tools") is None
        # System prompt is the structured-output instruction.
        assert "OUTPUT JSON ONLY" in kwargs["system"]

    def test_source_spans_traceable_to_raw_signals(
        self, conn: sqlite3.Connection, log_root: Path
    ) -> None:
        """The audit chain back to RawSignals: for each extracted field, the
        SourceSpan.signal_index resolves to a real signal whose text contains
        the quote (after normalisation)."""
        result = self._run(conn, log_root)
        for name in PROFILE_FIELD_NAMES:
            field = result.profile.field(name)
            if field.is_gap:
                continue
            for span in field.source_spans:
                assert 0 <= span.signal_index < _HAPPY_INPUT.signal_count
                signal_text = _normalise_for_substring(_HAPPY_INPUT.signals[span.signal_index].text)
                quote = _normalise_for_substring(span.quote)
                assert quote in signal_text, (
                    f"{name}: quote {span.quote!r} not substring of "
                    f"signal[{span.signal_index}].text"
                )

    def test_custom_workflow_id_honoured(self, conn: sqlite3.Connection, log_root: Path) -> None:
        with patch.object(extractor_mod, "complete", return_value=_llm_result(_HAPPY_BODY)):
            result = run_extractor(
                conn,
                raw_signals=_HAPPY_INPUT,
                workflow_id="wf-extractor-1",
                log_root=log_root,
            )
        assert result.workflow_id == "wf-extractor-1"


# ---------------------------------------------------------------------------
# Empty outcome — all-gaps profile, no LLM call (ADR-006 §1 empty contract)
# ---------------------------------------------------------------------------


class TestEmptyOutcome:
    def test_empty_signals_short_circuit_no_llm_call(
        self, conn: sqlite3.Connection, log_root: Path
    ) -> None:
        empty_input = RawSignals(company_name="Obscure Co", signals=[])
        with patch.object(extractor_mod, "complete") as mock_complete:
            result = run_extractor(conn, raw_signals=empty_input, log_root=log_root)
        # No LLM call was made — the extractor short-circuits empty input.
        mock_complete.assert_not_called()
        assert result.status is WorkflowStatus.COMPLETED
        assert result.cost_usd == 0.0
        # Every field is a gap with reason no_signals.
        for name in PROFILE_FIELD_NAMES:
            field = result.profile.field(name)
            assert field.is_gap
            assert field.gap is not None
            assert field.gap.reason == "no_signals"
        assert result.profile.gap_count == 5

    def test_empty_signals_workflow_completed_not_failed(
        self, conn: sqlite3.Connection, log_root: Path
    ) -> None:
        empty_input = RawSignals(company_name="Obscure Co", signals=[])
        result = run_extractor(conn, raw_signals=empty_input, log_root=log_root)
        wf = load_workflow(conn, result.workflow_id)
        assert wf is not None
        assert wf.status is WorkflowStatus.COMPLETED
        events = load_events(conn, result.workflow_id)
        types = [(e.structured_data or {}).get("event_type") for e in events]
        assert "workflow.completed" in types
        assert "workflow.failed" not in types

    def test_empty_signals_projection_all_false(
        self, conn: sqlite3.Connection, log_root: Path
    ) -> None:
        empty_input = RawSignals(company_name="Obscure Co", signals=[])
        result = run_extractor(conn, raw_signals=empty_input, log_root=log_root)
        events = load_events(conn, result.workflow_id)
        completed = next(
            e for e in events if (e.structured_data or {}).get("event_type") == "step.completed"
        )
        sd = completed.structured_data or {}
        for name in PROFILE_FIELD_NAMES:
            assert sd.get(f"has_{name}") is False
        assert sd.get("gap_count") == 5


# ---------------------------------------------------------------------------
# Sparse signals — mixed extracted + gaps
# ---------------------------------------------------------------------------


class TestSparseSignals:
    def test_sparse_signals_yields_mixed_profile(
        self, conn: sqlite3.Connection, log_root: Path
    ) -> None:
        """A single signal that only supports `industry` produces gaps for
        the other four fields — the ExtractionGap pattern in action."""
        sparse = RawSignals(
            company_name="Tiny Co",
            signals=[
                _signal("Tiny Co is a fintech startup.", url="https://example.com/0"),
            ],
        )
        body = json.dumps(
            {
                "size": {"gap": {"reason": "field_not_in_signals"}},
                "industry": {
                    "values": ["Fintech"],
                    "source_spans": [{"signal_index": 0, "quote": "fintech startup"}],
                },
                "tech_stack": {"gap": {"reason": "field_not_in_signals"}},
                "recent_news": {"gap": {"reason": "field_not_in_signals"}},
                "decision_makers": {"gap": {"reason": "field_not_in_signals"}},
            }
        )
        with patch.object(extractor_mod, "complete", return_value=_llm_result(body)):
            result = run_extractor(conn, raw_signals=sparse, log_root=log_root)
        assert result.status is WorkflowStatus.COMPLETED
        assert result.profile.industry.values == ["Fintech"]
        assert result.profile.size.is_gap
        assert result.profile.gap_count == 4


# ---------------------------------------------------------------------------
# Short-span case (inherited from ADR-003 addendum)
# ---------------------------------------------------------------------------


class TestShortSpanForcesGap:
    """When the upstream cited_text spans are short fragments, the Extractor
    must not over-reach beyond what the span text supports. The substring
    grounding validator is what enforces this: a quote that isn't present in
    the cited signal (verbatim, after normalisation) is downgraded to a gap.
    """

    def test_short_span_overreach_is_caught_and_gapped(
        self, conn: sqlite3.Connection, log_root: Path
    ) -> None:
        # The cited span is a short fragment — three words. The model
        # attempts to extract industry from it but the proposed quote
        # paraphrases beyond what the span actually says.
        short_span_input = RawSignals(
            company_name="Acme Corp",
            signals=[_signal("fintech startup, SF", url="https://example.com/0")],
        )
        body = json.dumps(
            {
                # Over-reach: the model claims the span supports a stack
                # that isn't in the cited fragment.
                "size": {
                    "values": ["~500 employees"],
                    "source_spans": [{"signal_index": 0, "quote": "approximately 500 employees"}],
                },
                # An honest extraction from the same short span — the quote
                # IS present in the cited fragment.
                "industry": {
                    "values": ["Fintech"],
                    "source_spans": [{"signal_index": 0, "quote": "fintech startup"}],
                },
                "tech_stack": {"gap": {"reason": "insufficient_source_span"}},
                "recent_news": {"gap": {"reason": "insufficient_source_span"}},
                "decision_makers": {"gap": {"reason": "insufficient_source_span"}},
            }
        )
        with patch.object(extractor_mod, "complete", return_value=_llm_result(body)):
            result = run_extractor(conn, raw_signals=short_span_input, log_root=log_root)

        # Workflow completed (not failed — gap-downgrade is not an error).
        assert result.status is WorkflowStatus.COMPLETED
        # Over-reach was caught: size becomes a span_not_grounded gap.
        assert result.profile.size.is_gap
        assert result.profile.size.gap is not None
        assert result.profile.size.gap.reason == "span_not_grounded"
        # Honest extraction survives.
        assert result.profile.industry.values == ["Fintech"]
        # The other three fields keep their model-emitted reason.
        assert result.profile.tech_stack.is_gap
        assert result.profile.tech_stack.gap is not None
        assert result.profile.tech_stack.gap.reason == "insufficient_source_span"
        # gap_count: size (downgraded) + tech_stack + recent_news + decision_makers = 4
        assert result.profile.gap_count == 4

    def test_short_span_gap_emitted_by_model_passes_through(
        self, conn: sqlite3.Connection, log_root: Path
    ) -> None:
        """When the model honestly returns a gap on a short span, the gap is
        preserved as-is (the grounding validator only touches extracted fields).
        """
        short_span_input = RawSignals(
            company_name="Acme",
            signals=[_signal("fintech startup, SF", url="https://example.com/0")],
        )
        body = json.dumps(
            {name: {"gap": {"reason": "insufficient_source_span"}} for name in PROFILE_FIELD_NAMES}
        )
        with patch.object(extractor_mod, "complete", return_value=_llm_result(body)):
            result = run_extractor(conn, raw_signals=short_span_input, log_root=log_root)
        assert result.status is WorkflowStatus.COMPLETED
        for name in PROFILE_FIELD_NAMES:
            field = result.profile.field(name)
            assert field.is_gap
            assert field.gap is not None
            # The MODEL's reason is preserved — not overwritten by grounding.
            assert field.gap.reason == "insufficient_source_span"


# ---------------------------------------------------------------------------
# Infrastructure failures
# ---------------------------------------------------------------------------


class TestInfrastructureFailure:
    def test_llm_error_marks_workflow_failed(
        self, conn: sqlite3.Connection, log_root: Path
    ) -> None:
        class APIBoom(RuntimeError):
            pass

        with patch.object(extractor_mod, "complete", side_effect=APIBoom("anthropic 5xx")):
            result = run_extractor(conn, raw_signals=_HAPPY_INPUT, log_root=log_root)

        # No uncaught crash.
        assert result.status is WorkflowStatus.FAILED
        assert result.error_step == AGENT_NAME
        assert "APIBoom" in (result.error_message or "")
        # Empty-but-typed all-gaps profile returned on failure.
        assert result.profile.gap_count == 5

        wf = load_workflow(conn, result.workflow_id)
        assert wf is not None
        assert wf.status is WorkflowStatus.FAILED
        assert wf.finished_at is not None

        steps = load_steps(conn, result.workflow_id)
        assert len(steps) == 1
        assert steps[0].status is StepStatus.FAILED
        assert "APIBoom" in (steps[0].error_message or "")

        events = load_events(conn, result.workflow_id)
        types = [(e.structured_data or {}).get("event_type") for e in events]
        assert "step.failed" in types
        assert "workflow.failed" in types

    def test_malformed_json_marks_workflow_failed(
        self, conn: sqlite3.Connection, log_root: Path
    ) -> None:
        bad_body = "{this is not valid json"
        with patch.object(extractor_mod, "complete", return_value=_llm_result(bad_body)):
            result = run_extractor(conn, raw_signals=_HAPPY_INPUT, log_root=log_root)
        assert result.status is WorkflowStatus.FAILED
        assert "ExtractorParseError" in (result.error_message or "")

    def test_schema_mismatch_marks_workflow_failed(
        self, conn: sqlite3.Connection, log_root: Path
    ) -> None:
        # Missing required fields (only `size`; misses other four).
        bad_body = '{"size": {"gap": {"reason": "x"}}}'
        with patch.object(extractor_mod, "complete", return_value=_llm_result(bad_body)):
            result = run_extractor(conn, raw_signals=_HAPPY_INPUT, log_root=log_root)
        assert result.status is WorkflowStatus.FAILED
        assert "ExtractorParseError" in (result.error_message or "")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCLI:
    def test_run_extractor_happy(self, tmp_path: Path) -> None:
        db = tmp_path / "wf.db"
        cits = [
            Citation(
                cited_text="Acme Corp has approximately 500 employees as of Q2 2026.",
                source_url="https://example.com/0",
                source_title="A",
            ),
            Citation(
                cited_text="Acme Corp is a fintech startup based in San Francisco.",
                source_url="https://example.com/1",
                source_title="B",
            ),
        ]
        researcher_llm = LLMResult(
            content="research text",
            model="claude-haiku-4-5-20251001",
            input_tokens=1000,
            output_tokens=200,
            cost_usd=0.034,
            jsonl_ref="data/logs/2026-05-14/wf-res.jsonl:1",
            stop_reason="end_turn",
            web_searches=2,
            citations=cits,
        )
        extractor_llm = _llm_result(_HAPPY_BODY, cost_usd=0.012)

        runner = CliRunner()
        with (
            patch.object(researcher_mod, "complete", return_value=researcher_llm),
            patch.object(extractor_mod, "complete", return_value=extractor_llm),
        ):
            result = runner.invoke(
                main,
                ["run-extractor", "--db", str(db), "Acme Corp"],
            )
        assert result.exit_code == 0, result.output
        # Both result blocks rendered.
        assert "status: COMPLETED" in result.output
        # Researcher block.
        assert "Automated research signals" in result.output
        # Extractor block.
        assert "Automated extraction" in result.output
        assert "size:" in result.output
        assert "~500 employees" in result.output
        assert "industry:" in result.output

    def test_run_extractor_researcher_failure_exits_nonzero(self, tmp_path: Path) -> None:
        db = tmp_path / "wf.db"

        class APIBoom(RuntimeError):
            pass

        runner = CliRunner()
        with patch.object(researcher_mod, "complete", side_effect=APIBoom("anthropic exploded")):
            result = runner.invoke(
                main,
                ["run-extractor", "--db", str(db), "Acme Corp"],
            )
        assert result.exit_code != 0
        # Researcher failure surfaces; extractor block not present.
        assert "status: FAILED" in result.output
        assert "anthropic exploded" in result.output

    def test_run_extractor_extractor_failure_exits_nonzero(self, tmp_path: Path) -> None:
        db = tmp_path / "wf.db"
        cits = [
            Citation(
                cited_text="Acme is a fintech startup.",
                source_url="https://example.com/0",
                source_title="A",
            )
        ]
        researcher_llm = LLMResult(
            content="ok",
            model="claude-haiku-4-5-20251001",
            input_tokens=100,
            output_tokens=20,
            cost_usd=0.005,
            jsonl_ref="data/logs/2026-05-14/wf-res.jsonl:1",
            stop_reason="end_turn",
            web_searches=1,
            citations=cits,
        )

        class ExtBoom(RuntimeError):
            pass

        runner = CliRunner()
        with (
            patch.object(researcher_mod, "complete", return_value=researcher_llm),
            patch.object(extractor_mod, "complete", side_effect=ExtBoom("sonnet exploded")),
        ):
            result = runner.invoke(
                main,
                ["run-extractor", "--db", str(db), "Acme Corp"],
            )
        assert result.exit_code != 0
        # Researcher succeeded but extractor failed.
        assert "Automated research signals" in result.output  # researcher rendered
        assert "sonnet exploded" in result.output


# ---------------------------------------------------------------------------
# Live smoke — one real Researcher + Extractor end-to-end. Skipped without key.
# ---------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY")
    or os.environ["ANTHROPIC_API_KEY"].startswith("sk-ant-REPLACE"),
    reason="ANTHROPIC_API_KEY not set; live smoke skipped.",
)
def test_live_extractor_round_trip(conn: sqlite3.Connection, log_root: Path) -> None:
    """End-to-end live: Researcher then Extractor against a well-known company.

    Verifies the full habitat round-trip with real network + real API for
    BOTH agents: workflows COMPLETED, steps recorded, events emitted, cost
    > 0, output_refs resolve, CompanyProfile is populated with real source-
    span refs that genuinely substring back into the Researcher's
    RawSignals. ExtractionGaps appear where the signals didn't support a
    field — captured for calibration.
    """
    from agent_habitat.agents.researcher import run_researcher

    start = datetime.now(UTC)
    researcher = run_researcher(conn, company_name="Anthropic", log_root=log_root)
    researcher_duration = (datetime.now(UTC) - start).total_seconds()
    assert researcher.status is WorkflowStatus.COMPLETED, (
        f"Researcher failed: {researcher.error_message}"
    )

    extract_start = datetime.now(UTC)
    extractor = run_extractor(conn, raw_signals=researcher.raw_signals, log_root=log_root)
    extractor_duration = (datetime.now(UTC) - extract_start).total_seconds()

    print(f"\n[live-extractor] researcher_duration_s={researcher_duration:.2f}")
    print(f"[live-extractor] researcher_cost_usd={researcher.cost_usd:.6f}")
    print(f"[live-extractor] researcher_signal_count={researcher.raw_signals.signal_count}")
    print(f"[live-extractor] researcher_source_count={researcher.raw_signals.source_count}")
    print(f"[live-extractor] extractor_duration_s={extractor_duration:.2f}")
    print(f"[live-extractor] extractor_status={extractor.status.value}")
    print(f"[live-extractor] extractor_cost_usd={extractor.cost_usd:.6f}")
    print(f"[live-extractor] extractor_gap_count={extractor.profile.gap_count}/5")
    for name in PROFILE_FIELD_NAMES:
        f = extractor.profile.field(name)
        if f.is_gap:
            assert f.gap is not None
            print(f"[live-extractor] {name}: GAP — {f.gap.reason}")
        else:
            for v in f.values:
                print(f"[live-extractor] {name}: VALUE — {v}")
            for span in f.source_spans:
                preview = span.quote[:120]
                print(f"[live-extractor]   span [signal {span.signal_index}] {preview!r}")

    assert extractor.status is WorkflowStatus.COMPLETED, (
        f"Extractor failed: {extractor.error_message}"
    )
    assert extractor.cost_usd > 0.0

    # Full habitat round-trip.
    wf = load_workflow(conn, extractor.workflow_id)
    assert wf is not None
    assert wf.status is WorkflowStatus.COMPLETED
    assert wf.cost_total_usd > 0.0

    steps = load_steps(conn, extractor.workflow_id)
    assert [s.agent_name for s in steps] == [AGENT_NAME]
    assert steps[0].status is StepStatus.COMPLETED
    assert steps[0].output_ref is not None

    # output_ref resolves to a real JSONL line.
    path_str, _, line_str = steps[0].output_ref.rpartition(":")
    jsonl_path = Path(path_str)
    assert jsonl_path.exists()
    record = json.loads(jsonl_path.read_text(encoding="utf-8").splitlines()[int(line_str) - 1])
    assert record["workflow_id"] == extractor.workflow_id
    assert record["agent_name"] == AGENT_NAME
    assert record["model"] == "claude-sonnet-4-6"

    # Every extracted field's source spans really substring back into the Researcher's signals.
    short_spans = 0
    overreach_gaps = 0
    for name in PROFILE_FIELD_NAMES:
        field = extractor.profile.field(name)
        if field.is_gap:
            assert field.gap is not None
            if field.gap.reason == "span_not_grounded":
                overreach_gaps += 1
            continue
        for span in field.source_spans:
            sig_text = _normalise_for_substring(
                researcher.raw_signals.signals[span.signal_index].text
            )
            quote = _normalise_for_substring(span.quote)
            assert quote in sig_text, (
                f"calibration: {name} span {span.quote!r} does not substring back into "
                f"signal[{span.signal_index}] — grounding chain broken"
            )
            # Calibration: how short were the upstream cited spans?
            if len(researcher.raw_signals.signals[span.signal_index].text) < 200:
                short_spans += 1
    print(f"[live-extractor] short_spans_used (<200 chars): {short_spans}")
    print(f"[live-extractor] overreach_gaps_caught: {overreach_gaps}")
