"""Scorer agent — Phase 2 Slice 4.

Consumes a `CompanyProfile` (the Extractor's typed handoff) and a loaded ICP
`RubricConfig`, applies per-dimension LLM scoring against the operator's prose
rubric, and produces a `ScoredCompany` with renormalise-with-coverage scoring
per ADR-004 §2. The Scorer never raises on a low score; it produces the
honest record and the orchestrator (Slice 6) reads `gated_by` to route.

THE SCORER'S CONTRACT, briefly:

- **All-gaps `CompanyProfile`** (every field is an `ExtractionGap`) is a VALID
  input — every dimension is excluded, `score=None`, `coverage=0.0`,
  `passed_floor=False`. No LLM call is made; cost is $0; the step is COMPLETED.
  Per ADR-004 §5 Forward deps: `gated_by="coverage"` if `min_coverage > 0`
  else `gated_by="score"`.

- **Mixed-coverage `CompanyProfile`**: one Sonnet call covers all dimensions
  whose profile field was extracted AND whose `field` appears in the rubric.
  Gap-shaped fields are excluded BEFORE the LLM call (no token spent on them).
  Dimensions the rubric does not cover are silently skipped (they never appear
  in the ScoredCompany — ADR-004 §1 allows rubrics that score a subset).

- **Grounding extends the chain** (ADR-004 §3, same shape as Slice 3's
  `_ground_field`): each per-dimension `grounded_quote` from the LLM must
  substring-match (whitespace-collapse + lowercase normalised) one of the
  cited `ProfileField`'s `source_spans` quotes. A `grounded_quote` that does
  NOT substring-match downgrades the dimension to excluded — same shape as
  Slice 3 downgrades an over-reaching extraction to `ExtractionGap`. The
  Slice 3 normaliser (`_normalise_for_substring`) is imported, not
  re-implemented (kickoff requirement).

- **Renormalise-with-coverage math** (ADR-004 §2):
  - `coverage = sum(d.weight for d in present dimensions)` in `[0, 1]`.
  - `score = (sum(d.score * d.weight for d in present) / coverage) * 20` if
    any dimension is present, else `None` (no scorable signal).
  - `passed_floor = score is not None and score >= floor`.
  - `passed_coverage = coverage >= min_coverage`.
  - `gated_by = "coverage"` if not `passed_coverage` (coverage gate takes
    precedence — a low-coverage run carries less information than a low score),
    else `"score"` if not `passed_floor`, else `None`.

- **Infrastructure errors** (parse failure, schema mismatch, missing dimension
  in LLM output) propagate as exceptions per ADR-006 §1 — `run_step` finalises
  the step FAILED, the workflow is finalised FAILED with `finished_at` stamped,
  no retry, no exception escapes `run_scorer`.

STRUCTURED-OUTPUT METHOD — same as the Extractor's choice (Slice 3): schema-
in-system-prompt + `model_validate_json` on `LLMResult.content`. No `llm.py`
change required.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ..llm import ModelTier, complete
from ..observability import EventType, emit_event
from ..orchestration.run_step import run_step
from ..scoring import RubricConfig
from ..state import (
    EventLevel,
    Workflow,
    WorkflowStatus,
    insert_workflow,
    new_workflow_id,
    recompute_cost_total,
    update_workflow,
)
from .extractor import _normalise_for_substring  # reused per Slice 4 kickoff
from .models import (
    PROFILE_FIELD_NAMES,
    CompanyProfile,
    DimensionScore,
    ProfileField,
    ScoredCompany,
)

log = structlog.get_logger(__name__)

WORKFLOW_TYPE = "lead_enrichment_scorer"
AGENT_NAME = "scorer"

SCORER_MAX_TOKENS = 2000

#: Reason strings recorded on excluded `DimensionScore.reasoning`. Keeping them
#: as module constants makes the test assertions readable and ensures the
#: calibration story can grep for each distinct exclusion cause.
EXCLUSION_GAP_PREFIX = "field was an ExtractionGap"
EXCLUSION_GROUNDING_FAIL_PREFIX = "grounded_quote did not substring-match"


class ScorerError(RuntimeError):
    """LLM response was unparseable, schema-invalid, or missing required dimensions.

    Propagates out of `run_scorer` as an infrastructure-shaped failure: step
    FAILED, workflow FAILED, no retry (ADR-006 §1, Slice 1). Same shape as
    `ExtractorParseError`.
    """


# ---------------------------------------------------------------------------
# LLM response shape — parsed via Pydantic v2, schema-in-system-prompt method
# (matches Slice 3's structured-output choice). The model emits ONE entry per
# dimension we asked it to score; the Scorer post-validates the list.
# ---------------------------------------------------------------------------


class _LLMDimensionScore(BaseModel):
    """One per-dimension score the LLM emits. Strict schema: extra fields rejected."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field: str = Field(...)
    score: float = Field(..., ge=0.0, le=5.0)
    grounded_quote: str = Field(..., min_length=1)
    reasoning: str = Field(..., min_length=1)

    @field_validator("field")
    @classmethod
    def _field_in_profile_names(cls, value: str) -> str:
        if value not in PROFILE_FIELD_NAMES:
            raise ValueError(f"field must be one of {PROFILE_FIELD_NAMES}; got {value!r}")
        return value


