"""Extractor agent — Phase 2 Slice 3.

Consumes a `RawSignals` (the Researcher's typed handoff) and produces a
`CompanyProfile` — five structured fields (size, industry, tech_stack,
recent_news, decision_makers) where every extracted field carries
`SourceSpan` refs back into the originating signals, and any field that
cannot be filled is surfaced as an explicit `ExtractionGap` (PATTERNS.md #2).

THE EXTRACTOR'S CONTRACT, briefly:

- Empty `RawSignals` is a VALID input — produces an all-gaps profile and a
  COMPLETED step (ADR-006 §1 empty-outcome contract, one hop downstream).
  No LLM call is made on empty input; the cost line is $0.
- Non-empty input: one Sonnet call through `llm.complete()`. The model is
  instructed to emit a JSON payload matching `CompanyProfile`'s shape.
  Returned content is parsed via Pydantic v2 (`model_validate_json`); any
  parse / schema failure raises and propagates → step FAILED → workflow
  FAILED (ADR-006 zero infrastructure retries).
- Substring grounding is ENFORCED post-parse: each `SourceSpan.quote` must
  appear, verbatim after whitespace+case normalisation, in
  `raw_signals.signals[span.signal_index].text`. Quotes that fail are
  downgraded to `ExtractionGap(reason="span_not_grounded")` — the
  Extractor never lets an over-reach through. This is also how the
  short-cited-text-span forward dependency from the ADR-003 addendum is
  handled: if a span is too narrow to support a field the model would
  otherwise extract, an over-reaching quote is caught and surfaced as a
  gap rather than allowed.

STRUCTURED-OUTPUT METHOD CHOICE.

The Researcher does NOT parse a structured JSON object out of the LLM
response — it reads typed `LLMResult.citations` produced by `llm.py`. So
there is no pre-existing project pattern for "parse a Pydantic object out
of `LLMResult.content`". The three API-level alternatives are:

  1. `messages.parse(output_format=Model)` — different SDK entrypoint;
     requires an `llm.py` change.
  2. Strict tool use (`tools=[{strict: True, ...}]` + `tool_choice`) —
     requires adding a `tool_choice` kwarg to `llm.complete()`.
  3. Schema-in-system-prompt + parse-from-content (chosen) — uses the
     existing `llm.complete()` contract unchanged; the Extractor parses
     `LLMResult.content` itself.

Choice rationale: (1) and (2) both require `llm.py` changes, which ADR-006
makes their own decision and the Slice 3 kickoff says STOP-and-flag for.
(3) consumes only what `llm.complete()` already returns and keeps the
Extractor's interface narrow. `ConfigDict(extra="forbid")` on every
Slice 3 Pydantic model makes the schema strict on parse — unexpected
fields the model invents are rejected, not silently accepted. The
trade-off accepted: the model can emit malformed JSON; we treat that as
an infrastructure failure (no retry), exactly the same way the Researcher
treats an API exception.
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
    PROFILE_FIELD_NAMES,
    CompanyProfile,
    ProfileField,
    RawSignals,
)

log = structlog.get_logger(__name__)

WORKFLOW_TYPE = "lead_enrichment_extractor"
AGENT_NAME = "extractor"

EXTRACTOR_MAX_TOKENS = 1500

SYSTEM_PROMPT = """You are a structured-extraction assistant for a lead-enrichment pipeline.

You receive a list of SIGNALS — verbatim source prose returned by a web research step. Each signal has an index and a text body. You produce a JSON document with five fields about the company:

  - size              (e.g. "~500 employees", "Series B startup")
  - industry          (e.g. "Fintech", "Healthcare middleware", "AI safety")
  - tech_stack        (e.g. "Python", "AWS", "React")
  - recent_news       (e.g. "Raised $30B in 2026", "Launched Claude Opus 4.7")
  - decision_makers   (e.g. "Dario Amodei (CEO)", "Daniela Amodei (President)")

