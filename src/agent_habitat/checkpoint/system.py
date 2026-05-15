"""Human-in-the-loop checkpoint request/approve/reject (Slice 5).

This module owns the audit + state-machine half of HITL approvals. Actually
pausing or resuming a *running* graph mid-execution is the orchestrator's
job (Phase 2); Slice 5 establishes the state fact (workflow `paused` /
`running` / `cancelled`) and the operator controls (approve / reject).

Public surface:

  request_checkpoint  — writes a CHECKPOINT-level `checkpoint.requested`
                        event, moves the workflow to PAUSED.
  approve_checkpoint  — writes an APPROVAL-level `checkpoint.approved`
                        event, moves the workflow back to RUNNING.
  reject_checkpoint   — writes an APPROVAL-level `checkpoint.rejected`
                        event, moves the workflow to CANCELLED (terminal).
  get_checkpoint      — load one checkpoint by id (pending or resolved).
  list_pending_checkpoints
                      — pending checkpoints, optionally filtered by workflow.
  is_workflow_paused_for_checkpoint
                      — halt-signal primitive the orchestrator will consult
                        before scheduling further work on a workflow
                        (mirrors `is_workflow_halted_by_budget`).

Persistence — additive on ADR-002's events table, no schema change:

  - Request:    one event row with `structured_data = {event_type:
                "checkpoint.requested", action, summary, proposed_payload?,
                requested_by?}`. The row's `events.id` IS the checkpoint id.
  - Resolution: one event row with `structured_data = {event_type:
                "checkpoint.{approved,rejected}", checkpoint_id, reviewer,
                note?}` referencing the request's id. A checkpoint is
                pending iff no resolution row references it.

Constraint: at most one pending checkpoint per workflow. A second request
on a still-paused workflow raises — multi-pending semantics would force
us to define which resolution event drives the workflow status transition,
and serialised resolution is the simpler, audit-clearer shape.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import Enum
from typing import Any

import structlog

from ..observability.events import EventType, emit_event
from ..state.models import EventLevel, WorkflowStatus
from ..state.persistence import load_workflow, update_workflow

log = structlog.get_logger(__name__)


CHECKPOINT_REQUESTED_MESSAGE_PREFIX = "checkpoint requested"
CHECKPOINT_APPROVED_MESSAGE_PREFIX = "checkpoint approved"
CHECKPOINT_REJECTED_MESSAGE_PREFIX = "checkpoint rejected"


class CheckpointResolution(str, Enum):
    """Terminal outcome of a checkpoint, recorded on the resolution event."""

    APPROVED = "approved"
    REJECTED = "rejected"


class CheckpointError(Exception):
    """Raised on unknown checkpoint ids or illegal state transitions.

    Distinct from generic ValueError so the CLI can surface a clean message
    without leaking traceback context to operators.
    """


@dataclass(frozen=True)
class Checkpoint:
    """One human-in-the-loop checkpoint record.

    `id` is the `events.id` of the `checkpoint.requested` row. Resolution
    fields are None while pending; once approved or rejected they carry
    the matching resolution event's payload.
    """

    id: int
    workflow_id: str
    step_id: int | None
    action: str
    summary: str
    requested_at: str
    requested_by: str | None
    proposed_payload: dict[str, Any] | None
    resolution: CheckpointResolution | None = None
    reviewer: str | None = None
    resolved_at: str | None = None
    note: str | None = None

    @property
    def is_pending(self) -> bool:
        return self.resolution is None


# ---------------------------------------------------------------------------
# Write side
# ---------------------------------------------------------------------------


def request_checkpoint(
    conn: sqlite3.Connection,
    *,
    workflow_id: str,
    action: str,
    summary: str,
    proposed_payload: dict[str, Any] | None = None,
    step_id: int | None = None,
    requested_by: str | None = None,
    now: datetime | None = None,
) -> Checkpoint:
    """Open a checkpoint and move the workflow to PAUSED.

    Raises `CheckpointError` if the workflow is unknown, already terminal
    (completed / failed / cancelled), or already has a pending checkpoint.
    """
    wf = load_workflow(conn, workflow_id)
    if wf is None:
        raise CheckpointError(f"unknown workflow: {workflow_id}")
    if wf.status in {
        WorkflowStatus.COMPLETED,
        WorkflowStatus.FAILED,
        WorkflowStatus.CANCELLED,
    }:
        raise CheckpointError(
            f"cannot request checkpoint on terminal workflow ({wf.status.value}): {workflow_id}"
        )
    if _find_pending_id(conn, workflow_id) is not None:
        raise CheckpointError(
            f"workflow {workflow_id} already has a pending checkpoint; "
            f"resolve it before requesting another"
        )

    ts = (now or datetime.now(UTC)).isoformat()
    payload: dict[str, Any] = {"action": action, "summary": summary}
    if proposed_payload is not None:
        payload["proposed_payload"] = proposed_payload
    if requested_by is not None:
        payload["requested_by"] = requested_by

    event_id = emit_event(
        conn,
        workflow_id=workflow_id,
        event_type=EventType.CHECKPOINT_REQUESTED,
        level=EventLevel.CHECKPOINT,
        message=f"{CHECKPOINT_REQUESTED_MESSAGE_PREFIX}: {action}",
        structured_data=payload,
        step_id=step_id,
        timestamp=ts,
    )

    wf.status = WorkflowStatus.PAUSED
    update_workflow(conn, wf)

    log.info(
        "checkpoint.requested",
        workflow_id=workflow_id,
        checkpoint_id=event_id,
        action=action,
        requested_by=requested_by,
    )

    return Checkpoint(
        id=event_id,
        workflow_id=workflow_id,
        step_id=step_id,
        action=action,
        summary=summary,
        requested_at=ts,
        requested_by=requested_by,
        proposed_payload=proposed_payload,
    )


def approve_checkpoint(
    conn: sqlite3.Connection,
    checkpoint_id: int,
    *,
    reviewer: str,
    note: str | None = None,
    now: datetime | None = None,
) -> Checkpoint:
    """Approve a pending checkpoint; transition the workflow back to RUNNING."""
    return _resolve(
        conn,
        checkpoint_id,
        resolution=CheckpointResolution.APPROVED,
        reviewer=reviewer,
        note=note,
        now=now,
    )


def reject_checkpoint(
    conn: sqlite3.Connection,
    checkpoint_id: int,
    *,
    reviewer: str,
    reason: str | None = None,
    now: datetime | None = None,
) -> Checkpoint:
    """Reject a pending checkpoint; transition the workflow to CANCELLED."""
    return _resolve(
        conn,
        checkpoint_id,
        resolution=CheckpointResolution.REJECTED,
        reviewer=reviewer,
        note=reason,
        now=now,
    )


def _resolve(
    conn: sqlite3.Connection,
    checkpoint_id: int,
    *,
    resolution: CheckpointResolution,
    reviewer: str,
    note: str | None,
    now: datetime | None,
) -> Checkpoint:
    cp = get_checkpoint(conn, checkpoint_id)
    if cp is None:
        raise CheckpointError(f"unknown checkpoint id: {checkpoint_id}")
    if not cp.is_pending:
        existing = cp.resolution.value if cp.resolution else "unknown"
        raise CheckpointError(f"checkpoint {checkpoint_id} already resolved ({existing})")

    wf = load_workflow(conn, cp.workflow_id)
    if wf is None:
        # Foreign-key prevents this in practice; defensive.
        raise CheckpointError(f"workflow disappeared: {cp.workflow_id}")

    ts = (now or datetime.now(UTC)).isoformat()
    payload: dict[str, Any] = {"checkpoint_id": checkpoint_id, "reviewer": reviewer}
    if note is not None:
        payload["note"] = note

    if resolution is CheckpointResolution.APPROVED:
        event_type = EventType.CHECKPOINT_APPROVED
        message = f"{CHECKPOINT_APPROVED_MESSAGE_PREFIX}: {cp.action}"
        new_status = WorkflowStatus.RUNNING
    else:
        event_type = EventType.CHECKPOINT_REJECTED
        message = f"{CHECKPOINT_REJECTED_MESSAGE_PREFIX}: {cp.action}"
        new_status = WorkflowStatus.CANCELLED

    emit_event(
        conn,
        workflow_id=cp.workflow_id,
        event_type=event_type,
        level=EventLevel.APPROVAL,
        message=message,
        structured_data=payload,
        step_id=cp.step_id,
        timestamp=ts,
    )

    wf.status = new_status
    if new_status is WorkflowStatus.CANCELLED:
        wf.finished_at = ts
    update_workflow(conn, wf)

    log.info(
        "checkpoint.resolved",
        workflow_id=cp.workflow_id,
        checkpoint_id=checkpoint_id,
        resolution=resolution.value,
        reviewer=reviewer,
    )

    return replace(
        cp,
        resolution=resolution,
        reviewer=reviewer,
        resolved_at=ts,
        note=note,
    )


# ---------------------------------------------------------------------------
# Read side
# ---------------------------------------------------------------------------


def get_checkpoint(
    conn: sqlite3.Connection,
    checkpoint_id: int,
) -> Checkpoint | None:
    """Load one checkpoint by id; None if no `checkpoint.requested` row matches."""
    row = conn.execute(
        """
        SELECT id, workflow_id, step_id, timestamp, structured_data
          FROM events
         WHERE id = ?
           AND json_extract(structured_data, '$.event_type') = ?
        """,
        (checkpoint_id, EventType.CHECKPOINT_REQUESTED.value),
    ).fetchone()
    if row is None:
        return None
    cp = _row_to_checkpoint(row)
    return _attach_resolution(conn, cp)


def list_pending_checkpoints(
    conn: sqlite3.Connection,
    workflow_id: str | None = None,
) -> list[Checkpoint]:
    """Pending checkpoints, oldest first. Optionally filtered by workflow."""
    base_sql = """
        SELECT e1.id, e1.workflow_id, e1.step_id, e1.timestamp, e1.structured_data
          FROM events AS e1
         WHERE json_extract(e1.structured_data, '$.event_type') = ?
           AND NOT EXISTS (
             SELECT 1 FROM events AS e2
              WHERE json_extract(e2.structured_data, '$.event_type') IN (?, ?)
                AND json_extract(e2.structured_data, '$.checkpoint_id') = e1.id
           )
    """
    if workflow_id is None:
        sql = base_sql + " ORDER BY e1.timestamp ASC, e1.id ASC"
        params: tuple[Any, ...] = (
            EventType.CHECKPOINT_REQUESTED.value,
            EventType.CHECKPOINT_APPROVED.value,
            EventType.CHECKPOINT_REJECTED.value,
        )
    else:
        sql = base_sql + " AND e1.workflow_id = ? ORDER BY e1.timestamp ASC, e1.id ASC"
        params = (
            EventType.CHECKPOINT_REQUESTED.value,
            EventType.CHECKPOINT_APPROVED.value,
            EventType.CHECKPOINT_REJECTED.value,
            workflow_id,
        )
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_checkpoint(r) for r in rows]


def is_workflow_paused_for_checkpoint(
    conn: sqlite3.Connection,
    workflow_id: str,
) -> bool:
    """True iff the workflow has an unresolved checkpoint.

    Reads only the events table (the same pattern Slice 3 uses in
    `is_workflow_halted_by_budget`) — independent of `workflows.status`,
    so the halt signal stays correct even if a status update lags.
    """
    return _find_pending_id(conn, workflow_id) is not None


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _find_pending_id(conn: sqlite3.Connection, workflow_id: str) -> int | None:
    row = conn.execute(
        """
        SELECT e1.id
          FROM events AS e1
         WHERE e1.workflow_id = ?
           AND json_extract(e1.structured_data, '$.event_type') = ?
           AND NOT EXISTS (
             SELECT 1 FROM events AS e2
              WHERE json_extract(e2.structured_data, '$.event_type') IN (?, ?)
                AND json_extract(e2.structured_data, '$.checkpoint_id') = e1.id
           )
         ORDER BY e1.timestamp ASC, e1.id ASC
         LIMIT 1
        """,
        (
            workflow_id,
            EventType.CHECKPOINT_REQUESTED.value,
            EventType.CHECKPOINT_APPROVED.value,
            EventType.CHECKPOINT_REJECTED.value,
        ),
    ).fetchone()
    return int(row["id"]) if row is not None else None


def _row_to_checkpoint(row: sqlite3.Row) -> Checkpoint:
    raw = row["structured_data"]
    payload: dict[str, Any] = json.loads(raw) if raw else {}
    proposed = payload.get("proposed_payload")
    if proposed is not None and not isinstance(proposed, dict):
        # Defensive: structured_data is conventioned as object-only by emit_event.
        proposed = None
    return Checkpoint(
        id=int(row["id"]),
        workflow_id=row["workflow_id"],
        step_id=row["step_id"],
        action=str(payload.get("action", "")),
        summary=str(payload.get("summary", "")),
        requested_at=row["timestamp"],
        requested_by=payload.get("requested_by"),
        proposed_payload=proposed,
    )


def _attach_resolution(conn: sqlite3.Connection, cp: Checkpoint) -> Checkpoint:
    row = conn.execute(
        """
        SELECT timestamp,
               structured_data,
               json_extract(structured_data, '$.event_type') AS etype
          FROM events
         WHERE json_extract(structured_data, '$.event_type') IN (?, ?)
           AND json_extract(structured_data, '$.checkpoint_id') = ?
         ORDER BY timestamp ASC, id ASC
         LIMIT 1
        """,
        (
            EventType.CHECKPOINT_APPROVED.value,
            EventType.CHECKPOINT_REJECTED.value,
            cp.id,
        ),
    ).fetchone()
    if row is None:
        return cp
    res_payload: dict[str, Any] = json.loads(row["structured_data"] or "{}")
    resolution = (
        CheckpointResolution.APPROVED
        if row["etype"] == EventType.CHECKPOINT_APPROVED.value
        else CheckpointResolution.REJECTED
    )
    return replace(
        cp,
        resolution=resolution,
        reviewer=res_payload.get("reviewer"),
        resolved_at=row["timestamp"],
        note=res_payload.get("note"),
    )