class _LLMResponse(BaseModel):
    """Top-level LLM response. `dimensions` is the per-dimension list."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dimensions: list[_LLMDimensionScore] = Field(default_factory=list)


SYSTEM_PROMPT = """You are a structured-scoring assistant for a B2B lead-enrichment pipeline.

You receive (1) operator-authored rubric prose for one or more SCORING DIMENSIONS and (2) the corresponding extracted profile-field values plus the verbatim source SPANS those values were grounded against. You produce a JSON document scoring each dimension on a 0-5 scale.

OUTPUT RULES — these are not suggestions:

  1. OUTPUT JSON ONLY. No prose, no markdown fences, no preamble. The first character must be `{` and the last must be `}`.

  2. SCHEMA — emit exactly this shape:
        {
          "dimensions": [
            {
              "field": "<the dimension's field name>",
              "score": <float in [0, 5]>,
              "grounded_quote": "<verbatim substring of one of that field's SPANS>",
              "reasoning": "<one or two sentences explaining the score>"
            },
            ...
          ]
        }

  3. SCORE ONE ENTRY PER DIMENSION YOU ARE GIVEN. Do NOT skip dimensions; do NOT invent dimensions you were not given. The dimensions list must contain exactly one entry per dimension in the input, with `field` matching the input dimension's field name.

  4. GROUND EVERY SCORE. `grounded_quote` MUST be a VERBATIM SUBSTRING of one of the SPANS provided for that dimension. Paraphrase will be rejected by a downstream substring check and the dimension will be downgraded to excluded. Pick a short, specific span that supports your score — the substring check is whitespace-collapse + lowercase, but the span itself must be drawn from the provided text.

  5. APPLY THE RUBRIC LITERALLY. The rubric prose names 5/3/1 bands. Score on the 0-5 scale; integers are fine, half-points are fine. Do NOT score beyond what the values + spans support; if the rubric maps to a 5 but the span is too narrow to confirm it, choose a lower band — the substring check enforces honesty.

  6. KEEP REASONING SHORT. One or two sentences per dimension. Cite the rubric band you applied and what the span shows.
