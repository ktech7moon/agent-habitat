"""Drafter agent — Phase 2 Slice 5.

Consumes a `ScoredCompany` (the Scorer's typed handoff) and produces a `Draft`
— a short outreach message personalised to the company plus an enumerable list
of `DraftClaim` records. Each claim anchors a verbatim span of the prose to one
non-excluded `ScoredCompany.dimensions[]` entry; that anchor is what the Slice 7
critic later walks (DimensionScore.grounded_quote → ProfileField.source_spans →
Signal.text → Citation.cited_text) when verifying the prose did not invent
facts the upstream chain never produced.

THE DRAFTER'S CONTRACT, briefly:

- **Input scope.** `drafter_node(scored_company=...)` consumes ONE typed
  upstream handoff: the ScoredCompany. ADR-006 §1's CrewState carries
  `raw_signals` and `profile` too; broadening the Drafter input to include
  them is an additive change Phase 2 Slice 6 (LangGraph orchestrator) can
  make if the prose noticeably suffers without them. Slice 5 scopes to the
  simplest shape that makes the critic's substring check mechanically
  possible — and the per-dimension `grounded_quote` + `reasoning` on
  ScoredCompany already carries everything the first hop of that check
  needs.

- **Below-floor / below-coverage input** (`scored_company.routes_to_draft is
  False`) raises `DrafterCalledBelowFloorError`. The orchestrator (Slice 6)
  owns routing to `terminate_no_draft`; a Drafter being called against a
  gated ScoredCompany is a caller bug, not a normal outcome — the explicit
  raise makes the bug loud rather than letting the Drafter silently produce
  prose against a record the operator already decided not to draft.

- **High-score-low-coverage input** (`routes_to_draft is True` but coverage
  is well below 1.0) proceeds NORMALLY — no in-prose hedging, no special
  branch. The operator already sees the coverage number on the
  ScoredCompany; the CLI surface (decision-support footer) discloses it
  per PATTERNS.md #4. The Drafter does not duplicate that disclosure into
  the prose itself — that would conflate two surfaces and risk the LLM
  inventing hedge phrasing that itself becomes an ungrounded claim.

- **Output shape (Draft).** Pydantic v2, `extra="forbid"`, `frozen=True`,
  matching the existing handoff models. Two structural invariants:
    * Each `DraftClaim.text` is a verbatim substring of `Draft.prose` after
      whitespace+case normalisation (model validator).
    * Each `DraftClaim.supporting_dimension` is a `PROFILE_FIELD_NAMES`
      member (per-DraftClaim field validator) AND, post-parse, points at a
      non-excluded `DimensionScore` on the input `ScoredCompany` — that
      cross-input check happens in `drafter_node` because the model itself
      does not see the input ScoredCompany.

- **Infrastructure error** (LLM raise, malformed JSON, schema mismatch,
  bad supporting_dimension) propagates per ADR-006 §1 — `run_step`
  finalises the step FAILED, the workflow is finalised FAILED with
  `finished_at` stamped, no retry, no exception escapes `run_drafter`.

- **Opus 4.7.** ADR-001 reserves Opus for the user-visible-prose agent;
  this is that agent. Cost per call lands in `LLMResult.cost_usd` via
  the existing `llm.complete()` contract — no `llm.py` change.

STRUCTURED-OUTPUT METHOD — same as Slice 3 (Extractor) and Slice 4
(Scorer): schema-in-system-prompt + Pydantic `model_validate_json` on
`LLMResult.content`. No `llm.py` change required.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from pydantic import ValidationError

from ..llm import ModelTier, complete
from ..observability import EventType, emit_event
from ..orchestration.run_step import run_step
from ..state import (
    EventLevel,
    Workflow,
    WorkflowStatus,
    insert_workflow,
    new_workflow_id,
    recompute_cost_total,
    update_workflow,
)
from .models import (
    Draft,
    DraftClaim,
    ScoredCompany,
)

log = structlog.get_logger(__name__)

WORKFLOW_TYPE = "lead_enrichment_drafter"
AGENT_NAME = "drafter"

DRAFTER_MAX_TOKENS = 2000


class DrafterError(RuntimeError):
    """Base class for any drafter-shaped failure.

    Slice 6 (orchestrator) can `except DrafterError` for blanket handling
    or distinguish via the subclass. Same posture as `ExtractorParseError`
    / `ScorerError`: an infrastructure-shaped failure → step FAILED,
    workflow FAILED, no retry (ADR-006 §1, Slice 1).
    """


class DrafterParseError(DrafterError):
    """LLM response was unparseable, schema-invalid, or violated cross-input invariants.

    Raised when:
      - The LLM output is not valid JSON.
      - The JSON does not match the `Draft` schema (extra fields, missing
        required fields, claim.text not in prose, supporting_dimension not
        a PROFILE_FIELD_NAMES value).
      - A claim's `supporting_dimension` references a dimension that is
        excluded on the input ScoredCompany (post-parse cross-input check).
    """


class DrafterCalledBelowFloorError(DrafterError):
    """`drafter_node` was called against a ScoredCompany that did not pass both gates.

    Routing is the orchestrator's job (ADR-006 §1: `terminate_no_draft`).
    A Drafter being invoked here means the orchestrator failed to route —
    or, in the standalone-CLI path, that the CLI bypassed the gating
    check. Raising loudly is preferable to silently drafting prose
    against a record the operator already gated out.
    """


SYSTEM_PROMPT = """You are a senior outreach copywriter for a B2B lead-enrichment pipeline.

