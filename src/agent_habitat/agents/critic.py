"""Critic agent — Phase 2 Slice 7.

The user-visible end of ADR-006 §3's fabrication-resistance chain. Consumes
the full upstream evidence trail — `Draft`, `ScoredCompany`, `CompanyProfile`,
`RawSignals` — and produces a `Critique` that PASSES every `DraftClaim` whose
substring chain holds end-to-end, or FAILS specific claims with a precise
hop-pointer + human-readable explanation + Mode-2 classification.

THE TWO-MODE DESIGN (ADR-006 §3, kickoff Mode-2 Option B):

  MODE 1 — MECHANICAL SUBSTRING CHECK (no LLM, deterministic).
    For each claim, walk the five-hop chain:
        claim.text → DimensionScore.grounded_quote → ProfileField.source_spans[].quote
                  → Signal.text → Citation.cited_text (by Researcher construction)
    Each hop is `_normalise_for_substring`d (imported from extractor.py — Slice 3's
    normaliser; do NOT invent a second). The chain passes iff EVERY hop holds.
    All-claims-pass → Critique(passed=True), no LLM call, near-zero cost.

  MODE 2 — LLM-JUDGED FAILURE EXPLANATION + CLASSIFICATION (Haiku, only on failure).
    When a claim fails the mechanical chain, ONE Haiku call attaches:
      - a human-readable explanation pinpointing the failing hop + the
        nearest verbatim upstream quote the Drafter should embed on retry;
      - a classification (`fixable_paraphrase` | `fabricated`) — Mode-2
        Option B metadata used by the orchestrator's retry economics.
    The substring check is the FINAL ARBITER on pass/fail; the LLM does
    not override it (ADR-006 §3).

PROFILE INPUT (small interpretation choice, documented).
  The kickoff signature names `(draft, scored_company, raw_signals)`. Hops 3-4
  (`grounded_quote ⊆ source_span.quote ⊆ Signal.text`) require ProfileField
  data which lives on `CompanyProfile`, not on `ScoredCompany.dimensions[]`.
  `CompanyProfile` is already on `CrewState`; the Critic takes it as an
  additive kwarg so the five-hop chain is mechanically possible without
  re-fetching upstream output. No upstream agent changes.

HOP 5 BOUNDARY (small interpretation choice, documented).
  By Researcher contract (`Signal` docstring), `Signal.text` IS a verbatim
  `Citation.cited_text` span — the two are the same string. The Critic's
  input does not carry separate `Citation` objects. Hop 5 is verified as
  the Researcher's STRUCTURAL invariant: the Signal carries non-empty
  citation-origin markers (`source_url`, `text`). A Signal with an empty
  `source_url` is a "naked" signal that does not trace to a web_search
  citation — that's the failure mode hop 5 catches.

LAYER STRUCTURE (matches Extractor / Scorer / Drafter post-refactor pattern).
  - Layer A `critic_node(*, draft, scored_company, profile, raw_signals,
    workflow_id, log_root=None) -> CriticNodeOutput`. DB-pure: no
    sqlite3.Connection, no run_step, no insert_workflow.
  - Layer B `run_critic(conn, ...) -> CriticResult`. Owns the workflow
    lifecycle + run_step audit envelope. The LangGraph orchestrator
    (Slice 7 part 2) wraps `critic_node` directly via a sibling adapter
    in `crew_graph.py`.

HAIKU TIER. Per ADR-006 §3 + CLAUDE.md model routing table, the Critic
runs on Haiku — cheap, fast, mechanical-judgment task. Mode-2 calls only
fire on failure; happy-path runs spend $0 on the Critic.

ERROR POLICY. Infrastructure failures (Mode-2 LLM raises, malformed JSON,
schema mismatch) propagate per ADR-006 §1 Slice-1 zero-retry. `run_step`
finalises the step FAILED, workflow finalises FAILED with `finished_at`
stamped, no retry, no exception escapes `run_critic`.
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
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

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
from .extractor import _normalise_for_substring  # Slice 3's normaliser — reused per kickoff.
from .models import (
    CHAIN_HOPS,
    VERDICT_CLASSIFICATIONS,
    ClaimVerdict,
    CompanyProfile,
    Critique,
    DimensionScore,
    Draft,
    DraftClaim,
    ProfileField,
    RawSignals,
    ScoredCompany,
    Signal,
    SourceSpan,
)

log = structlog.get_logger(__name__)

WORKFLOW_TYPE = "lead_enrichment_critic"
AGENT_NAME = "critic"

CRITIC_MAX_TOKENS = 600


class CriticError(RuntimeError):
    """Base class for any Critic-shaped failure.

    Infrastructure-shaped: step FAILED, workflow FAILED, no retry
    (ADR-006 §1, Slice 1). Same posture as `ExtractorParseError` /
    `ScorerError` / `DrafterError`.
    """


class CriticParseError(CriticError):
    """A Mode-2 Haiku call returned a response that was not valid JSON matching
    the per-failure shape (`{"classification": ..., "explanation": ...}`).
    """


# ---------------------------------------------------------------------------
# Mode-2 LLM response shape (per-failure). Parsed via Pydantic v2 with
# extra="forbid" — same structured-output method the other Phase 2 agents
# use (schema-in-system-prompt + model_validate_json on LLMResult.content).
# ---------------------------------------------------------------------------


class _LLMFailureJudgement(BaseModel):
    """One per-failure judgement the Haiku call returns."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    classification: str = Field(...)
    explanation: str = Field(..., min_length=1)

    @field_validator("classification")
    @classmethod
    def _classification_in_allowed(cls, value: str) -> str:
        # The Mode-2 call should only ever return one of the two failure
        # classifications. "passed" coming back here is a model error.
        if value not in ("fixable_paraphrase", "fabricated"):
            raise ValueError(
                f"classification must be 'fixable_paraphrase' or 'fabricated'; got {value!r}"
            )
        return value