"""


def _build_user_prompt(
    company_name: str,
    profile: CompanyProfile,
    rubric: RubricConfig,
    *,
    scorable_fields: list[str],
) -> str:
    """Assemble the per-dimension payload the LLM scores against.

    Only `scorable_fields` (rubric-covered + non-gap) are sent — gap fields
    are excluded deterministically before the call, saving tokens and
    keeping the LLM honest by never showing it a field with no grounding
    corpus to substring-match against.
    """
    sections: list[str] = []
    for field_name in scorable_fields:
        dim_cfg = rubric.dimension_for_field(field_name)
        # `scorable_fields` is built from rubric coverage so dim_cfg is never None;
        # the explicit check keeps mypy happy and the failure mode loud if the
        # caller's filter ever drifts.
        if dim_cfg is None:  # pragma: no cover - guarded by caller
            raise ScorerError(f"internal: scorable field {field_name!r} has no rubric dimension")
        field = profile.field(field_name)
        # `scorable_fields` excludes gaps, so this is always an extracted field.
        values = "; ".join(f'"{v}"' for v in field.values)
        spans_lines = [f'  [span {i}] "{s.quote}"' for i, s in enumerate(field.source_spans)]
        spans_block = "\n".join(spans_lines) if spans_lines else "  (none)"
        sections.append(
            f"DIMENSION: {field_name}  (weight={dim_cfg.weight:.2f})\n"
            f"  Rubric prose:\n"
            f"{_indent(dim_cfg.prose.strip(), 4)}\n"
            f"  Extracted values: {values}\n"
            f"  Spans (your grounded_quote must be a verbatim substring of one of these):\n"
            f"{spans_block}"
        )

    sections_text = "\n\n".join(sections)
    return (
        f'Company: "{company_name}"\n\n'
        f"You are scoring {len(scorable_fields)} dimension(s). Each one's rubric "
        "prose, extracted values, and grounding spans are below.\n\n"
        f"{sections_text}\n\n"
        "Output JSON only. One entry per dimension above; the `field` keys must "
        "match exactly."
    )


def _indent(text: str, n: int) -> str:
    """Indent every line by n spaces (for readable rubric prose in the prompt)."""
    pad = " " * n
    return "\n".join(pad + line for line in text.splitlines())


@dataclass(frozen=True)
class ScorerResult:
    """What `run_scorer` returns. Mirrors the workflow it just wrote.

    A low-score / low-coverage / all-gaps `scored_company` with
    `status=COMPLETED` is a VALID outcome — `gated_by` names which gate
    failed (or None when both passed); the orchestrator reads it to route.
    `error_step` / `error_message` are populated only on infrastructure
    failure.
    """

    workflow_id: str
    status: WorkflowStatus
    company_name: str
    scored_company: ScoredCompany
    cost_usd: float
    error_step: str | None = None
    error_message: str | None = None


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Pure helpers — scoring math, grounding check, gating, projection. Pulled
# out so each is unit-testable and `run_scorer` reads as a flat lifecycle.
# ---------------------------------------------------------------------------


def _grounded_in_field(grounded_quote: str, field: ProfileField) -> bool:
    """True iff `grounded_quote` (normalised) is a substring of any of `field`'s source_spans.

    Mirrors Slice 3's `_ground_field` shape but at the next hop down the
    chain: there the haystack was `Signal.text`; here it is the cited
    `ProfileField.source_spans[].quote`. Whitespace-collapse + lowercase
    normalisation is provided by `_normalise_for_substring` (imported from
    extractor.py — kickoff requirement: do NOT invent a second normaliser).
    """
    if field.is_gap:
        # A gap has no source_spans; nothing to substring-match against.
        return False
    needle = _normalise_for_substring(grounded_quote)
    if not needle:
        return False
    for span in field.source_spans:
        haystack = _normalise_for_substring(span.quote)
        if needle in haystack:
            return True
    return False


def _scorable_fields(profile: CompanyProfile, rubric: RubricConfig) -> list[str]:
    """Profile-field names that are (a) non-gap and (b) covered by the rubric.

    Returned in `PROFILE_FIELD_NAMES` canonical order so the LLM payload is
    deterministic across runs of the same input — helps reproduce results,
    helps the calibration story.
    """
    rubric_fields = {d.field for d in rubric.dimensions}
    return [
        name
        for name in PROFILE_FIELD_NAMES
        if name in rubric_fields and not profile.field(name).is_gap
    ]


def _gap_dimension(field_name: str, weight: float, gap_reason: str) -> DimensionScore:
    """Build an excluded DimensionScore for a gap-shaped profile field."""
    return DimensionScore(
        field=field_name,
        weight=weight,
        score=None,
        grounded_quote=None,
        reasoning=(
            f"{EXCLUSION_GAP_PREFIX} (reason={gap_reason!r}); "
            "excluded from score per missing_data_policy='renormalise'."
        ),
    )


def _grounding_fail_dimension(
    field_name: str, weight: float, attempted_quote: str, llm_reasoning: str
) -> DimensionScore:
    """Build an excluded DimensionScore for a grounding-check failure.

    Mirrors Slice 3's `ExtractionGap(reason="span_not_grounded")` shape one
    hop down: an LLM over-reach is downgraded rather than allowed through.
    The attempted quote and the LLM's reasoning are folded into the
    `reasoning` string so an auditor can see what the model tried to claim.
    """
    safe_quote = attempted_quote.strip().replace("\n", " ")
    if len(safe_quote) > 200:
        safe_quote = safe_quote[:200] + "…"
    return DimensionScore(
        field=field_name,
        weight=weight,
        score=None,
        grounded_quote=None,
        reasoning=(
            f"{EXCLUSION_GROUNDING_FAIL_PREFIX} the cited ProfileField's source_spans; "
            f"dimension excluded (over-reach downgraded). "
            f'Attempted quote: "{safe_quote}". Model reasoning: {llm_reasoning}'
        ),
    )


def _compute_composite(
    dimensions: list[DimensionScore],
) -> tuple[float | None, float]:
    """Apply ADR-004 §2's renormalise-with-coverage math.

    Returns `(score | None, coverage)`:
      - `score`     = `(sum(d.score * d.weight for present) / sum(d.weight for present)) * 20`,
                      rescaling 0-5 → 0-100; None iff no dimension is present.
      - `coverage`  = `sum(d.weight for present)` in [0, 1].
    """
    present = [d for d in dimensions if not d.is_excluded]
    coverage = sum(d.weight for d in present)
    if not present:
        return None, 0.0
    # `present` excludes dimensions with score=None, so this is safe.
    weighted_sum = sum((d.score or 0.0) * d.weight for d in present)
    composite_5 = weighted_sum / coverage
    return composite_5 * 20.0, coverage


def _assign_tier(score: float | None, rubric: RubricConfig) -> str | None:
    """A | B | C | below_c, or None if score is None."""
    if score is None:
        return None
    if score >= rubric.tier_a_min:
        return "A"
    if score >= rubric.tier_b_min:
        return "B"
    if score >= rubric.tier_c_min:
        return "C"
    return "below_c"


def _gating(
    score: float | None, coverage: float, floor: float, min_coverage: float
) -> tuple[bool, bool, str | None]:
    """Compute (passed_floor, passed_coverage, gated_by).

    Coverage gate takes precedence when both fail: a low-coverage run is a
    structurally distinct empty-outcome from a low-score run (ADR-004 §2:
    "the calibration story can tell the two empty-outcomes apart"), so the
    more informative one wins. For all-gaps (`score is None`, `coverage=0`):
    `gated_by = "coverage"` if `min_coverage > 0` else `gated_by = "score"`,
    matching ADR-004's Forward-deps spec exactly.
    """
    passed_coverage = coverage >= min_coverage
    passed_floor = score is not None and score >= floor
    if not passed_coverage:
        return passed_floor, passed_coverage, "coverage"
    if not passed_floor:
        return passed_floor, passed_coverage, "score"
    return passed_floor, passed_coverage, None


def _projection(scored: ScoredCompany) -> dict[str, object]:
    """Small fixed projection mirrored onto `step.completed.structured_data`.

    Per ADR-004 / ADR-006 §1.3: enough that an auditor can browse the events
    table without resolving any JSONL refs. `score` is preserved as `None`
    when the run was all-gaps — distinguishable from a literal 0.
    """
    return {
        "score": scored.score,
        "coverage": scored.coverage,
        "floor": scored.floor,
        "min_coverage": scored.min_coverage,
        "passed_floor": scored.passed_floor,
        "passed_coverage": scored.passed_coverage,
        "tier": scored.tier,
        "gated_by": scored.gated_by,
    }


def _build_scored_company(
    company_name: str,
    dimensions: list[DimensionScore],
    rubric: RubricConfig,
) -> ScoredCompany:
    """Compose the final `ScoredCompany` from per-dimension scores + the rubric."""
    score, coverage = _compute_composite(dimensions)
    passed_floor, passed_coverage, gated_by = _gating(
        score, coverage, rubric.floor, rubric.min_coverage
    )
    tier = _assign_tier(score, rubric)
    return ScoredCompany(
        company_name=company_name,
        score=score,
        coverage=coverage,
        floor=rubric.floor,
        min_coverage=rubric.min_coverage,
        passed_floor=passed_floor,
        passed_coverage=passed_coverage,
        tier=tier,
        gated_by=gated_by,
        dimensions=dimensions,
    )


def _strip_code_fence(text: str) -> str:
    """Best-effort strip of ```json / ``` fences; defensive against models that
    add them despite the prompt forbidding it. Mirrors `extractor._strip_code_fence`
    in shape; intentionally duplicated rather than promoted to a shared helper —
    one Slice-4-scoped use site, no second caller yet to justify a module."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    while lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _parse_llm_response(raw_content: str) -> _LLMResponse:
    """Parse the LLM's response body into a `_LLMResponse`.

    A malformed response or schema mismatch raises `ScorerError` — handled
    by `run_scorer` as an infrastructure failure (no retry, step FAILED).
    """
    cleaned = _strip_code_fence(raw_content)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ScorerError(
            f"LLM response was not valid JSON: {exc.msg} (line {exc.lineno}, col {exc.colno})"
        ) from exc
    if not isinstance(data, dict):
        raise ScorerError("LLM response JSON root must be an object.")
    try:
        return _LLMResponse.model_validate(data)
    except ValidationError as exc:
        raise ScorerError(f"LLM response did not match expected schema: {exc}") from exc