You receive a SCORED COMPANY: a composite score, per-dimension breakdowns with grounded quotes, and the model's per-dimension reasoning. You produce a JSON document with two parts: a short outreach message PROSE and an enumerable list of CLAIMS — one entry per concrete factual span in the prose, each anchored to one of the input dimensions.

OUTPUT RULES — these are not suggestions:

  1. OUTPUT JSON ONLY. No prose, no markdown fences, no preamble. The first character must be `{` and the last must be `}`.

  2. SCHEMA — emit exactly this shape:
        {
          "prose": "<the outreach message — one to three short paragraphs>",
          "claims": [
            {
              "text": "<verbatim span from `prose` — the factual claim>",
              "supporting_dimension": "<one of the dimension `field` values you were given>"
            },
            ...
          ]
        }

  3. PROSE GUIDELINES.
       - One to three short paragraphs, target ~80-220 words total.
       - Address the operator's prospect (a decision-maker at the company), not the operator themselves.
       - Lead with one specific, grounded observation about the company; pivot to a relevant capability or service the operator can offer; close with a low-friction next step.
       - Do NOT use the score, coverage, or tier in the prose. Those are operator-internal artefacts. Write what the prospect would read.
       - Do NOT invent facts. Every concrete claim about the company must trace to one of the dimensions you were given — see CLAIMS rule below.

  4. CLAIMS RULE — THIS IS LOAD-BEARING.
       - Every concrete factual claim about the COMPANY in the prose MUST be enumerated as one entry in `claims`.
       - `claims[i].text` MUST be a VERBATIM SUBSTRING of `prose`. Whitespace and case are normalised by a downstream check, but the substring itself must appear in the prose. If you write "raised a $50M Series B" in the prose, the claim text must be "raised a $50M Series B" (or a contiguous substring of that), not a paraphrase.
       - `claims[i].supporting_dimension` MUST be one of the dimension `field` values you were given (e.g. "industry", "tech_stack", "recent_news"). Do NOT cite a dimension that was not given to you. Do NOT cite a dimension that was excluded.
       - You may have multiple claims pointing to the same dimension. You may also have claims that are short factual phrases (e.g. "fintech" anchored to "industry") — short is fine; specific is better than long.
       - Generic prose (greetings, transitions, closing CTAs) does NOT need to be in `claims`. Only concrete factual claims about the company do.

  5. NEVER INVENT. If the dimensions do not support a claim, do NOT make it. A short, dimension-grounded outreach is the correct answer; a long, fabricated outreach is a defect. The downstream critic will REJECT any claim whose substring chain does not hold.

  6. KEEP TONE PROFESSIONAL AND DIRECT. Match a senior-engineer-to-senior-engineer register: respectful, concrete, no exclamation marks, no marketing fluff, no presumed familiarity.