SYSTEM_PROMPT = """You are the fabrication-resistance critic for a B2B lead-enrichment pipeline.

A mechanical substring check has already determined that one DraftClaim does NOT substring-match its upstream evidence. Your job is NOT to overrule that check — the substring check is final. Your job is to:

  1. Classify the failure as either "fixable_paraphrase" or "fabricated".
  2. Write a one-or-two-sentence explanation the Drafter can read and act on during a retry.

CLASSIFICATIONS — pick exactly one:

  "fixable_paraphrase" — the claim is a FAITHFUL restatement of the upstream evidence but lost the verbatim substring property. Examples:
    - dropped corporate suffix ("Anthropic PBC" → "Anthropic")
    - synonym substitution ("teamed with" → "partnership with")
    - paraphrased structure ("raised $50M" → "$50M in funding")
    The upstream evidence contains the factual content; the Drafter can fix the claim on retry by copying the upstream quote verbatim.

  "fabricated" — the claim asserts a fact that has NO upstream support at all. Examples:
    - invented funding amount with no source
    - invented executive name not in any signal
    - invented partnership the upstream evidence does not mention
    The retry cannot fix a fabricated claim — the upstream evidence does not support it.

OUTPUT RULES:

  1. OUTPUT JSON ONLY. No prose, no markdown fences, no preamble.
  2. SCHEMA — emit exactly this shape:
        {
          "classification": "fixable_paraphrase" | "fabricated",
          "explanation": "<one or two sentences>"
        }
  3. The explanation MUST reference the upstream quote (if any) and what the Drafter should do on retry.
  4. Be honest: if the upstream evidence is missing entirely, classify "fabricated" — do not invent grounding to be charitable.
"""


def _build_failure_user_prompt(
    *,
    claim_text: str,
    failed_hop: str,
    upstream_quote: str | None,
    company_name: str,
) -> str:
    """Per-failure user prompt — one Haiku call per failed claim.

    Keeping each Mode-2 call to one failure keeps the prompt narrow and the
    cost predictable. Empirically, multi-failure prompts produce diluted
    classifications; one-at-a-time is cleaner.
    """
    upstream_block = (
        f'Nearest upstream quote (one hop above the failure):\n  "{upstream_quote}"\n'
        if upstream_quote is not None
        else "Nearest upstream quote: (none — no candidate at the failing hop)\n"
    )
    return (
        f'Company: "{company_name}"\n'
        f'Claim text (from the Drafter): "{claim_text}"\n'
        f"Failed hop: {failed_hop}\n"
        f"{upstream_block}\n"
        "Classify and explain. Output JSON only."
    )


# ---------------------------------------------------------------------------
# Layer A return + Layer B result shapes.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CriticNodeOutput:
    """Layer A return shape — what the pure critic node produces.

    `output_ref` is `None` on the happy path (no LLM call). On Mode-2 the
    Critic may make N Haiku calls (one per failure) and the `output_ref`
    is the LAST jsonl_ref written — sufficient for the run_step audit
    contract (the JSONL file holds all N lines; the line index is the
    most recent one). `structured_data` carries the projection
    `{passed, claim_count, failure_count, mode_2_calls}`.
    """

    critique: Critique
    cost_usd: float
    output_ref: str | None
    structured_data: dict[str, Any]


