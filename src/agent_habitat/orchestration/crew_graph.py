"""LangGraph orchestrator for the Phase 2 lead-enrichment crew — Slice 6 + Slice 7.

Wires the five Layer A node functions (`researcher_node`, `extractor_node`,
`scorer_node`, `drafter_node`, `critic_node`) into one LangGraph
`StateGraph(CrewState)` with SQLite-backed checkpointing. The crew shape
follows ADR-006 §1 including Slice 7's bounded-retry edge:

    START → researcher → extractor → scorer → ROUTE ─┐
                                                      ├─ if gated  → terminate_no_draft → END
                                                      │
                                                      └─ if passed → request_drafter_approval (interrupt)
                                                                         │
                                                                         ROUTE ─┐
                                                                                ├─ approved → drafter → critic → ROUTE ─┐
                                                                                │                                       ├─ passed → END
                                                                                │                                       │
                                                                                │                                       ├─ failed + retries==0 → drafter (retry) → critic → ROUTE
                                                                                │                                       │
                                                                                │                                       └─ failed + retries>=1 → terminate_with_critic_failure → END
                                                                                │
                                                                                └─ rejected → terminate_no_draft → END

Slice 7 contract (ADR-006 §1 + §3 — bounded fabrication retry):
  - The Critic node walks the five-hop substring chain for every DraftClaim.
  - First failure: `fabrication_retries` is incremented and the graph routes
    back to the drafter with `state["critique"]` attached; the drafter adapter
    passes the prior Critique as the Drafter's `prior_critique` kwarg.
  - Second failure: workflow finalises FAILED with `terminate_reason =
    "critic_failure"`; the `agent.fabrication_detected` ERROR-level event is
    emitted via `run_critic`'s shared emission path. ADR-006 §1 specifies
    persistent fabrication is halt-worthy, not a benign empty-outcome.

PERSISTENCE OWNERSHIP (Slice 6 STOP #2 — Option A).

The orchestrator owns the workflow row; each agent-bearing node adapter
wraps its Layer A call in `run_step(...)` (which is already step-only —
ADR-006 §2). The existing Layer B wrappers (`run_researcher` &c.) stay
UNCHANGED for standalone CLI use; the orchestrator is a parallel caller
of the same Layer A nodes that also uses the same `run_step` envelope. No
new wrapper layer. The audit shape ADR-002 specifies — one `workflows`
row + N `workflow_steps` rows + an `events` narrative — is preserved.

PERSISTENCE WIRING (Slice 6 STOP #3 — ADR-002 Option 1 in practice).

SqliteSaver writes its own tables (`checkpoints`, `writes`) to the SAME
`data/state/agent_habitat.db` file used by ADR-002's audit tables. The
two writers do NOT share a connection (they share a file) — independent
transactions, independent failure domains, ADR-002's saga-with-
idempotency shape. The SqliteSaver connection is opened with
`check_same_thread=False` defensively (LangGraph internals may execute
on a thread pool in some paths); the audit-table connection retains the
existing `check_same_thread=True`. `workflow_id` is used verbatim as
LangGraph's `thread_id` — uuid4 hex satisfies LangGraph's "opaque text"
contract.

HUMAN CHECKPOINT (Slice 6 STOP #4 — Option A).

`request_drafter_approval` is a non-agent node that writes a
`checkpoint.requested` row via the existing CheckpointSystem
(`request_checkpoint`, which moves the workflow to PAUSED), then calls
LangGraph's `interrupt()`. The graph pauses; the operator approves or
rejects via the existing `agent-habitat checkpoint approve|reject` CLI.
On resume the orchestrator passes `Command(resume={"approved": bool})`
keyed by the checkpoint's resolution.

LangGraph re-executes interrupted nodes from the top on resume, which
means `request_checkpoint` would be called a second time without a
guard — writing a duplicate CHECKPOINT_REQUESTED row and re-pausing the
workflow. The `_find_latest_drafter_approval` short-circuit below
detects the resolved checkpoint from the first pass and returns
immediately on the second, satisfying the idempotency contract without
modifying CheckpointSystem.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import structlog
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt

from ..agents.critic import (
    AGENT_NAME as CRITIC_AGENT_NAME,
)
from ..agents.critic import (
    critic_node,
)
from ..agents.drafter import (
    AGENT_NAME as DRAFTER_AGENT_NAME,
)
from ..agents.drafter import (
    drafter_node,
)
from ..agents.extractor import (
    AGENT_NAME as EXTRACTOR_AGENT_NAME,
)
from ..agents.extractor import (
    extractor_node,
)
from ..agents.models import (
    CompanyProfile,
    Critique,
    Draft,
    RawSignals,
    ScoredCompany,
)
from ..agents.researcher import (
    AGENT_NAME as RESEARCHER_AGENT_NAME,
)
from ..agents.researcher import (
    DEFAULT_MAX_SEARCHES,
    researcher_node,
)
from ..agents.scorer import (
    AGENT_NAME as SCORER_AGENT_NAME,
)
from ..agents.scorer import (
    scorer_node,
)
from ..checkpoint.system import (
    Checkpoint,
    CheckpointResolution,
    get_checkpoint,
    request_checkpoint,
)
from ..observability.events import EventType, emit_event
from ..scoring import RubricConfig
from ..state.models import (
    EventLevel,
    Workflow,
    WorkflowStatus,
    new_workflow_id,
)
from ..state.persistence import (
    insert_workflow,
    load_workflow,
    recompute_cost_total,
    update_workflow,
)
from .crew_state import (
    TERMINATE_REASON_COVERAGE_GATED,
    TERMINATE_REASON_CRITIC_FAILURE,
    TERMINATE_REASON_REJECTED,
    TERMINATE_REASON_SCORE_GATED,
    CrewState,
)
from .run_step import run_step

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Public surface — types + entry points.
# ---------------------------------------------------------------------------


WORKFLOW_TYPE = "lead_enrichment_crew"

#: Action name written into the `checkpoint.requested` event's structured_data
#: by `request_drafter_approval`. The same string identifies the approve_drafter
#: checkpoint when the orchestrator's resume path looks it up.
CHECKPOINT_ACTION_APPROVE_DRAFTER = "approve_drafter"


# Step indices for the agent adapters. Hard-coded (and contiguous) per the
# linear topology — ADR-002's `UNIQUE (workflow_id, step_index)` constraint is
# the only requirement; contiguity is convention. Slice 7 reserves indices
# 6/7 for the bounded-retry drafter+critic pair so a retried workflow still
# satisfies the UNIQUE constraint with one row per (initial, retry) attempt.
STEP_INDEX_RESEARCHER = 1
STEP_INDEX_EXTRACTOR = 2
STEP_INDEX_SCORER = 3
STEP_INDEX_DRAFTER = 4
STEP_INDEX_CRITIC = 5
STEP_INDEX_DRAFTER_RETRY = 6
STEP_INDEX_CRITIC_RETRY = 7


@dataclass(frozen=True)
class CrewResult:
    """Outcome of one orchestrator invocation (initial run or resume).

    Mirrors the per-agent `*Result` shapes for consistency. `status`
    captures the workflow's terminal status after this invocation:

      - RUNNING  → not a terminal outcome; should not appear in a result.
      - PAUSED   → the graph hit an interrupt; the operator must resolve
                   the checkpoint (`agent-habitat checkpoint approve|reject`)
                   and then resume (`agent-habitat run-crew --resume <id>`).
      - COMPLETED → the graph reached END. If `draft is not None` the
                    workflow produced an outreach draft; else
                    `terminate_reason` names why (gated by score/coverage,
                    or operator-rejected).
      - FAILED   → an agent node raised an unrecoverable error
                   (`error_step` + `error_message` populated).
      - CANCELLED → the workflow was rejected at the checkpoint; the
                   audit row already reflects this (CheckpointSystem
                   wrote `workflow.status = CANCELLED` on rejection).
                   `terminate_reason = "rejected"`.
    """

    workflow_id: str
    status: WorkflowStatus
    company_name: str
    raw_signals: RawSignals | None = None
    profile: CompanyProfile | None = None
    scored_company: ScoredCompany | None = None
    draft: Draft | None = None
    critique: Critique | None = None
    fabrication_retries: int = 0
    terminate_reason: str | None = None
    pending_checkpoint_id: int | None = None
    cost_usd: float = 0.0
    error_step: str | None = None
    error_message: str | None = None


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Graph factory.
# ---------------------------------------------------------------------------


def build_crew_graph(
    conn: sqlite3.Connection,
    rubric: RubricConfig,
    *,
    saver: SqliteSaver,
    log_root: Path | None = None,
    max_searches: int = DEFAULT_MAX_SEARCHES,
    now: Callable[[], datetime] | None = None,
) -> CompiledStateGraph[CrewState, None, CrewState, CrewState]:
    """Assemble the crew StateGraph and compile it against `saver`.

    `conn` is the audit-table connection (closed over by the adapters so
    each `run_step` block lands rows on the right database). `saver` is
    a `SqliteSaver` wired to the SAME .db file via its own connection —
    see ADR-002 Option 1 and Slice 6 STOP #3 for why two connections.
    `rubric` is preloaded by the caller (same shape as the standalone
    Scorer CLI: `scoring.load_rubric(path)`); the Slice 4 Scorer's
    rubric handling is unchanged.
    """
    clock = now or _utcnow

    def _researcher_adapter(state: CrewState) -> dict[str, Any]:
        wf_id = state["workflow_id"]
        company_name = state["company_name"]
        with run_step(
            conn,
            workflow_id=wf_id,
            step_index=STEP_INDEX_RESEARCHER,
            agent_name=RESEARCHER_AGENT_NAME,
            now=clock,
        ) as step:
            node_out = researcher_node(
                company_name=company_name,
                workflow_id=wf_id,
                max_searches=max_searches,
                log_root=log_root,
                now=clock,
            )
            step.record_cost(node_out.cost_usd)
            if node_out.output_ref is not None:
                step.record_output_ref(node_out.output_ref)
            step.record_structured_data(node_out.structured_data)
        return {"raw_signals": node_out.raw_signals}

    def _extractor_adapter(state: CrewState) -> dict[str, Any]:
        wf_id = state["workflow_id"]
        raw_signals = state["raw_signals"]
        with run_step(
            conn,
            workflow_id=wf_id,
            step_index=STEP_INDEX_EXTRACTOR,
            agent_name=EXTRACTOR_AGENT_NAME,
            now=clock,
        ) as step:
            node_out = extractor_node(
                raw_signals=raw_signals,
                workflow_id=wf_id,
                log_root=log_root,
            )
            step.record_cost(node_out.cost_usd)
            if node_out.output_ref is not None:
                step.record_output_ref(node_out.output_ref)
            step.record_structured_data(node_out.structured_data)
        return {"profile": node_out.profile}

    def _scorer_adapter(state: CrewState) -> dict[str, Any]:
        wf_id = state["workflow_id"]
        profile = state["profile"]
        with run_step(
            conn,
            workflow_id=wf_id,
            step_index=STEP_INDEX_SCORER,
            agent_name=SCORER_AGENT_NAME,
            now=clock,
        ) as step:
            node_out = scorer_node(
                profile=profile,
                rubric=rubric,
                workflow_id=wf_id,
                log_root=log_root,
            )
            step.record_cost(node_out.cost_usd)
            if node_out.output_ref is not None:
                step.record_output_ref(node_out.output_ref)
            step.record_structured_data(node_out.structured_data)
        return {"scored_company": node_out.scored_company}

    def _drafter_adapter(state: CrewState) -> dict[str, Any]:
        wf_id = state["workflow_id"]
        scored_company = state["scored_company"]
        retries = state.get("fabrication_retries", 0)
        prior_critique = state.get("critique") if retries > 0 else None
        step_index = STEP_INDEX_DRAFTER_RETRY if retries > 0 else STEP_INDEX_DRAFTER
        with run_step(
            conn,
            workflow_id=wf_id,
            step_index=step_index,
            agent_name=DRAFTER_AGENT_NAME,
            now=clock,
        ) as step:
            node_out = drafter_node(
                scored_company=scored_company,
                workflow_id=wf_id,
                log_root=log_root,
                prior_critique=prior_critique,
            )
            step.record_cost(node_out.cost_usd)
            step.record_output_ref(node_out.output_ref)
            step.record_structured_data(node_out.structured_data)
        return {"draft": node_out.draft}

    def _critic_adapter(state: CrewState) -> dict[str, Any]:
        wf_id = state["workflow_id"]
        draft = state["draft"]
        scored_company = state["scored_company"]
        profile = state["profile"]
        raw_signals = state["raw_signals"]
        retries = state.get("fabrication_retries", 0)
        step_index = STEP_INDEX_CRITIC_RETRY if retries > 0 else STEP_INDEX_CRITIC
        with run_step(
            conn,
            workflow_id=wf_id,
            step_index=step_index,
            agent_name=CRITIC_AGENT_NAME,
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
            step.record_cost(node_out.cost_usd)
            if node_out.output_ref is not None:
                step.record_output_ref(node_out.output_ref)
            step.record_structured_data(node_out.structured_data)
        update: dict[str, Any] = {"critique": node_out.critique}
        # Bump retries on every failure so the router can distinguish
        # "first failure → retry" (retries: 0 → 1) from "second failure →
        # halt" (retries: 1 → 2). ADR-006 §1: ONE retry budget — the
        # router refuses to schedule a second retry. A passed critique
        # leaves retries untouched (so an approved workflow records 0 or
        # 1 depending on whether the retry path was needed — useful audit
        # data for Slice 8 calibration).
        if not node_out.critique.passed:
            update["fabrication_retries"] = retries + 1
        return update

    def _terminate_with_critic_failure(state: CrewState) -> dict[str, Any]:
        # Persistent fabrication after the bounded retry. ADR-006 §1 specifies
        # this is halt-worthy, not a benign empty-outcome. The node writes the
        # terminate_reason; `_invoke_and_finalise` reads it and finalises the
        # workflow as FAILED (rather than COMPLETED) per the terminate-reason
        # discrimination below.
        return {"terminate_reason": TERMINATE_REASON_CRITIC_FAILURE}

    def _request_drafter_approval(state: CrewState) -> dict[str, Any]:
        wf_id = state["workflow_id"]
        scored_company = state["scored_company"]
        # Resume idempotency: LangGraph re-executes interrupted nodes from
        # the top on resume. If a resolved approve_drafter checkpoint already
        # exists for this workflow, reuse its resolution rather than writing
        # a second CHECKPOINT_REQUESTED row.
        existing = _find_latest_drafter_approval(conn, wf_id)
        if existing is not None and not existing.is_pending:
            approved = existing.resolution is CheckpointResolution.APPROVED
            log.info(
                "crew.checkpoint.resume_reused",
                workflow_id=wf_id,
                checkpoint_id=existing.id,
                approved=approved,
            )
            return {"drafter_approved": approved}
        if existing is None:
            cp = request_checkpoint(
                conn,
                workflow_id=wf_id,
                action=CHECKPOINT_ACTION_APPROVE_DRAFTER,
                summary=_summary_for_human(scored_company),
                proposed_payload=_proposed_payload(scored_company),
                now=clock(),
            )
        else:
            # An existing pending checkpoint — unusual (would mean a prior
            # interrupted run never got a resolution) but reuse it rather
            # than reject. Defensive: should not arise from the standard
            # initial-run path because the checkpoint is created here.
            cp = existing
        resume_value = interrupt({"checkpoint_id": cp.id})
        # `Command(resume={"approved": bool})` is what the orchestrator's
        # resume entry point passes. If a caller resumes with anything else,
        # treat truthiness; the audit row is the source of truth either way.
        if isinstance(resume_value, dict):
            approved = bool(resume_value.get("approved"))
        else:
            approved = bool(resume_value)
        return {"drafter_approved": approved}

    def _terminate_no_draft(state: CrewState) -> dict[str, Any]:
        # Decide which empty-outcome reason applies based on what's set in
        # state. Reading scored_company (set by the scorer) AND drafter_approved
        # (set by the checkpoint node) lets us distinguish gated vs rejected
        # without a separate state key.
        sc = state.get("scored_company")
        if sc is None:
            # Defensive: only reachable if a router routed here without a
            # scorer output. Keep the audit honest by naming the source.
            reason = TERMINATE_REASON_SCORE_GATED
        elif state.get("drafter_approved") is False:
            reason = TERMINATE_REASON_REJECTED
        elif sc.gated_by == "coverage":
            reason = TERMINATE_REASON_COVERAGE_GATED
        else:
            reason = TERMINATE_REASON_SCORE_GATED
        return {"terminate_reason": reason}

    def _route_after_scorer(state: CrewState) -> str:
        sc = state.get("scored_company")
        if sc is None or not sc.routes_to_draft:
            return "terminate_no_draft"
        return "request_drafter_approval"

    def _route_after_approval(state: CrewState) -> str:
        if state.get("drafter_approved"):
            return "drafter"
        return "terminate_no_draft"

    def _route_after_critic(state: CrewState) -> str:
        critique = state.get("critique")
        if critique is None or critique.passed:
            return "end"
        # ADR-006 §1: ONE bounded retry. The critic adapter bumps
        # fabrication_retries on every failure, so retries==1 means "first
        # failure, retry permitted"; retries>=2 means "second failure,
        # halt loudly". A passed critique skips this branch entirely.
        if state.get("fabrication_retries", 0) >= 2:
            return "terminate_with_critic_failure"
        return "drafter"

    builder: StateGraph[CrewState, None, CrewState, CrewState] = StateGraph(CrewState)
    builder.add_node("researcher", _researcher_adapter)
    builder.add_node("extractor", _extractor_adapter)
    builder.add_node("scorer", _scorer_adapter)
    builder.add_node("request_drafter_approval", _request_drafter_approval)
    builder.add_node("drafter", _drafter_adapter)
    builder.add_node("critic", _critic_adapter)
    builder.add_node("terminate_no_draft", _terminate_no_draft)
    builder.add_node("terminate_with_critic_failure", _terminate_with_critic_failure)

    builder.add_edge(START, "researcher")
    builder.add_edge("researcher", "extractor")
    builder.add_edge("extractor", "scorer")
    builder.add_conditional_edges(
        "scorer",
        _route_after_scorer,
        {
            "request_drafter_approval": "request_drafter_approval",
            "terminate_no_draft": "terminate_no_draft",
        },
    )
    builder.add_conditional_edges(
        "request_drafter_approval",
        _route_after_approval,
        {"drafter": "drafter", "terminate_no_draft": "terminate_no_draft"},
    )
    builder.add_edge("drafter", "critic")
    builder.add_conditional_edges(
        "critic",
        _route_after_critic,
        {
            "end": END,
            "drafter": "drafter",
            "terminate_with_critic_failure": "terminate_with_critic_failure",
        },
    )
    builder.add_edge("terminate_no_draft", END)
    builder.add_edge("terminate_with_critic_failure", END)

    return builder.compile(checkpointer=saver)


# ---------------------------------------------------------------------------
# Resume idempotency — find the latest approve_drafter checkpoint event row.
# ---------------------------------------------------------------------------


def _find_latest_drafter_approval(
    conn: sqlite3.Connection,
    workflow_id: str,
) -> Checkpoint | None:
    """Most recent `checkpoint.requested` event with action=approve_drafter,
    rehydrated via `get_checkpoint` so its resolution (if any) is attached.

    Returns None if no approve_drafter checkpoint has been requested for
    this workflow yet — the first-execution path of
    `_request_drafter_approval`.

    Uses raw SQL against the events table (the same shape CheckpointSystem
    uses internally) rather than extending the CheckpointSystem public
    surface — the kickoff scopes the checkpoint module as unchanged in
    this slice.
    """
    row = conn.execute(
        """
        SELECT id FROM events
         WHERE workflow_id = ?
           AND json_extract(structured_data, '$.event_type') = ?
           AND json_extract(structured_data, '$.action') = ?
         ORDER BY id DESC
         LIMIT 1
        """,
        (
            workflow_id,
            EventType.CHECKPOINT_REQUESTED.value,
            CHECKPOINT_ACTION_APPROVE_DRAFTER,
        ),
    ).fetchone()
    if row is None:
        return None
    return get_checkpoint(conn, int(row["id"]))


# ---------------------------------------------------------------------------
# Human-readable summary + proposed_payload helpers for request_checkpoint.
# ---------------------------------------------------------------------------


def _summary_for_human(scored_company: ScoredCompany) -> str:
    """One-paragraph summary an operator sees from `checkpoint show`.

    Keeps the score + coverage spread visible per PATTERNS.md #4 — the
    same disclosure that lands in the Drafter CLI footer — so the
    operator's approve/reject decision is informed by the same metadata
    that would appear in the eventual draft surface.
    """
    sc = scored_company
    score_str = f"{sc.score:.2f}/100" if sc.score is not None else "(no scorable signal)"
    coverage_pct = sc.coverage * 100.0
    tier_str = sc.tier if sc.tier is not None else "(no tier)"
    return (
        f"Approve drafting outreach for {sc.company_name!r}? "
        f"Score {score_str} (floor {sc.floor:.1f}, tier {tier_str}); "
        f"rubric coverage {coverage_pct:.0f}% (min_coverage "
        f"{sc.min_coverage:.0%}). Per-dimension breakdown is in the "
        f"proposed_payload."
    )


def _proposed_payload(scored_company: ScoredCompany) -> dict[str, Any]:
    """Structured-data attached to the checkpoint row. Operator sees this
    via `checkpoint show`; carries the per-dimension audit for the
    approval decision. Pydantic models are serialised via `model_dump`
    so the payload is plain dicts (json-serialisable into the events
    `structured_data` TEXT column)."""
    return {"scored_company": scored_company.model_dump(mode="json")}


# ---------------------------------------------------------------------------
# Public entry points — initial run + resume.
# ---------------------------------------------------------------------------


def run_crew(
    conn: sqlite3.Connection,
    *,
    company_name: str,
    rubric: RubricConfig,
    workflow_id: str | None = None,
    saver: SqliteSaver | None = None,
    saver_conn: sqlite3.Connection | None = None,
    log_root: Path | None = None,
    max_searches: int = DEFAULT_MAX_SEARCHES,
    now: Callable[[], datetime] | None = None,
) -> CrewResult:
    """Execute one full crew run end-to-end through the habitat.

    Inserts a `workflow_type='lead_enrichment_crew'` workflow row, opens
    (or accepts an injected) SqliteSaver against the same .db file,
    builds the graph, and invokes it. Three normal-exit shapes:

      - The graph reaches END at `terminate_no_draft` → `CrewResult.status =
        COMPLETED`, `draft is None`, `terminate_reason` names which gate
        fired. The workflow row's status is COMPLETED, not FAILED — gating
        is a valid empty-outcome per ADR-006 §1.
      - The graph reaches END at `drafter` → `CrewResult.status =
        COMPLETED`, `draft is not None`.
      - The graph pauses at `request_drafter_approval` (interrupt) →
        `CrewResult.status = PAUSED`, `pending_checkpoint_id` set. The
        workflow row's status is PAUSED (CheckpointSystem already moved
        it). The operator approves or rejects via the existing CLI; the
        caller resumes via `resume_crew(conn, workflow_id=...)`.

    Infrastructure error (LLM raise, parse error, etc.) propagates out of
    a node, is caught here, finalises the workflow as FAILED. No retry
    (ADR-006 §1, Slice 1).
    """
    clock = now or _utcnow
    wf_id = workflow_id or new_workflow_id()

    workflow = Workflow(
        id=wf_id,
        workflow_type=WORKFLOW_TYPE,
        status=WorkflowStatus.RUNNING,
        started_at=clock().isoformat(),
        metadata={"company_name": company_name},
    )
    insert_workflow(conn, workflow)
    emit_event(
        conn,
        workflow_id=wf_id,
        event_type=EventType.WORKFLOW_STARTED,
        level=EventLevel.INFO,
        message=f"crew workflow started for {company_name!r}",
        structured_data={"company_name": company_name, "workflow_type": WORKFLOW_TYPE},
        timestamp=clock(),
    )
    log.info("crew.run.start", workflow_id=wf_id, company_name=company_name)

    # SqliteSaver wiring. Caller may inject for tests; otherwise we open
    # a separate connection to the same DB file with check_same_thread=False
    # (defensive — LangGraph internals may execute on a thread pool in
    # some paths). ADR-002's "two writers, same file" architecture.
    owns_saver = False
    if saver is None:
        if saver_conn is None:
            db_path = _resolve_db_path(conn)
            saver_conn = sqlite3.connect(db_path, check_same_thread=False)
            # WAL on the SqliteSaver connection too — same property as the
            # audit-DB connection (see state/schema.py::connect): concurrent
            # readers + one writer, no "database is locked" errors when the
            # two writers (audit + checkpoint) hit the file in quick
            # succession. WAL is database-wide so this is a no-op if the
            # audit-DB connection already set it.
            saver_conn.execute("PRAGMA journal_mode = WAL")
        saver = SqliteSaver(saver_conn)
        saver.setup()
        owns_saver = True

    try:
        graph = build_crew_graph(
            conn,
            rubric,
            saver=saver,
            log_root=log_root,
            max_searches=max_searches,
            now=clock,
        )
        cfg: dict[str, Any] = {"configurable": {"thread_id": wf_id}}
        initial_state: CrewState = {
            "workflow_id": wf_id,
            "company_name": company_name,
        }
        return _invoke_and_finalise(
            conn=conn,
            graph=graph,
            cfg=cfg,
            invoke_input=initial_state,
            workflow=workflow,
            company_name=company_name,
            clock=clock,
        )
    finally:
        if owns_saver and saver_conn is not None:
            saver_conn.close()


def resume_crew(
    conn: sqlite3.Connection,
    *,
    workflow_id: str,
    rubric: RubricConfig,
    saver: SqliteSaver | None = None,
    saver_conn: sqlite3.Connection | None = None,
    log_root: Path | None = None,
    max_searches: int = DEFAULT_MAX_SEARCHES,
    now: Callable[[], datetime] | None = None,
) -> CrewResult:
    """Resume a paused crew workflow.

    Reads the latest approve_drafter checkpoint for this workflow; the
    checkpoint MUST be resolved (approved or rejected) before resume is
    legal. Construction parallels `run_crew` exactly so the same graph
    factory runs against the same SqliteSaver-backed thread state.

    Approval path: invokes `Command(resume={"approved": True})`; graph
    routes through the drafter to END; workflow finalised as COMPLETED.
    Rejection path: the workflow is already CANCELLED by
    `reject_checkpoint`. We do NOT invoke the graph — the audit row is
    the terminal fact. `CrewResult.status = CANCELLED`,
    `terminate_reason = "rejected"`.
    """
    clock = now or _utcnow
    workflow = load_workflow(conn, workflow_id)
    if workflow is None:
        raise ValueError(f"unknown workflow: {workflow_id}")

    cp = _find_latest_drafter_approval(conn, workflow_id)
    if cp is None:
        raise ValueError(f"workflow {workflow_id} has no approve_drafter checkpoint to resume from")
    if cp.is_pending:
        raise ValueError(
            f"workflow {workflow_id} has a pending approve_drafter checkpoint (id={cp.id}); "
            "approve or reject it before resuming"
        )

    if cp.resolution is CheckpointResolution.REJECTED:
        # The audit row already moved the workflow to CANCELLED on rejection.
        # Surface that as the resume outcome; the graph is not re-invoked
        # (the orchestrator does NOT override a terminal CheckpointSystem
        # state with a COMPLETED finalize).
        log.info(
            "crew.resume.rejected",
            workflow_id=workflow_id,
            checkpoint_id=cp.id,
        )
        cost_total = recompute_cost_total(conn, workflow_id)
        return CrewResult(
            workflow_id=workflow_id,
            status=WorkflowStatus.CANCELLED,
            company_name=str(workflow.metadata.get("company_name", "")),
            terminate_reason=TERMINATE_REASON_REJECTED,
            cost_usd=cost_total,
        )

    # Approval path. Build the graph, invoke Command(resume=...).
    owns_saver = False
    if saver is None:
        if saver_conn is None:
            db_path = _resolve_db_path(conn)
            saver_conn = sqlite3.connect(db_path, check_same_thread=False)
            # WAL on the SqliteSaver connection (see run_crew for rationale).
            saver_conn.execute("PRAGMA journal_mode = WAL")
        saver = SqliteSaver(saver_conn)
        saver.setup()
        owns_saver = True

    try:
        graph = build_crew_graph(
            conn,
            rubric,
            saver=saver,
            log_root=log_root,
            max_searches=max_searches,
            now=clock,
        )
        cfg: dict[str, Any] = {"configurable": {"thread_id": workflow_id}}
        # Restore the workflow's runtime status to RUNNING — approve_checkpoint
        # already did this, but be defensive in case a caller bypassed it.
        if workflow.status is not WorkflowStatus.RUNNING:
            update_workflow(
                conn,
                workflow.model_copy(update={"status": WorkflowStatus.RUNNING}),
            )
        return _invoke_and_finalise(
            conn=conn,
            graph=graph,
            cfg=cfg,
            invoke_input=Command(resume={"approved": True}),
            workflow=workflow,
            company_name=str(workflow.metadata.get("company_name", "")),
            clock=clock,
        )
    finally:
        if owns_saver and saver_conn is not None:
            saver_conn.close()


def _invoke_and_finalise(
    *,
    conn: sqlite3.Connection,
    graph: CompiledStateGraph[CrewState, None, CrewState, CrewState],
    cfg: dict[str, Any],
    invoke_input: Any,
    workflow: Workflow,
    company_name: str,
    clock: Callable[[], datetime],
) -> CrewResult:
    """Run `graph.invoke(...)`, then translate the outcome into a CrewResult.

    Shared between `run_crew` and `resume_crew`. Owns the workflow
    lifecycle finalisation (COMPLETED / PAUSED / FAILED + cost rollup +
    terminal event emission) — Slice 6 STOP #2 Option A.
    """
    wf_id = workflow.id
    raw_signals: RawSignals | None = None
    profile: CompanyProfile | None = None
    scored_company: ScoredCompany | None = None
    draft: Draft | None = None
    terminate_reason: str | None = None

    try:
        # `graph.invoke` accepts CrewState | Command[Any] | None; both shapes
        # land here (initial state dict from run_crew, Command from resume_crew).
        # Cast keeps mypy strict happy without narrowing in the caller.
        result_state = graph.invoke(cast(Any, invoke_input), cast(Any, cfg))
    except Exception as exc:
        finished = clock().isoformat()
        update_workflow(
            conn,
            workflow.model_copy(update={"status": WorkflowStatus.FAILED, "finished_at": finished}),
        )
        error_msg = f"{type(exc).__name__}: {exc}"
        # `run_step` already finalised the offending step row FAILED + emitted
        # step.failed. Add the workflow-level workflow.failed event here.
        emit_event(
            conn,
            workflow_id=wf_id,
            event_type=EventType.WORKFLOW_FAILED,
            level=EventLevel.ERROR,
            message=f"crew workflow failed: {error_msg}",
            structured_data={"error": error_msg},
            timestamp=clock(),
        )
        cost_total = recompute_cost_total(conn, wf_id)
        log.warning("crew.run.failed", workflow_id=wf_id, error=error_msg)
        # Pull the step row's agent_name to surface a sensible error_step.
        # If multiple steps exist (e.g. extractor ran but scorer raised), the
        # last running-step is the offending one — but `run_step` already
        # FAILED it, so a simpler heuristic: read the workflow_steps for the
        # most recent FAILED step's agent_name.
        error_step = _latest_failed_step_agent(conn, wf_id)
        return CrewResult(
            workflow_id=wf_id,
            status=WorkflowStatus.FAILED,
            company_name=company_name,
            cost_usd=cost_total,
            error_step=error_step,
            error_message=error_msg,
        )

    # Successful invoke. Three terminal shapes:
    #   (a) `__interrupt__` present → paused at the checkpoint.
    #   (b) `draft` set in state    → drafter ran, graph reached END.
    #   (c) `terminate_reason` set  → early termination, graph reached END.
    critique: Critique | None = None
    fabrication_retries = 0
    if isinstance(result_state, dict):
        raw_signals = result_state.get("raw_signals")
        profile = result_state.get("profile")
        scored_company = result_state.get("scored_company")
        draft = result_state.get("draft")
        critique = result_state.get("critique")
        fabrication_retries = result_state.get("fabrication_retries", 0)
        terminate_reason = result_state.get("terminate_reason")
        interrupts = result_state.get("__interrupt__")
    else:  # pragma: no cover - LangGraph always returns dict for our shape
        interrupts = None

    if interrupts:
        # Paused. CheckpointSystem.request_checkpoint already moved the
        # workflow to PAUSED and wrote the audit row; we don't touch the
        # workflow status here. Surface the pending checkpoint id so the
        # caller can render the "approve" instructions.
        cp = _find_latest_drafter_approval(conn, wf_id)
        pending_id = cp.id if cp is not None and cp.is_pending else None
        cost_total = recompute_cost_total(conn, wf_id)
        log.info(
            "crew.run.paused",
            workflow_id=wf_id,
            checkpoint_id=pending_id,
        )
        return CrewResult(
            workflow_id=wf_id,
            status=WorkflowStatus.PAUSED,
            company_name=company_name,
            raw_signals=raw_signals,
            profile=profile,
            scored_company=scored_company,
            pending_checkpoint_id=pending_id,
            cost_usd=cost_total,
        )

    # Reached END. Two terminal shapes here:
    #   - critic_failure → ADR-006 §1 says persistent fabrication is
    #     halt-worthy; finalise FAILED.
    #   - everything else (score_gated, coverage_gated, rejected,
    #     draft-produced) → COMPLETED.
    cost_total = recompute_cost_total(conn, wf_id)
    finished = clock().isoformat()
    is_critic_failure = terminate_reason == TERMINATE_REASON_CRITIC_FAILURE
    terminal_status = WorkflowStatus.FAILED if is_critic_failure else WorkflowStatus.COMPLETED
    finalised = workflow.model_copy(
        update={
            "status": terminal_status,
            "finished_at": finished,
            "cost_total_usd": cost_total,
        }
    )
    update_workflow(conn, finalised)

    if is_critic_failure:
        # Persistent fabrication after the bounded retry. The critic's
        # `agent.fabrication_detected` event is already on the audit chain
        # via the critic adapter's run_step. Add the workflow-level
        # workflow.failed event so the workflow's terminal fact is loud.
        error_msg = (
            f"crew workflow halted on persistent fabrication after one bounded retry "
            f"(critique.failure_count="
            f"{critique.failure_count if critique is not None else 'unknown'})"
        )
        emit_event(
            conn,
            workflow_id=wf_id,
            event_type=EventType.WORKFLOW_FAILED,
            level=EventLevel.ERROR,
            message=error_msg,
            structured_data={
                "company_name": company_name,
                "terminate_reason": terminate_reason,
                "fabrication_retries": fabrication_retries,
                "failure_count": (critique.failure_count if critique is not None else None),
                "all_fabricated": (critique.all_fabricated if critique is not None else None),
            },
            timestamp=clock(),
        )
        log.warning(
            "crew.run.critic_failure",
            workflow_id=wf_id,
            company_name=company_name,
            fabrication_retries=fabrication_retries,
            failure_count=(critique.failure_count if critique is not None else None),
        )
        return CrewResult(
            workflow_id=wf_id,
            status=WorkflowStatus.FAILED,
            company_name=company_name,
            raw_signals=raw_signals,
            profile=profile,
            scored_company=scored_company,
            draft=draft,
            critique=critique,
            fabrication_retries=fabrication_retries,
            terminate_reason=terminate_reason,
            cost_usd=cost_total,
            error_step=CRITIC_AGENT_NAME,
            error_message=error_msg,
        )

    if terminate_reason is not None:
        # Empty-outcome COMPLETED. ADR-006 §1 names this case workflow.note.
        emit_event(
            conn,
            workflow_id=wf_id,
            event_type=EventType.WORKFLOW_NOTE,
            level=EventLevel.INFO,
            message=f"crew workflow terminated without draft: {terminate_reason}",
            structured_data={
                "company_name": company_name,
                "terminate_reason": terminate_reason,
                "score": scored_company.score if scored_company is not None else None,
                "coverage": scored_company.coverage if scored_company is not None else None,
                "gated_by": scored_company.gated_by if scored_company is not None else None,
            },
            timestamp=clock(),
        )

    emit_event(
        conn,
        workflow_id=wf_id,
        event_type=EventType.WORKFLOW_COMPLETED,
        level=EventLevel.INFO,
        message=f"crew workflow completed for {company_name!r}",
        structured_data={
            "company_name": company_name,
            "cost_total_usd": cost_total,
            "produced_draft": draft is not None,
            "terminate_reason": terminate_reason,
            "critique_passed": critique.passed if critique is not None else None,
            "fabrication_retries": fabrication_retries,
        },
        timestamp=clock(),
    )
    log.info(
        "crew.run.complete",
        workflow_id=wf_id,
        company_name=company_name,
        cost_usd=cost_total,
        produced_draft=draft is not None,
        terminate_reason=terminate_reason,
        critique_passed=critique.passed if critique is not None else None,
        fabrication_retries=fabrication_retries,
    )

    return CrewResult(
        workflow_id=wf_id,
        status=WorkflowStatus.COMPLETED,
        company_name=company_name,
        raw_signals=raw_signals,
        profile=profile,
        scored_company=scored_company,
        draft=draft,
        critique=critique,
        fabrication_retries=fabrication_retries,
        terminate_reason=terminate_reason,
        cost_usd=cost_total,
    )


# ---------------------------------------------------------------------------
# Helpers — DB path resolution + latest-failed-step lookup.
# ---------------------------------------------------------------------------


def _resolve_db_path(conn: sqlite3.Connection) -> str:
    """Recover the on-disk path for an open sqlite3.Connection.

    Used so the SqliteSaver can open its own connection to the SAME .db
    file without the caller having to hand the path in explicitly. For
    in-memory databases (`":memory:"`) returns `":memory:"` — but in-memory
    requires injecting `saver_conn` instead because SQLite's `:memory:`
    is per-connection.
    """
    row = conn.execute("PRAGMA database_list").fetchone()
    # PRAGMA database_list rows: (seq, name, file). main DB is seq=0.
    if row is None:
        return ":memory:"
    file_path = row["file"]
    return file_path if file_path else ":memory:"


def _latest_failed_step_agent(conn: sqlite3.Connection, workflow_id: str) -> str | None:
    """Agent name of the most-recently-failed step for this workflow.

    Surfaces the offending agent on a FAILED CrewResult without re-doing
    the work `run_step` already recorded.
    """
    row = conn.execute(
        """
        SELECT agent_name FROM workflow_steps
         WHERE workflow_id = ? AND status = 'failed'
         ORDER BY id DESC LIMIT 1
        """,
        (workflow_id,),
    ).fetchone()
    return str(row["agent_name"]) if row is not None else None


__all__ = [
    "CHECKPOINT_ACTION_APPROVE_DRAFTER",
    "STEP_INDEX_DRAFTER",
    "STEP_INDEX_EXTRACTOR",
    "STEP_INDEX_RESEARCHER",
    "STEP_INDEX_SCORER",
    "WORKFLOW_TYPE",
    "CrewResult",
    "build_crew_graph",
    "resume_crew",
    "run_crew",
]