"""


def _build_user_prompt(scored_company: ScoredCompany) -> str:
    """Assemble the per-dimension payload the LLM drafts against.

    Excluded dimensions are sent for context but explicitly marked as
    NOT scorable so the model does not cite them. The rationale is
    included even for excluded dimensions — an auditor reading the
    JSONL telemetry can see what the Drafter saw without resolving the
    ScoredCompany separately.
    """
    sections: list[str] = []
    for dim in scored_company.dimensions:
        if dim.is_excluded:
            sections.append(
                f"DIMENSION: {dim.field}  (weight={dim.weight:.2f}): EXCLUDED — do not cite.\n"
                f"  reason: {dim.reasoning}"
            )
            continue
        # Scored dimension — give the model the grounded quote + reasoning.
        # `grounded_quote` is non-None iff `is_excluded` is False (model invariant).
        assert dim.grounded_quote is not None and dim.score is not None
        sections.append(
            f"DIMENSION: {dim.field}  (weight={dim.weight:.2f}, score={dim.score:.1f}/5)\n"
            f'  grounded_quote: "{dim.grounded_quote}"\n'
            f"  reasoning: {dim.reasoning}"
        )

    sections_text = "\n\n".join(sections)
    score_str = (
        f"{scored_company.score:.1f}/100" if scored_company.score is not None else "(no score)"
    )
    coverage_pct = scored_company.coverage * 100.0
    return (
        f'Company: "{scored_company.company_name}"\n'
        f"Operator-internal context (do NOT include in prose): "
        f"score={score_str}, coverage={coverage_pct:.0f}%, tier={scored_company.tier}.\n\n"
        "DIMENSIONS the scorer produced (cite ONLY non-excluded ones; "
        "`supporting_dimension` in your output must be one of the `field` values below):\n\n"
        f"{sections_text}\n\n"
        "Output JSON only. The prose addresses the prospect; the claims enumerate "
        "every concrete factual claim in the prose."
    )


@dataclass(frozen=True)
class DrafterNodeOutput:
    """Layer A return shape — what the pure drafter node produces.

    The wrapper (`run_drafter`) applies `cost_usd` / `output_ref` /
    `structured_data` onto the `run_step` recorder; the LangGraph
    orchestrator (Phase 2 Slice 6) will do the same. `draft` is the typed
    agent output the operator (and the Slice 7 critic) consume.

    `output_ref` is always non-None for the Drafter — it always makes one
    Opus call (no empty-input short-circuit; the orchestrator prevents
    drafter invocation on gated input via `routes_to_draft`).
    """

    draft: Draft
    cost_usd: float
    output_ref: str
    structured_data: dict[str, Any]


@dataclass(frozen=True)
class DrafterResult:
    """What `run_drafter` returns. Mirrors the workflow it just wrote.

    `draft` is None on infrastructure failure (the Drafter has no natural
    "empty draft" fallback; an absent draft is the honest shape). On
    successful completion `draft` is the produced Draft.

    `error_step` / `error_message` are populated only on failure.
    """

    workflow_id: str
    status: WorkflowStatus
    company_name: str
    draft: Draft | None
    cost_usd: float
    error_step: str | None = None
    error_message: str | None = None


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Pure helpers — parse, post-parse cross-input validation, projection.
# ---------------------------------------------------------------------------


def _strip_code_fence(text: str) -> str:
    """Best-effort strip of ```json / ``` fences. Defensive; the prompt forbids them.

    Mirrors `extractor._strip_code_fence` / `scorer._strip_code_fence` in shape;
    intentionally duplicated rather than promoted to a shared helper — three
    use sites, each with one-line semantics, and promotion would require a
    new shared module no other module needs.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    while lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _parse_draft(raw_content: str, company_name: str) -> Draft:
    """Parse the LLM response body into a `Draft`.

    The prompt does not require the model to emit `company_name` (it's
    operator-internal — set here). If the model emits one anyway, it must
    agree with the input; disagreement is treated as an LLM error.
    """
    cleaned = _strip_code_fence(raw_content)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise DrafterParseError(
            f"LLM response was not valid JSON: {exc.msg} (line {exc.lineno}, col {exc.colno})"
        ) from exc
    if not isinstance(data, dict):
        raise DrafterParseError("LLM response JSON root must be an object.")
    if "company_name" in data and data["company_name"] != company_name:
        raise DrafterParseError(
            "LLM response included a company_name that disagrees with the input."
        )
    data["company_name"] = company_name
    try:
        return Draft.model_validate(data)
    except ValidationError as exc:
        raise DrafterParseError(f"LLM response did not match Draft schema: {exc}") from exc