OUTPUT RULES — these are not suggestions:

  1. OUTPUT JSON ONLY. No prose, no markdown fences, no preamble. The first character must be `{` and the last must be `}`.

  2. EVERY EXTRACTED FIELD CARRIES SOURCE SPANS. For each value you extract you MUST attach a list of source_spans. Each span has:
       - signal_index: the integer index of the SIGNAL the value came from.
       - quote: a VERBATIM SUBSTRING of that signal's text — the exact words you derived the value from. Paraphrase will be rejected by a downstream substring check and downgraded to a gap.

  3. IF THE SIGNALS DO NOT SUPPORT A FIELD, RETURN A GAP. Do NOT guess, fabricate, or over-reach beyond what the cited text actually says. A short, narrow cited span may legitimately not support a field — that is a gap, not a reason to stretch. Use the gap shape:
       {"gap": {"reason": "<one of the conventional reasons>"}}
     Conventional reasons:
       - "field_not_in_signals"     — signals do not mention this field at all
       - "insufficient_source_span" — cited spans are too narrow to support extraction

  4. SURFACING A GAP IS THE CORRECT ANSWER WHEN THE DATA IS NOT THERE. A gappy profile is a HIGH-QUALITY result; a fabricated profile is a defect.

SCHEMA (emit exactly this shape):

{
  "size":             <field>,
  "industry":         <field>,
  "tech_stack":       <field>,
  "recent_news":      <field>,
  "decision_makers":  <field>
}

where <field> is one of:

  Extracted form:
    {
      "values": ["<extracted value>", ...],
      "source_spans": [
        {"signal_index": <int>, "quote": "<verbatim substring of that signal>"},
        ...
      ]
    }

  Gap form:
    {"gap": {"reason": "<conventional reason>"}}

Do NOT add fields that are not in the schema. Do NOT wrap the JSON in markdown.
"""


def _user_prompt(raw_signals: RawSignals) -> str:
    signal_payload = [
        {"signal_index": i, "text": sig.text} for i, sig in enumerate(raw_signals.signals)
    ]
    body = json.dumps(signal_payload, ensure_ascii=False, indent=2)
    return (
        f'Company: "{raw_signals.company_name}"\n\n'
        f"SIGNALS ({raw_signals.signal_count} total, {raw_signals.source_count} distinct sources):\n"
        f"{body}\n\n"
        "Extract the structured profile. Output JSON only."
    )


@dataclass(frozen=True)
class ExtractorNodeOutput:
    """Layer A return shape — what the pure extractor node produces.

    The wrapper (`run_extractor`) applies `cost_usd` / `output_ref` /
    `structured_data` onto the `run_step` recorder; the LangGraph
    orchestrator (Phase 2 Slice 6) will do the same. `profile` is the
    typed agent output the scorer / drafter consume.

    `output_ref` is `None` on the empty-input short-circuit (no LLM call
    was made, no JSONL line was written).
    """

    profile: CompanyProfile
    cost_usd: float
    output_ref: str | None
    structured_data: dict[str, Any]


@dataclass(frozen=True)
class ExtractorResult:
    """What `run_extractor` returns. Mirrors the workflow it just wrote.

    All-gaps `profile` with `status=COMPLETED` is a VALID outcome — see
    module docstring and ADR-006 §1's empty-outcome contract.
    `error_step` / `error_message` are populated only when the run failed.
    """

    workflow_id: str
    status: WorkflowStatus
    company_name: str
    profile: CompanyProfile
    cost_usd: float
    error_step: str | None = None
    error_message: str | None = None


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Pure helpers — parse + substring grounding. Pulled out so they're
# directly testable and so `run_extractor` reads as a flat lifecycle.
# ---------------------------------------------------------------------------


def _normalise_for_substring(s: str) -> str:
    """Whitespace-collapse + lowercase. Matches ADR-006 §3's substring discipline.

    Same normalisation we'd expect the Slice 7 critic's substring check to
    apply to claims against `Signal.text` — keeping the rule consistent
    one hop upstream means the chain is uniform end-to-end.
    """
    return " ".join(s.split()).lower()


class ExtractorParseError(RuntimeError):
    """The LLM's response was not valid JSON matching the CompanyProfile schema.

    Propagates out of `run_extractor` as an infrastructure-shaped failure:
    step FAILED, workflow FAILED, no retry (ADR-006 §1, Slice 1).
    """


def _strip_code_fence(text: str) -> str:
    """Best-effort strip of ```json / ``` fences. Defensive; the prompt forbids them."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    while lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _all_gaps_profile(company_name: str, reason: str) -> CompanyProfile:
    """Build a CompanyProfile where every field is a gap with the given reason.

    Used for the empty-input path (`reason="no_signals"`) and as the
    fallback profile in `ExtractorResult` on infrastructure failure.
    """
    gaps = {name: ProfileField.as_gap(reason) for name in PROFILE_FIELD_NAMES}
    return CompanyProfile(company_name=company_name, **gaps)


