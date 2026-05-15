"""Deterministic tests for the Scorer agent — Phase 2 Slice 4.

Covers:
  - Rubric loader: valid load, every documented failure mode.
  - Models: `DimensionScore` + `ScoredCompany` validation, frozen, extra=forbid.
  - Pure scoring math: full / partial / all-gaps coverage; tier assignment;
    gating with coverage-precedence.
  - Grounding check: pass + downgrade on over-reach; reuses the Slice 3
    normaliser.
  - End-to-end `run_scorer` with a mocked LLM: happy run, gap-heavy run,
    all-gaps short-circuit, below-floor / below-coverage gating, schema
    failure → step+workflow FAILED.
  - CLI: happy three-stage path; researcher / extractor / scorer failures
    each exit non-zero.

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

from agent_habitat.agents import extractor as extractor_mod
from agent_habitat.agents import researcher as researcher_mod
from agent_habitat.agents import scorer as scorer_mod
from agent_habitat.agents.models import (
    PROFILE_FIELD_NAMES,
    CompanyProfile,
    DimensionScore,
    ProfileField,
    ScoredCompany,
    Signal,
    SourceSpan,
)
from agent_habitat.agents.scorer import (
    AGENT_NAME,
    EXCLUSION_GAP_PREFIX,
    EXCLUSION_GROUNDING_FAIL_PREFIX,
    WORKFLOW_TYPE,
    run_scorer,
)
from agent_habitat.cli import main
from agent_habitat.llm import Citation, LLMResult
from agent_habitat.observability import EventType
from agent_habitat.scoring import (
    DimensionConfig,
    RubricConfig,
    RubricConfigError,
    load_rubric,
)
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


def _signal(text: str, url: str = "https://example.com/s") -> Signal:
    return Signal(
        text=text,
        source_url=url,
        source_title=None,
        retrieved_at=datetime(2026, 5, 14, tzinfo=UTC),
    )


def _llm_result(content: str, *, cost_usd: float = 0.005) -> LLMResult:
    return LLMResult(
        content=content,
        model="claude-sonnet-4-6",
        input_tokens=1500,
        output_tokens=300,
        cost_usd=cost_usd,
        jsonl_ref="data/logs/2026-05-14/wf-scorer.jsonl:1",
        stop_reason="end_turn",
        web_searches=0,
        citations=[],
    )


def _five_dim_rubric(
    *,
    floor: float = 55.0,
    min_coverage: float = 0.0,
    tier_a_min: float = 80.0,
    tier_b_min: float = 65.0,
    tier_c_min: float = 55.0,
    weights: tuple[float, float, float, float, float] = (0.30, 0.20, 0.20, 0.20, 0.10),
) -> RubricConfig:
    """One rubric per PROFILE_FIELD_NAMES — the bundled-shape default."""
    dims = [
        DimensionConfig(name=name, field=name, weight=w, prose=f"Score {name} 0-5.")
        for name, w in zip(PROFILE_FIELD_NAMES, weights, strict=True)
    ]
    return RubricConfig(
        floor=floor,
        min_coverage=min_coverage,
        tier_a_min=tier_a_min,
        tier_b_min=tier_b_min,
        tier_c_min=tier_c_min,
        missing_data_policy="renormalise",
        dimensions=dims,
    )


def _extracted_field(value: str, *, signal_text: str) -> ProfileField:
    """One extracted field whose source_span quote is a substring of `signal_text`."""
    # Use a short slice that's verbatim inside signal_text.
    quote = signal_text  # whole signal text — easy to substring-match against
    return ProfileField(
        values=[value],
        source_spans=[SourceSpan(signal_index=0, quote=quote)],
    )


def _full_profile(company: str = "Acme Corp") -> CompanyProfile:
    """A profile with every field extracted (full coverage)."""
    return CompanyProfile(
        company_name=company,
        size=_extracted_field("~500 employees", signal_text="approximately 500 employees"),
        industry=_extracted_field("Fintech", signal_text="a fintech startup"),
        tech_stack=_extracted_field("Python", signal_text="built on Python and AWS"),
        recent_news=_extracted_field(
            "Series B raise", signal_text="raised a $50M Series B led by Sequoia"
        ),
        decision_makers=_extracted_field(
            "Jane Doe (CTO)", signal_text="Jane Doe, CTO, joined in 2026"
        ),
    )


def _partial_profile(company: str = "Acme Corp") -> CompanyProfile:
    """A profile where 2/5 fields are extracted, 3/5 are gaps (Slice-3-typical)."""
    return CompanyProfile(
        company_name=company,
        size=ProfileField.as_gap("field_not_in_signals"),
        industry=_extracted_field("Fintech", signal_text="a fintech startup"),
        tech_stack=ProfileField.as_gap("field_not_in_signals"),
        recent_news=_extracted_field(
            "Series B raise", signal_text="raised a $50M Series B led by Sequoia"
        ),
        decision_makers=ProfileField.as_gap("span_not_grounded"),
    )


def _all_gaps_profile(company: str = "Acme Corp") -> CompanyProfile:
    return CompanyProfile(
        company_name=company,
        size=ProfileField.as_gap("no_signals"),
        industry=ProfileField.as_gap("no_signals"),
        tech_stack=ProfileField.as_gap("no_signals"),
        recent_news=ProfileField.as_gap("no_signals"),
        decision_makers=ProfileField.as_gap("no_signals"),
    )


def _llm_body_for_fields(field_specs: list[dict[str, object]]) -> str:
    """Build a JSON body the LLM "returns". One entry per scored field."""
    return json.dumps({"dimensions": field_specs})


# ---------------------------------------------------------------------------
# Rubric loader
# ---------------------------------------------------------------------------


class TestRubricLoader:
    def test_valid_rubric_loads(self, tmp_path: Path) -> None:
        p = tmp_path / "rubric.toml"
        p.write_text(
            "[defaults]\n"
            "floor = 55.0\n"
            "min_coverage = 0.0\n"
            "tier_a_min = 80.0\n"
            "tier_b_min = 65.0\n"
            "tier_c_min = 55.0\n"
            'missing_data_policy = "renormalise"\n'
            "\n"
            "[dimensions.industry]\n"
            'field = "industry"\n'
            "weight = 0.6\n"
            'prose = "Score industry."\n'
            "\n"
            "[dimensions.size]\n"
            'field = "size"\n'
            "weight = 0.4\n"
            'prose = "Score size."\n'
        )
        r = load_rubric(p)
        assert r.floor == 55.0
        assert r.tier_a_min == 80.0
        assert len(r.dimensions) == 2
        assert {d.field for d in r.dimensions} == {"industry", "size"}
        assert sum(d.weight for d in r.dimensions) == pytest.approx(1.0)

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(RubricConfigError, match="not found"):
            load_rubric(tmp_path / "nope.toml")

    def test_malformed_toml_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "rubric.toml"
        p.write_text("[defaults\nthis is not toml")
        with pytest.raises(RubricConfigError, match="malformed TOML"):
            load_rubric(p)

    def test_missing_defaults_section_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "rubric.toml"
        p.write_text("[dimensions.x]\nfield='size'\nweight=1.0\nprose='x'\n")
        with pytest.raises(RubricConfigError, match="defaults"):
            load_rubric(p)

    @pytest.mark.parametrize(
        "missing_key",
        ["floor", "min_coverage", "tier_a_min", "tier_b_min", "tier_c_min", "missing_data_policy"],
    )
    def test_missing_required_defaults_key_raises(self, tmp_path: Path, missing_key: str) -> None:
        p = tmp_path / "rubric.toml"
        defaults = {
            "floor": 55.0,
            "min_coverage": 0.0,
            "tier_a_min": 80.0,
            "tier_b_min": 65.0,
            "tier_c_min": 55.0,
            "missing_data_policy": "renormalise",
        }
        del defaults[missing_key]
        lines = ["[defaults]"]
        for k, v in defaults.items():
            lines.append(f"{k} = {v!r}" if isinstance(v, str) else f"{k} = {v}")
        lines.append("")
        lines.append("[dimensions.size]")
        lines.append('field = "size"')
        lines.append("weight = 1.0")
        lines.append('prose = "x"')
        p.write_text("\n".join(lines) + "\n")
        with pytest.raises(RubricConfigError, match=missing_key):
            load_rubric(p)

    def test_weights_not_summing_to_one_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "rubric.toml"
        p.write_text(
            "[defaults]\n"
            "floor = 55.0\n"
            "min_coverage = 0.0\n"
            "tier_a_min = 80.0\n"
            "tier_b_min = 65.0\n"
            "tier_c_min = 55.0\n"
            'missing_data_policy = "renormalise"\n'
            "\n"
            "[dimensions.industry]\n"
            'field = "industry"\n'
            "weight = 0.5\n"
            'prose = "x"\n'
            "[dimensions.size]\n"
            'field = "size"\n'
            "weight = 0.6\n"  # 0.5 + 0.6 = 1.1, not 1.0
            'prose = "x"\n'
        )
        with pytest.raises(RubricConfigError, match="weights must sum to 1.0"):
            load_rubric(p)

    def test_invalid_field_name_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "rubric.toml"
        p.write_text(
            "[defaults]\n"
            "floor = 55.0\n"
            "min_coverage = 0.0\n"
            "tier_a_min = 80.0\n"
            "tier_b_min = 65.0\n"
            "tier_c_min = 55.0\n"
            'missing_data_policy = "renormalise"\n'
            "\n"
            "[dimensions.bogus]\n"
            'field = "not_a_real_profile_field"\n'
            "weight = 1.0\n"
            'prose = "x"\n'
        )
        with pytest.raises(RubricConfigError, match="not_a_real_profile_field"):
            load_rubric(p)

    @pytest.mark.parametrize(
        "tiers",
        [
            # tier_a_min not > tier_b_min
            (50.0, 80.0, 55.0),
            # tier_b_min not > tier_c_min
            (80.0, 50.0, 55.0),
            # tier_c_min < floor
            (80.0, 65.0, 40.0),  # floor=55
        ],
    )
    def test_misordered_tier_thresholds_raise(
        self, tmp_path: Path, tiers: tuple[float, float, float]
    ) -> None:
        a, b, c = tiers
        p = tmp_path / "rubric.toml"
        p.write_text(
            "[defaults]\n"
            "floor = 55.0\n"
            "min_coverage = 0.0\n"
            f"tier_a_min = {a}\n"
            f"tier_b_min = {b}\n"
            f"tier_c_min = {c}\n"
            'missing_data_policy = "renormalise"\n'
            "\n"
            "[dimensions.size]\n"
            'field = "size"\n'
            "weight = 1.0\n"
            'prose = "x"\n'
        )
        with pytest.raises(RubricConfigError, match="tier thresholds"):
            load_rubric(p)

    def test_invalid_missing_data_policy_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "rubric.toml"
        p.write_text(
            "[defaults]\n"
            "floor = 55.0\n"
            "min_coverage = 0.0\n"
            "tier_a_min = 80.0\n"
            "tier_b_min = 65.0\n"
            "tier_c_min = 55.0\n"
            'missing_data_policy = "gap_is_zero"\n'  # rejected
            "\n"
            "[dimensions.size]\n"
            'field = "size"\n'
            "weight = 1.0\n"
            'prose = "x"\n'
        )
        with pytest.raises(RubricConfigError, match="renormalise"):
            load_rubric(p)

    def test_no_dimensions_section_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "rubric.toml"
        p.write_text(
            "[defaults]\n"
            "floor = 55.0\n"
            "min_coverage = 0.0\n"
            "tier_a_min = 80.0\n"
            "tier_b_min = 65.0\n"
            "tier_c_min = 55.0\n"
            'missing_data_policy = "renormalise"\n'
        )
        with pytest.raises(RubricConfigError, match="dimensions"):
            load_rubric(p)

    def test_dimension_missing_required_key_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "rubric.toml"
        p.write_text(
            "[defaults]\n"
            "floor = 55.0\n"
            "min_coverage = 0.0\n"
            "tier_a_min = 80.0\n"
            "tier_b_min = 65.0\n"
            "tier_c_min = 55.0\n"
            'missing_data_policy = "renormalise"\n'
            "\n"
            "[dimensions.size]\n"
            'field = "size"\n'
            # weight + prose missing
        )
        with pytest.raises(RubricConfigError, match="weight"):
            load_rubric(p)

    def test_duplicate_field_across_dimensions_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "rubric.toml"
        p.write_text(
            "[defaults]\n"
            "floor = 55.0\n"
            "min_coverage = 0.0\n"
            "tier_a_min = 80.0\n"
            "tier_b_min = 65.0\n"
            "tier_c_min = 55.0\n"
            'missing_data_policy = "renormalise"\n'
            "\n"
            "[dimensions.a]\n"
            'field = "size"\n'
            "weight = 0.5\n"
            'prose = "x"\n'
            "\n"
            "[dimensions.b]\n"
            'field = "size"\n'  # duplicate
            "weight = 0.5\n"
            'prose = "x"\n'
        )
        with pytest.raises(RubricConfigError, match="duplicate"):
            load_rubric(p)

    def test_bundled_rubric_loads(self) -> None:
        """The shipped template must always parse — guards against regressions."""
        r = load_rubric(Path("config/rubric.toml"))
        assert r.floor > 0
        assert 0.0 <= r.min_coverage <= 1.0
        assert sum(d.weight for d in r.dimensions) == pytest.approx(1.0)
        for d in r.dimensions:
            assert d.field in PROFILE_FIELD_NAMES


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class TestDimensionScoreModel:
    def test_scored_dimension_validates(self) -> None:
        d = DimensionScore(
            field="size",
            weight=0.2,
            score=4.0,
            grounded_quote="approximately 500",
            reasoning="size fits Series B band",
        )
        assert d.is_excluded is False

    def test_excluded_dimension_validates(self) -> None:
        d = DimensionScore(
            field="size",
            weight=0.2,
            score=None,
            grounded_quote=None,
            reasoning="field was a gap",
        )
        assert d.is_excluded is True

    def test_mixed_score_and_quote_rejected(self) -> None:
        # score set but quote missing
        with pytest.raises(ValidationError):
            DimensionScore(
                field="size",
                weight=0.2,
                score=4.0,
                grounded_quote=None,
                reasoning="x",
            )
        # quote set but score missing
        with pytest.raises(ValidationError):
            DimensionScore(
                field="size",
                weight=0.2,
                score=None,
                grounded_quote="some quote",
                reasoning="x",
            )

    def test_invalid_field_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            DimensionScore(
                field="not_a_field",
                weight=0.1,
                score=3.0,
                grounded_quote="x",
                reasoning="x",
            )

    def test_score_out_of_range_rejected(self) -> None:
        with pytest.raises(ValidationError):
            DimensionScore(field="size", weight=0.1, score=5.5, grounded_quote="x", reasoning="x")

    def test_extra_forbid(self) -> None:
        with pytest.raises(ValidationError):
            DimensionScore.model_validate(
                {
                    "field": "size",
                    "weight": 0.2,
                    "score": 3.0,
                    "grounded_quote": "x",
                    "reasoning": "x",
                    "unexpected": 1,
                }
            )

    def test_frozen(self) -> None:
        d = DimensionScore(field="size", weight=0.2, score=3.0, grounded_quote="x", reasoning="x")
        with pytest.raises(ValidationError):
            d.score = 4.0  # type: ignore[misc]


class TestScoredCompanyModel:
    def _scored(self, **overrides: object) -> ScoredCompany:
        defaults: dict[str, object] = {
            "company_name": "Acme",
            "score": 75.0,
            "coverage": 1.0,
            "floor": 55.0,
            "min_coverage": 0.0,
            "passed_floor": True,
            "passed_coverage": True,
            "tier": "B",
            "gated_by": None,
            "dimensions": [
                DimensionScore(
                    field=name,
                    weight=0.2,
                    score=3.0,
                    grounded_quote="x",
                    reasoning="x",
                )
                for name in PROFILE_FIELD_NAMES
            ],
        }
        defaults.update(overrides)
        return ScoredCompany(**defaults)  # type: ignore[arg-type]

    def test_validates(self) -> None:
        sc = self._scored()
        assert sc.score == 75.0
        assert sc.routes_to_draft is True

    def test_tier_and_score_must_travel_together(self) -> None:
        # tier set but score None
        with pytest.raises(ValidationError):
            self._scored(score=None, tier="A", passed_floor=False)
        # score set but tier None
        with pytest.raises(ValidationError):
            self._scored(score=75.0, tier=None)

    def test_invalid_tier_rejected(self) -> None:
        with pytest.raises(ValidationError):
            self._scored(tier="bogus")

    def test_invalid_gated_by_rejected(self) -> None:
        with pytest.raises(ValidationError):
            self._scored(gated_by="other")

    def test_extra_forbid(self) -> None:
        with pytest.raises(ValidationError):
            ScoredCompany.model_validate(
                {
                    "company_name": "Acme",
                    "score": 75.0,
                    "coverage": 1.0,
                    "floor": 55.0,
                    "min_coverage": 0.0,
                    "passed_floor": True,
                    "passed_coverage": True,
                    "tier": "B",
                    "gated_by": None,
                    "dimensions": [],
                    "unexpected": 1,
                }
            )

    def test_frozen(self) -> None:
        sc = self._scored()
        with pytest.raises(ValidationError):
            sc.score = 90.0  # type: ignore[misc]

    def test_round_trip(self) -> None:
        sc = self._scored()
        payload = sc.model_dump()
        sc2 = ScoredCompany.model_validate(payload)
        assert sc2 == sc

    def test_routes_to_draft_false_when_gated(self) -> None:
        sc = self._scored(score=30.0, tier="below_c", passed_floor=False, gated_by="score")
        assert sc.routes_to_draft is False


# ---------------------------------------------------------------------------
# Pure scoring math + grounding check
# ---------------------------------------------------------------------------


class TestComputeComposite:
    def test_full_coverage_renormalises_to_one(self) -> None:
        # 5 dimensions, all scored at 3.0, weights summing to 1.0
        # composite = (sum(3.0 * w) / sum(w)) * 20 = 3.0 / 1.0 * 20 = 60.0
        dims = [
            DimensionScore(field=name, weight=w, score=3.0, grounded_quote="x", reasoning="x")
            for name, w in zip(PROFILE_FIELD_NAMES, (0.30, 0.20, 0.20, 0.20, 0.10))
        ]
        score, coverage = scorer_mod._compute_composite(dims)
        assert score == pytest.approx(60.0)
        assert coverage == pytest.approx(1.0)

    def test_partial_coverage_renormalises_over_present(self) -> None:
        # 2 scored, 3 gap-excluded. Scored weights: 0.30 + 0.20 = 0.50.
        # Scores: 5.0 (industry, w=0.30), 2.0 (recent_news, w=0.20).
        # Weighted sum: 5*0.30 + 2*0.20 = 1.5 + 0.4 = 1.9. Avg: 1.9/0.50 = 3.8.
        # Composite: 3.8 * 20 = 76.0.
        dims = [
            DimensionScore(field="size", weight=0.30, reasoning="gap"),
            DimensionScore(
                field="industry",
                weight=0.20,
                score=5.0,
                grounded_quote="x",
                reasoning="x",
            ),
            DimensionScore(field="tech_stack", weight=0.20, reasoning="gap"),
            DimensionScore(
                field="recent_news",
                weight=0.10,
                score=2.0,
                grounded_quote="x",
                reasoning="x",
            ),
            DimensionScore(field="decision_makers", weight=0.20, reasoning="gap"),
        ]
        # Recompute with the weights actually used in dims:
        # present = industry (w=0.20, s=5.0), recent_news (w=0.10, s=2.0)
        # weight_sum = 0.30; weighted = 5*0.20 + 2*0.10 = 1.0 + 0.2 = 1.2.
        # composite_5 = 1.2 / 0.30 = 4.0; composite_100 = 80.0; coverage = 0.30.
        score, coverage = scorer_mod._compute_composite(dims)
        assert score == pytest.approx(80.0)
        assert coverage == pytest.approx(0.30)

    def test_all_excluded_score_is_none(self) -> None:
        dims = [
            DimensionScore(field=name, weight=0.2, reasoning="gap") for name in PROFILE_FIELD_NAMES
        ]
        score, coverage = scorer_mod._compute_composite(dims)
        assert score is None
        assert coverage == 0.0


class TestAssignTier:
    @pytest.fixture
    def rubric(self) -> RubricConfig:
        return _five_dim_rubric()  # tier_a_min=80, tier_b_min=65, tier_c_min=55, floor=55

    @pytest.mark.parametrize(
        "score, expected",
        [
            (None, None),
            (95.0, "A"),
            (80.0, "A"),  # >= tier_a_min
            (79.9, "B"),
            (65.0, "B"),
            (64.9, "C"),
            (55.0, "C"),
            (54.9, "below_c"),
            (0.0, "below_c"),
        ],
    )
    def test_band_assignment(
        self, rubric: RubricConfig, score: float | None, expected: str | None
    ) -> None:
        assert scorer_mod._assign_tier(score, rubric) == expected


class TestGating:
    def test_both_passed_yields_none(self) -> None:
        passed_f, passed_c, gated = scorer_mod._gating(
            score=70.0, coverage=1.0, floor=55.0, min_coverage=0.5
        )
        assert passed_f is True
        assert passed_c is True
        assert gated is None

    def test_score_below_floor_gated_by_score(self) -> None:
        passed_f, passed_c, gated = scorer_mod._gating(
            score=40.0, coverage=1.0, floor=55.0, min_coverage=0.0
        )
        assert passed_f is False
        assert passed_c is True
        assert gated == "score"

    def test_coverage_below_min_gated_by_coverage(self) -> None:
        passed_f, passed_c, gated = scorer_mod._gating(
            score=85.0, coverage=0.10, floor=55.0, min_coverage=0.50
        )
        assert passed_f is True
        assert passed_c is False
        assert gated == "coverage"

    def test_coverage_takes_precedence_when_both_fail(self) -> None:
        # Both gates fail → coverage wins (more informative empty-outcome).
        passed_f, passed_c, gated = scorer_mod._gating(
            score=20.0, coverage=0.10, floor=55.0, min_coverage=0.50
        )
        assert passed_f is False
        assert passed_c is False
        assert gated == "coverage"

    def test_all_gaps_with_min_coverage_zero_gated_by_score(self) -> None:
        # ADR-004 §5 Forward deps: all-gaps + min_coverage=0 → gated_by="score".
        passed_f, passed_c, gated = scorer_mod._gating(
            score=None, coverage=0.0, floor=55.0, min_coverage=0.0
        )
        assert passed_f is False
        assert passed_c is True  # 0.0 >= 0.0
        assert gated == "score"

    def test_all_gaps_with_min_coverage_positive_gated_by_coverage(self) -> None:
        # ADR-004 §5 Forward deps: all-gaps + min_coverage>0 → gated_by="coverage".
        passed_f, passed_c, gated = scorer_mod._gating(
            score=None, coverage=0.0, floor=55.0, min_coverage=0.10
        )
        assert passed_f is False
        assert passed_c is False
        assert gated == "coverage"


class TestGroundingCheck:
    def test_substring_match_passes(self) -> None:
        field = _extracted_field("Fintech", signal_text="Acme is a leading fintech startup in NYC")
        assert scorer_mod._grounded_in_field("fintech startup", field) is True

    def test_whitespace_collapse_and_case_normalised(self) -> None:
        field = _extracted_field("Fintech", signal_text="Acme is a leading fintech startup in NYC")
        # Different whitespace + case still grounds because of normalisation.
        assert scorer_mod._grounded_in_field("FINTECH    Startup", field) is True

    def test_overreach_fails(self) -> None:
        field = _extracted_field("Fintech", signal_text="Acme is a leading fintech startup")
        # "regulated industries" is not anywhere in the source_spans.
        assert scorer_mod._grounded_in_field("regulated industries", field) is False

    def test_gap_field_returns_false(self) -> None:
        gap = ProfileField.as_gap("no_signals")
        assert scorer_mod._grounded_in_field("anything", gap) is False

    def test_empty_quote_returns_false(self) -> None:
        field = _extracted_field("x", signal_text="anything")
        assert scorer_mod._grounded_in_field("   ", field) is False

    def test_check_uses_slice3_normaliser(self) -> None:
        """Reused normaliser is `extractor._normalise_for_substring` (kickoff requirement)."""
        # Hard property: import path is the Slice 3 module, not a re-implementation here.
        from agent_habitat.agents.extractor import _normalise_for_substring as ext_norm
        from agent_habitat.agents.scorer import _normalise_for_substring as scorer_norm

        assert ext_norm is scorer_norm  # same function object


# ---------------------------------------------------------------------------
# `run_scorer` happy path + projection + audit chain
# ---------------------------------------------------------------------------


class TestRunHappy:
    def test_full_coverage_run(self, conn: sqlite3.Connection, log_root: Path) -> None:
        rubric = _five_dim_rubric(weights=(0.30, 0.20, 0.20, 0.20, 0.10))
        profile = _full_profile()

        body = _llm_body_for_fields(
            [
                {
                    "field": "size",
                    "score": 4.0,
                    "grounded_quote": "approximately 500 employees",
                    "reasoning": "matches Series B band",
                },
                {
                    "field": "industry",
                    "score": 5.0,
                    "grounded_quote": "fintech startup",
                    "reasoning": "regulated industry — top band",
                },
                {
                    "field": "tech_stack",
                    "score": 4.0,
                    "grounded_quote": "Python and AWS",
                    "reasoning": "Python stack",
                },
                {
                    "field": "recent_news",
                    "score": 5.0,
                    "grounded_quote": "Series B led by Sequoia",
                    "reasoning": "trigger present",
                },
                {
                    "field": "decision_makers",
                    "score": 5.0,
                    "grounded_quote": "Jane Doe, CTO",
                    "reasoning": "senior technical leader",
                },
            ]
        )
        llm_res = _llm_result(body, cost_usd=0.0123)
        with patch.object(scorer_mod, "complete", return_value=llm_res):
            result = run_scorer(
                conn,
                profile=profile,
                rubric=rubric,
                log_root=log_root,
            )

        assert result.status is WorkflowStatus.COMPLETED
        assert result.error_step is None
        sc = result.scored_company
        # composite: weighted avg = 4*0.3 + 5*0.2 + 4*0.2 + 5*0.2 + 5*0.1 = 1.2+1+0.8+1+0.5 = 4.5
        # composite_100 = 90.0
        assert sc.score == pytest.approx(90.0)
        assert sc.coverage == pytest.approx(1.0)
        assert sc.tier == "A"
        assert sc.gated_by is None
        assert sc.passed_floor is True
        assert sc.passed_coverage is True
        assert all(not d.is_excluded for d in sc.dimensions)
        assert len(sc.dimensions) == 5

    def test_workflow_persists_with_step_and_events(
        self, conn: sqlite3.Connection, log_root: Path
    ) -> None:
        rubric = _five_dim_rubric()
        profile = _full_profile()
        body = _llm_body_for_fields(
            [
                {
                    "field": name,
                    "score": 3.0,
                    "grounded_quote": _profile_quote(profile, name),
                    "reasoning": "x",
                }
                for name in PROFILE_FIELD_NAMES
            ]
        )
        llm_res = _llm_result(body, cost_usd=0.005)
        with patch.object(scorer_mod, "complete", return_value=llm_res):
            result = run_scorer(conn, profile=profile, rubric=rubric, log_root=log_root)

        wf = load_workflow(conn, result.workflow_id)
        assert wf is not None
        assert wf.status is WorkflowStatus.COMPLETED
        assert wf.workflow_type == WORKFLOW_TYPE
        assert wf.cost_total_usd == pytest.approx(0.005)
        steps = load_steps(conn, result.workflow_id)
        assert len(steps) == 1
        assert steps[0].agent_name == AGENT_NAME
        assert steps[0].status is StepStatus.COMPLETED
        assert steps[0].cost_usd == pytest.approx(0.005)
        assert steps[0].output_ref == "data/logs/2026-05-14/wf-scorer.jsonl:1"
        events = load_events(conn, result.workflow_id)
        event_types = [e.structured_data.get("event_type") for e in events]
        assert EventType.WORKFLOW_STARTED.value in event_types
        assert EventType.STEP_STARTED.value in event_types
        assert EventType.STEP_COMPLETED.value in event_types
        assert EventType.WORKFLOW_COMPLETED.value in event_types

    def test_projection_mirrored_onto_step_completed(
        self, conn: sqlite3.Connection, log_root: Path
    ) -> None:
        rubric = _five_dim_rubric()
        profile = _full_profile()
        body = _llm_body_for_fields(
            [
                {
                    "field": name,
                    "score": 3.0,
                    "grounded_quote": _profile_quote(profile, name),
                    "reasoning": "x",
                }
                for name in PROFILE_FIELD_NAMES
            ]
        )
        with patch.object(scorer_mod, "complete", return_value=_llm_result(body)):
            result = run_scorer(conn, profile=profile, rubric=rubric, log_root=log_root)
        events = load_events(conn, result.workflow_id)
        step_completed = next(
            e
            for e in events
            if e.structured_data.get("event_type") == EventType.STEP_COMPLETED.value
        )
        sd = step_completed.structured_data
        # Per ADR-004 / ADR-006 §1.3 projection:
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
            assert key in sd, f"missing projection key {key!r}"
        assert sd["coverage"] == pytest.approx(1.0)
        assert sd["floor"] == 55.0
        assert sd["min_coverage"] == 0.0
        # gated_by is None for a passing run — it should be present as None.
        assert sd["gated_by"] is None


def _profile_quote(profile: CompanyProfile, field_name: str) -> str:
    """Pull the first source_span quote off a profile field — for happy-path mocks."""
    pf = profile.field(field_name)
    assert not pf.is_gap
    return pf.source_spans[0].quote


# ---------------------------------------------------------------------------
# All-gaps / partial-coverage / below-floor / below-coverage outcomes
# ---------------------------------------------------------------------------


class TestEmptyAndGappy:
    def test_all_gaps_short_circuits_no_llm_call(
        self, conn: sqlite3.Connection, log_root: Path
    ) -> None:
        rubric = _five_dim_rubric(min_coverage=0.0)
        profile = _all_gaps_profile()
        # Mock should NOT be called.
        with patch.object(scorer_mod, "complete") as m:
            result = run_scorer(conn, profile=profile, rubric=rubric, log_root=log_root)
            m.assert_not_called()

        assert result.status is WorkflowStatus.COMPLETED
        sc = result.scored_company
        assert sc.score is None
        assert sc.coverage == 0.0
        assert sc.tier is None
        assert sc.passed_floor is False
        # min_coverage=0.0 → passed_coverage=True (vacuous); gated_by="score" per ADR-004.
        assert sc.passed_coverage is True
        assert sc.gated_by == "score"
        assert all(d.is_excluded for d in sc.dimensions)
        assert result.cost_usd == pytest.approx(0.0)
        # Every excluded dimension's reasoning carries the gap-prefix.
        for d in sc.dimensions:
            assert EXCLUSION_GAP_PREFIX in d.reasoning

    def test_all_gaps_with_min_coverage_positive_gated_by_coverage(
        self, conn: sqlite3.Connection, log_root: Path
    ) -> None:
        rubric = _five_dim_rubric(min_coverage=0.20)
        profile = _all_gaps_profile()
        with patch.object(scorer_mod, "complete") as m:
            result = run_scorer(conn, profile=profile, rubric=rubric, log_root=log_root)
            m.assert_not_called()
        sc = result.scored_company
        assert sc.gated_by == "coverage"
        assert sc.passed_coverage is False

    def test_partial_coverage_renormalises(self, conn: sqlite3.Connection, log_root: Path) -> None:
        rubric = _five_dim_rubric(weights=(0.30, 0.20, 0.20, 0.20, 0.10))
        profile = _partial_profile()  # only industry + recent_news are extracted
        body = _llm_body_for_fields(
            [
                {
                    "field": "industry",
                    "score": 5.0,
                    "grounded_quote": "fintech startup",
                    "reasoning": "top band",
                },
                {
                    "field": "recent_news",
                    "score": 4.0,
                    "grounded_quote": "Series B led by Sequoia",
                    "reasoning": "trigger present",
                },
            ]
        )
        with patch.object(scorer_mod, "complete", return_value=_llm_result(body)):
            result = run_scorer(conn, profile=profile, rubric=rubric, log_root=log_root)
        sc = result.scored_company
        # present weights: industry 0.20, recent_news 0.20 → coverage = 0.40
        # weighted: 5*0.20 + 4*0.20 = 1.0 + 0.8 = 1.8; avg = 4.5; composite = 90.0
        assert sc.coverage == pytest.approx(0.40)
        assert sc.score == pytest.approx(90.0)
        assert sc.tier == "A"
        # gated_by: coverage 0.4 >= min_coverage 0.0 → passed; score 90 >= 55 → passed; None
        assert sc.gated_by is None

    def test_below_floor_completed_not_failed(
        self, conn: sqlite3.Connection, log_root: Path
    ) -> None:
        rubric = _five_dim_rubric(floor=80.0, tier_a_min=85.0, tier_b_min=82.0, tier_c_min=80.0)
        # All scores at 2.0 → composite = 40 → below floor
        profile = _full_profile()
        body = _llm_body_for_fields(
            [
                {
                    "field": name,
                    "score": 2.0,
                    "grounded_quote": _profile_quote(profile, name),
                    "reasoning": "x",
                }
                for name in PROFILE_FIELD_NAMES
            ]
        )
        with patch.object(scorer_mod, "complete", return_value=_llm_result(body)):
            result = run_scorer(conn, profile=profile, rubric=rubric, log_root=log_root)
        # The Scorer does NOT raise on a low score — it produces the honest record.
        assert result.status is WorkflowStatus.COMPLETED
        sc = result.scored_company
        assert sc.score == pytest.approx(40.0)
        assert sc.passed_floor is False
        assert sc.tier == "below_c"
        assert sc.gated_by == "score"
        assert result.error_step is None

    def test_below_min_coverage_completed_not_failed(
        self, conn: sqlite3.Connection, log_root: Path
    ) -> None:
        rubric = _five_dim_rubric(min_coverage=0.50, weights=(0.30, 0.20, 0.20, 0.20, 0.10))
        profile = _partial_profile()  # coverage = 0.40 < 0.50
        body = _llm_body_for_fields(
            [
                {
                    "field": "industry",
                    "score": 5.0,
                    "grounded_quote": "fintech startup",
                    "reasoning": "x",
                },
                {
                    "field": "recent_news",
                    "score": 5.0,
                    "grounded_quote": "Series B led by Sequoia",
                    "reasoning": "x",
                },
            ]
        )
        with patch.object(scorer_mod, "complete", return_value=_llm_result(body)):
            result = run_scorer(conn, profile=profile, rubric=rubric, log_root=log_root)
        assert result.status is WorkflowStatus.COMPLETED  # gated, but not failed.
        sc = result.scored_company
        assert sc.coverage == pytest.approx(0.40)
        assert sc.passed_coverage is False
        assert sc.gated_by == "coverage"


# ---------------------------------------------------------------------------
# Grounding-failure downgrade through the agent
# ---------------------------------------------------------------------------


class TestGroundingDowngrade:
    def test_overreaching_quote_downgrades_dimension(
        self, conn: sqlite3.Connection, log_root: Path
    ) -> None:
        rubric = _five_dim_rubric()
        profile = _full_profile()
        # All quotes legit EXCEPT industry — the LLM over-reaches.
        body = _llm_body_for_fields(
            [
                {
                    "field": "size",
                    "score": 4.0,
                    "grounded_quote": _profile_quote(profile, "size"),
                    "reasoning": "x",
                },
                {
                    "field": "industry",
                    "score": 5.0,
                    # NOT in the source_spans — over-reach.
                    "grounded_quote": "regulated industries with strict compliance",
                    "reasoning": "regulated industry — top band",
                },
                {
                    "field": "tech_stack",
                    "score": 4.0,
                    "grounded_quote": _profile_quote(profile, "tech_stack"),
                    "reasoning": "x",
                },
                {
                    "field": "recent_news",
                    "score": 4.0,
                    "grounded_quote": _profile_quote(profile, "recent_news"),
                    "reasoning": "x",
                },
                {
                    "field": "decision_makers",
                    "score": 5.0,
                    "grounded_quote": _profile_quote(profile, "decision_makers"),
                    "reasoning": "x",
                },
            ]
        )
        with patch.object(scorer_mod, "complete", return_value=_llm_result(body)):
            result = run_scorer(conn, profile=profile, rubric=rubric, log_root=log_root)
        sc = result.scored_company
        industry = next(d for d in sc.dimensions if d.field == "industry")
        assert industry.is_excluded is True
        assert industry.score is None
        assert industry.grounded_quote is None
        assert EXCLUSION_GROUNDING_FAIL_PREFIX in industry.reasoning
        # Coverage is full minus industry's 0.20 weight.
        assert sc.coverage == pytest.approx(0.80)
        # The other 4 dimensions are still scored.
        scored = [d for d in sc.dimensions if not d.is_excluded]
        assert len(scored) == 4


# ---------------------------------------------------------------------------
# Infrastructure failures
# ---------------------------------------------------------------------------


class TestInfrastructureFailure:
    def test_malformed_json_response_fails_workflow(
        self, conn: sqlite3.Connection, log_root: Path
    ) -> None:
        rubric = _five_dim_rubric()
        profile = _full_profile()
        # Truncated JSON
        llm_res = _llm_result("{not valid json")
        with patch.object(scorer_mod, "complete", return_value=llm_res):
            result = run_scorer(conn, profile=profile, rubric=rubric, log_root=log_root)
        assert result.status is WorkflowStatus.FAILED
        assert result.error_step == AGENT_NAME
        assert result.error_message is not None
        assert "ScorerError" in result.error_message
        wf = load_workflow(conn, result.workflow_id)
        assert wf is not None
        assert wf.status is WorkflowStatus.FAILED
        assert wf.finished_at is not None
        steps = load_steps(conn, result.workflow_id)
        assert steps[0].status is StepStatus.FAILED

    def test_schema_mismatch_fails_workflow(self, conn: sqlite3.Connection, log_root: Path) -> None:
        rubric = _five_dim_rubric()
        profile = _full_profile()
        # Missing required keys on each dimension.
        body = json.dumps({"dimensions": [{"field": "size", "score": 3.0}]})
        llm_res = _llm_result(body)
        with patch.object(scorer_mod, "complete", return_value=llm_res):
            result = run_scorer(conn, profile=profile, rubric=rubric, log_root=log_root)
        assert result.status is WorkflowStatus.FAILED
        assert "ScorerError" in (result.error_message or "")

    def test_missing_dimension_in_llm_output_fails(
        self, conn: sqlite3.Connection, log_root: Path
    ) -> None:
        rubric = _five_dim_rubric()
        profile = _full_profile()
        # LLM returns only 3 of 5 expected dimensions.
        body = _llm_body_for_fields(
            [
                {
                    "field": name,
                    "score": 3.0,
                    "grounded_quote": _profile_quote(profile, name),
                    "reasoning": "x",
                }
                for name in PROFILE_FIELD_NAMES[:3]
            ]
        )
        with patch.object(scorer_mod, "complete", return_value=_llm_result(body)):
            result = run_scorer(conn, profile=profile, rubric=rubric, log_root=log_root)
        assert result.status is WorkflowStatus.FAILED
        assert "omitted required dimension" in (result.error_message or "")

    def test_extra_dimension_in_llm_output_fails(
        self, conn: sqlite3.Connection, log_root: Path
    ) -> None:
        # Rubric only scores 2 dimensions; LLM returns 3.
        rubric_2dim = RubricConfig(
            floor=55.0,
            min_coverage=0.0,
            tier_a_min=80.0,
            tier_b_min=65.0,
            tier_c_min=55.0,
            missing_data_policy="renormalise",
            dimensions=[
                DimensionConfig(name="industry", field="industry", weight=0.5, prose="x"),
                DimensionConfig(name="recent_news", field="recent_news", weight=0.5, prose="x"),
            ],
        )
        profile = _full_profile()
        body = _llm_body_for_fields(
            [
                {
                    "field": "industry",
                    "score": 4.0,
                    "grounded_quote": _profile_quote(profile, "industry"),
                    "reasoning": "x",
                },
                {
                    "field": "recent_news",
                    "score": 4.0,
                    "grounded_quote": _profile_quote(profile, "recent_news"),
                    "reasoning": "x",
                },
                {
                    "field": "size",  # not in rubric
                    "score": 4.0,
                    "grounded_quote": _profile_quote(profile, "size"),
                    "reasoning": "x",
                },
            ]
        )
        with patch.object(scorer_mod, "complete", return_value=_llm_result(body)):
            result = run_scorer(conn, profile=profile, rubric=rubric_2dim, log_root=log_root)
        assert result.status is WorkflowStatus.FAILED
        assert "unexpected dimension" in (result.error_message or "")

    def test_llm_api_exception_fails_workflow(
        self, conn: sqlite3.Connection, log_root: Path
    ) -> None:
        rubric = _five_dim_rubric()
        profile = _full_profile()

        class APIBoom(RuntimeError):
            pass

        with patch.object(scorer_mod, "complete", side_effect=APIBoom("anthropic exploded")):
            result = run_scorer(conn, profile=profile, rubric=rubric, log_root=log_root)
        assert result.status is WorkflowStatus.FAILED
        assert "APIBoom" in (result.error_message or "")
        # No retry — single attempt.
        wf = load_workflow(conn, result.workflow_id)
        assert wf is not None
        assert wf.finished_at is not None


# ---------------------------------------------------------------------------
# Code-fence stripping (defensive)
# ---------------------------------------------------------------------------


class TestCodeFenceStrip:
    def test_fenced_response_parses_via_strip(
        self, conn: sqlite3.Connection, log_root: Path
    ) -> None:
        rubric = _five_dim_rubric()
        profile = _full_profile()
        body = _llm_body_for_fields(
            [
                {
                    "field": name,
                    "score": 3.0,
                    "grounded_quote": _profile_quote(profile, name),
                    "reasoning": "x",
                }
                for name in PROFILE_FIELD_NAMES
            ]
        )
        fenced = f"```json\n{body}\n```"
        with patch.object(scorer_mod, "complete", return_value=_llm_result(fenced)):
            result = run_scorer(conn, profile=profile, rubric=rubric, log_root=log_root)
        assert result.status is WorkflowStatus.COMPLETED


# ---------------------------------------------------------------------------
# CLI tests — full Researcher → Extractor → Scorer chain
# ---------------------------------------------------------------------------


class TestCLI:
    def _build_researcher_llm(self) -> LLMResult:
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
        return LLMResult(
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

    def _build_extractor_body(self) -> str:
        return json.dumps(
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

    def _build_scorer_body(self) -> str:
        # The extractor's profile has only size + industry extracted; the
        # scorer will only score those two dimensions.
        return _llm_body_for_fields(
            [
                {
                    "field": "size",
                    "score": 4.0,
                    "grounded_quote": "approximately 500 employees",
                    "reasoning": "Series B band",
                },
                {
                    "field": "industry",
                    "score": 5.0,
                    "grounded_quote": "fintech startup",
                    "reasoning": "regulated industry",
                },
            ]
        )

    def _rubric_file(self, tmp_path: Path) -> Path:
        p = tmp_path / "rubric.toml"
        p.write_text(
            "[defaults]\n"
            "floor = 55.0\n"
            "min_coverage = 0.0\n"
            "tier_a_min = 80.0\n"
            "tier_b_min = 65.0\n"
            "tier_c_min = 55.0\n"
            'missing_data_policy = "renormalise"\n'
            "\n"
            "[dimensions.size]\n"
            'field = "size"\n'
            "weight = 0.3\n"
            'prose = "Score size."\n'
            "[dimensions.industry]\n"
            'field = "industry"\n'
            "weight = 0.3\n"
            'prose = "Score industry."\n'
            "[dimensions.tech_stack]\n"
            'field = "tech_stack"\n'
            "weight = 0.1\n"
            'prose = "Score tech_stack."\n'
            "[dimensions.recent_news]\n"
            'field = "recent_news"\n'
            "weight = 0.2\n"
            'prose = "Score recent_news."\n'
            "[dimensions.decision_makers]\n"
            'field = "decision_makers"\n'
            "weight = 0.1\n"
            'prose = "Score decision_makers."\n'
        )
        return p

    def test_run_scorer_happy(self, tmp_path: Path) -> None:
        db = tmp_path / "wf.db"
        rubric_file = self._rubric_file(tmp_path)

        researcher_llm = self._build_researcher_llm()
        extractor_llm = _llm_result(self._build_extractor_body(), cost_usd=0.009)
        scorer_llm = _llm_result(self._build_scorer_body(), cost_usd=0.006)

        runner = CliRunner()
        with (
            patch.object(researcher_mod, "complete", return_value=researcher_llm),
            patch.object(extractor_mod, "complete", return_value=extractor_llm),
            patch.object(scorer_mod, "complete", return_value=scorer_llm),
        ):
            result = runner.invoke(
                main,
                [
                    "run-scorer",
                    "--db",
                    str(db),
                    "--rubric",
                    str(rubric_file),
                    "Acme Corp",
                ],
            )
        assert result.exit_code == 0, result.output
        # All three result blocks rendered.
        assert "Automated research signals" in result.output
        assert "Automated extraction" in result.output
        assert "Automated ICP scoring" in result.output
        # Scorer output reports the score, coverage, tier, gated-by line.
        assert "Score    :" in result.output
        assert "Coverage :" in result.output
        assert "Tier     :" in result.output
        assert "Gated by :" in result.output

    def test_run_scorer_researcher_failure_exits_nonzero(self, tmp_path: Path) -> None:
        db = tmp_path / "wf.db"
        rubric_file = self._rubric_file(tmp_path)

        class APIBoom(RuntimeError):
            pass

        runner = CliRunner()
        with patch.object(researcher_mod, "complete", side_effect=APIBoom("anthropic exploded")):
            result = runner.invoke(
                main,
                [
                    "run-scorer",
                    "--db",
                    str(db),
                    "--rubric",
                    str(rubric_file),
                    "Acme Corp",
                ],
            )
        assert result.exit_code != 0
        assert "status: FAILED" in result.output
        assert "anthropic exploded" in result.output
        # Extractor + Scorer blocks should not be present.
        assert "Automated extraction" not in result.output
        assert "Automated ICP scoring" not in result.output

    def test_run_scorer_extractor_failure_exits_nonzero(self, tmp_path: Path) -> None:
        db = tmp_path / "wf.db"
        rubric_file = self._rubric_file(tmp_path)
        researcher_llm = self._build_researcher_llm()

        class ExtBoom(RuntimeError):
            pass

        runner = CliRunner()
        with (
            patch.object(researcher_mod, "complete", return_value=researcher_llm),
            patch.object(extractor_mod, "complete", side_effect=ExtBoom("sonnet exploded")),
        ):
            result = runner.invoke(
                main,
                [
                    "run-scorer",
                    "--db",
                    str(db),
                    "--rubric",
                    str(rubric_file),
                    "Acme Corp",
                ],
            )
        assert result.exit_code != 0
        assert "Automated research signals" in result.output
        # Extractor block IS rendered, but in FAILED form (no decision footer).
        assert "Failed at step: extractor" in result.output
        assert "sonnet exploded" in result.output
        # Scorer block should not be present.
        assert "Automated ICP scoring" not in result.output

    def test_run_scorer_scorer_failure_exits_nonzero(self, tmp_path: Path) -> None:
        db = tmp_path / "wf.db"
        rubric_file = self._rubric_file(tmp_path)
        researcher_llm = self._build_researcher_llm()
        extractor_llm = _llm_result(self._build_extractor_body(), cost_usd=0.009)

        class ScorerBoom(RuntimeError):
            pass

        runner = CliRunner()
        with (
            patch.object(researcher_mod, "complete", return_value=researcher_llm),
            patch.object(extractor_mod, "complete", return_value=extractor_llm),
            patch.object(scorer_mod, "complete", side_effect=ScorerBoom("scorer api boom")),
        ):
            result = runner.invoke(
                main,
                [
                    "run-scorer",
                    "--db",
                    str(db),
                    "--rubric",
                    str(rubric_file),
                    "Acme Corp",
                ],
            )
        assert result.exit_code != 0
        # All three blocks rendered; the scorer block carries the error.
        assert "Automated research signals" in result.output
        assert "Automated extraction" in result.output
        assert "scorer api boom" in result.output

    def test_run_scorer_with_missing_rubric_fails_cleanly(self, tmp_path: Path) -> None:
        db = tmp_path / "wf.db"
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "run-scorer",
                "--db",
                str(db),
                "--rubric",
                str(tmp_path / "does-not-exist.toml"),
                "Acme Corp",
            ],
        )
        assert result.exit_code != 0
        assert "rubric config not found" in result.output


# ---------------------------------------------------------------------------
# Live smoke — one real Researcher + Extractor + Scorer end-to-end.
# Skipped without API key. Runs ONCE per session.
# ---------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY")
    or os.environ["ANTHROPIC_API_KEY"].startswith("sk-ant-REPLACE"),
    reason="ANTHROPIC_API_KEY not set; live smoke skipped.",
)
def test_live_scorer_round_trip(conn: sqlite3.Connection, log_root: Path) -> None:
    """End-to-end live: Researcher → Extractor → Scorer against a real company.

    Verifies the full habitat round-trip for all three agents using the
    bundled `config/rubric.toml` as the rubric: workflows COMPLETED, steps
    recorded, events emitted, costs rolled up, ScoredCompany has real
    score/coverage/tier with grounded per-dimension reasoning, and the
    grounded_quotes ACTUALLY substring-match the cited ProfileField's
    source_spans (the load-bearing fabrication-resistance check).
    """
    from agent_habitat.agents.researcher import run_researcher

    rubric = load_rubric(Path("config/rubric.toml"))

    # Use a well-known company with a real public footprint; same shape as
    # Slice 3's live smoke.
    research = run_researcher(conn, company_name="Anthropic", max_searches=3, log_root=log_root)
    assert research.status is WorkflowStatus.COMPLETED

    extract = extractor_mod.run_extractor(conn, raw_signals=research.raw_signals, log_root=log_root)
    assert extract.status is WorkflowStatus.COMPLETED

    score = run_scorer(conn, profile=extract.profile, rubric=rubric, log_root=log_root)
    assert score.status is WorkflowStatus.COMPLETED, score.error_message
    sc = score.scored_company

    # ScoredCompany has real shape.
    assert sc.company_name == "Anthropic"
    assert sc.coverage is not None
    # Every dimension has reasoning regardless of excluded/scored.
    assert len(sc.dimensions) == len(rubric.dimensions)
    for d in sc.dimensions:
        assert d.reasoning, f"dimension {d.field} has empty reasoning"
        if not d.is_excluded:
            # Grounding-quote MUST substring-match the cited ProfileField's source_spans.
            pf = extract.profile.field(d.field)
            assert d.grounded_quote is not None
            assert scorer_mod._grounded_in_field(d.grounded_quote, pf), (
                f"live-smoke grounding failure on dim {d.field}: "
                f"quote={d.grounded_quote!r}, "
                f"spans={[s.quote for s in pf.source_spans]!r}"
            )

    # Workflow + step + events persisted (3 workflows, one per agent).
    for wf_id in (research.workflow_id, extract.workflow_id, score.workflow_id):
        wf = load_workflow(conn, wf_id)
        assert wf is not None
        assert wf.status is WorkflowStatus.COMPLETED
        assert wf.cost_total_usd is not None

    # The scorer step's output_ref points at a real JSONL line.
    scorer_steps = load_steps(conn, score.workflow_id)
    assert len(scorer_steps) == 1
    if not extract.profile.gap_count == len(PROFILE_FIELD_NAMES):
        # An all-gaps extractor output skips the LLM call; output_ref is then None.
        assert scorer_steps[0].output_ref is not None

    print("\n--- LIVE SCORER CALIBRATION ---")
    print(f"  Researcher cost : ${research.cost_usd:.6f}")
    print(f"  Extractor cost  : ${extract.cost_usd:.6f}")
    print(f"  Scorer cost     : ${score.cost_usd:.6f}")
    print(f"  TOTAL           : ${research.cost_usd + extract.cost_usd + score.cost_usd:.6f}")
    print(f"  Extractor gaps  : {extract.profile.gap_count}/{len(PROFILE_FIELD_NAMES)}")
    print(f"  Composite score : {sc.score}")
    print(f"  Coverage        : {sc.coverage:.2%}")
    print(f"  Tier            : {sc.tier}")
    print(f"  Gated by        : {sc.gated_by}")
    for d in sc.dimensions:
        status = "EXCLUDED" if d.is_excluded else f"{d.score:.1f}/5"
        print(f"  - {d.field:18s} (w={d.weight:.2f}): {status}")
        if d.grounded_quote is not None:
            preview = d.grounded_quote.strip().replace("\n", " ")
            if len(preview) > 100:
                preview = preview[:100] + "…"
            print(f"      grounded_quote: {preview!r}")