def _check_claims_against_input(draft: Draft, scored_company: ScoredCompany) -> None:
    """Post-parse cross-input invariant: each claim's supporting_dimension must
    point at a non-excluded `DimensionScore` on the input `ScoredCompany`.

    This is the check the Draft model itself cannot do — the model has no
    handle on the input ScoredCompany. Failure raises `DrafterParseError`,
    same shape as a schema violation: an LLM that cites an excluded
    dimension has produced an over-reach the Drafter must not let through.
    """
    by_field = {dim.field: dim for dim in scored_company.dimensions}
    for i, claim in enumerate(draft.claims):
        dim = by_field.get(claim.supporting_dimension)
        if dim is None:
            raise DrafterParseError(
                f"claims[{i}].supporting_dimension {claim.supporting_dimension!r} "
                "is not present on the input ScoredCompany.dimensions; "
                f"available: {sorted(by_field.keys())!r}"
            )
        if dim.is_excluded:
            raise DrafterParseError(
                f"claims[{i}].supporting_dimension {claim.supporting_dimension!r} "
                "is excluded on the input ScoredCompany — excluded dimensions have "
                "no grounded_quote and cannot anchor a Draft claim."
            )


def _projection(draft: Draft) -> dict[str, object]:
    """Small fixed projection mirrored onto `step.completed.structured_data`.

    Per ADR-006 §1.3 + Slice 5 spec: enough that an auditor can browse the
    events table without resolving any JSONL refs. `paragraph_count` and
    `char_count` are the named projections; `claim_count` is added because
    a Draft with zero claims is the failure mode the critic cares about
    most (nothing to ground), so surfacing it in the projection lets the
    calibration story spot it.
    """
    return {
        "paragraph_count": draft.paragraph_count,
        "char_count": draft.char_count,
        "claim_count": draft.claim_count,
    }


# ---------------------------------------------------------------------------
# Layer A — pure drafter node.
# ---------------------------------------------------------------------------


def drafter_node(
    *,
    scored_company: ScoredCompany,
    workflow_id: str,
    log_root: Path | None = None,
) -> DrafterNodeOutput:
    """Layer A: pure drafter logic.

    Calls Opus 4.7 once with the per-dimension payload, parses the JSON
    response into a `Draft`, runs the cross-input invariant check, and
    returns the typed `Draft` plus the telemetry the wrapper (or LangGraph
    orchestrator) records onto its step.

    Does NOT touch the database, does NOT call `run_step`, does NOT
    insert/update workflows. Parse / schema / cross-input failures raise
    `DrafterParseError` (subclass of `DrafterError`); a below-floor or
    below-coverage `scored_company` raises `DrafterCalledBelowFloorError`
    (subclass of `DrafterError`) — orchestrator routing bug, not a
    normal outcome (ADR-006 §1, Slice 1).

    `workflow_id` flows through to `llm.complete()` for telemetry
    attribution only — no workflow row is written here.
    """
    if not scored_company.routes_to_draft:
        raise DrafterCalledBelowFloorError(
            f"drafter_node called against a gated ScoredCompany "
            f"(gated_by={scored_company.gated_by!r}, "
            f"score={scored_company.score!r}, coverage={scored_company.coverage!r}); "
            "the orchestrator should have routed this to terminate_no_draft."
        )

    company_name = scored_company.company_name
    llm_result = complete(
        model_tier=ModelTier.OPUS,
        messages=[{"role": "user", "content": _build_user_prompt(scored_company)}],
        workflow_id=workflow_id,
        agent_name=AGENT_NAME,
        system=SYSTEM_PROMPT,
        max_tokens=DRAFTER_MAX_TOKENS,
        log_root=log_root,
    )
    draft = _parse_draft(llm_result.content, company_name)
    _check_claims_against_input(draft, scored_company)
    return DrafterNodeOutput(
        draft=draft,
        cost_usd=llm_result.cost_usd,
        output_ref=llm_result.jsonl_ref,
        structured_data=_projection(draft),
    )


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------