def _parse_profile(raw_content: str, company_name: str) -> CompanyProfile:
    """Parse the LLM's response body into a CompanyProfile.

    The model is instructed to emit `{size, industry, tech_stack, recent_news,
    decision_makers}` without `company_name`; we set `company_name` here so the
    schema-with-extra=forbid contract holds end-to-end.
    """
    cleaned = _strip_code_fence(raw_content)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ExtractorParseError(
            f"LLM response was not valid JSON: {exc.msg} (line {exc.lineno}, col {exc.colno})"
        ) from exc
    if not isinstance(data, dict):
        raise ExtractorParseError("LLM response JSON root must be an object.")
    if "company_name" in data and data["company_name"] != company_name:
        raise ExtractorParseError(
            "LLM response included a company_name that disagrees with the input."
        )
    data["company_name"] = company_name
    try:
        return CompanyProfile.model_validate(data)
    except ValidationError as exc:
        raise ExtractorParseError(
            f"LLM response did not match CompanyProfile schema: {exc}"
        ) from exc


def _ground_field(field: ProfileField, raw_signals: RawSignals) -> ProfileField:
    """Verify every SourceSpan grounds against the cited Signal.text.

    If the field is already a gap, returns it unchanged. If any span has an
    out-of-range `signal_index` or a `quote` that is not a substring of the
    cited signal (after normalisation), the entire field is downgraded to
    a gap with reason `span_not_grounded` — over-reach is never let through.
    """
    if field.is_gap:
        return field
    n_signals = len(raw_signals.signals)
    for span in field.source_spans:
        if span.signal_index >= n_signals:
            return ProfileField.as_gap("span_not_grounded")
        signal_norm = _normalise_for_substring(raw_signals.signals[span.signal_index].text)
        quote_norm = _normalise_for_substring(span.quote)
        if quote_norm not in signal_norm:
            return ProfileField.as_gap("span_not_grounded")
    return field


def _ground_profile(profile: CompanyProfile, raw_signals: RawSignals) -> CompanyProfile:
    """Apply `_ground_field` to every field — returns a new CompanyProfile (frozen)."""
    grounded = {
        name: _ground_field(profile.field(name), raw_signals) for name in PROFILE_FIELD_NAMES
    }
    return CompanyProfile(company_name=profile.company_name, **grounded)


def _projection(profile: CompanyProfile) -> dict[str, object]:
    """Small fixed projection mirrored onto `step.completed.structured_data`.

    Per ADR-006 §1.3: enough that an auditor can browse the events table
    without resolving any JSONL refs. We surface one `has_<field>` boolean
    per profile field plus the gap count.
    """
    proj: dict[str, object] = {
        f"has_{name}": not profile.field(name).is_gap for name in PROFILE_FIELD_NAMES
    }
    proj["gap_count"] = profile.gap_count
    return proj


# ---------------------------------------------------------------------------
# Layer A — pure extractor node.
# ---------------------------------------------------------------------------


def extractor_node(
    *,
    raw_signals: RawSignals,
    workflow_id: str,
    log_root: Path | None = None,
) -> ExtractorNodeOutput:
    """Layer A: pure extractor logic.

    Empty `raw_signals.signals` short-circuits to an all-gaps profile with
    NO LLM call (cost $0, no `output_ref`). Non-empty input: one Sonnet
    call, JSON parse, substring-grounding pass — returning the typed
    `CompanyProfile` plus the telemetry the wrapper (or LangGraph
    orchestrator) records onto its step.

    Does NOT touch the database, does NOT call `run_step`, does NOT
    insert/update workflows. Parse / schema / grounding failures raise
    `ExtractorParseError` for the caller to convert into a FAILED step
    (ADR-006 §1, Slice 1).

    `workflow_id` flows through to `llm.complete()` for telemetry
    attribution only — no workflow row is written here.
    """
    company_name = raw_signals.company_name
    if raw_signals.signal_count == 0:
        profile = _all_gaps_profile(company_name, reason="no_signals")
        return ExtractorNodeOutput(
            profile=profile,
            cost_usd=0.0,
            output_ref=None,
            structured_data=_projection(profile),
        )
    llm_result = complete(
        model_tier=ModelTier.SONNET,
        messages=[{"role": "user", "content": _user_prompt(raw_signals)}],
        workflow_id=workflow_id,
        agent_name=AGENT_NAME,
        system=SYSTEM_PROMPT,
        max_tokens=EXTRACTOR_MAX_TOKENS,
        log_root=log_root,
    )
    parsed = _parse_profile(llm_result.content, company_name)
    profile = _ground_profile(parsed, raw_signals)
    return ExtractorNodeOutput(
        profile=profile,
        cost_usd=llm_result.cost_usd,
        output_ref=llm_result.jsonl_ref,
        structured_data=_projection(profile),
    )


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------


