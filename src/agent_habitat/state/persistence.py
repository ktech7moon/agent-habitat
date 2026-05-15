"""SQLite persistence for ADR-002's audit tables.

API surface (Slice 2):

  init_db(db_path)                          → ensure schema
  insert_workflow(conn, workflow)           → INSERT into workflows
  update_workflow(conn, workflow)           → UPSERT-by-id semantics
  insert_step(conn, step)                   → INSERT, returns assigned id
  update_step(conn, step)                   → UPDATE by id
  insert_event(conn, event)                 → INSERT, returns assigned id

  load_workflow(conn, wf_id)                → Workflow | None
  load_steps(conn, wf_id)                   → list[WorkflowStep]
  load_events(conn, wf_id)                  → list[Event]
  load_full(conn, wf_id)                    → (Workflow, steps, events) | None

  workflows_by_status(conn, status)         → list[Workflow]
  recompute_cost_total(conn, wf_id)         → float (updates workflows.cost_total_usd)

  reconcile_orphan_steps(conn, now=None)    → list[int] (step ids reconciled)

Out of scope for Slice 2: budget caps (Slice 3), the full ObservabilityLayer
(Slice 4), LangGraph `SqliteSaver` wiring (Phase 2 orchestrator). This module
only owns agent-habitat's three tables and the operations that act on them.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from .models import (
    Event,
    EventLevel,
    StepStatus,
    Workflow,
    WorkflowStatus,
    WorkflowStep,
)
from .schema import DEFAULT_DB_PATH, connect, init_schema

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def init_db(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open the connection, create tables if needed, return the open connection.

    Caller owns the connection lifecycle and is responsible for closing it.
    """
    conn = connect(db_path)
    init_schema(conn)
    return conn


# ---------------------------------------------------------------------------
# JSON column helpers — `metadata` / `structured_data` are TEXT columns
# holding JSON. Centralise (de)serialization so callers always see dicts.
# ---------------------------------------------------------------------------


def _dump_json(value: dict[str, Any] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, sort_keys=True)


def _load_json_obj(raw: str | None) -> dict[str, Any]:
    if raw is None or raw == "":
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError(f"expected JSON object in column, got {type(parsed).__name__}")
    return parsed