def run_drafter(
    conn: sqlite3.Connection,
    *,
    scored_company: ScoredCompany,
    workflow_id: str | None = None,
    log_root: Path | None = None,
    now: Callable[[], datetime] | None = None,
) -> DrafterResult:
    """Execute one drafter run end-to-end through the habitat.

    Layer B wrapper: owns the workflow lifecycle and `run_step` audit
    envelope, delegating the pure agent logic to `drafter_node`. The
    LangGraph orchestrator (Phase 2 Slice 6) will wrap `drafter_node`
    directly; this function preserves the standalone-CLI entry point.

    Calling against a gated `scored_company` (`routes_to_draft is False`)
    is treated as an infrastructure failure: `drafter_node` raises
    `DrafterCalledBelowFloorError`, `run_step` finalises the step
    FAILED, the workflow finalises FAILED. The CLI surface SHOULD check
    `scored_company.routes_to_draft` before calling `run_drafter` and
    print a clearly-labelled "no draft" outcome instead — that is the
    Slice 5 stand-in for the orchestrator's `terminate_no_draft` routing.

    Non-gated input: one Opus call. Parse / schema / cross-input
    invariant failures propagate as infrastructure errors — `run_step`
    finalises FAILED, workflow finalises FAILED with `finished_at`
    stamped, and no exception escapes this function.
    """
    clock = now or _utcnow
    wf_id = workflow_id or new_workflow_id()
    company_name = scored_company.company_name

    workflow = Workflow(
        id=wf_id,
        workflow_type=WORKFLOW_TYPE,
        status=WorkflowStatus.RUNNING,
        started_at=clock().isoformat(),
        metadata={
            "company_name": company_name,
            "input_score": scored_company.score,
            "input_coverage": scored_company.coverage,
            "input_tier": scored_company.tier,
        },
    )
    insert_workflow(conn, workflow)
    emit_event(
        conn,
        workflow_id=wf_id,
        event_type=EventType.WORKFLOW_STARTED,
        level=EventLevel.INFO,
        message=f"drafter workflow started for {company_name!r}",
        structured_data={
            "company_name": company_name,
            "workflow_type": WORKFLOW_TYPE,
            "input_score": scored_company.score,
            "input_coverage": scored_company.coverage,
            "input_tier": scored_company.tier,
        },
        timestamp=clock(),
    )
    log.info(
        "drafter.run.start",
        workflow_id=wf_id,
        company_name=company_name,
        score=scored_company.score,
        coverage=scored_company.coverage,
        tier=scored_company.tier,
    )

    draft: Draft | None = None

    try:
        with run_step(
            conn,
            workflow_id=wf_id,
            step_index=1,
            agent_name=AGENT_NAME,
            now=clock,
        ) as step:
            node_out = drafter_node(
                scored_company=scored_company,
                workflow_id=wf_id,
                log_root=log_root,
            )
            draft = node_out.draft
            step.record_cost(node_out.cost_usd)
            step.record_output_ref(node_out.output_ref)
            step.record_structured_data(node_out.structured_data)
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
            message=f"drafter workflow failed: {error_msg}",
            structured_data={"failed_step": AGENT_NAME, "error": error_msg},
            timestamp=clock(),
        )
        cost_total = recompute_cost_total(conn, wf_id)
        log.warning(
            "drafter.run.failed",
            workflow_id=wf_id,
            company_name=company_name,
            error=error_msg,
        )
        return DrafterResult(
            workflow_id=wf_id,
            status=WorkflowStatus.FAILED,
            company_name=company_name,
            draft=None,
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
    # `draft` is non-None on the success path: the try-block sets it before
    # `run_step` exits cleanly, so reaching this line means the assignment ran.
    assert draft is not None
    emit_event(
        conn,
        workflow_id=wf_id,
        event_type=EventType.WORKFLOW_COMPLETED,
        level=EventLevel.INFO,
        message=f"drafter workflow completed for {company_name!r}",
        structured_data={
            "company_name": company_name,
            "cost_total_usd": cost_total,
            "paragraph_count": draft.paragraph_count,
            "char_count": draft.char_count,
            "claim_count": draft.claim_count,
        },
        timestamp=clock(),
    )
    log.info(
        "drafter.run.complete",
        workflow_id=wf_id,
        company_name=company_name,
        paragraph_count=draft.paragraph_count,
        char_count=draft.char_count,
        claim_count=draft.claim_count,
        cost_usd=cost_total,
    )

    return DrafterResult(
        workflow_id=wf_id,
        status=WorkflowStatus.COMPLETED,
        company_name=company_name,
        draft=draft,
        cost_usd=cost_total,
    )


# Re-export Draft / DraftClaim here for the convenience of callers that
# import from `agent_habitat.agents.drafter` directly. The agents/__init__.py
# re-exports the same names from `agent_habitat.agents.models`.
__all__ = [
    "AGENT_NAME",
    "DRAFTER_MAX_TOKENS",
    "Draft",
    "DraftClaim",
    "DrafterCalledBelowFloorError",
    "DrafterError",
    "DrafterNodeOutput",
    "DrafterParseError",
    "DrafterResult",
    "SYSTEM_PROMPT",
    "WORKFLOW_TYPE",
    "drafter_node",
    "run_drafter",
]
