"""Deterministic tests for the Critic agent — Phase 2 Slice 7.

Covers:
  - Critique + ClaimVerdict models: validation, frozen, extra=forbid,
    invariants between passed / failed_hop / classification / explanation.
  - Mechanical substring check (Mode 1): every hop's all-pass and per-hop
    failure paths; normaliser-identity pin (Critic imports
    `_normalise_for_substring` from Extractor — same function object).
  - LLM-judged Mode 2: per-failure Haiku call shape + parse + verdict.
  - Layer A `critic_node` purity: no DB, no run_step, exceptions propagate.
  - Layer B `run_critic`: full habitat round-trip; infra failure.
  - Drafter `prior_critique` parameter: when supplied prepends retry
    preface; when omitted behaves identically to the Slice 5 baseline.
  - Orchestrator graph integration: drafter → critic → END on pass;
    drafter → critic → drafter (retry) → critic → END on first-failure-
    then-pass; drafter → critic → drafter → critic → terminate_with_critic_failure
    on persistent failure; fabrication_retries increments correctly.
  - RED-TEAM SMOKE: synthetic Drafts replicating Slice 5's documented
    failure patterns ("Anthropic PBC" → "Anthropic", "teamed with" →
    "partnership with") plus genuine fabrications. Verifies the
    mechanical chain catches every failure and Mode 2 classifies each.

Live smoke (`@pytest.mark.live`) at the bottom: full chain with real Opus,
guarded by ANTHROPIC_API_KEY.
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
from langgraph.checkpoint.sqlite import SqliteSaver
from pydantic import ValidationError

from agent_habitat.agents import critic as critic_mod
from agent_habitat.agents import drafter as drafter_mod
from agent_habitat.agents import extractor as extractor_mod
from agent_habitat.agents.critic import (
    AGENT_NAME,
    WORKFLOW_TYPE,
    ClaimVerdict,
    CriticParseError,
    Critique,
    _find_dimension,
    _find_grounding_span,
    _parse_failure_judgement,
    _signal_traces_to_citation,
    _strip_code_fence,
    _walk_claim_chain,
    critic_node,
    run_critic,
)
from agent_habitat.agents.extractor import _normalise_for_substring as ext_norm
from agent_habitat.agents.models import (
    CompanyProfile,
    DimensionScore,
    Draft,
    DraftClaim,
    ExtractionGap,
    ProfileField,
    RawSignals,
    ScoredCompany,
    Signal,
    SourceSpan,
)
from agent_habitat.llm import LLMResult
from agent_habitat.observability import EventType
from agent_habitat.orchestration.crew_graph import (
    STEP_INDEX_CRITIC,
    STEP_INDEX_CRITIC_RETRY,
    STEP_INDEX_DRAFTER,
    STEP_INDEX_DRAFTER_RETRY,
    run_crew,
)
from agent_habitat.orchestration.crew_state import (
    TERMINATE_REASON_CRITIC_FAILURE,
)
from agent_habitat.scoring import RubricConfig
from agent_habitat.scoring.rubric import DimensionConfig
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


def _llm_result(
    content: str, *, cost_usd: float = 0.001, model: str = "claude-haiku-4-5-20251001"
) -> LLMResult:
    """Build an LLMResult fixture mimicking a Haiku Mode-2 response."""
    return LLMResult(
        content=content,
        model=model,
        input_tokens=150,
        output_tokens=60,
        cost_usd=cost_usd,
        jsonl_ref="data/logs/2026-05-15/wf-critic.jsonl:1",
        stop_reason="end_turn",
        web_searches=0,
        citations=[],
    )


# ---------------------------------------------------------------------------
# Fixture factory — a five-hop-clean evidence chain. Tests perturb individual
# hops to exercise the per-hop failure modes.
# ---------------------------------------------------------------------------


def _signal(
    *,
    text: str = "Acme Corp raised a $50M Series B led by Sequoia in 2026.",
    source_url: str = "https://news.example.com/acme-series-b",
) -> Signal:
    return Signal(
        text=text,
        source_url=source_url,
        source_title="Acme funding",
        retrieved_at=datetime(2026, 5, 15, tzinfo=UTC),
    )


def _raw_signals(*signals: Signal, company_name: str = "Acme Corp") -> RawSignals:
    return RawSignals(company_name=company_name, signals=list(signals))


def _profile_field(
    *,
    values: list[str],
    spans: list[tuple[int, str]],
) -> ProfileField:
    return ProfileField(
        values=values,
        source_spans=[SourceSpan(signal_index=i, quote=q) for i, q in spans],
    )


def _profile_gap(reason: str = "no_signals") -> ProfileField:
    return ProfileField(gap=ExtractionGap(reason=reason))


def _profile(
    *,
    company_name: str = "Acme Corp",
    industry: ProfileField | None = None,
    recent_news: ProfileField | None = None,
    tech_stack: ProfileField | None = None,
    size: ProfileField | None = None,
    decision_makers: ProfileField | None = None,
) -> CompanyProfile:
    return CompanyProfile(
        company_name=company_name,
        size=size or _profile_gap(),
        industry=industry or _profile_gap(),
        tech_stack=tech_stack or _profile_gap(),
        recent_news=recent_news or _profile_gap(),
        decision_makers=decision_makers or _profile_gap(),
    )


def _dim(
    *,
    field: str,
    grounded_quote: str | None,
    score: float | None = 4.0,
    weight: float = 0.25,
    reasoning: str = "fixture-scored.",
) -> DimensionScore:
    return DimensionScore(
        field=field,
        weight=weight,
        score=score,
        grounded_quote=grounded_quote,
        reasoning=reasoning,
    )


def _scored_company(
    *,
    dimensions: list[DimensionScore],
    company_name: str = "Acme Corp",
    score: float | None = 80.0,
) -> ScoredCompany:
    return ScoredCompany(
        company_name=company_name,
        score=score,
        coverage=0.8,
        floor=50.0,
        min_coverage=0.0,
        passed_floor=score is not None and score >= 50.0,
        passed_coverage=True,
        tier="A",
        gated_by=None,
        dimensions=dimensions,
    )


def _build_clean_evidence() -> tuple[Draft, ScoredCompany, CompanyProfile, RawSignals]:
    """A five-hop-clean chain: claim ⊆ grounded_quote ⊆ source_span ⊆ Signal.text,
    and the Signal traces to a citation (non-empty source_url + text).
    """
    signal = _signal(
        text="Acme Corp raised a $50M Series B led by Sequoia in 2026.",
    )
    profile = _profile(
        recent_news=_profile_field(
            values=["raised a $50M Series B"],
            spans=[(0, "Acme Corp raised a $50M Series B led by Sequoia in 2026.")],
        ),
        industry=_profile_field(
            values=["fintech"],
            spans=[(0, "Acme Corp raised a $50M Series B led by Sequoia in 2026.")],
        ),
    )
    scored = _scored_company(
        dimensions=[
            _dim(field="size", grounded_quote=None, score=None),
            _dim(field="industry", grounded_quote="Acme Corp", weight=0.2),
            _dim(field="tech_stack", grounded_quote=None, score=None),
            _dim(field="recent_news", grounded_quote="raised a $50M Series B", weight=0.4),
            _dim(field="decision_makers", grounded_quote=None, score=None),
        ],
    )
    prose = (
        "Hi team,\n\n"
        "Saw that Acme Corp closed a major round — raised a $50M Series B is "
        "exactly the kind of growth moment my team supports.\n\n"
        "Happy to chat."
    )
    draft = Draft(
        company_name="Acme Corp",
        prose=prose,
        claims=[
            DraftClaim(text="raised a $50M Series B", supporting_dimension="recent_news"),
            DraftClaim(text="Acme Corp", supporting_dimension="industry"),
        ],
    )
    return draft, scored, profile, _raw_signals(signal)


# ---------------------------------------------------------------------------
# Critique + ClaimVerdict models
# ---------------------------------------------------------------------------


class TestClaimVerdictModel:
    def test_passed_verdict_round_trips(self) -> None:
        v = ClaimVerdict(
            claim_text="x",
            supporting_dimension="industry",
            passed=True,
            classification="passed",
        )
        assert ClaimVerdict.model_validate(v.model_dump()) == v

    def test_failed_verdict_round_trips(self) -> None:
        v = ClaimVerdict(
            claim_text="x",
            supporting_dimension="industry",
            passed=False,
            failed_hop="claim_in_grounded_quote",
            explanation="paraphrase, not substring",
            classification="fixable_paraphrase",
            upstream_quote="Acme Corp",
        )
        assert ClaimVerdict.model_validate(v.model_dump()) == v

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ClaimVerdict.model_validate(
                {
                    "claim_text": "x",
                    "supporting_dimension": "industry",
                    "passed": True,
                    "classification": "passed",
                    "rogue": 1,
                }
            )

    def test_frozen(self) -> None:
        v = ClaimVerdict(
            claim_text="x", supporting_dimension="industry", passed=True, classification="passed"
        )
        with pytest.raises(ValidationError):
            v.passed = False  # type: ignore[misc]

    def test_passed_requires_passed_classification(self) -> None:
        with pytest.raises(ValidationError):
            ClaimVerdict(
                claim_text="x",
                supporting_dimension="industry",
                passed=True,
                classification="fixable_paraphrase",
            )

    def test_passed_forbids_failed_hop(self) -> None:
        with pytest.raises(ValidationError):
            ClaimVerdict(
                claim_text="x",
                supporting_dimension="industry",
                passed=True,
                failed_hop="claim_in_prose",
                classification="passed",
            )

    def test_passed_forbids_explanation(self) -> None:
        with pytest.raises(ValidationError):
            ClaimVerdict(
                claim_text="x",
                supporting_dimension="industry",
                passed=True,
                explanation="should be empty",
                classification="passed",
            )

    def test_failed_requires_failed_hop(self) -> None:
        with pytest.raises(ValidationError):
            ClaimVerdict(
                claim_text="x",
                supporting_dimension="industry",
                passed=False,
                explanation="x",
                classification="fixable_paraphrase",
            )

    def test_failed_requires_non_passed_classification(self) -> None:
        with pytest.raises(ValidationError):
            ClaimVerdict(
                claim_text="x",
                supporting_dimension="industry",
                passed=False,
                failed_hop="claim_in_prose",
                explanation="x",
                classification="passed",
            )

    def test_failed_requires_non_empty_explanation(self) -> None:
        with pytest.raises(ValidationError):
            ClaimVerdict(
                claim_text="x",
                supporting_dimension="industry",
                passed=False,
                failed_hop="claim_in_prose",
                explanation="",
                classification="fixable_paraphrase",
            )

    def test_unknown_supporting_dimension_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ClaimVerdict(
                claim_text="x",
                supporting_dimension="not_a_field",
                passed=True,
                classification="passed",
            )

    def test_unknown_failed_hop_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ClaimVerdict(
                claim_text="x",
                supporting_dimension="industry",
                passed=False,
                failed_hop="not_a_hop",
                explanation="x",
                classification="fabricated",
            )

    def test_unknown_classification_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ClaimVerdict(
                claim_text="x",
                supporting_dimension="industry",
                passed=True,
                classification="not_a_classification",
            )


class TestCritiqueModel:
    def test_round_trip_empty_passes(self) -> None:
        c = Critique(company_name="Acme", passed=True, verdicts=[])
        c2 = Critique.model_validate_json(c.model_dump_json())
        assert c2 == c
        assert c.claim_count == 0
        assert c.failure_count == 0

    def test_passed_matches_verdicts(self) -> None:
        v_pass = ClaimVerdict(
            claim_text="x", supporting_dimension="industry", passed=True, classification="passed"
        )
        c = Critique(company_name="Acme", passed=True, verdicts=[v_pass])
        assert c.claim_count == 1
        assert c.failure_count == 0

    def test_drift_rejected(self) -> None:
        v_fail = ClaimVerdict(
            claim_text="x",
            supporting_dimension="industry",
            passed=False,
            failed_hop="claim_in_prose",
            explanation="x",
            classification="fabricated",
        )
        with pytest.raises(ValidationError):
            Critique(company_name="Acme", passed=True, verdicts=[v_fail])

    def test_all_fabricated_true(self) -> None:
        v = ClaimVerdict(
            claim_text="x",
            supporting_dimension="industry",
            passed=False,
            failed_hop="claim_in_prose",
            explanation="x",
            classification="fabricated",
        )
        c = Critique(company_name="Acme", passed=False, verdicts=[v])
        assert c.all_fabricated is True

    def test_all_fabricated_false_when_mixed(self) -> None:
        v1 = ClaimVerdict(
            claim_text="x",
            supporting_dimension="industry",
            passed=False,
            failed_hop="claim_in_prose",
            explanation="x",
            classification="fabricated",
        )
        v2 = ClaimVerdict(
            claim_text="y",
            supporting_dimension="industry",
            passed=False,
            failed_hop="claim_in_prose",
            explanation="y",
            classification="fixable_paraphrase",
        )
        c = Critique(company_name="Acme", passed=False, verdicts=[v1, v2])
        assert c.all_fabricated is False

    def test_all_fabricated_false_when_no_failures(self) -> None:
        c = Critique(company_name="Acme", passed=True, verdicts=[])
        assert c.all_fabricated is False

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Critique.model_validate(
                {"company_name": "Acme", "passed": True, "verdicts": [], "rogue": 1}
            )

    def test_frozen(self) -> None:
        c = Critique(company_name="Acme", passed=True, verdicts=[])
        with pytest.raises(ValidationError):
            c.passed = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Normaliser identity pin — Critic must reuse Extractor's _normalise_for_substring
# ---------------------------------------------------------------------------


class TestNormaliserIdentity:
    def test_critic_imports_extractor_normaliser(self) -> None:
        # `is` test: the function the Critic uses must be the SAME object the
        # Extractor and Scorer use — no parallel normaliser anywhere.
        from agent_habitat.agents.critic import _normalise_for_substring as critic_norm

        assert critic_norm is ext_norm

    def test_scorer_normaliser_pinned_for_completeness(self) -> None:
        # Belt-and-braces — Slice 4's reuse should still hold.
        from agent_habitat.agents.scorer import _normalise_for_substring as scorer_norm

        assert scorer_norm is ext_norm


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestStripCodeFence:
    def test_no_fence_passthrough(self) -> None:
        assert _strip_code_fence('{"x": 1}') == '{"x": 1}'

    def test_strips_json_fence(self) -> None:
        assert _strip_code_fence('```json\n{"x": 1}\n```') == '{"x": 1}'

    def test_strips_bare_fence(self) -> None:
        assert _strip_code_fence('```\n{"x": 1}\n```') == '{"x": 1}'


class TestParseFailureJudgement:
    def test_happy_fixable(self) -> None:
        body = json.dumps({"classification": "fixable_paraphrase", "explanation": "paraphrase"})
        j = _parse_failure_judgement(body)
        assert j.classification == "fixable_paraphrase"
        assert j.explanation == "paraphrase"

    def test_happy_fabricated(self) -> None:
        body = json.dumps({"classification": "fabricated", "explanation": "invented"})
        j = _parse_failure_judgement(body)
        assert j.classification == "fabricated"

    def test_unknown_classification_rejected(self) -> None:
        body = json.dumps({"classification": "ambiguous", "explanation": "x"})
        with pytest.raises(CriticParseError):
            _parse_failure_judgement(body)

    def test_invalid_json_rejected(self) -> None:
        with pytest.raises(CriticParseError):
            _parse_failure_judgement("not json at all")

    def test_extra_field_rejected(self) -> None:
        body = json.dumps({"classification": "fabricated", "explanation": "x", "rogue": 1})
        with pytest.raises(CriticParseError):
            _parse_failure_judgement(body)

    def test_empty_explanation_rejected(self) -> None:
        body = json.dumps({"classification": "fabricated", "explanation": ""})
        with pytest.raises(CriticParseError):
            _parse_failure_judgement(body)


class TestSignalTracesToCitation:
    def test_well_formed_signal_passes(self) -> None:
        assert _signal_traces_to_citation(_signal()) is True

    def test_empty_source_url_fails(self) -> None:
        sig = Signal(
            text="some prose",
            source_url="",
            source_title=None,
            retrieved_at=datetime(2026, 5, 15, tzinfo=UTC),
        )
        assert _signal_traces_to_citation(sig) is False

    def test_whitespace_only_url_fails(self) -> None:
        sig = Signal(
            text="some prose",
            source_url="   ",
            source_title=None,
            retrieved_at=datetime(2026, 5, 15, tzinfo=UTC),
        )
        assert _signal_traces_to_citation(sig) is False


# ---------------------------------------------------------------------------
# Mechanical substring check — _walk_claim_chain
# ---------------------------------------------------------------------------


class TestChainWalkAllPass:
    def test_happy_chain_passes(self) -> None:
        draft, scored, profile, raw = _build_clean_evidence()
        for claim in draft.claims:
            walk = _walk_claim_chain(
                claim=claim,
                draft=draft,
                scored_company=scored,
                profile=profile,
                raw_signals=raw,
            )
            assert walk.passed is True
            assert walk.failed_hop is None
            assert walk.upstream_quote is None


class TestChainWalkPerHop:
    def test_hop1_claim_not_in_prose(self) -> None:
        # We can't construct a Draft with claim.text outside prose (the Draft
        # model_validator rejects it), but we can build the Draft via
        # model_validate from a dict that bypasses... actually no, the
        # validator fires there too. Instead, construct a parallel claim
        # whose text differs after prose tampering by passing a hand-built
        # DraftClaim and a prose that won't contain it. Easier: test
        # _walk_claim_chain with a hand-crafted DraftClaim whose text isn't
        # in the draft.prose, bypassing Draft's invariant for the test.
        # The cleanest path: use a CRAFTED claim that the walk treats as
        # input — we call _walk_claim_chain with a claim that doesn't
        # substring-match the prose of a Draft we constructed.
        draft, scored, profile, raw = _build_clean_evidence()
        rogue_claim = DraftClaim(text="raised a $50M Series B", supporting_dimension="recent_news")
        # Construct a Draft with the SAME claim but DIFFERENT prose (the
        # validator requires claim.text ⊆ prose so we manually use a Draft
        # whose prose does not contain the claim).
        # Use a minimal Draft whose prose contains the substring "raised a
        # $50M Series B" but a HAND-CRAFTED rogue claim that targets a
        # phrase NOT in prose — for this we need a separate construct.
        # Approach: build a Draft whose prose deliberately omits the claim
        # then verify the walk catches it. Pydantic blocks this at the
        # Draft level, so we test via the helper with a custom prose-bare
        # draft string by passing claim_text in the call manually.
        # The simplest way: build a Draft whose prose is "foo bar" and
        # whose claim is "foo" (passes invariant), THEN before calling the
        # walk, we mutate (frozen — can't). So we craft a parallel Draft.
        bare_draft = Draft(
            company_name="Acme Corp",
            prose="foo bar baz",
            claims=[DraftClaim(text="foo", supporting_dimension="recent_news")],
        )
        walk = _walk_claim_chain(
            claim=rogue_claim,  # not a substring of bare_draft.prose
            draft=bare_draft,
            scored_company=scored,
            profile=profile,
            raw_signals=raw,
        )
        assert walk.passed is False
        assert walk.failed_hop == "claim_in_prose"

    def test_hop2_claim_not_in_grounded_quote(self) -> None:
        # The Slice 5 documented pattern: "Anthropic" ⊄ "Anthropic PBC ..."
        # after the Drafter drops "PBC".
        signal = _signal(
            text="Anthropic PBC is in early talks with investors.",
            source_url="https://news.example.com/anthropic",
        )
        profile = _profile(
            recent_news=_profile_field(
                values=["funding talks"],
                spans=[(0, "Anthropic PBC is in early talks with investors.")],
            )
        )
        scored = _scored_company(
            dimensions=[
                _dim(
                    field="recent_news",
                    grounded_quote="Anthropic PBC is in early talks",
                    weight=1.0,
                )
            ]
        )
        prose = "Anthropic is in early talks with investors — let's chat."
        draft = Draft(
            company_name="Anthropic",
            prose=prose,
            claims=[
                DraftClaim(text="Anthropic is in early talks", supporting_dimension="recent_news")
            ],
        )
        walk = _walk_claim_chain(
            claim=draft.claims[0],
            draft=draft,
            scored_company=scored,
            profile=profile,
            raw_signals=_raw_signals(signal),
        )
        assert walk.passed is False
        assert walk.failed_hop == "claim_in_grounded_quote"
        assert walk.upstream_quote == "Anthropic PBC is in early talks"

    def test_hop3_grounded_quote_not_in_source_span(self) -> None:
        # Grounded_quote refers to something NOT present in the profile field's
        # source_spans — the dimension over-reached beyond the Scorer's
        # grounding contract (this should be impossible if the Scorer's
        # _grounded_in_field ran, but the Critic re-verifies as the auditable
        # cold-storage check).
        signal = _signal(text="Acme is a fintech.")
        profile = _profile(
            industry=_profile_field(values=["fintech"], spans=[(0, "Acme is a fintech.")])
        )
        scored = _scored_company(
            dimensions=[
                _dim(
                    field="industry",
                    grounded_quote="some text not in any source_span",
                    weight=1.0,
                )
            ]
        )
        # The claim DOES substring-match its grounded_quote (hop 2 passes).
        prose = "Acme — some text not in any source_span — interesting."
        draft = Draft(
            company_name="Acme Corp",
            prose=prose,
            claims=[
                DraftClaim(text="some text not in any source_span", supporting_dimension="industry")
            ],
        )
        walk = _walk_claim_chain(
            claim=draft.claims[0],
            draft=draft,
            scored_company=scored,
            profile=profile,
            raw_signals=_raw_signals(signal),
        )
        assert walk.passed is False
        assert walk.failed_hop == "grounded_quote_in_source_span"

    def test_hop4_source_span_not_in_signal(self) -> None:
        # The source_span quote is NOT a substring of the cited Signal.text.
        # Construct: source_span.signal_index points at signals[0], but
        # signals[0].text does not contain the quote.
        signal = _signal(text="Different signal content entirely.")
        profile = _profile(
            industry=_profile_field(
                values=["fintech"],
                # span CLAIMS to be from signal 0 but isn't a substring of it.
                # Pydantic's ProfileField does NOT enforce this (the
                # Extractor's _ground_field validator runs OUTSIDE the model).
                spans=[(0, "Acme is a fintech.")],
            )
        )
        scored = _scored_company(
            dimensions=[_dim(field="industry", grounded_quote="Acme is a fintech.", weight=1.0)]
        )
        prose = "Saw that Acme is a fintech. — let's talk."
        draft = Draft(
            company_name="Acme Corp",
            prose=prose,
            claims=[DraftClaim(text="Acme is a fintech.", supporting_dimension="industry")],
        )
        walk = _walk_claim_chain(
            claim=draft.claims[0],
            draft=draft,
            scored_company=scored,
            profile=profile,
            raw_signals=_raw_signals(signal),
        )
        assert walk.passed is False
        assert walk.failed_hop == "source_span_in_signal"

    def test_hop5_signal_does_not_trace_to_citation(self) -> None:
        # Signal has empty source_url — the Researcher contract marker is
        # broken. Hops 1-4 pass; hop 5 catches the missing citation origin.
        naked_signal = Signal(
            text="Acme is a fintech.",
            source_url="",  # naked — does not trace to a web_search citation
            source_title=None,
            retrieved_at=datetime(2026, 5, 15, tzinfo=UTC),
        )
        profile = _profile(
            industry=_profile_field(values=["fintech"], spans=[(0, "Acme is a fintech.")])
        )
        scored = _scored_company(
            dimensions=[_dim(field="industry", grounded_quote="Acme is a fintech.", weight=1.0)]
        )
        prose = "Saw that Acme is a fintech. — let's talk."
        draft = Draft(
            company_name="Acme Corp",
            prose=prose,
            claims=[DraftClaim(text="Acme is a fintech.", supporting_dimension="industry")],
        )
        walk = _walk_claim_chain(
            claim=draft.claims[0],
            draft=draft,
            scored_company=scored,
            profile=profile,
            raw_signals=_raw_signals(naked_signal),
        )
        assert walk.passed is False
        assert walk.failed_hop == "signal_traces_to_citation"


class TestChainWalkEdgeCases:
    def test_dimension_excluded_fails_at_hop2(self) -> None:
        signal = _signal(text="content")
        profile = _profile(industry=_profile_field(values=["x"], spans=[(0, "content")]))
        scored = _scored_company(
            dimensions=[_dim(field="industry", grounded_quote=None, score=None, weight=1.0)]
        )
        prose = "we are content together"
        # supporting_dimension references an excluded dim — hop 2 fails since
        # there is no grounded_quote to substring-match against.
        draft = Draft(
            company_name="Acme",
            prose=prose,
            claims=[DraftClaim(text="content", supporting_dimension="industry")],
        )
        walk = _walk_claim_chain(
            claim=draft.claims[0],
            draft=draft,
            scored_company=scored,
            profile=profile,
            raw_signals=_raw_signals(signal),
        )
        assert walk.passed is False
        assert walk.failed_hop == "claim_in_grounded_quote"
        assert walk.upstream_quote is None

    def test_normalisation_collapses_whitespace_and_case(self) -> None:
        # The chain must use the same normalisation across hops.
        signal = _signal(text="ACME corp raised A $50m series B.")
        profile = _profile(
            recent_news=_profile_field(
                values=["funding"],
                spans=[(0, "ACME corp raised A $50m series B.")],
            )
        )
        scored = _scored_company(
            dimensions=[
                _dim(
                    field="recent_news",
                    grounded_quote="acme corp   raised  a  $50M\nSeries B",
                    weight=1.0,
                )
            ]
        )
        prose = "Acme    corp raised a $50M Series B — let's chat."
        draft = Draft(
            company_name="Acme Corp",
            prose=prose,
            claims=[
                DraftClaim(
                    text="acme corp RAISED a $50M Series B", supporting_dimension="recent_news"
                )
            ],
        )
        walk = _walk_claim_chain(
            claim=draft.claims[0],
            draft=draft,
            scored_company=scored,
            profile=profile,
            raw_signals=_raw_signals(signal),
        )
        assert walk.passed is True


# ---------------------------------------------------------------------------
# Find-dimension / find-grounding-span helpers
# ---------------------------------------------------------------------------


class TestFindDimension:
    def test_returns_dim_when_present(self) -> None:
        d = _dim(field="industry", grounded_quote="x", weight=1.0)
        sc = _scored_company(dimensions=[d])
        assert _find_dimension(sc, "industry") is d

    def test_returns_none_when_absent(self) -> None:
        sc = _scored_company(dimensions=[_dim(field="industry", grounded_quote="x")])
        assert _find_dimension(sc, "tech_stack") is None


class TestFindGroundingSpan:
    def test_returns_span_when_present(self) -> None:
        pf = _profile_field(values=["v"], spans=[(0, "some long quote here")])
        span = _find_grounding_span("some long quote", pf)
        assert span is not None
        assert span.signal_index == 0

    def test_returns_none_when_absent(self) -> None:
        pf = _profile_field(values=["v"], spans=[(0, "some long quote here")])
        assert _find_grounding_span("not present", pf) is None

    def test_gap_field_returns_none(self) -> None:
        pf = _profile_gap()
        assert _find_grounding_span("anything", pf) is None


# ---------------------------------------------------------------------------
# Layer A — critic_node
# ---------------------------------------------------------------------------


class TestCriticNodeAllPass:
    def test_no_llm_call_on_happy_path(self, log_root: Path) -> None:
        draft, scored, profile, raw = _build_clean_evidence()
        with patch.object(critic_mod, "complete") as p:
            out = critic_node(
                draft=draft,
                scored_company=scored,
                profile=profile,
                raw_signals=raw,
                workflow_id="wf-test",
                log_root=log_root,
            )
        assert out.critique.passed is True
        assert out.critique.failure_count == 0
        assert out.cost_usd == 0.0
        assert out.output_ref is None
        assert p.call_count == 0  # MODE 1 only — no Haiku call.

    def test_projection_on_pass(self) -> None:
        draft, scored, profile, raw = _build_clean_evidence()
        with patch.object(critic_mod, "complete"):
            out = critic_node(
                draft=draft,
                scored_company=scored,
                profile=profile,
                raw_signals=raw,
                workflow_id="wf-test",
            )
        assert out.structured_data["passed"] is True
        assert out.structured_data["mode_2_calls"] == 0
        assert out.structured_data["failure_count"] == 0
        assert out.structured_data["all_fabricated"] is False


class TestCriticNodeOnFailure:
    def test_mode_2_called_per_failure(self, log_root: Path) -> None:
        # Construct a Draft with two claims, one passes (recent_news) and one
        # fails hop 2 (industry — claim doesn't substring-match grounded_quote).
        signal = _signal(text="Acme Corp raised a $50M Series B.")
        profile = _profile(
            recent_news=_profile_field(
                values=["funding"],
                spans=[(0, "Acme Corp raised a $50M Series B.")],
            ),
            industry=_profile_field(
                values=["fintech"], spans=[(0, "Acme Corp raised a $50M Series B.")]
            ),
        )
        scored = _scored_company(
            dimensions=[
                _dim(field="industry", grounded_quote="Acme Corp", weight=0.5),
                _dim(field="recent_news", grounded_quote="raised a $50M Series B", weight=0.5),
            ]
        )
        prose = "Acme just closed: raised a $50M Series B; love what AcmeBrand is building."
        draft = Draft(
            company_name="Acme Corp",
            prose=prose,
            claims=[
                DraftClaim(text="raised a $50M Series B", supporting_dimension="recent_news"),
                # Hop 2 will fail: "AcmeBrand" ⊄ "Acme Corp" (grounded_quote).
                DraftClaim(text="AcmeBrand", supporting_dimension="industry"),
            ],
        )
        mode_2_body = json.dumps(
            {
                "classification": "fabricated",
                "explanation": "Claim references a brand name not present in upstream evidence.",
            }
        )
        with patch.object(critic_mod, "complete", return_value=_llm_result(mode_2_body)) as p:
            out = critic_node(
                draft=draft,
                scored_company=scored,
                profile=profile,
                raw_signals=_raw_signals(signal),
                workflow_id="wf-test",
                log_root=log_root,
            )
        assert p.call_count == 1  # exactly one Mode 2 call for the failed claim.
        assert out.critique.passed is False
        assert out.critique.claim_count == 2
        assert out.critique.failure_count == 1
        # The failed verdict has the right shape.
        failed = next(v for v in out.critique.verdicts if not v.passed)
        assert failed.failed_hop == "claim_in_grounded_quote"
        assert failed.classification == "fabricated"
        assert "Acme Corp" == failed.upstream_quote

    def test_mode_2_propagates_parse_error(self, log_root: Path) -> None:
        draft, scored, profile, raw = _build_clean_evidence()
        # Force a failure by feeding a claim that won't match grounded_quote.
        rogue = DraftClaim(text="raised a $50M Series B", supporting_dimension="recent_news")
        bare_prose = "raised a $50M Series B last week."
        bare_draft = Draft(company_name="Acme Corp", prose=bare_prose, claims=[rogue])
        # The dimension's grounded_quote is "raised a $50M Series B", so the
        # claim WILL match hop 2. We instead break hop 4 by giving an
        # un-matched signal.
        bad_signal = _signal(text="completely different text.")
        with patch.object(critic_mod, "complete", return_value=_llm_result("not valid json")):
            with pytest.raises(CriticParseError):
                critic_node(
                    draft=bare_draft,
                    scored_company=scored,
                    profile=profile,
                    raw_signals=_raw_signals(bad_signal),
                    workflow_id="wf-test",
                    log_root=log_root,
                )


class TestCriticNodePurity:
    def test_no_db_touched_on_happy_path(self) -> None:
        # Calling critic_node WITHOUT a sqlite3.Connection works — Layer A is
        # DB-pure. Slice 6's test_node_pure.py is the broader purity contract.
        draft, scored, profile, raw = _build_clean_evidence()
        with patch.object(critic_mod, "complete"):
            out = critic_node(
                draft=draft,
                scored_company=scored,
                profile=profile,
                raw_signals=raw,
                workflow_id="wf-pure",
            )
        assert out.critique.passed is True


# ---------------------------------------------------------------------------
# Layer B — run_critic
# ---------------------------------------------------------------------------


class TestRunCritic:
    def test_happy_round_trip(self, conn: sqlite3.Connection, log_root: Path) -> None:
        draft, scored, profile, raw = _build_clean_evidence()
        with patch.object(critic_mod, "complete") as p:
            result = run_critic(
                conn,
                draft=draft,
                scored_company=scored,
                profile=profile,
                raw_signals=raw,
                workflow_id="wf-critic-happy",
                log_root=log_root,
            )
        assert result.status is WorkflowStatus.COMPLETED
        assert result.critique is not None
        assert result.critique.passed is True
        assert result.cost_usd == 0.0
        assert p.call_count == 0

        wf = load_workflow(conn, "wf-critic-happy")
        assert wf is not None
        assert wf.status is WorkflowStatus.COMPLETED
        assert wf.workflow_type == WORKFLOW_TYPE

        steps = load_steps(conn, "wf-critic-happy")
        assert len(steps) == 1
        assert steps[0].agent_name == AGENT_NAME
        assert steps[0].status is StepStatus.COMPLETED
        assert steps[0].cost_usd == 0.0

    def test_step_completed_projection(self, conn: sqlite3.Connection, log_root: Path) -> None:
        draft, scored, profile, raw = _build_clean_evidence()
        with patch.object(critic_mod, "complete"):
            run_critic(
                conn,
                draft=draft,
                scored_company=scored,
                profile=profile,
                raw_signals=raw,
                workflow_id="wf-critic-proj",
                log_root=log_root,
            )
        events = load_events(conn, "wf-critic-proj")
        step_completed = [
            e
            for e in events
            if e.structured_data.get("event_type") == EventType.STEP_COMPLETED.value
        ]
        assert len(step_completed) == 1
        sd = step_completed[0].structured_data
        assert sd["passed"] is True
        assert sd["claim_count"] == 2
        assert sd["failure_count"] == 0
        assert sd["mode_2_calls"] == 0

    def test_fabrication_event_emitted_on_failure(
        self, conn: sqlite3.Connection, log_root: Path
    ) -> None:
        # Force a hop-2 failure to trigger the agent.fabrication_detected event.
        signal = _signal(text="Anthropic PBC is in early talks with investors.")
        profile = _profile(
            recent_news=_profile_field(
                values=["funding talks"],
                spans=[(0, "Anthropic PBC is in early talks with investors.")],
            )
        )
        scored = _scored_company(
            dimensions=[
                _dim(
                    field="recent_news",
                    grounded_quote="Anthropic PBC is in early talks",
                    weight=1.0,
                )
            ]
        )
        prose = "Anthropic is in early talks with investors — happy to chat."
        draft = Draft(
            company_name="Anthropic",
            prose=prose,
            claims=[
                DraftClaim(text="Anthropic is in early talks", supporting_dimension="recent_news")
            ],
        )
        mode_2_body = json.dumps(
            {
                "classification": "fixable_paraphrase",
                "explanation": "Dropped 'PBC' corporate suffix; embed verbatim quote on retry.",
            }
        )
        with patch.object(critic_mod, "complete", return_value=_llm_result(mode_2_body)):
            result = run_critic(
                conn,
                draft=draft,
                scored_company=scored,
                profile=profile,
                raw_signals=_raw_signals(signal),
                workflow_id="wf-critic-fail",
                log_root=log_root,
            )
        assert result.status is WorkflowStatus.COMPLETED
        assert result.critique is not None
        assert result.critique.passed is False
        events = load_events(conn, "wf-critic-fail")
        fab_events = [
            e
            for e in events
            if e.structured_data.get("event_type") == EventType.FABRICATION_DETECTED.value
        ]
        assert len(fab_events) == 1
        sd = fab_events[0].structured_data
        assert sd["failure_count"] == 1
        assert "claim_in_grounded_quote" in sd["failed_hops"]

    def test_infra_failure_finalises_failed(self, conn: sqlite3.Connection, log_root: Path) -> None:
        # Force the Mode-2 call by giving a failing chain, then make complete()
        # raise.
        signal = _signal(text="Anthropic PBC is in early talks.")
        profile = _profile(
            recent_news=_profile_field(
                values=["x"],
                spans=[(0, "Anthropic PBC is in early talks.")],
            )
        )
        scored = _scored_company(
            dimensions=[
                _dim(
                    field="recent_news",
                    grounded_quote="Anthropic PBC is in early talks",
                    weight=1.0,
                )
            ]
        )
        prose = "Anthropic is in early talks — chat?"
        draft = Draft(
            company_name="Anthropic",
            prose=prose,
            claims=[
                DraftClaim(text="Anthropic is in early talks", supporting_dimension="recent_news")
            ],
        )
        with patch.object(critic_mod, "complete", side_effect=RuntimeError("API 500")):
            result = run_critic(
                conn,
                draft=draft,
                scored_company=scored,
                profile=profile,
                raw_signals=_raw_signals(signal),
                workflow_id="wf-critic-infra-fail",
                log_root=log_root,
            )
        assert result.status is WorkflowStatus.FAILED
        assert result.critique is None
        assert result.error_step == AGENT_NAME
        assert result.error_message is not None
        assert "API 500" in result.error_message


# ---------------------------------------------------------------------------
# Drafter prior_critique behaviour
# ---------------------------------------------------------------------------


def _drafter_llm_body(
    prose: str = "Hello there. Fintech focus.", claims: list[dict] | None = None
) -> str:
    if claims is None:
        claims = [{"text": "Fintech focus", "supporting_dimension": "industry"}]
    return json.dumps({"prose": prose, "claims": claims})


def _drafter_llm_result(body: str | None = None) -> LLMResult:
    return LLMResult(
        content=body or _drafter_llm_body(),
        model="claude-opus-4-7",
        input_tokens=1500,
        output_tokens=200,
        cost_usd=0.04,
        jsonl_ref="data/logs/2026-05-15/wf-d.jsonl:1",
        stop_reason="end_turn",
        web_searches=0,
        citations=[],
    )


class TestDrafterPriorCritique:
    def test_prior_critique_none_keeps_baseline_prompt(self) -> None:
        # Calling drafter_node without prior_critique behaves identically to
        # the Slice 5 baseline: no RETRY_PROMPT_PREFACE in the user prompt.
        scored = _scored_company(
            dimensions=[_dim(field="industry", grounded_quote="fintech", weight=1.0)]
        )
        with patch.object(drafter_mod, "complete", return_value=_drafter_llm_result()) as p:
            drafter_mod.drafter_node(scored_company=scored, workflow_id="wf-no-critique")
        sent = p.call_args.kwargs["messages"][0]["content"]
        assert "PRIOR ATTEMPT WAS REJECTED" not in sent

    def test_prior_critique_prepends_retry_preface(self) -> None:
        # Layer A drafter_node receives a Critique and the retry preface is
        # included AS THE FIRST BLOCK of the user prompt.
        scored = _scored_company(
            dimensions=[_dim(field="industry", grounded_quote="fintech", weight=1.0)]
        )
        critique = Critique(
            company_name="Acme",
            passed=False,
            verdicts=[
                ClaimVerdict(
                    claim_text="Anthropic",
                    supporting_dimension="industry",
                    passed=False,
                    failed_hop="claim_in_grounded_quote",
                    explanation="dropped corporate suffix; use 'Anthropic PBC' verbatim",
                    classification="fixable_paraphrase",
                    upstream_quote="Anthropic PBC is in early talks",
                )
            ],
        )
        with patch.object(drafter_mod, "complete", return_value=_drafter_llm_result()) as p:
            drafter_mod.drafter_node(
                scored_company=scored,
                workflow_id="wf-retry",
                prior_critique=critique,
            )
        sent = p.call_args.kwargs["messages"][0]["content"]
        assert sent.startswith("PRIOR ATTEMPT WAS REJECTED")
        assert "fixable_paraphrase" in sent
        # The verbatim upstream quote is in the prompt — the load-bearing
        # signal for the retry path (Slice 5 calibration finding).
        assert "Anthropic PBC is in early talks" in sent
        # Excluded "passed" verdicts are NOT included — only failures.
        assert "FAILURE 1:" in sent

    def test_prior_critique_does_not_change_layer_b_signature(self) -> None:
        # run_drafter's signature is unchanged — no prior_critique kwarg
        # leaked into Layer B. Existing test_drafter.py is the cross-check.
        import inspect

        sig = inspect.signature(drafter_mod.run_drafter)
        assert "prior_critique" not in sig.parameters


# ---------------------------------------------------------------------------
# Orchestrator integration — critic + retry edge
# ---------------------------------------------------------------------------


def _rubric() -> RubricConfig:
    return RubricConfig(
        floor=0.0,
        min_coverage=0.0,
        tier_a_min=80.0,
        tier_b_min=60.0,
        tier_c_min=0.0,
        missing_data_policy="renormalise",
        dimensions=[
            DimensionConfig(
                name="industry", field="industry", weight=1.0, prose="Score 5 for fintech."
            ),
        ],
    )


def _stub_researcher_llm_result(signal_text: str) -> LLMResult:
    """A researcher LLMResult with one Citation that surfaces verbatim source text.

    The Researcher converts citations → Signals, so we wire the citation
    `cited_text` to the prose we want surfaced as `Signal.text`.
    """
    from agent_habitat.llm import Citation

    return LLMResult(
        content="research summary",
        model="claude-haiku-4-5-20251001",
        input_tokens=100,
        output_tokens=80,
        cost_usd=0.001,
        jsonl_ref="data/logs/2026-05-15/wf-r.jsonl:1",
        stop_reason="end_turn",
        web_searches=1,
        citations=[
            Citation(
                cited_text=signal_text,
                source_url="https://news.example.com/article",
                source_title="article",
            )
        ],
    )


def _stub_extractor_llm_result(signal_text: str) -> LLMResult:
    body = json.dumps(
        {
            "size": {"gap": {"reason": "field_not_in_signals"}},
            "industry": {
                "values": ["fintech"],
                "source_spans": [{"signal_index": 0, "quote": signal_text}],
            },
            "tech_stack": {"gap": {"reason": "field_not_in_signals"}},
            "recent_news": {"gap": {"reason": "field_not_in_signals"}},
            "decision_makers": {"gap": {"reason": "field_not_in_signals"}},
        }
    )
    return LLMResult(
        content=body,
        model="claude-sonnet-4-6",
        input_tokens=200,
        output_tokens=100,
        cost_usd=0.005,
        jsonl_ref="data/logs/2026-05-15/wf-e.jsonl:1",
        stop_reason="end_turn",
        web_searches=0,
        citations=[],
    )


def _stub_scorer_llm_result(signal_text: str) -> LLMResult:
    body = json.dumps(
        {
            "dimensions": [
                {
                    "field": "industry",
                    "score": 5.0,
                    "grounded_quote": signal_text,
                    "reasoning": "Direct fintech match.",
                }
            ]
        }
    )
    return LLMResult(
        content=body,
        model="claude-sonnet-4-6",
        input_tokens=300,
        output_tokens=80,
        cost_usd=0.008,
        jsonl_ref="data/logs/2026-05-15/wf-s.jsonl:1",
        stop_reason="end_turn",
        web_searches=0,
        citations=[],
    )


def _stub_drafter_llm_clean(signal_text: str) -> LLMResult:
    """Drafter response whose claim substring-matches the grounded_quote."""
    prose = f"Saw your work — {signal_text} — happy to chat."
    # claim.text MUST be substring of prose AND substring of grounded_quote.
    # signal_text is both the prose substring and the grounded_quote.
    body = json.dumps(
        {
            "prose": prose,
            "claims": [
                {"text": signal_text, "supporting_dimension": "industry"},
            ],
        }
    )
    return _drafter_llm_result(body)


def _stub_drafter_llm_paraphrase(signal_text: str) -> LLMResult:
    """Drafter response that paraphrases the grounded_quote — hop 2 will fail."""
    # The Slice 5 pattern: grounded_quote = "Acme Corp is a fintech."
    # Drafter outputs claim.text = "Acme is a fintech." (dropped "Corp").
    paraphrase = signal_text.replace("Acme Corp", "Acme")
    prose = f"Saw your work — {paraphrase} — happy to chat."
    body = json.dumps(
        {
            "prose": prose,
            "claims": [
                {"text": paraphrase, "supporting_dimension": "industry"},
            ],
        }
    )
    return _drafter_llm_result(body)


class TestCrewGraphCriticIntegration:
    def test_critic_passes_workflow_completes(
        self, conn: sqlite3.Connection, db_path: Path
    ) -> None:
        signal_text = "Acme Corp is a fintech startup."
        saver_conn = sqlite3.connect(str(db_path), check_same_thread=False)
        saver = SqliteSaver(saver_conn)
        saver.setup()
        try:
            with (
                patch.object(
                    drafter_mod, "complete", return_value=_stub_drafter_llm_clean(signal_text)
                ),
                patch.object(critic_mod, "complete") as critic_patch,
                patch.object(
                    extractor_mod, "complete", return_value=_stub_extractor_llm_result(signal_text)
                ),
                patch(
                    "agent_habitat.agents.researcher.complete",
                    return_value=_stub_researcher_llm_result(signal_text),
                ),
                patch(
                    "agent_habitat.agents.scorer.complete",
                    return_value=_stub_scorer_llm_result(signal_text),
                ),
            ):
                result = run_crew(
                    conn,
                    company_name="Acme",
                    rubric=_rubric(),
                    saver=saver,
                    workflow_id="wf-pass",
                )
                if result.status is WorkflowStatus.PAUSED:
                    from agent_habitat.checkpoint.system import approve_checkpoint

                    assert result.pending_checkpoint_id is not None
                    approve_checkpoint(conn, result.pending_checkpoint_id, reviewer="test")
                    from agent_habitat.orchestration.crew_graph import resume_crew

                    result = resume_crew(
                        conn,
                        workflow_id="wf-pass",
                        rubric=_rubric(),
                        saver=saver,
                    )
            assert result.status is WorkflowStatus.COMPLETED
            assert result.draft is not None
            assert result.critique is not None
            assert result.critique.passed is True
            assert result.fabrication_retries == 0
            assert critic_patch.call_count == 0  # No Mode-2 — chain held.
            # Five steps: researcher, extractor, scorer, drafter, critic.
            steps = sorted(load_steps(conn, "wf-pass"), key=lambda s: s.step_index)
            agent_names = [s.agent_name for s in steps]
            assert agent_names == [
                "researcher",
                "extractor",
                "scorer",
                "drafter",
                "critic",
            ]
        finally:
            saver_conn.close()

    def test_critic_fails_first_retries_second_passes(
        self, conn: sqlite3.Connection, db_path: Path
    ) -> None:
        signal_text = "Acme Corp is a fintech startup."
        saver_conn = sqlite3.connect(str(db_path), check_same_thread=False)
        saver = SqliteSaver(saver_conn)
        saver.setup()
        # The drafter call is invoked TWICE — first paraphrase, then clean.
        drafter_responses = iter(
            [
                _stub_drafter_llm_paraphrase(signal_text),
                _stub_drafter_llm_clean(signal_text),
            ]
        )
        critic_response = _llm_result(
            json.dumps(
                {
                    "classification": "fixable_paraphrase",
                    "explanation": "Dropped 'Corp'; embed verbatim quote on retry.",
                }
            )
        )

        def drafter_side_effect(**_kwargs: object) -> LLMResult:
            return next(drafter_responses)

        try:
            with (
                patch.object(drafter_mod, "complete", side_effect=drafter_side_effect),
                patch.object(critic_mod, "complete", return_value=critic_response),
                patch.object(
                    extractor_mod, "complete", return_value=_stub_extractor_llm_result(signal_text)
                ),
                patch(
                    "agent_habitat.agents.researcher.complete",
                    return_value=_stub_researcher_llm_result(signal_text),
                ),
                patch(
                    "agent_habitat.agents.scorer.complete",
                    return_value=_stub_scorer_llm_result(signal_text),
                ),
            ):
                result = run_crew(
                    conn,
                    company_name="Acme",
                    rubric=_rubric(),
                    saver=saver,
                    workflow_id="wf-retry-pass",
                )
                if result.status is WorkflowStatus.PAUSED:
                    from agent_habitat.checkpoint.system import approve_checkpoint
                    from agent_habitat.orchestration.crew_graph import resume_crew

                    assert result.pending_checkpoint_id is not None
                    approve_checkpoint(conn, result.pending_checkpoint_id, reviewer="test")
                    result = resume_crew(
                        conn,
                        workflow_id="wf-retry-pass",
                        rubric=_rubric(),
                        saver=saver,
                    )
            assert result.status is WorkflowStatus.COMPLETED
            assert result.draft is not None
            assert result.critique is not None
            assert result.critique.passed is True
            assert result.fabrication_retries == 1
            # Seven steps: r, e, s, drafter (4), critic (5), drafter-retry (6),
            # critic-retry (7).
            steps = sorted(load_steps(conn, "wf-retry-pass"), key=lambda s: s.step_index)
            indices = [s.step_index for s in steps]
            assert STEP_INDEX_DRAFTER in indices
            assert STEP_INDEX_CRITIC in indices
            assert STEP_INDEX_DRAFTER_RETRY in indices
            assert STEP_INDEX_CRITIC_RETRY in indices
        finally:
            saver_conn.close()

    def test_critic_fails_twice_terminates_with_failure(
        self, conn: sqlite3.Connection, db_path: Path
    ) -> None:
        signal_text = "Acme Corp is a fintech startup."
        saver_conn = sqlite3.connect(str(db_path), check_same_thread=False)
        saver = SqliteSaver(saver_conn)
        saver.setup()
        # Drafter paraphrases both times — critic fails both times.
        critic_response = _llm_result(
            json.dumps(
                {
                    "classification": "fixable_paraphrase",
                    "explanation": "Same paraphrase pattern on retry — model did not fix.",
                }
            )
        )
        try:
            with (
                patch.object(
                    drafter_mod,
                    "complete",
                    return_value=_stub_drafter_llm_paraphrase(signal_text),
                ),
                patch.object(critic_mod, "complete", return_value=critic_response),
                patch.object(
                    extractor_mod, "complete", return_value=_stub_extractor_llm_result(signal_text)
                ),
                patch(
                    "agent_habitat.agents.researcher.complete",
                    return_value=_stub_researcher_llm_result(signal_text),
                ),
                patch(
                    "agent_habitat.agents.scorer.complete",
                    return_value=_stub_scorer_llm_result(signal_text),
                ),
            ):
                result = run_crew(
                    conn,
                    company_name="Acme",
                    rubric=_rubric(),
                    saver=saver,
                    workflow_id="wf-double-fail",
                )
                if result.status is WorkflowStatus.PAUSED:
                    from agent_habitat.checkpoint.system import approve_checkpoint
                    from agent_habitat.orchestration.crew_graph import resume_crew

                    assert result.pending_checkpoint_id is not None
                    approve_checkpoint(conn, result.pending_checkpoint_id, reviewer="test")
                    result = resume_crew(
                        conn,
                        workflow_id="wf-double-fail",
                        rubric=_rubric(),
                        saver=saver,
                    )
            assert result.status is WorkflowStatus.FAILED
            assert result.terminate_reason == TERMINATE_REASON_CRITIC_FAILURE
            assert result.fabrication_retries == 2
            assert result.critique is not None
            assert result.critique.passed is False
            wf = load_workflow(conn, "wf-double-fail")
            assert wf is not None
            assert wf.status is WorkflowStatus.FAILED
        finally:
            saver_conn.close()


# ---------------------------------------------------------------------------
# RED-TEAM SMOKE — the load-bearing test. Synthetic Drafts replicating the
# Slice 5 documented failure patterns + invented fabrications.
# ---------------------------------------------------------------------------


class TestRedTeamSmoke:
    """The defining test for this slice — verify the Critic catches the EXACT
    failure patterns the Slice 5 live smoke documented, alongside genuine
    fabrications. Each case is checked individually so any future change to
    the chain check that loosens detection breaks ONE named test (calibrated
    diagnosis, not a single "smoke" failure to debug).

    Slice 5 patterns (recorded in STATUS.md "Phase 2 Slice 5 Live Smoke
    Calibration"):
      A. "Anthropic PBC" → "Anthropic"           — dropped corporate suffix
      B. "teamed with"   → "partnership with"    — synonym substitution
    Plus invented fabrications:
      C. funding amount the upstream evidence does not mention
      D. partnership the upstream evidence does not mention
    """

    @pytest.fixture
    def anthropic_chain(self) -> tuple[CompanyProfile, RawSignals]:
        """A real-ish upstream chain for the Anthropic red-team cases.

        Signal text and ProfileField source_spans mirror the Slice 5 live
        smoke's grounded quotes verbatim — the critic walks these as the
        canonical "ground truth" against which paraphrases must fail.
        """
        signal_a = _signal(
            text=(
                "Anthropic PBC is in early talks with investors to raise at "
                "least $30 billion in fresh financing."
            ),
            source_url="https://news.example.com/anthropic-funding",
        )
        signal_b = _signal(
            text=(
                "Anthropic teamed with the Gates Foundation on a $200 million "
                "health-and-education-focused AI initiative."
            ),
            source_url="https://news.example.com/anthropic-gates",
        )
        profile = _profile(
            company_name="Anthropic",
            recent_news=_profile_field(
                values=["funding talks", "Gates Foundation initiative"],
                spans=[
                    (
                        0,
                        "Anthropic PBC is in early talks with investors to "
                        "raise at least $30 billion in fresh financing.",
                    ),
                    (
                        1,
                        "Anthropic teamed with the Gates Foundation on a "
                        "$200 million health-and-education-focused AI initiative.",
                    ),
                ],
            ),
        )
        return profile, _raw_signals(signal_a, signal_b, company_name="Anthropic")

    def test_pattern_a_anthropic_pbc_dropped_suffix_caught(
        self, anthropic_chain: tuple[CompanyProfile, RawSignals], log_root: Path
    ) -> None:
        profile, raw = anthropic_chain
        scored = _scored_company(
            company_name="Anthropic",
            dimensions=[
                _dim(
                    field="recent_news",
                    grounded_quote=(
                        "Anthropic PBC is in early talks with investors to "
                        "raise at least $30 billion in fresh financing"
                    ),
                    weight=1.0,
                )
            ],
        )
        # Drafter dropped "PBC" — the Slice 5 documented pattern.
        rogue_claim_text = (
            "Anthropic is in early talks with investors to raise at least "
            "$30 billion in fresh financing"
        )
        prose = f"Saw that {rogue_claim_text}. Worth a chat?"
        draft = Draft(
            company_name="Anthropic",
            prose=prose,
            claims=[DraftClaim(text=rogue_claim_text, supporting_dimension="recent_news")],
        )
        # Mode-2 returns a classification (test fixture stand-in for Haiku).
        mode_2_body = json.dumps(
            {
                "classification": "fixable_paraphrase",
                "explanation": (
                    "Dropped 'PBC' corporate suffix. On retry embed the "
                    "verbatim upstream quote including 'Anthropic PBC'."
                ),
            }
        )
        with patch.object(critic_mod, "complete", return_value=_llm_result(mode_2_body)):
            out = critic_node(
                draft=draft,
                scored_company=scored,
                profile=profile,
                raw_signals=raw,
                workflow_id="wf-redteam-A",
                log_root=log_root,
            )
        assert out.critique.passed is False
        v = out.critique.verdicts[0]
        assert v.passed is False
        assert v.failed_hop == "claim_in_grounded_quote"
        assert v.classification == "fixable_paraphrase"
        assert v.upstream_quote is not None
        assert (
            "Anthropic PBC" in v.upstream_quote
        )  # the verbatim phrase Drafter must embed on retry.

    def test_pattern_b_teamed_with_synonym_substitution_caught(
        self, anthropic_chain: tuple[CompanyProfile, RawSignals], log_root: Path
    ) -> None:
        profile, raw = anthropic_chain
        scored = _scored_company(
            company_name="Anthropic",
            dimensions=[
                _dim(
                    field="recent_news",
                    grounded_quote=(
                        "Anthropic teamed with the Gates Foundation on a "
                        "$200 million health-and-education-focused AI initiative"
                    ),
                    weight=1.0,
                )
            ],
        )
        # Drafter substituted "teamed with" → "partnership with" — Slice 5.
        rogue_claim_text = (
            "partnership with the Gates Foundation on a $200 million "
            "health-and-education-focused AI initiative"
        )
        prose = f"Their {rogue_claim_text} is exactly our wheelhouse."
        draft = Draft(
            company_name="Anthropic",
            prose=prose,
            claims=[DraftClaim(text=rogue_claim_text, supporting_dimension="recent_news")],
        )
        mode_2_body = json.dumps(
            {
                "classification": "fixable_paraphrase",
                "explanation": "'teamed with' → 'partnership with' is a synonym substitution.",
            }
        )
        with patch.object(critic_mod, "complete", return_value=_llm_result(mode_2_body)):
            out = critic_node(
                draft=draft,
                scored_company=scored,
                profile=profile,
                raw_signals=raw,
                workflow_id="wf-redteam-B",
                log_root=log_root,
            )
        assert out.critique.passed is False
        v = out.critique.verdicts[0]
        assert v.failed_hop == "claim_in_grounded_quote"
        assert v.classification == "fixable_paraphrase"
        assert v.upstream_quote is not None
        assert "teamed with" in v.upstream_quote

    def test_pattern_c_invented_funding_amount_caught(
        self, anthropic_chain: tuple[CompanyProfile, RawSignals], log_root: Path
    ) -> None:
        profile, raw = anthropic_chain
        scored = _scored_company(
            company_name="Anthropic",
            dimensions=[
                _dim(
                    field="recent_news",
                    grounded_quote=(
                        "Anthropic PBC is in early talks with investors to "
                        "raise at least $30 billion in fresh financing"
                    ),
                    weight=1.0,
                )
            ],
        )
        # Genuine fabrication: $99 billion is NOT in any upstream evidence.
        rogue_claim_text = "raised exactly $99 billion at a $200 billion valuation"
        prose = f"Saw that they {rogue_claim_text} — congrats."
        draft = Draft(
            company_name="Anthropic",
            prose=prose,
            claims=[DraftClaim(text=rogue_claim_text, supporting_dimension="recent_news")],
        )
        mode_2_body = json.dumps(
            {
                "classification": "fabricated",
                "explanation": (
                    "The upstream evidence mentions $30 billion in talks, not "
                    "$99 billion raised. The claim is unsupported."
                ),
            }
        )
        with patch.object(critic_mod, "complete", return_value=_llm_result(mode_2_body)):
            out = critic_node(
                draft=draft,
                scored_company=scored,
                profile=profile,
                raw_signals=raw,
                workflow_id="wf-redteam-C",
                log_root=log_root,
            )
        assert out.critique.passed is False
        v = out.critique.verdicts[0]
        assert v.failed_hop == "claim_in_grounded_quote"
        assert v.classification == "fabricated"
        assert out.critique.all_fabricated is True

    def test_pattern_d_invented_partnership_caught(
        self, anthropic_chain: tuple[CompanyProfile, RawSignals], log_root: Path
    ) -> None:
        profile, raw = anthropic_chain
        scored = _scored_company(
            company_name="Anthropic",
            dimensions=[
                _dim(
                    field="recent_news",
                    grounded_quote=(
                        "Anthropic teamed with the Gates Foundation on a "
                        "$200 million health-and-education-focused AI initiative"
                    ),
                    weight=1.0,
                )
            ],
        )
        # Genuine fabrication: invented partnership with a fictional entity.
        rogue_claim_text = "joint venture with the Acme Conglomerate on enterprise sales"
        prose = f"Their {rogue_claim_text} is exactly our wheelhouse."
        draft = Draft(
            company_name="Anthropic",
            prose=prose,
            claims=[DraftClaim(text=rogue_claim_text, supporting_dimension="recent_news")],
        )
        mode_2_body = json.dumps(
            {
                "classification": "fabricated",
                "explanation": (
                    "No upstream evidence of an Acme Conglomerate partnership; "
                    "the upstream cites a Gates Foundation initiative."
                ),
            }
        )
        with patch.object(critic_mod, "complete", return_value=_llm_result(mode_2_body)):
            out = critic_node(
                draft=draft,
                scored_company=scored,
                profile=profile,
                raw_signals=raw,
                workflow_id="wf-redteam-D",
                log_root=log_root,
            )
        assert out.critique.passed is False
        v = out.critique.verdicts[0]
        assert v.failed_hop == "claim_in_grounded_quote"
        assert v.classification == "fabricated"

    def test_mixed_red_team_all_failures_caught(
        self, anthropic_chain: tuple[CompanyProfile, RawSignals], log_root: Path
    ) -> None:
        """The integration test: all four red-team claims in ONE Draft.

        The Critic must catch every failure with the right hop pointer, and
        Mode 2 must classify each correctly. This is the "the contract works
        end-to-end" assertion for Slice 7.
        """
        profile, raw = anthropic_chain
        scored = _scored_company(
            company_name="Anthropic",
            dimensions=[
                _dim(
                    field="recent_news",
                    grounded_quote=(
                        "Anthropic PBC is in early talks with investors to raise "
                        "at least $30 billion in fresh financing"
                    ),
                    weight=0.5,
                ),
                _dim(
                    field="industry",
                    grounded_quote=(
                        "Anthropic teamed with the Gates Foundation on a $200 "
                        "million health-and-education-focused AI initiative"
                    ),
                    weight=0.5,
                ),
            ],
        )
        prose = (
            "Saw that Anthropic is in early talks with investors to raise at least "
            "$30 billion in fresh financing — congrats. Their partnership with the "
            "Gates Foundation on a $200 million health-and-education-focused AI "
            "initiative is exactly our wheelhouse. We also know they raised exactly "
            "$99 billion at a $200 billion valuation. Their joint venture with the "
            "Acme Conglomerate on enterprise sales is impressive too."
        )
        draft = Draft(
            company_name="Anthropic",
            prose=prose,
            claims=[
                DraftClaim(
                    text=(
                        "Anthropic is in early talks with investors to raise at "
                        "least $30 billion in fresh financing"
                    ),
                    supporting_dimension="recent_news",
                ),
                DraftClaim(
                    text=(
                        "partnership with the Gates Foundation on a $200 million "
                        "health-and-education-focused AI initiative"
                    ),
                    supporting_dimension="industry",
                ),
                DraftClaim(
                    text="raised exactly $99 billion at a $200 billion valuation",
                    supporting_dimension="recent_news",
                ),
                DraftClaim(
                    text="joint venture with the Acme Conglomerate on enterprise sales",
                    supporting_dimension="industry",
                ),
            ],
        )

        # Per-claim Mode-2 responses. We feed responses in order: A, B, C, D.
        responses = iter(
            [
                _llm_result(
                    json.dumps(
                        {
                            "classification": "fixable_paraphrase",
                            "explanation": "Dropped 'PBC' — embed verbatim 'Anthropic PBC' on retry.",
                        }
                    )
                ),
                _llm_result(
                    json.dumps(
                        {
                            "classification": "fixable_paraphrase",
                            "explanation": "'teamed with' → 'partnership with' is a synonym swap.",
                        }
                    )
                ),
                _llm_result(
                    json.dumps(
                        {
                            "classification": "fabricated",
                            "explanation": "$99 billion is unsupported — upstream says $30 billion talks.",
                        }
                    )
                ),
                _llm_result(
                    json.dumps(
                        {
                            "classification": "fabricated",
                            "explanation": "No upstream evidence of Acme Conglomerate venture.",
                        }
                    )
                ),
            ]
        )

        def side(**_kwargs: object) -> LLMResult:
            return next(responses)

        with patch.object(critic_mod, "complete", side_effect=side) as p:
            out = critic_node(
                draft=draft,
                scored_company=scored,
                profile=profile,
                raw_signals=raw,
                workflow_id="wf-redteam-mixed",
                log_root=log_root,
            )
        # Four failures, four Mode-2 calls. Every failure caught.
        assert p.call_count == 4
        assert out.critique.passed is False
        assert out.critique.claim_count == 4
        assert out.critique.failure_count == 4

        # Failed hop is "claim_in_grounded_quote" for every red-team case —
        # all four are Drafter-level over-reaches against the dimension's
        # grounded_quote (consistent with the Slice 5 calibration finding).
        hops = [v.failed_hop for v in out.critique.verdicts]
        assert hops == ["claim_in_grounded_quote"] * 4

        # Classifications: A, B are fixable; C, D are fabricated.
        classifications = [v.classification for v in out.critique.verdicts]
        assert classifications == [
            "fixable_paraphrase",
            "fixable_paraphrase",
            "fabricated",
            "fabricated",
        ]

        # `all_fabricated` is False because two of four are fixable.
        assert out.critique.all_fabricated is False


# ---------------------------------------------------------------------------
# LIVE smoke (guarded). Full crew end-to-end. Skipped without API key.
# ---------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="needs ANTHROPIC_API_KEY")
def test_live_critic_via_run_crew(tmp_path: Path) -> None:
    """End-to-end live smoke: run_crew on a real company, observe critic
    behaviour against Opus's first-contact output. Records the result for
    Slice 7 calibration evidence.

    The smoke EITHER terminates COMPLETED with a passing critique (the
    happy path — the chain held first try), OR exercises the bounded
    retry edge with a real Opus retry (the path Slice 5 predicted would
    fire). Both outcomes are recorded; neither is a test failure.
    """
    from agent_habitat.checkpoint.system import approve_checkpoint
    from agent_habitat.orchestration.crew_graph import resume_crew

    db = tmp_path / "live.db"
    conn = init_db(db)
    log_root = tmp_path / "logs"
    saver_conn = sqlite3.connect(str(db), check_same_thread=False)
    saver = SqliteSaver(saver_conn)
    saver.setup()
    try:
        # Use a relaxed rubric so the workflow gets past the floor gate.
        # See Slice 5 STATUS for the rationale on relaxed rubrics in live smokes.
        rubric = RubricConfig(
            floor=0.0,
            min_coverage=0.0,
            tier_a_min=80.0,
            tier_b_min=60.0,
            tier_c_min=0.0,
            missing_data_policy="renormalise",
            dimensions=[
                DimensionConfig(
                    name="industry", field="industry", weight=0.5, prose="Score 5 for AI safety."
                ),
                DimensionConfig(
                    name="recent_news",
                    field="recent_news",
                    weight=0.5,
                    prose="Score 5 for funding signal.",
                ),
            ],
        )
        result = run_crew(
            conn,
            company_name="Anthropic",
            rubric=rubric,
            saver=saver,
            log_root=log_root,
        )
        if result.status is WorkflowStatus.PAUSED:
            assert result.pending_checkpoint_id is not None
            approve_checkpoint(conn, result.pending_checkpoint_id, reviewer="live-test")
            result = resume_crew(
                conn,
                workflow_id=result.workflow_id,
                rubric=rubric,
                saver=saver,
                log_root=log_root,
            )
        # Three structurally valid outcomes:
        #   (a) COMPLETED, draft produced, critique passed, no retry.
        #   (b) COMPLETED, draft produced, critique passed, retry fired.
        #   (c) FAILED, terminate_reason == critic_failure, retry exhausted.
        # The test passes if the workflow REACHED a terminal state — that
        # the bounded-retry edge is in place. The detailed observation
        # (which of a/b/c happened) is the Slice 7 calibration signal.
        assert result.status in (WorkflowStatus.COMPLETED, WorkflowStatus.FAILED)
        print(
            f"\nLIVE SMOKE — status={result.status.value} "
            f"draft_produced={result.draft is not None} "
            f"critique_passed={result.critique.passed if result.critique else None} "
            f"fabrication_retries={result.fabrication_retries} "
            f"cost_usd=${result.cost_usd:.6f} "
            f"terminate_reason={result.terminate_reason!r}"
        )
    finally:
        saver_conn.close()
        conn.close()