def _load_json_opt(raw: str | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    return _load_json_obj(raw)


# ---------------------------------------------------------------------------
# workflows
# ---------------------------------------------------------------------------


def insert_workflow(conn: sqlite3.Connection, wf: Workflow) -> None:
    """Insert a new workflow row. Fails if `id` already exists."""
    with conn:
        conn.execute(
            """
            INSERT INTO workflows (
                id, workflow_type, status, started_at, finished_at,
                cost_total_usd, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                wf.id,
                wf.workflow_type,
                wf.status.value,
                wf.started_at,
                wf.finished_at,
                wf.cost_total_usd,
                _dump_json(wf.metadata) if wf.metadata else None,
            ),
        )


def update_workflow(conn: sqlite3.Connection, wf: Workflow) -> None:
    """Overwrite an existing workflow row by id. Raises if id does not exist."""
    with conn:
        cur = conn.execute(
            """
            UPDATE workflows
               SET workflow_type   = ?,
                   status          = ?,
                   started_at      = ?,
                   finished_at     = ?,
                   cost_total_usd  = ?,
                   metadata        = ?
             WHERE id = ?
            """,
            (
                wf.workflow_type,
                wf.status.value,
                wf.started_at,
                wf.finished_at,
                wf.cost_total_usd,
                _dump_json(wf.metadata) if wf.metadata else None,
                wf.id,
            ),
        )
        if cur.rowcount == 0:
            raise KeyError(f"workflow id not found: {wf.id}")


def load_workflow(conn: sqlite3.Connection, workflow_id: str) -> Workflow | None:
    row = conn.execute("SELECT * FROM workflows WHERE id = ?", (workflow_id,)).fetchone()
    return _row_to_workflow(row) if row is not None else None


def workflows_by_status(conn: sqlite3.Connection, status: WorkflowStatus) -> list[Workflow]:
    """All workflows in the given status, ordered by started_at ascending."""
    rows = conn.execute(
        "SELECT * FROM workflows WHERE status = ? ORDER BY started_at ASC",
        (status.value,),
    ).fetchall()
    return [_row_to_workflow(r) for r in rows]


def _row_to_workflow(row: sqlite3.Row) -> Workflow:
    return Workflow(
        id=row["id"],
        workflow_type=row["workflow_type"],
        status=WorkflowStatus(row["status"]),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        cost_total_usd=row["cost_total_usd"],
        metadata=_load_json_obj(row["metadata"]),
    )


# ---------------------------------------------------------------------------
# workflow_steps
# ---------------------------------------------------------------------------


def insert_step(conn: sqlite3.Connection, step: WorkflowStep) -> int:
    """Insert a new step row; return the SQLite-assigned id.

    The returned id is also written back onto the caller's `step.id` so the
    caller can subsequently `update_step(step)` without re-querying.
    """
    with conn:
        cur = conn.execute(
            """
            INSERT INTO workflow_steps (
                workflow_id, step_index, agent_name, status,
                started_at, finished_at, input_ref, output_ref,
                cost_usd, error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                step.workflow_id,
                step.step_index,
                step.agent_name,
                step.status.value,
                step.started_at,
                step.finished_at,
                step.input_ref,
                step.output_ref,
                step.cost_usd,
                step.error_message,
            ),
        )
        assigned = int(cur.lastrowid) if cur.lastrowid is not None else 0
    step.id = assigned
    return assigned


def update_step(conn: sqlite3.Connection, step: WorkflowStep) -> None:
    """Overwrite an existing step row by id. Raises if id is unset or not found."""
    if step.id is None:
        raise ValueError("update_step requires step.id; call insert_step first")
    with conn:
        cur = conn.execute(
            """
            UPDATE workflow_steps
               SET workflow_id   = ?,
                   step_index    = ?,
                   agent_name    = ?,
                   status        = ?,
                   started_at    = ?,
                   finished_at   = ?,
                   input_ref     = ?,
                   output_ref    = ?,
                   cost_usd      = ?,
                   error_message = ?
             WHERE id = ?
            """,
            (
                step.workflow_id,
                step.step_index,
                step.agent_name,
                step.status.value,
                step.started_at,
                step.finished_at,
                step.input_ref,
                step.output_ref,
                step.cost_usd,
                step.error_message,
                step.id,
            ),
        )
        if cur.rowcount == 0:
            raise KeyError(f"workflow_steps id not found: {step.id}")


def load_steps(conn: sqlite3.Connection, workflow_id: str) -> list[WorkflowStep]:
    """All steps for a workflow, ordered by step_index."""
    rows = conn.execute(
        """
        SELECT * FROM workflow_steps
         WHERE workflow_id = ?
         ORDER BY step_index ASC
        """,
        (workflow_id,),
    ).fetchall()
    return [_row_to_step(r) for r in rows]


def _row_to_step(row: sqlite3.Row) -> WorkflowStep:
    return WorkflowStep(
        id=row["id"],
        workflow_id=row["workflow_id"],
        step_index=row["step_index"],
        agent_name=row["agent_name"],
        status=StepStatus(row["status"]),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        input_ref=row["input_ref"],
        output_ref=row["output_ref"],
        cost_usd=row["cost_usd"],
        error_message=row["error_message"],
    )


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------


def insert_event(conn: sqlite3.Connection, event: Event) -> int:
    """Insert a new event row; return the SQLite-assigned id."""
    with conn:
        cur = conn.execute(
            """
            INSERT INTO events (
                workflow_id, step_id, timestamp, level, message, structured_data
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event.workflow_id,
                event.step_id,
                event.timestamp,
                event.level.value,
                event.message,
                _dump_json(event.structured_data),
            ),
        )
        assigned = int(cur.lastrowid) if cur.lastrowid is not None else 0
    event.id = assigned
    return assigned


def load_events(conn: sqlite3.Connection, workflow_id: str) -> list[Event]:
    """All events for a workflow, ordered by timestamp then id (tiebreak)."""
    rows = conn.execute(
        """
        SELECT * FROM events
         WHERE workflow_id = ?
         ORDER BY timestamp ASC, id ASC
        """,
        (workflow_id,),
    ).fetchall()
    return [_row_to_event(r) for r in rows]


def _row_to_event(row: sqlite3.Row) -> Event:
    return Event(
        id=row["id"],
        workflow_id=row["workflow_id"],
        step_id=row["step_id"],
        timestamp=row["timestamp"],
        level=EventLevel(row["level"]),
        message=row["message"],
        structured_data=_load_json_opt(row["structured_data"]),
    )


# ---------------------------------------------------------------------------
# Compound load + cost rollup
# ---------------------------------------------------------------------------


def load_full(
    conn: sqlite3.Connection, workflow_id: str
) -> tuple[Workflow, list[WorkflowStep], list[Event]] | None:
    """Load a workflow plus its steps and events. None if the workflow is missing."""
    wf = load_workflow(conn, workflow_id)
    if wf is None:
        return None
    return wf, load_steps(conn, workflow_id), load_events(conn, workflow_id)


def recompute_cost_total(conn: sqlite3.Connection, workflow_id: str) -> float:
    """Sum `workflow_steps.cost_usd` and write the result into
    `workflows.cost_total_usd`. Returns the new total.

    Per ADR-002, aggregation is Slice 2's job; budget caps and halt-on-exceed
    are Slice 3 and not implemented here. This function is the single
    aggregation primitive callers should use after writing or updating a
    step row.
    """
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM workflow_steps WHERE workflow_id = ?",
        (workflow_id,),
    ).fetchone()
    total = float(row["total"])
    with conn:
        cur = conn.execute(
            "UPDATE workflows SET cost_total_usd = ? WHERE id = ?",
            (total, workflow_id),
        )
        if cur.rowcount == 0:
            raise KeyError(f"workflow id not found: {workflow_id}")
    return total


# ---------------------------------------------------------------------------
# Orphaned-step reconciliation (ADR-002 "startup sweep")
# ---------------------------------------------------------------------------

# Contract resolved (ADR-002 underspecified what "reconcile" means without the
# orchestrator wired). ADR-002 says orphaned in-progress steps reconcile to
# "failed with a synthesized event, or resume." The "or resume" branch
# previously required infrastructure that Slice 2 lacked; Phase 2 Slice 6
# wires LangGraph's SqliteSaver into the same .db file, so we can finally
# distinguish "lost" from "resumable" — see `has_langgraph_checkpoint` and
# the Slice 6 STOP #3c reconciliation guard.

ORPHAN_ERROR_MESSAGE = "orphaned-on-startup: reconciled by reconcile_orphan_steps"
ORPHAN_EVENT_MESSAGE = "step reconciled from running → failed by startup sweep"


def has_langgraph_checkpoint(
    conn: sqlite3.Connection,
    workflow_id: str,
) -> bool:
    """True iff SqliteSaver has at least one checkpoint row for this thread_id.

    ADR-002 Option 1: SqliteSaver writes the `checkpoints` table to the
    SAME `.db` file as the audit tables, keyed on `thread_id` =
    `workflow_id`. `reconcile_orphan_steps` consults this to decide
    whether an orphan step belongs to a workflow that can be RESUMED
    from a live checkpoint (skip — orchestrator owns it) vs lost
    (mark FAILED — Slice 1 deterministic behaviour).

    The `checkpoints` table may not exist on a fresh DB (SqliteSaver
    creates it on first `setup()`). Treat `no such table` as "no live
    checkpoint" so this function is safe to call on pre-orchestrator
    databases — matching the Slice 2 reconciliation behaviour exactly
    when no orchestrator has ever run.
    """
    try:
        row = conn.execute(
            "SELECT 1 FROM checkpoints WHERE thread_id = ? LIMIT 1",
            (workflow_id,),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return False
        raise
    return row is not None


def reconcile_orphan_steps(
    conn: sqlite3.Connection,
    now: datetime | None = None,
) -> list[int]:
    """Mark every orphaned in-progress step as failed; emit one event per step.

    An orphaned step is one with `status='running' AND finished_at IS NULL`
    — i.e. a step that started but never completed, the signature of a
    crashed or hard-killed run. Per ADR-002's recovery design, these need
    to be reconciled before any new work proceeds.

    Slice 6 STOP #3c — reconciliation-vs-resume guard. Orphans whose
    workflow has a live LangGraph checkpoint (`has_langgraph_checkpoint`)
    are LEFT ALONE: the Phase 2 orchestrator can RESUME from that
    checkpoint instead of declaring the workflow lost. Orphans without a
    live checkpoint reconcile FAILED exactly as in Slice 2.

    Returns the list of step ids that were reconciled (empty if none).

    Idempotent: re-running after a successful sweep finds no orphans and is
    a no-op. Safe to call on a fresh DB.
    """
    ts = (now or datetime.now(UTC)).isoformat()

    rows = conn.execute(
        """
        SELECT id, workflow_id, step_index, agent_name
          FROM workflow_steps
         WHERE status = 'running' AND finished_at IS NULL
         ORDER BY id ASC
        """
    ).fetchall()

    if not rows:
        return []

    reconciled_ids: list[int] = []
    skipped_for_resume: list[int] = []
    with conn:
        for row in rows:
            step_id = int(row["id"])
            workflow_id = str(row["workflow_id"])
            if has_langgraph_checkpoint(conn, workflow_id):
                # Resume-capable: do not reconcile. The orchestrator
                # consumes the live LangGraph state on resume.
                skipped_for_resume.append(step_id)
                continue
            conn.execute(
                """
                UPDATE workflow_steps
                   SET status = 'failed',
                       finished_at = ?,
                       error_message = ?
                 WHERE id = ?
                """,
                (ts, ORPHAN_ERROR_MESSAGE, step_id),
            )
            conn.execute(
                """
                INSERT INTO events (
                    workflow_id, step_id, timestamp, level, message, structured_data
                ) VALUES (?, ?, ?, 'warn', ?, ?)
                """,
                (
                    workflow_id,
                    step_id,
                    ts,
                    ORPHAN_EVENT_MESSAGE,
                    _dump_json(
                        {
                            "step_id": step_id,
                            "step_index": row["step_index"],
                            "agent_name": row["agent_name"],
                            "reconciled_at": ts,
                        }
                    ),
                ),
            )
            reconciled_ids.append(step_id)

    log.info(
        "state.reconcile.orphans_swept",
        count=len(reconciled_ids),
        step_ids=reconciled_ids,
        skipped_for_resume=skipped_for_resume,
    )
    return reconciled_ids