def run_extractor(
    conn: sqlite3.Connection,
    *,
    raw_signals: RawSignals,
    workflow_id: str | None = None,
    log_root: Path | None = None,
    now: Callable[[], datetime] | None = None,
) -> ExtractorResult:
    """Execute one extractor run end-to-end through the habitat.

    Layer B wrapper: owns the workflow lifecycle and `run_step` audit
    envelope, delegating the pure agent logic to `extractor_node`. The
    LangGraph orchestrator (Phase 2 Slice 6) will wrap `extractor_node`
    directly; this function preserves the standalone-CLI entry point.

    Empty `raw_signals.signals` short-circuits to an all-gaps profile with
    no LLM call — COMPLETED workflow, cost $0, projection records the all-
    gaps state (auditability).

    Non-empty input: one Sonnet call. Parse / schema / substring-grounding
    failures from the response propagate as infrastructure errors —
    `run_step` finalises FAILED, workflow finalises FAILED with
    `finished_at` stamped, and no exception escapes this function.
    """
    clock = now or _utcnow
    wf_id = workflow_id or new_workflow_id()
    company_name = raw_signals.company_name

    workflow = Workflow(
        id=wf_id,
        workflow_type=WORKFLOW_TYPE,
        status=WorkflowStatus.RUNNING,
        started_at=clock().isoformat(),
        metadata={
            "company_name": company_name,
            "input_signal_count": raw_signals.signal_count,
            "input_source_count": raw_signals.source_count,
        },
    )
    insert_workflow(conn, workflow)
    emit_event(
        conn,
        workflow_id=wf_id,
        event_type=EventType.WORKFLOW_STARTED,
        level=EventLevel.INFO,
        message=f"extractor workflow started for {company_name!r}",
        structured_data={
            "company_name": company_name,
            "workflow_type": WORKFLOW_TYPE,
            "input_signal_count": raw_signals.signal_count,
        },
        timestamp=clock(),
    )
    log.info(
        "extractor.run.start",
        workflow_id=wf_id,
        company_name=company_name,
        signal_count=raw_signals.signal_count,
    )

    profile = _all_gaps_profile(company_name, reason="no_signals")

    try:
        with run_step(
            conn,
            workflow_id=wf_id,
            step_index=1,
            agent_name=AGENT_NAME,
            now=clock,
        ) as step:
            node_out = extractor_node(
                raw_signals=raw_signals,
                workflow_id=wf_id,
                log_root=log_root,
            )
            profile = node_out.profile
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
            message=f"extractor workflow failed: {error_msg}",
            structured_data={"failed_step": AGENT_NAME, "error": error_msg},
            timestamp=clock(),
        )
        cost_total = recompute_cost_total(conn, wf_id)
        log.warning(
            "extractor.run.failed",
            workflow_id=wf_id,
            company_name=company_name,
            error=error_msg,
        )
        return ExtractorResult(
            workflow_id=wf_id,
            status=WorkflowStatus.FAILED,
            company_name=company_name,
            profile=_all_gaps_profile(company_name, reason="extraction_failed"),
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
        message=f"extractor workflow completed for {company_name!r}",
        structured_data={
            "company_name": company_name,
            "cost_total_usd": cost_total,
            "gap_count": profile.gap_count,
            "extracted_count": profile.extracted_count,
        },
        timestamp=clock(),
    )
    log.info(
        "extractor.run.complete",
        workflow_id=wf_id,
        company_name=company_name,
        gap_count=profile.gap_count,
        extracted_count=profile.extracted_count,
        cost_usd=cost_total,
    )

    return ExtractorResult(
        workflow_id=wf_id,
        status=WorkflowStatus.COMPLETED,
        company_name=company_name,
        profile=profile,
        cost_usd=cost_total,
    )


__all__ = [
    "AGENT_NAME",
    "EXTRACTOR_MAX_TOKENS",
    "ExtractorNodeOutput",
    "ExtractorParseError",
    "ExtractorResult",
    "SYSTEM_PROMPT",
    "WORKFLOW_TYPE",
    "extractor_node",
    "run_extractor",
]
