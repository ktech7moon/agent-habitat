"""Deterministic tests for the Drafter agent — Phase 2 Slice 5.

Covers:
  - Models: `DraftClaim` + `Draft` validation, frozen, extra=forbid;
    claim.text-must-appear-in-prose model invariant;
    supporting_dimension-must-be-PROFILE_FIELD_NAMES field invariant.
  - Pure helpers: `_strip_code_fence`, `_parse_draft`,
    `_check_claims_against_input`, `_projection`.
  - Layer A `drafter_node` with LLM mocked: happy path; below-floor
    raises; below-coverage raises; high-score-low-coverage proceeds;
    schema mismatch raises DrafterParseError; cross-input invariant
    violation (claim references excluded dimension) raises
    DrafterParseError; LLM raises propagates (Layer A is pure: no DB
    touched).
  - Layer B `run_drafter` with LLM mocked: full habitat round-trip
    (workflow + step + events + cost rollup + output_ref); LLM raises
    → step+workflow FAILED, exception swallowed; below-floor →
    workflow FAILED.
  - CLI: happy four-stage path; scorer gates → no-draft block printed,
    workflow ends successfully (exit 0); upstream failures exit non-zero.

NO live API call here; the live smoke lives at the bottom guarded by
`@pytest.mark.live` and `ANTHROPIC_API_KEY`.
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

from agent_habitat.agents import drafter as drafter_mod
from agent_habitat.agents import extractor as extractor_mod
from agent_habitat.agents import researcher as researcher_mod
from agent_habitat.agents import scorer as scorer_mod
from agent_habitat.agents.drafter import (
    AGENT_NAME,
    DRAFTER_MAX_TOKENS,
    Draft,
    DraftClaim,
    DrafterCalledBelowFloorError,
    DrafterError,
    DrafterParseError,
    WORKFLOW_TYPE,
    _check_claims_against_input,
    _parse_draft,
    _projection,
    _strip_code_fence,
    drafter_node,
    run_drafter,
)
from agent_habitat.agents.models import (
    PROFILE_FIELD_NAMES,
    DimensionScore,
    ScoredCompany,
    Signal,
)
from agent_habitat.cli import main
from agent_habitat.llm import Citation, LLMResult
from agent_habitat.observability import EventType
from agent_habitat.state import (
    StepStatus,
    WorkflowStatus,
    init_db,
    load_events,
    load_steps,
    load_workflow,
)


# ---------------------------------------------------------------------------
# Fixtures + helpers
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


def _llm_result(content: str, *, cost_usd: float = 0.04) -> LLMResult:
    """Build an LLMResult fixture mimicking an Opus 4.7 response."""
    return LLMResult(
        content=content,
        model="claude-opus-4-7",
        input_tokens=1500,
        output_tokens=400,
        cost_usd=cost_usd,
        jsonl_ref="data/logs/2026-05-14/wf-drafter.jsonl:1",
        stop_reason="end_turn",
        web_searches=0,
        citations=[],
    )


def _scored_company(
    *,
    score: float | None = 82.0,
    coverage: float = 0.7,
    floor: float = 55.0,
    min_coverage: float = 0.0,
    tier: str | None = "A",
    gated_by: str | None = None,
    company_name: str = "Acme Corp",
    dimensions: list[DimensionScore] | None = None,
) -> ScoredCompany:
    """A reasonable ScoredCompany for happy-path tests.

    By default `routes_to_draft=True` (gated_by=None), score=82, coverage=70%,
    tier=A. Three of five dimensions are scored, two are excluded — matches
    the Slice 4 live-smoke shape.
    """
    if dimensions is None:
        dimensions = [
            DimensionScore(
                field="size",
                weight=0.30,
                score=None,
                grounded_quote=None,
                reasoning="excluded — field was an ExtractionGap (reason='no_signals').",
            ),
            DimensionScore(
                field="industry",
                weight=0.20,
                score=3.0,
                grounded_quote="a fintech startup",
                reasoning="Fintech band per the rubric — 3/5.",
            ),
            DimensionScore(
                field="tech_stack",
                weight=0.20,
                score=5.0,
                grounded_quote="Python and AWS",
                reasoning="Strong overlap with operator's stack — 5/5.",
            ),
            DimensionScore(
                field="recent_news",
                weight=0.20,
                score=5.0,
                grounded_quote="raised a $50M Series B",
                reasoning="Recent funding signal — 5/5.",
            ),
            DimensionScore(
                field="decision_makers",
                weight=0.10,
                score=None,
                grounded_quote=None,
                reasoning="excluded — field was an ExtractionGap (reason='no_signals').",
            ),
        ]
    return ScoredCompany(
        company_name=company_name,
        score=score,
        coverage=coverage,
        floor=floor,
        min_coverage=min_coverage,
        passed_floor=score is not None and score >= floor,
        passed_coverage=coverage >= min_coverage,
        tier=tier,
        gated_by=gated_by,
        dimensions=dimensions,
    )


def _gated_scored_company(
    *,
    company_name: str = "Acme Corp",
    score: float = 30.0,
    coverage: float = 0.5,
    floor: float = 55.0,
    min_coverage: float = 0.6,
    gated_by: str = "score",
) -> ScoredCompany:
    """A ScoredCompany that did NOT pass both gates (routes_to_draft=False)."""
    dims = [
        DimensionScore(
            field=name,
            weight=0.20,
            score=2.0,
            grounded_quote=f"sample quote for {name}",
            reasoning=f"low-band {name}.",
        )
        for name in PROFILE_FIELD_NAMES
    ]
    return ScoredCompany(
        company_name=company_name,
        score=score,
        coverage=coverage,
        floor=floor,
        min_coverage=min_coverage,
        passed_floor=score >= floor,
        passed_coverage=coverage >= min_coverage,
        tier="below_c",
        gated_by=gated_by,
        dimensions=dims,
    )


_GOOD_PROSE = (
    "Hi there,\n\n"
    "I noticed your work as a fintech startup, particularly the recent "
    "$50M Series B. Your stack on Python and AWS is a strong overlap with "
    "the kind of compliance-grade integrations my team builds.\n\n"
    "If a 30-minute conversation about regulated-industry agent patterns "
    "would be useful, I'd be happy to set one up."
)


def _good_llm_body() -> str:
    """A well-formed JSON body the LLM would return on the happy path.

    Three claims, each anchored to a non-excluded dimension on the
    `_scored_company()` default. Each claim.text appears verbatim in
    `_GOOD_PROSE`.
    """
    return json.dumps(
        {
            "prose": _GOOD_PROSE,
            "claims": [
                {"text": "fintech startup", "supporting_dimension": "industry"},
                {"text": "Python and AWS", "supporting_dimension": "tech_stack"},
                {"text": "$50M Series B", "supporting_dimension": "recent_news"},
            ],
        }
    )


# ---------------------------------------------------------------------------
# DraftClaim model
# ---------------------------------------------------------------------------


class TestDraftClaim:
    def test_valid_claim_round_trips(self) -> None:
        claim = DraftClaim(text="$50M Series B", supporting_dimension="recent_news")
        # round-trip via JSON
        data = claim.model_dump()
        assert DraftClaim.model_validate(data) == claim

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            DraftClaim.model_validate(
                {
                    "text": "x",
                    "supporting_dimension": "industry",
                    "evil_extra": True,
                }
            )

    def test_frozen(self) -> None:
        claim = DraftClaim(text="x", supporting_dimension="industry")
        with pytest.raises(ValidationError):
            claim.text = "y"  # type: ignore[misc]

    def test_empty_text_rejected(self) -> None:
        with pytest.raises(ValidationError):
            DraftClaim(text="", supporting_dimension="industry")

    @pytest.mark.parametrize("name", PROFILE_FIELD_NAMES)
    def test_each_profile_field_name_accepted(self, name: str) -> None:
        DraftClaim(text="x", supporting_dimension=name)

    def test_unknown_supporting_dimension_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            DraftClaim(text="x", supporting_dimension="not_a_real_field")
        assert "supporting_dimension must be one of" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Draft model
# ---------------------------------------------------------------------------


class TestDraft:
    def test_valid_draft_round_trips(self) -> None:
        d = Draft.model_validate(json.loads(_good_llm_body()) | {"company_name": "Acme Corp"})
        # round-trip
        d2 = Draft.model_validate_json(d.model_dump_json())
        assert d2 == d

    def test_extra_field_rejected(self) -> None:
        body = json.loads(_good_llm_body()) | {"company_name": "Acme", "rogue": 1}
        with pytest.raises(ValidationError):
            Draft.model_validate(body)

    def test_frozen(self) -> None:
        d = Draft.model_validate(json.loads(_good_llm_body()) | {"company_name": "Acme"})
        with pytest.raises(ValidationError):
            d.prose = "tampered"  # type: ignore[misc]

    def test_empty_prose_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Draft(company_name="Acme", prose="", claims=[])

    def test_empty_claims_list_allowed_structurally(self) -> None:
        # Pure marketing prose with no enumerated claims: structurally valid
        # (the critic would have nothing to ground; that's a semantic
        # judgment, not a schema judgment).
        d = Draft(company_name="Acme", prose="Hi there, hope you're well.", claims=[])
        assert d.claim_count == 0

    def test_claim_text_not_in_prose_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            Draft(
                company_name="Acme",
                prose="Hi there, just checking in.",
                claims=[
                    DraftClaim(text="$50M Series B", supporting_dimension="recent_news"),
                ],
            )
        assert "is not a substring of prose" in str(exc_info.value)

    def test_claim_text_in_prose_after_normalisation(self) -> None:
        # whitespace+case normalisation lets the substring match across
        # whitespace differences.
        d = Draft(
            company_name="Acme",
            prose="They   raised   a    $50M\nSeries B last quarter.",
            claims=[DraftClaim(text="$50m series b", supporting_dimension="recent_news")],
        )
        assert d.claim_count == 1

    def test_claim_text_normalises_to_empty_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Draft(
                company_name="Acme",
                prose="Hi there",
                claims=[DraftClaim(text="   ", supporting_dimension="industry")],
            )

    def test_paragraph_count_one(self) -> None:
        d = Draft(
            company_name="Acme",
            prose="Single block of text with no paragraph break.",
            claims=[],
        )
        assert d.paragraph_count == 1

    def test_paragraph_count_three(self) -> None:
        d = Draft(
            company_name="Acme",
            prose="First.\n\nSecond paragraph.\n\nThird paragraph.",
            claims=[],
        )
        assert d.paragraph_count == 3

    def test_char_count(self) -> None:
        d = Draft(company_name="Acme", prose="Hello world.", claims=[])
        assert d.char_count == len("Hello world.")

    def test_claim_count(self) -> None:
        d = Draft.model_validate(json.loads(_good_llm_body()) | {"company_name": "Acme"})
        assert d.claim_count == 3


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestStripCodeFence:
    def test_no_fence_passthrough(self) -> None:
        assert _strip_code_fence('{"prose": "x", "claims": []}') == '{"prose": "x", "claims": []}'

    def test_strips_json_fence(self) -> None:
        assert _strip_code_fence('```json\n{"x": 1}\n```') == '{"x": 1}'

    def test_strips_bare_fence(self) -> None:
        assert _strip_code_fence('```\n{"x": 1}\n```') == '{"x": 1}'


class TestParseDraft:
    def test_happy(self) -> None:
        d = _parse_draft(_good_llm_body(), company_name="Acme Corp")
        assert d.company_name == "Acme Corp"
        assert d.claim_count == 3

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(DrafterParseError) as exc_info:
            _parse_draft("not json", company_name="Acme")
        assert "valid JSON" in str(exc_info.value)

    def test_root_not_object_raises(self) -> None:
        with pytest.raises(DrafterParseError):
            _parse_draft("[1, 2, 3]", company_name="Acme")

    def test_company_name_disagreement_rejected(self) -> None:
        body = json.dumps(
            {
                "company_name": "Globex",
                "prose": _GOOD_PROSE,
                "claims": [
                    {"text": "fintech startup", "supporting_dimension": "industry"},
                ],
            }
        )
        with pytest.raises(DrafterParseError) as exc_info:
            _parse_draft(body, company_name="Acme Corp")
        assert "disagrees" in str(exc_info.value)

    def test_schema_mismatch_raises(self) -> None:
        body = json.dumps({"prose": _GOOD_PROSE, "claims": [{"text": "x"}]})
        with pytest.raises(DrafterParseError) as exc_info:
            _parse_draft(body, company_name="Acme")
        assert "Draft schema" in str(exc_info.value)

    def test_handles_code_fence(self) -> None:
        wrapped = "```json\n" + _good_llm_body() + "\n```"
        d = _parse_draft(wrapped, company_name="Acme Corp")
        assert d.claim_count == 3


class TestCheckClaimsAgainstInput:
    def test_happy(self) -> None:
        sc = _scored_company()
        d = _parse_draft(_good_llm_body(), company_name=sc.company_name)
        # Should not raise.
        _check_claims_against_input(d, sc)

    def test_unknown_dimension_rejected(self) -> None:
        # Claim references a field name that the rubric/dimensions list does
        # not contain. (Dimensions are limited to the 5 PROFILE_FIELD_NAMES,
        # and the model validator on DraftClaim.supporting_dimension already
        # restricts to those — so build a ScoredCompany with a SUBSET of
        # dimensions to trigger the "not present on input" branch.)
        partial = _scored_company(
            dimensions=[
                DimensionScore(
                    field="industry",
                    weight=1.0,
                    score=3.0,
                    grounded_quote="a fintech startup",
                    reasoning="band",
                ),
            ]
        )
        d = Draft(
            company_name=partial.company_name,
            prose="Their tech_stack includes Python and AWS.",
            claims=[
                DraftClaim(text="Python and AWS", supporting_dimension="tech_stack"),
            ],
        )
        with pytest.raises(DrafterParseError) as exc_info:
            _check_claims_against_input(d, partial)
        assert "not present on the input ScoredCompany.dimensions" in str(exc_info.value)

    def test_excluded_dimension_rejected(self) -> None:
        sc = _scored_company()
        # `size` is excluded on the default _scored_company() fixture.
        # Build a Draft that cites it.
        d = Draft(
            company_name=sc.company_name,
            prose="They are a roughly 500-employee company.",
            claims=[
                DraftClaim(text="500-employee", supporting_dimension="size"),
            ],
        )
        with pytest.raises(DrafterParseError) as exc_info:
            _check_claims_against_input(d, sc)
        assert "is excluded on the input ScoredCompany" in str(exc_info.value)


class TestProjection:
    def test_keys(self) -> None:
        d = _parse_draft(_good_llm_body(), company_name="Acme Corp")
        proj = _projection(d)
        assert set(proj.keys()) == {"paragraph_count", "char_count", "claim_count"}
        assert proj["claim_count"] == 3
        assert proj["char_count"] == len(_GOOD_PROSE)
        assert proj["paragraph_count"] == 3


# ---------------------------------------------------------------------------
# Layer A: drafter_node (LLM mocked, NO database)
# ---------------------------------------------------------------------------


class TestDrafterNode:
    def test_happy_path(self, log_root: Path) -> None:
        sc = _scored_company()
        with patch.object(drafter_mod, "complete", return_value=_llm_result(_good_llm_body())):
            out = drafter_node(scored_company=sc, workflow_id="wf-test", log_root=log_root)
        assert out.draft.company_name == "Acme Corp"
        assert out.draft.claim_count == 3
        assert out.cost_usd == pytest.approx(0.04)
        assert out.output_ref == "data/logs/2026-05-14/wf-drafter.jsonl:1"
        assert out.structured_data == {
            "paragraph_count": 3,
            "char_count": len(_GOOD_PROSE),
            "claim_count": 3,
        }

    def test_below_floor_raises(self) -> None:
        sc = _gated_scored_company(gated_by="score")
        with patch.object(drafter_mod, "complete") as p:
            with pytest.raises(DrafterCalledBelowFloorError) as exc_info:
                drafter_node(scored_company=sc, workflow_id="wf-test")
            p.assert_not_called()
        # Subclass of DrafterError so `except DrafterError` catches both.
        assert isinstance(exc_info.value, DrafterError)
        assert "gated_by='score'" in str(exc_info.value)

    def test_below_coverage_raises(self) -> None:
        sc = _gated_scored_company(gated_by="coverage", coverage=0.2, min_coverage=0.6)
        with patch.object(drafter_mod, "complete") as p:
            with pytest.raises(DrafterCalledBelowFloorError):
                drafter_node(scored_company=sc, workflow_id="wf-test")
            p.assert_not_called()

    def test_high_score_low_coverage_proceeds(self) -> None:
        # gated_by=None (passes both gates), but coverage is well below 1.0.
        # Per the kickoff design choice: proceed normally; the operator sees
        # coverage on the ScoredCompany separately. The CLI's footer is what
        # discloses coverage; the Drafter does not hedge in prose.
        sc = _scored_company(coverage=0.4, min_coverage=0.0)
        assert sc.routes_to_draft is True
        with patch.object(drafter_mod, "complete", return_value=_llm_result(_good_llm_body())):
            out = drafter_node(scored_company=sc, workflow_id="wf-test")
        # Produced a normal Draft — no special hedging branch.
        assert out.draft.claim_count == 3

    def test_schema_mismatch_raises_no_db_touched(self) -> None:
        # Layer A is pure: even on parse failure, nothing should attempt a
        # DB write (we provide no conn; raising before any persistence call
        # would assert that path). Missing required field `prose` is the
        # schema violation here (`claims` has a default and is permissibly
        # absent).
        sc = _scored_company()
        bad_body = json.dumps({"claims": []})
        with patch.object(drafter_mod, "complete", return_value=_llm_result(bad_body)):
            with pytest.raises(DrafterParseError):
                drafter_node(scored_company=sc, workflow_id="wf-test")

    def test_invalid_json_raises(self) -> None:
        sc = _scored_company()
        with patch.object(drafter_mod, "complete", return_value=_llm_result("not json")):
            with pytest.raises(DrafterParseError):
                drafter_node(scored_company=sc, workflow_id="wf-test")

    def test_claim_references_excluded_dim_raises(self) -> None:
        sc = _scored_company()  # `size` and `decision_makers` are excluded.
        bad_body = json.dumps(
            {
                "prose": "They have roughly 500 employees and great decision_makers.",
                "claims": [
                    {"text": "500 employees", "supporting_dimension": "size"},
                ],
            }
        )
        with patch.object(drafter_mod, "complete", return_value=_llm_result(bad_body)):
            with pytest.raises(DrafterParseError) as exc_info:
                drafter_node(scored_company=sc, workflow_id="wf-test")
        assert "excluded" in str(exc_info.value)

    def test_claim_text_not_in_prose_raises(self) -> None:
        sc = _scored_company()
        # The prose does NOT contain the claim text — the Draft model
        # validator catches this at parse time (DrafterParseError).
        bad_body = json.dumps(
            {
                "prose": "Generic outreach with no concrete claims.",
                "claims": [
                    {"text": "$50M Series B", "supporting_dimension": "recent_news"},
                ],
            }
        )
        with patch.object(drafter_mod, "complete", return_value=_llm_result(bad_body)):
            with pytest.raises(DrafterParseError) as exc_info:
                drafter_node(scored_company=sc, workflow_id="wf-test")
        assert "not a substring of prose" in str(exc_info.value)

    def test_llm_raises_propagates(self) -> None:
        sc = _scored_company()
        with patch.object(drafter_mod, "complete", side_effect=RuntimeError("API 500")):
            with pytest.raises(RuntimeError, match="API 500"):
                drafter_node(scored_company=sc, workflow_id="wf-test")

    def test_uses_opus_tier(self) -> None:
        sc = _scored_company()
        captured: dict[str, object] = {}

        def fake(**kwargs: object) -> LLMResult:
            captured.update(kwargs)
            return _llm_result(_good_llm_body())

        with patch.object(drafter_mod, "complete", side_effect=fake):
            drafter_node(scored_company=sc, workflow_id="wf-test")

        # Critical: the Drafter is the user-visible-prose agent and must use Opus 4.7.
        assert captured["model_tier"] is drafter_mod.ModelTier.OPUS
        assert captured["agent_name"] == AGENT_NAME
        assert captured["max_tokens"] == DRAFTER_MAX_TOKENS

    def test_excluded_dimensions_marked_in_prompt_but_scored_dims_carry_quotes(self) -> None:
        sc = _scored_company()
        captured: dict[str, object] = {}

        def fake(**kwargs: object) -> LLMResult:
            captured.update(kwargs)
            return _llm_result(_good_llm_body())

        with patch.object(drafter_mod, "complete", side_effect=fake):
            drafter_node(scored_company=sc, workflow_id="wf-test")

        # The user prompt should:
        #   - include each scored dimension's grounded_quote
        #   - mark each excluded dimension as EXCLUDED — do not cite
        messages: list[dict[str, str]] = captured["messages"]  # type: ignore[assignment]
        assert isinstance(messages, list) and len(messages) == 1
        prompt = messages[0]["content"]
        assert "EXCLUDED" in prompt
        assert "raised a $50M Series B" in prompt  # one of the grounded quotes
        # Operator-internal context is in the prompt but explicitly flagged "do NOT include in prose".
        assert "do NOT include in prose" in prompt


# ---------------------------------------------------------------------------
# Layer B: run_drafter (LLM mocked, full habitat round-trip)
# ---------------------------------------------------------------------------


class TestRunDrafter:
    def test_happy_round_trip(self, conn: sqlite3.Connection, log_root: Path) -> None:
        sc = _scored_company()
        with patch.object(drafter_mod, "complete", return_value=_llm_result(_good_llm_body())):
            result = run_drafter(
                conn,
                scored_company=sc,
                workflow_id="wf-drafter-1",
                log_root=log_root,
            )
        assert result.status is WorkflowStatus.COMPLETED
        assert result.workflow_id == "wf-drafter-1"
        assert result.company_name == "Acme Corp"
        assert result.draft is not None
        assert result.draft.claim_count == 3
        assert result.cost_usd == pytest.approx(0.04)
        assert result.error_step is None
        assert result.error_message is None

        # workflow row
        wf = load_workflow(conn, "wf-drafter-1")
        assert wf is not None
        assert wf.status is WorkflowStatus.COMPLETED
        assert wf.workflow_type == WORKFLOW_TYPE
        assert wf.cost_total_usd == pytest.approx(0.04)
        assert wf.finished_at is not None

        # one step row, COMPLETED, with output_ref
        steps = load_steps(conn, "wf-drafter-1")
        assert len(steps) == 1
        step = steps[0]
        assert step.agent_name == AGENT_NAME
        assert step.status is StepStatus.COMPLETED
        assert step.cost_usd == pytest.approx(0.04)
        assert step.output_ref == "data/logs/2026-05-14/wf-drafter.jsonl:1"
        assert step.finished_at is not None

        # events emitted
        events = load_events(conn, "wf-drafter-1")
        event_types = {e.structured_data.get("event_type") for e in events}
        assert EventType.WORKFLOW_STARTED.value in event_types
        assert EventType.STEP_STARTED.value in event_types
        assert EventType.STEP_COMPLETED.value in event_types
        assert EventType.WORKFLOW_COMPLETED.value in event_types

    def test_step_completed_carries_projection(
        self, conn: sqlite3.Connection, log_root: Path
    ) -> None:
        sc = _scored_company()
        with patch.object(drafter_mod, "complete", return_value=_llm_result(_good_llm_body())):
            result = run_drafter(conn, scored_company=sc, workflow_id="wf-proj", log_root=log_root)
        assert result.status is WorkflowStatus.COMPLETED

        events = load_events(conn, "wf-proj")
        step_completed = [
            e
            for e in events
            if e.structured_data.get("event_type") == EventType.STEP_COMPLETED.value
        ]
        assert len(step_completed) == 1
        sd = step_completed[0].structured_data
        assert sd["paragraph_count"] == 3
        assert sd["char_count"] == len(_GOOD_PROSE)
        assert sd["claim_count"] == 3
        assert sd["step_name"] == AGENT_NAME

    def test_llm_raises_workflow_failed(self, conn: sqlite3.Connection, log_root: Path) -> None:
        sc = _scored_company()
        with patch.object(drafter_mod, "complete", side_effect=RuntimeError("API 500")):
            result = run_drafter(conn, scored_company=sc, workflow_id="wf-fail", log_root=log_root)
        assert result.status is WorkflowStatus.FAILED
        assert result.draft is None
        assert result.error_step == AGENT_NAME
        assert result.error_message is not None
        assert "API 500" in result.error_message

        wf = load_workflow(conn, "wf-fail")
        assert wf is not None
        assert wf.status is WorkflowStatus.FAILED
        assert wf.finished_at is not None

        steps = load_steps(conn, "wf-fail")
        assert len(steps) == 1
        assert steps[0].status is StepStatus.FAILED
        assert steps[0].finished_at is not None

        events = load_events(conn, "wf-fail")
        event_types = {e.structured_data.get("event_type") for e in events}
        assert EventType.STEP_FAILED.value in event_types
        assert EventType.WORKFLOW_FAILED.value in event_types

    def test_below_floor_workflow_failed(self, conn: sqlite3.Connection, log_root: Path) -> None:
        sc = _gated_scored_company(gated_by="score")
        with patch.object(drafter_mod, "complete") as p:
            result = run_drafter(conn, scored_company=sc, workflow_id="wf-gated", log_root=log_root)
            p.assert_not_called()
        assert result.status is WorkflowStatus.FAILED
        assert result.draft is None
        assert result.error_step == AGENT_NAME
        assert result.error_message is not None
        assert "DrafterCalledBelowFloorError" in result.error_message

    def test_parse_error_workflow_failed(self, conn: sqlite3.Connection, log_root: Path) -> None:
        sc = _scored_company()
        with patch.object(drafter_mod, "complete", return_value=_llm_result("not json")):
            result = run_drafter(conn, scored_company=sc, workflow_id="wf-parse", log_root=log_root)
        assert result.status is WorkflowStatus.FAILED
        assert result.error_message is not None
        assert "DrafterParseError" in result.error_message
        # Cost is still recomputed from the FAILED step row (which has cost_usd=0
        # because record_cost was never called before the exception).
        assert result.cost_usd == 0.0


# ---------------------------------------------------------------------------
# CLI (run-drafter)
# ---------------------------------------------------------------------------


def _stub_signal(text: str) -> Signal:
    return Signal(
        text=text,
        source_url="https://example.com/s",
        source_title="Example",
        retrieved_at=datetime(2026, 5, 14, tzinfo=UTC),
    )


def _stub_researcher_llm(citation_text: str) -> LLMResult:
    """Researcher returns an LLMResult with one citation that becomes one Signal."""
    return LLMResult(
        content="Some summary text.",
        model="claude-haiku-4-5-20251001",
        input_tokens=300,
        output_tokens=80,
        cost_usd=0.001,
        jsonl_ref="data/logs/2026-05-14/wf-r.jsonl:1",
        stop_reason="end_turn",
        web_searches=1,
        citations=[
            Citation(
                cited_text=citation_text,
                source_url="https://example.com/acme",
                source_title="Acme overview",
            )
        ],
    )


def _stub_extractor_llm(signal_text: str) -> LLMResult:
    """Extractor returns a CompanyProfile JSON with all five fields extracted, all
    grounded against the single signal text (so the substring check passes)."""
    body = json.dumps(
        {
            "size": {
                "values": ["~500 employees"],
                "source_spans": [{"signal_index": 0, "quote": signal_text}],
            },
            "industry": {
                "values": ["Fintech"],
                "source_spans": [{"signal_index": 0, "quote": signal_text}],
            },
            "tech_stack": {
                "values": ["Python", "AWS"],
                "source_spans": [{"signal_index": 0, "quote": signal_text}],
            },
            "recent_news": {
                "values": ["$50M Series B"],
                "source_spans": [{"signal_index": 0, "quote": signal_text}],
            },
            "decision_makers": {
                "values": ["Jane Doe (CTO)"],
                "source_spans": [{"signal_index": 0, "quote": signal_text}],
            },
        }
    )
    return LLMResult(
        content=body,
        model="claude-sonnet-4-6",
        input_tokens=400,
        output_tokens=200,
        cost_usd=0.005,
        jsonl_ref="data/logs/2026-05-14/wf-e.jsonl:1",
        stop_reason="end_turn",
        web_searches=0,
        citations=[],
    )


def _stub_scorer_llm(signal_text: str, *, score: float = 5.0) -> LLMResult:
    body = json.dumps(
        {
            "dimensions": [
                {
                    "field": name,
                    "score": score,
                    "grounded_quote": signal_text,
                    "reasoning": f"Strong signal for {name} per rubric.",
                }
                for name in PROFILE_FIELD_NAMES
            ]
        }
    )
    return LLMResult(
        content=body,
        model="claude-sonnet-4-6",
        input_tokens=600,
        output_tokens=300,
        cost_usd=0.008,
        jsonl_ref="data/logs/2026-05-14/wf-s.jsonl:1",
        stop_reason="end_turn",
        web_searches=0,
        citations=[],
    )


def _stub_drafter_llm() -> LLMResult:
    return _llm_result(_good_llm_body())


def _rubric_toml(
    *,
    floor: float = 55.0,
    tier_a_min: float = 80.0,
    tier_b_min: float = 65.0,
    tier_c_min: float = 55.0,
) -> str:
    """Build a minimal valid rubric TOML body matching `config/rubric.toml`'s shape.

    Operator-tunable scalars live under `[defaults]`; each dimension is a
    `[dimensions.<name>]` table per ADR-004. Weights sum to 1.0.
    """
    return (
        "[defaults]\n"
        f"floor = {floor}\n"
        "min_coverage = 0.0\n"
        f"tier_a_min = {tier_a_min}\n"
        f"tier_b_min = {tier_b_min}\n"
        f"tier_c_min = {tier_c_min}\n"
        'missing_data_policy = "renormalise"\n'
        "\n"
        "[dimensions.size]\n"
        'field = "size"\nweight = 0.30\nprose = "Score size."\n\n'
        "[dimensions.industry]\n"
        'field = "industry"\nweight = 0.20\nprose = "Score industry."\n\n'
        "[dimensions.tech_stack]\n"
        'field = "tech_stack"\nweight = 0.20\nprose = "Score stack."\n\n'
        "[dimensions.recent_news]\n"
        'field = "recent_news"\nweight = 0.20\nprose = "Score news."\n\n'
        "[dimensions.decision_makers]\n"
        'field = "decision_makers"\nweight = 0.10\nprose = "Score people."\n'
    )


_RUBRIC_TOML_BODY = _rubric_toml()


class TestCli:
    def test_happy_four_stage(self, db_path: Path, tmp_path: Path) -> None:
        signal_text = (
            "Acme Corp is a fintech startup with about 500 employees. "
            "Their stack uses Python and AWS. They recently raised a $50M Series B. "
            "Jane Doe is the CTO."
        )
        rubric_path = tmp_path / "rubric.toml"
        # Write a minimal rubric file the CLI can load.
        rubric_path.write_text(_RUBRIC_TOML_BODY)

        with (
            patch.object(
                researcher_mod, "complete", return_value=_stub_researcher_llm(signal_text)
            ),
            patch.object(extractor_mod, "complete", return_value=_stub_extractor_llm(signal_text)),
            patch.object(scorer_mod, "complete", return_value=_stub_scorer_llm(signal_text)),
            patch.object(drafter_mod, "complete", return_value=_stub_drafter_llm()),
        ):
            runner = CliRunner()
            res = runner.invoke(
                main,
                [
                    "run-drafter",
                    "Acme Corp",
                    "--db",
                    str(db_path),
                    "--rubric",
                    str(rubric_path),
                ],
            )
        assert res.exit_code == 0, res.output
        # Drafter section should render with the coverage-aware disclosure.
        assert "Draft prose:" in res.output
        assert "Enumerated claims" in res.output
        assert "against the rubric" in res.output  # PATTERNS.md #4 disclosure
        assert "decision support" in res.output

    def test_scorer_gates_no_draft_block(self, db_path: Path, tmp_path: Path) -> None:
        # Use a high floor so the scorer gates the workflow → CLI prints
        # the "no draft" block instead of invoking the Drafter.
        signal_text = "Acme Corp does X but provides little detail."
        rubric_path = tmp_path / "rubric.toml"
        rubric_path.write_text(
            # Floor is impossible to clear at this LLM-stub score (~10/100);
            # tier thresholds satisfy tier_a > tier_b > tier_c >= floor.
            _rubric_toml(floor=99.0, tier_a_min=100.0, tier_b_min=99.5, tier_c_min=99.0)
        )

        with (
            patch.object(
                researcher_mod, "complete", return_value=_stub_researcher_llm(signal_text)
            ),
            patch.object(extractor_mod, "complete", return_value=_stub_extractor_llm(signal_text)),
            patch.object(
                scorer_mod, "complete", return_value=_stub_scorer_llm(signal_text, score=0.5)
            ),
            patch.object(drafter_mod, "complete") as drafter_p,
        ):
            runner = CliRunner()
            res = runner.invoke(
                main,
                [
                    "run-drafter",
                    "Acme Corp",
                    "--db",
                    str(db_path),
                    "--rubric",
                    str(rubric_path),
                ],
            )
            drafter_p.assert_not_called()
        assert res.exit_code == 0, res.output
        assert "NO DRAFT" in res.output
        assert "gated by" in res.output
        # The Drafter agent itself was NOT invoked: no Drafter workflow row.
        conn = init_db(db_path)
        try:
            # All scorer-tagged workflows should exist; no drafter ones.
            rows = conn.execute("SELECT workflow_type FROM workflows ORDER BY rowid").fetchall()
        finally:
            conn.close()
        types = [r[0] for r in rows]
        assert "lead_enrichment_drafter" not in types

    def test_scorer_failure_exits_nonzero(self, db_path: Path, tmp_path: Path) -> None:
        signal_text = "Acme is a fintech."
        rubric_path = tmp_path / "rubric.toml"
        rubric_path.write_text(_RUBRIC_TOML_BODY)

        with (
            patch.object(
                researcher_mod, "complete", return_value=_stub_researcher_llm(signal_text)
            ),
            patch.object(extractor_mod, "complete", return_value=_stub_extractor_llm(signal_text)),
            patch.object(scorer_mod, "complete", side_effect=RuntimeError("scorer API 500")),
            patch.object(drafter_mod, "complete") as drafter_p,
        ):
            runner = CliRunner()
            res = runner.invoke(
                main,
                [
                    "run-drafter",
                    "Acme Corp",
                    "--db",
                    str(db_path),
                    "--rubric",
                    str(rubric_path),
                ],
            )
            drafter_p.assert_not_called()
        assert res.exit_code != 0

    def test_drafter_failure_exits_nonzero(self, db_path: Path, tmp_path: Path) -> None:
        signal_text = (
            "Acme Corp is a fintech startup with about 500 employees. "
            "Their stack uses Python and AWS. They recently raised a $50M Series B. "
            "Jane Doe is the CTO."
        )
        rubric_path = tmp_path / "rubric.toml"
        rubric_path.write_text(_RUBRIC_TOML_BODY)

        with (
            patch.object(
                researcher_mod, "complete", return_value=_stub_researcher_llm(signal_text)
            ),
            patch.object(extractor_mod, "complete", return_value=_stub_extractor_llm(signal_text)),
            patch.object(scorer_mod, "complete", return_value=_stub_scorer_llm(signal_text)),
            patch.object(drafter_mod, "complete", side_effect=RuntimeError("opus 500")),
        ):
            runner = CliRunner()
            res = runner.invoke(
                main,
                [
                    "run-drafter",
                    "Acme Corp",
                    "--db",
                    str(db_path),
                    "--rubric",
                    str(rubric_path),
                ],
            )
        assert res.exit_code != 0
        assert "FAILED" in res.output

    def test_missing_rubric_clean_error(self, db_path: Path, tmp_path: Path) -> None:
        runner = CliRunner()
        res = runner.invoke(
            main,
            [
                "run-drafter",
                "Acme Corp",
                "--db",
                str(db_path),
                "--rubric",
                str(tmp_path / "missing.toml"),
            ],
        )
        assert res.exit_code != 0
        assert "missing" in res.output.lower() or "no such file" in res.output.lower()


# ---------------------------------------------------------------------------
# Live smoke — full Researcher → Extractor → Scorer → Drafter chain
# ---------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY")
    or os.environ["ANTHROPIC_API_KEY"].startswith("sk-ant-REPLACE"),
    reason="ANTHROPIC_API_KEY not set; live smoke skipped.",
)
def test_live_drafter_round_trip(conn: sqlite3.Connection, log_root: Path) -> None:
    """End-to-end live: Researcher → Extractor → Scorer → Drafter against a real company.

    This is the load-bearing calibration moment for Slice 5: does the
    Opus 4.7 prose produce claims that ACTUALLY substring-match their
    cited upstream evidence on first contact, or does the model
    over-reach? The Extractor's first live smoke (Slice 3) caught two
    Sonnet over-reaches; the Scorer's caught zero. Opus is more capable
    but the prose temptation is higher — record what we see for Slice 7.

    Note: the Slice 5 Drafter input is just the ScoredCompany; the
    fabrication-resistance check the Drafter performs internally is the
    cross-input invariant ("each claim's supporting_dimension is a
    non-excluded dimension on the input ScoredCompany"). The full
    substring-grounding check (claim.text → DimensionScore.grounded_quote
    → ProfileField.source_spans → Signal.text) is the Slice 7 critic's
    job; what we record here is the calibration observation that
    informs that critic.
    """
    from agent_habitat.agents.researcher import run_researcher
    from agent_habitat.scoring import load_rubric

    # Load the bundled rubric, then RELAX the floor / tier thresholds for the
    # calibration. Reason: the bundled rubric is tuned for production and may
    # gate any one company on this slice (Anthropic gated on the Slice-5
    # first run with score=20 / coverage=20% → below floor=55). The point
    # of this live smoke is to observe Opus's first-contact prose behaviour,
    # not to test the bundled rubric — so swap to a permissive variant
    # whose floor any non-pathological score will clear.
    bundled = load_rubric(Path("config/rubric.toml"))
    rubric = bundled.model_copy(
        update={
            "floor": 0.0,
            "tier_a_min": 80.0,
            "tier_b_min": 65.0,
            "tier_c_min": 0.0,
        }
    )

    research = run_researcher(conn, company_name="Anthropic", max_searches=3, log_root=log_root)
    assert research.status is WorkflowStatus.COMPLETED, research.error_message

    extract = extractor_mod.run_extractor(conn, raw_signals=research.raw_signals, log_root=log_root)
    assert extract.status is WorkflowStatus.COMPLETED, extract.error_message

    score = scorer_mod.run_scorer(conn, profile=extract.profile, rubric=rubric, log_root=log_root)
    assert score.status is WorkflowStatus.COMPLETED, score.error_message
    sc = score.scored_company

    if not sc.routes_to_draft:
        pytest.skip(
            f"ScoredCompany was gated ({sc.gated_by}); Drafter would not run. "
            f"score={sc.score} coverage={sc.coverage}"
        )

    draft_result = run_drafter(conn, scored_company=sc, log_root=log_root)
    assert draft_result.status is WorkflowStatus.COMPLETED, draft_result.error_message
    assert draft_result.draft is not None

    # Persisted state for all four workflows.
    for wf_id in (
        research.workflow_id,
        extract.workflow_id,
        score.workflow_id,
        draft_result.workflow_id,
    ):
        wf = load_workflow(conn, wf_id)
        assert wf is not None
        assert wf.status is WorkflowStatus.COMPLETED
        assert wf.cost_total_usd is not None

    # The drafter step's output_ref points at a real JSONL line.
    drafter_steps = load_steps(conn, draft_result.workflow_id)
    assert len(drafter_steps) == 1
    assert drafter_steps[0].output_ref is not None

    # SLICE 7 PRECONDITION CHECK — perform the substring chain check here
    # (manually; the critic doesn't exist yet) and record what we find.
    # For each Draft claim, verify its text substring-matches the cited
    # dimension's grounded_quote (the first hop). This is exactly the
    # check the critic will mechanise.
    from agent_habitat.agents.extractor import _normalise_for_substring

    by_field = {d.field: d for d in sc.dimensions}
    over_reaches: list[tuple[str, str, str]] = []
    for c in draft_result.draft.claims:
        dim = by_field[c.supporting_dimension]
        assert dim.grounded_quote is not None, "claim should not target excluded dim"
        claim_norm = _normalise_for_substring(c.text)
        quote_norm = _normalise_for_substring(dim.grounded_quote)
        if claim_norm not in quote_norm:
            over_reaches.append((c.supporting_dimension, c.text, dim.grounded_quote))

    print("\n--- LIVE DRAFTER CALIBRATION ---")
    print(f"  Researcher cost : ${research.cost_usd:.6f}")
    print(f"  Extractor cost  : ${extract.cost_usd:.6f}")
    print(f"  Scorer cost     : ${score.cost_usd:.6f}")
    print(f"  Drafter cost    : ${draft_result.cost_usd:.6f}")
    total = research.cost_usd + extract.cost_usd + score.cost_usd + draft_result.cost_usd
    print(f"  TOTAL           : ${total:.6f}")
    print(f"  Score           : {sc.score}")
    print(f"  Coverage        : {sc.coverage:.2%}")
    print(f"  Tier            : {sc.tier}")
    print(f"  Paragraphs      : {draft_result.draft.paragraph_count}")
    print(f"  Chars           : {draft_result.draft.char_count}")
    print(f"  Claims          : {draft_result.draft.claim_count}")
    print("  --- DRAFT PROSE ---")
    print(draft_result.draft.prose)
    print("  --- CLAIMS ---")
    for i, c in enumerate(draft_result.draft.claims, start=1):
        print(f"    [{i}] dim={c.supporting_dimension}: {c.text!r}")
    if over_reaches:
        print("  *** GROUNDING-CHECK OVER-REACHES (Slice 7 will need to catch) ***")
        for dim_name, claim_text, quote in over_reaches:
            print(f"    - dim={dim_name}: claim={claim_text!r} NOT IN quote={quote!r}")
    else:
        print(
            "  GROUNDING SURVIVED FIRST CONTACT — every claim substring-matched its cited "
            "DimensionScore.grounded_quote. (The full chain to Signal.text is the critic's job.)"
        )