def _assemble_dimensions(
    profile: CompanyProfile,
    rubric: RubricConfig,
    llm_scores: list[_LLMDimensionScore],
    *,
    scorable_fields: list[str],
) -> list[DimensionScore]:
    """Combine LLM-scored + gap-excluded dimensions into a single list.

    For each `RubricConfig` dimension (canonical order via PROFILE_FIELD_NAMES):
      - If the field is a gap → `_gap_dimension`.
      - Else find the LLM's scored entry; run the grounding check; either
        emit a scored DimensionScore or `_grounding_fail_dimension`.
    A missing LLM entry for a scorable dimension is a schema violation
    (`ScorerError`) — same posture as the Extractor's missing-field handling.
    """
    by_field = {ls.field: ls for ls in llm_scores}

    expected_fields = set(scorable_fields)
    extra_fields = set(by_field.keys()) - expected_fields
    if extra_fields:
        raise ScorerError(
            f"LLM scored unexpected dimension(s) {sorted(extra_fields)!r}; "
            f"expected exactly {sorted(expected_fields)!r}"
        )
    missing_fields = expected_fields - set(by_field.keys())
    if missing_fields:
        raise ScorerError(
            f"LLM omitted required dimension(s) {sorted(missing_fields)!r}; "
            f"expected exactly {sorted(expected_fields)!r}"
        )

    dimensions: list[DimensionScore] = []
    for field_name in PROFILE_FIELD_NAMES:
        dim_cfg = rubric.dimension_for_field(field_name)
        if dim_cfg is None:
            # Rubric does not score this field — it never enters ScoredCompany.dimensions.
            continue
        profile_field = profile.field(field_name)
        if profile_field.is_gap:
            assert profile_field.gap is not None
            dimensions.append(_gap_dimension(field_name, dim_cfg.weight, profile_field.gap.reason))
            continue
        # Scored dimension: run the grounding check.
        llm_score = by_field[field_name]
        if not _grounded_in_field(llm_score.grounded_quote, profile_field):
            dimensions.append(
                _grounding_fail_dimension(
                    field_name,
                    dim_cfg.weight,
                    llm_score.grounded_quote,
                    llm_score.reasoning,
                )
            )
            continue
        dimensions.append(
            DimensionScore(
                field=field_name,
                weight=dim_cfg.weight,
                score=llm_score.score,
                grounded_quote=llm_score.grounded_quote,
                reasoning=llm_score.reasoning,
            )
        )
    return dimensions