@dataclass(frozen=True)
class CriticResult:
    """What `run_critic` returns. Mirrors the per-agent `*Result` shapes.

    `critique` is None on infrastructure failure; the Critic has no natural
    "empty critique" fallback (a failed Mode-2 call gives us no signal at
    all about whether the chain held). `error_step` / `error_message` are
    populated only on failure.
    """

    workflow_id: str
    status: WorkflowStatus
    company_name: str
    critique: Critique | None
    cost_usd: float
    error_step: str | None = None
    error_message: str | None = None


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Pure helpers — the five-hop chain walk + Mode-2 parse + projection.
# ---------------------------------------------------------------------------


def _strip_code_fence(text: str) -> str:
    """Best-effort strip of ```json / ``` fences. Defensive; the prompt forbids them.

    Mirrors the helper used by Extractor / Scorer / Drafter; intentionally
    duplicated rather than promoted to a shared helper (four use sites, each
    with one-line semantics, and promotion would require a new shared
    module no other module needs).
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


def _parse_failure_judgement(raw_content: str) -> _LLMFailureJudgement:
    """Parse a Mode-2 Haiku response into a `_LLMFailureJudgement`."""
    cleaned = _strip_code_fence(raw_content)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise CriticParseError(
            f"LLM response was not valid JSON: {exc.msg} (line {exc.lineno}, col {exc.colno})"
        ) from exc
    if not isinstance(data, dict):
        raise CriticParseError("LLM response JSON root must be an object.")
    try:
        return _LLMFailureJudgement.model_validate(data)
    except ValidationError as exc:
        raise CriticParseError(
            f"LLM response did not match the expected failure-judgement schema: {exc}"
        ) from exc


@dataclass(frozen=True)
class _HopWalkResult:
    """Outcome of walking the five-hop chain for one claim.

    `passed` is True iff every hop substring-grounded. On failure,
    `failed_hop` names the breaking hop and `upstream_quote` carries the
    nearest verbatim upstream text the Drafter should embed on retry
    (None when no candidate exists — e.g. the supporting_dimension is
    excluded on the input ScoredCompany).
    """

    passed: bool
    failed_hop: str | None
    upstream_quote: str | None


def _find_dimension(scored_company: ScoredCompany, field_name: str) -> DimensionScore | None:
    """Locate the `DimensionScore` with `field == field_name` (or None)."""
    for dim in scored_company.dimensions:
        if dim.field == field_name:
            return dim
    return None


def _find_grounding_span(grounded_quote: str, profile_field: ProfileField) -> SourceSpan | None:
    """First source_span whose normalised quote contains `grounded_quote` (or None).

    Mirrors the Scorer's `_grounded_in_field` shape, but RETURNS the matching
    span (rather than a bool) so the chain walk can carry it forward to
    the next hop. The Scorer already enforced this hop holds; we re-verify
    as the auditable cold-storage check (ADR-006 §3 invariant: the
    substring check is re-runnable from cold storage with no LLM).
    """
    if profile_field.is_gap:
        return None
    needle = _normalise_for_substring(grounded_quote)
    if not needle:
        return None
    for span in profile_field.source_spans:
        if needle in _normalise_for_substring(span.quote):
            return span
    return None


def _signal_traces_to_citation(signal: Signal) -> bool:
    """Hop-5 structural invariant: the Signal traces to a web_search citation.

    Per Signal's docstring, `text` MUST be a verbatim `Citation.cited_text`
    span (Researcher contract). The Critic does not receive Citation
    objects directly (RawSignals doesn't carry them), but it verifies the
    contract markers:

      - `source_url` is populated (non-empty after strip) — every citation
        carries a source URL.
      - `text` is populated (non-empty after strip) — the citation
        contributed some readable prose.

    A Signal that fails either marker is a "naked" signal — it did not
    come from a web_search citation, so the Researcher's grounding
    contract is broken for it.
    """
    return bool(signal.source_url.strip()) and bool(signal.text.strip())


def _walk_claim_chain(
    *,
    claim: DraftClaim,
    draft: Draft,
    scored_company: ScoredCompany,
    profile: CompanyProfile,
    raw_signals: RawSignals,
) -> _HopWalkResult:
    """Walk the five-hop substring chain for one DraftClaim.

    Returns the first failing hop (or `passed=True` if every hop held).
    All comparisons use `_normalise_for_substring` (whitespace-collapse +
    lowercase). The walk is deterministic and pure — no LLM, no I/O.
    """
    claim_norm = _normalise_for_substring(claim.text)
    if not claim_norm:
        # Caught one hop above by Draft's model_validator (claim.text
        # normalising to empty is rejected at parse time), but defensive.
        return _HopWalkResult(passed=False, failed_hop="claim_in_prose", upstream_quote=None)

    # Hop 1: claim.text ⊆ Draft.prose. Already enforced by Draft's
    # model_validator at parse time; we re-verify defensively to catch any
    # downstream tampering and to give Mode-2 a meaningful failed_hop.
    if claim_norm not in _normalise_for_substring(draft.prose):
        return _HopWalkResult(passed=False, failed_hop="claim_in_prose", upstream_quote=draft.prose)

    # Locate the dimension. If absent or excluded, there is nothing upstream
    # to substring-match against — surface a failure at hop 2.
    dim = _find_dimension(scored_company, claim.supporting_dimension)
    if dim is None or dim.is_excluded or dim.grounded_quote is None:
        return _HopWalkResult(
            passed=False, failed_hop="claim_in_grounded_quote", upstream_quote=None
        )

    # Hop 2: claim.text ⊆ DimensionScore.grounded_quote. This is the hop the
    # Slice 5 live smoke catches ("Anthropic" ⊄ "Anthropic PBC is in early
    # talks..." after the Drafter dropped "PBC").
    if claim_norm not in _normalise_for_substring(dim.grounded_quote):
        return _HopWalkResult(
            passed=False,
            failed_hop="claim_in_grounded_quote",
            upstream_quote=dim.grounded_quote,
        )

    # Look up the ProfileField for the dimension's field. If the field is
    # a gap or absent (should not happen given the dimension wasn't
    # excluded — that's a Scorer contract violation), fail hop 3.
    try:
        profile_field = profile.field(claim.supporting_dimension)
    except KeyError:
        return _HopWalkResult(
            passed=False,
            failed_hop="grounded_quote_in_source_span",
            upstream_quote=dim.grounded_quote,
        )
    if profile_field.is_gap:
        return _HopWalkResult(
            passed=False,
            failed_hop="grounded_quote_in_source_span",
            upstream_quote=dim.grounded_quote,
        )

    # Hop 3: DimensionScore.grounded_quote ⊆ some ProfileField.source_spans[].quote.
    span = _find_grounding_span(dim.grounded_quote, profile_field)
    if span is None:
        # The Scorer should have caught this and excluded the dimension; if
        # we got here, the upstream state is inconsistent. Report the
        # nearest upstream candidate the Drafter can see.
        return _HopWalkResult(
            passed=False,
            failed_hop="grounded_quote_in_source_span",
            upstream_quote=dim.grounded_quote,
        )

    # Hop 4: SourceSpan.quote ⊆ Signal.text (signal at span.signal_index).
    if span.signal_index >= len(raw_signals.signals):
        return _HopWalkResult(
            passed=False,
            failed_hop="source_span_in_signal",
            upstream_quote=span.quote,
        )
    signal = raw_signals.signals[span.signal_index]
    if _normalise_for_substring(span.quote) not in _normalise_for_substring(signal.text):
        return _HopWalkResult(
            passed=False,
            failed_hop="source_span_in_signal",
            upstream_quote=signal.text,
        )

    # Hop 5: Signal traces to a web_search Citation (structural invariant —
    # Signal.text IS the verbatim Citation.cited_text by Researcher
    # contract; we verify the citation-origin markers).
    if not _signal_traces_to_citation(signal):
        return _HopWalkResult(
            passed=False,
            failed_hop="signal_traces_to_citation",
            upstream_quote=signal.text,
        )

    return _HopWalkResult(passed=True, failed_hop=None, upstream_quote=None)


def _projection(critique: Critique, *, mode_2_calls: int) -> dict[str, object]:
    """Small fixed projection mirrored onto `step.completed.structured_data`.

    Per ADR-006 §1.3: enough that an auditor can browse the events table
    without resolving any JSONL refs. `mode_2_calls` surfaces how many
    Haiku judgements the Critic actually made (zero on the happy path).
    """
    return {
        "passed": critique.passed,
        "claim_count": critique.claim_count,
        "failure_count": critique.failure_count,
        "mode_2_calls": mode_2_calls,
        "all_fabricated": critique.all_fabricated,
    }


# ---------------------------------------------------------------------------
# Layer A — pure critic node.
# ---------------------------------------------------------------------------


def critic_node(
    *,
    draft: Draft,
    scored_company: ScoredCompany,
    profile: CompanyProfile,
    raw_signals: RawSignals,
    workflow_id: str,
    log_root: Path | None = None,
) -> CriticNodeOutput:
    """Layer A: pure critic logic.

    Walks the mechanical five-hop substring chain for every `DraftClaim`.
    On all-pass returns `Critique(passed=True)` with zero LLM cost. On any
    failure, makes one Haiku call PER FAILED CLAIM to attach a Mode-2
    explanation + classification — the substring check still determines
    pass/fail, the Mode-2 metadata only annotates the failure for the
    Drafter retry prompt and the orchestrator's retry economics.

    Does NOT touch the database, does NOT call `run_step`, does NOT
    insert/update workflows. Mode-2 parse / schema failures raise
    `CriticParseError`; the caller converts to a FAILED step.

    `workflow_id` flows through to `llm.complete()` for telemetry
    attribution only — no workflow row is written here.
    """
    company_name = draft.company_name
    verdicts: list[ClaimVerdict] = []
    cost_total: float = 0.0
    last_output_ref: str | None = None
    mode_2_calls = 0

    for claim in draft.claims:
        walk = _walk_claim_chain(
            claim=claim,
            draft=draft,
            scored_company=scored_company,
            profile=profile,
            raw_signals=raw_signals,
        )
        if walk.passed:
            verdicts.append(
                ClaimVerdict(
                    claim_text=claim.text,
                    supporting_dimension=claim.supporting_dimension,
                    passed=True,
                    failed_hop=None,
                    explanation="",
                    classification="passed",
                    upstream_quote=None,
                )
            )
            continue

        # Mode-2: one Haiku call to classify + explain this failure.
        assert walk.failed_hop is not None  # _HopWalkResult invariant.
        llm_result = complete(
            model_tier=ModelTier.HAIKU,
            messages=[
                {
                    "role": "user",
                    "content": _build_failure_user_prompt(
                        claim_text=claim.text,
                        failed_hop=walk.failed_hop,
                        upstream_quote=walk.upstream_quote,
                        company_name=company_name,
                    ),
                }
            ],
            workflow_id=workflow_id,
            agent_name=AGENT_NAME,
            system=SYSTEM_PROMPT,
            max_tokens=CRITIC_MAX_TOKENS,
            log_root=log_root,
        )
        cost_total += llm_result.cost_usd
        last_output_ref = llm_result.jsonl_ref
        mode_2_calls += 1
        judgement = _parse_failure_judgement(llm_result.content)
        verdicts.append(
            ClaimVerdict(
                claim_text=claim.text,
                supporting_dimension=claim.supporting_dimension,
                passed=False,
                failed_hop=walk.failed_hop,
                explanation=judgement.explanation,
                classification=judgement.classification,
                upstream_quote=walk.upstream_quote,
            )
        )

    critique = Critique(
        company_name=company_name,
        passed=all(v.passed for v in verdicts),
        verdicts=verdicts,
    )
    return CriticNodeOutput(
        critique=critique,
        cost_usd=cost_total,
        output_ref=last_output_ref,
        structured_data=_projection(critique, mode_2_calls=mode_2_calls),
    )


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------


def run_critic(
    conn: sqlite3.Connection,
    *,
    draft: Draft,
    scored_company: ScoredCompany,
    profile: CompanyProfile,
    raw_signals: RawSignals,
    workflow_id: str | None = None,
    log_root: Path | None = None,
    now: Callable[[], datetime] | None = None,
) -> CriticResult:
    """Execute one critic run end-to-end through the habitat.

    Layer B wrapper: owns the workflow lifecycle and `run_step` audit
    envelope, delegating the pure agent logic to `critic_node`. The
    LangGraph orchestrator wraps `critic_node` directly via the
    `crew_graph.py` adapter; this function preserves the standalone-CLI
    entry point (used by `agent-habitat run-critic`).

    All-pass critique → no LLM call, $0 cost, COMPLETED. Any failure →
    one Haiku call per failed claim, COMPLETED, Critique.passed=False.
    Infrastructure error (Mode-2 LLM raises, parse failure, schema
    mismatch) → step FAILED, workflow FAILED, no exception escapes.
    """
    clock = now or _utcnow
    wf_id = workflow_id or new_workflow_id()
    company_name = draft.company_name

    workflow = Workflow(
        id=wf_id,
        workflow_type=WORKFLOW_TYPE,
        status=WorkflowStatus.RUNNING,
        started_at=clock().isoformat(),
        metadata={
            "company_name": company_name,
            "input_claim_count": draft.claim_count,
        },
    )
    insert_workflow(conn, workflow)
    emit_event(
        conn,
        workflow_id=wf_id,
        event_type=EventType.WORKFLOW_STARTED,
        level=EventLevel.INFO,
        message=f"critic workflow started for {company_name!r}",
        structured_data={
            "company_name": company_name,
            "workflow_type": WORKFLOW_TYPE,
            "input_claim_count": draft.claim_count,
        },
        timestamp=clock(),
    )
    log.info(
        "critic.run.start",
        workflow_id=wf_id,
        company_name=company_name,
        claim_count=draft.claim_count,
    )

    critique: Critique | None = None

    try:
        with run_step(
            conn,
            workflow_id=wf_id,
            step_index=1,
            agent_name=AGENT_NAME,
            now=clock,
        ) as step:
            node_out = critic_node(
                draft=draft,
                scored_company=scored_company,
                profile=profile,
                raw_signals=raw_signals,
                workflow_id=wf_id,
                log_root=log_root,
            )
            critique = node_out.critique
            step.record_cost(node_out.cost_usd)
            if node_out.output_ref is not None:
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
            message=f"critic workflow failed: {error_msg}",
            structured_data={"failed_step": AGENT_NAME, "error": error_msg},
            timestamp=clock(),
        )
        cost_total = recompute_cost_total(conn, wf_id)
        log.warning(
            "critic.run.failed",
            workflow_id=wf_id,
            company_name=company_name,
            error=error_msg,
        )
        return CriticResult(
            workflow_id=wf_id,
            status=WorkflowStatus.FAILED,
            company_name=company_name,
            critique=None,
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
    assert critique is not None

    # If the critique caught any failure, emit the fabrication-detected
    # event taxonomy member from Slice 4 — its first writer (ADR-006 §3,
    # the events.py FABRICATION_DETECTED taxonomy member existed in
    # anticipation of this slice).
    if not critique.passed:
        emit_event(
            conn,
            workflow_id=wf_id,
            event_type=EventType.FABRICATION_DETECTED,
            level=EventLevel.WARN,
            message=(
                f"critic flagged {critique.failure_count} of {critique.claim_count} "
                f"claims for {company_name!r}"
            ),
            structured_data={
                "company_name": company_name,
                "failure_count": critique.failure_count,
                "claim_count": critique.claim_count,
                "all_fabricated": critique.all_fabricated,
                "failed_hops": sorted(
                    {v.failed_hop for v in critique.verdicts if v.failed_hop is not None}
                ),
            },
            timestamp=clock(),
        )

    emit_event(
        conn,
        workflow_id=wf_id,
        event_type=EventType.WORKFLOW_COMPLETED,
        level=EventLevel.INFO,
        message=f"critic workflow completed for {company_name!r}",
        structured_data={
            "company_name": company_name,
            "cost_total_usd": cost_total,
            "passed": critique.passed,
            "failure_count": critique.failure_count,
            "claim_count": critique.claim_count,
        },
        timestamp=clock(),
    )
    log.info(
        "critic.run.complete",
        workflow_id=wf_id,
        company_name=company_name,
        passed=critique.passed,
        failure_count=critique.failure_count,
        claim_count=critique.claim_count,
        cost_usd=cost_total,
    )

    return CriticResult(
        workflow_id=wf_id,
        status=WorkflowStatus.COMPLETED,
        company_name=company_name,
        critique=critique,
        cost_usd=cost_total,
    )


__all__ = [
    "AGENT_NAME",
    "CRITIC_MAX_TOKENS",
    "CHAIN_HOPS",
    "VERDICT_CLASSIFICATIONS",
    "ClaimVerdict",
    "CriticError",
    "CriticNodeOutput",
    "CriticParseError",
    "CriticResult",
    "Critique",
    "SYSTEM_PROMPT",
    "WORKFLOW_TYPE",
    "critic_node",
    "run_critic",
]