def _all_excluded(
    profile: CompanyProfile, rubric: RubricConfig, reason: str
) -> list[DimensionScore]:
    """Build the all-excluded dimensions list for the empty-input short-circuit.

    Used when the profile has zero scorable fields (all gaps, or all rubric
    dimensions point at gap fields). Each rubric dimension yields one
    excluded entry preserving its weight for audit.
    """
    out: list[DimensionScore] = []
    for field_name in PROFILE_FIELD_NAMES:
        dim_cfg = rubric.dimension_for_field(field_name)
        if dim_cfg is None:
            continue
        profile_field = profile.field(field_name)
        gap_reason = (
            profile_field.gap.reason
            if profile_field.is_gap and profile_field.gap is not None
            else reason
        )
        out.append(_gap_dimension(field_name, dim_cfg.weight, gap_reason))
    return out


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------


def run_scorer(
    conn: sqlite3.Connection,
    *,
    profile: CompanyProfile,
    rubric: RubricConfig,
    workflow_id: str | None = None,
    log_root: Path | None = None,
    now: Callable[[], datetime] | None = None,
) -> ScorerResult:
    """Execute one scorer run end-to-end through the habitat.

    `rubric` is a pre-loaded `RubricConfig` (callers use `scoring.load_rubric`
    to read `config/rubric.toml`). Passing the rubric object rather than a
    path keeps `run_scorer` test-friendly (callers compose without touching
    the filesystem) and matches the Extractor's pattern of taking the upstream
    typed object directly.

    All-gaps / no-scorable-fields input short-circuits to an all-excluded
    ScoredCompany with no LLM call ($0 cost), step + workflow COMPLETED. A
    workflow that gates on score or coverage is still a COMPLETED workflow —
    the gating fact is on the ScoredCompany; the orchestrator (Slice 6)
    reads it to route to `terminate_no_draft`.

    Non-empty input: one Sonnet call. Parse / schema / missing-dimension
    failures from the response propagate as infrastructure errors — `run_step`
    finalises FAILED, workflow finalises FAILED with `finished_at` stamped,
    and no exception escapes this function.
    """
    clock = now or _utcnow
    wf_id = workflow_id or new_workflow_id()
    company_name = profile.company_name

    workflow = Workflow(
        id=wf_id,
        workflow_type=WORKFLOW_TYPE,
        status=WorkflowStatus.RUNNING,
        started_at=clock().isoformat(),
        metadata={
            "company_name": company_name,
            "input_extracted_count": profile.extracted_count,
            "input_gap_count": profile.gap_count,
        },
    )
    insert_workflow(conn, workflow)
    emit_event(
        conn,
        workflow_id=wf_id,
        event_type=EventType.WORKFLOW_STARTED,
        level=EventLevel.INFO,
        message=f"scorer workflow started for {company_name!r}",
        structured_data={
            "company_name": company_name,
            "workflow_type": WORKFLOW_TYPE,
            "input_extracted_count": profile.extracted_count,
            "input_gap_count": profile.gap_count,
        },
        timestamp=clock(),
    )
    log.info(
        "scorer.run.start",
        workflow_id=wf_id,
        company_name=company_name,
        extracted_count=profile.extracted_count,
        gap_count=profile.gap_count,
    )

    scorable = _scorable_fields(profile, rubric)
    fallback_scored = _build_scored_company(
        company_name, _all_excluded(profile, rubric, reason="no_signals"), rubric
    )

    try:
        with run_step(
            conn,
            workflow_id=wf_id,
            step_index=1,
            agent_name=AGENT_NAME,
            now=clock,
        ) as step:
            if not scorable:
                # No scorable fields — either all-gaps profile or no rubric
                # dimension matches a non-gap field. No LLM call.
                dimensions = _all_excluded(profile, rubric, reason="no_signals")
                scored = _build_scored_company(company_name, dimensions, rubric)
                step.record_cost(0.0)
            else:
                llm_result = complete(
                    model_tier=ModelTier.SONNET,
                    messages=[
                        {
                            "role": "user",
                            "content": _build_user_prompt(
                                company_name,
                                profile,
                                rubric,
                                scorable_fields=scorable,
                            ),
                        }
                    ],
                    workflow_id=wf_id,
                    agent_name=AGENT_NAME,
                    system=SYSTEM_PROMPT,
                    max_tokens=SCORER_MAX_TOKENS,
                    log_root=log_root,
                )
                parsed = _parse_llm_response(llm_result.content)
                dimensions = _assemble_dimensions(
                    profile, rubric, parsed.dimensions, scorable_fields=scorable
                )
                scored = _build_scored_company(company_name, dimensions, rubric)
                step.record_cost(llm_result.cost_usd)
                step.record_output_ref(llm_result.jsonl_ref)

            step.record_structured_data(_projection(scored))
    except Exception as exc:
        finished = clock().isoformat()
        updated = workflow.model_copy(
            update={"status": WorkflowStatus.FAILED, "finished_at": finished}
        )
        update_workflow(conn, updated)
        error_msg = f"{type(exc).__name__}: {exc}"
        emit_event(
            conn,
            workflow_id=wf_id,
            event_type=EventType.WORKFLOW_FAILED,
            level=EventLevel.ERROR,
            message=f"scorer workflow failed: {error_msg}",
            structured_data={"failed_step": AGENT_NAME, "error": error_msg},
            timestamp=clock(),
        )
        cost_total = recompute_cost_total(conn, wf_id)
        log.warning(
            "scorer.run.failed",
            workflow_id=wf_id,
            company_name=company_name,
            error=error_msg,
        )
        return ScorerResult(
            workflow_id=wf_id,
            status=WorkflowStatus.FAILED,
            company_name=company_name,
            scored_company=fallback_scored,
            cost_usd=cost_total,
            error_step=AGENT_NAME,
            error_message=error_msg,
        )

    cost_total = recompute_cost_total(conn, wf_id)
    finished = clock().isoformat()
    completed = workflow.model_copy(
        update={
            "status": WorkflowStatus.COMPLETED,
            "finished_at": finished,
            "cost_total_usd": cost_total,
        }
    )
    update_workflow(conn, completed)
    emit_event(
        conn,
        workflow_id=wf_id,
        event_type=EventType.WORKFLOW_COMPLETED,
        level=EventLevel.INFO,
        message=f"scorer workflow completed for {company_name!r}",
        structured_data={
            "company_name": company_name,
            "cost_total_usd": cost_total,
            "score": scored.score,
            "coverage": scored.coverage,
            "tier": scored.tier,
            "gated_by": scored.gated_by,
        },
        timestamp=clock(),
    )
    log.info(
        "scorer.run.complete",
        workflow_id=wf_id,
        company_name=company_name,
        score=scored.score,
        coverage=scored.coverage,
        tier=scored.tier,
        gated_by=scored.gated_by,
        cost_usd=cost_total,
    )

    return ScorerResult(
        workflow_id=wf_id,
        status=WorkflowStatus.COMPLETED,
        company_name=company_name,
        scored_company=scored,
        cost_usd=cost_total,
    )


__all__ = [
    "AGENT_NAME",
    "EXCLUSION_GAP_PREFIX",
    "EXCLUSION_GROUNDING_FAIL_PREFIX",
    "SCORER_MAX_TOKENS",
    "SYSTEM_PROMPT",
    "WORKFLOW_TYPE",
    "ScorerError",
    "ScorerResult",
    "run_scorer",
]
