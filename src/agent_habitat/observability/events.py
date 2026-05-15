"""Unified event emission over ADR-002's `events` table.

Slice 4 establishes a single conventioned write path for the `events` table
so that future agent actions, orchestrator transitions, and HITL checkpoints
all land with a predictable shape. Slice 3's `budget.exceeded` event already
embodies the convention (its halt-signal query uses
`json_extract($.event_type)`); this module formalises it.

CONVENTIONS

  structured_data shape contract:
    Every row written via `emit_event()` carries an `event_type` string at
    the top level of `structured_data`. Additional keys are free-form per
    event type and should be documented alongside their `EventType` member
    below. The `event_type` key supports SQL-side `json_extract` filtering
    without app-side JSON parsing — Slice 3's `is_workflow_halted_by_budget`
    is the canonical example.

  Level semantics — what each `EventLevel` MEANS at the observability layer:
    INFO       — normal lifecycle progress. No operator attention needed.
    WARN       — recoverable anomaly. Operator should know; workflow continues.
    ERROR      — halt-worthy. Orchestrator must NOT schedule further work
                 against this workflow until reconciled. `budget.exceeded`
                 (Slice 3) is the prototype; future fabrication/safety halts
                 use the same shape.
    CHECKPOINT — human-in-the-loop pause point requested (Slice 5).
    APPROVAL   — human-in-the-loop decision recorded; payload carries the
                 decision (approve/reject) and the reviewer.

Event-type taxonomy:
    The `EventType` enum is the SOURCE-OF-TRUTH list. New event types should
    be added there, not invented at call sites. Slice 3's `budget.exceeded`
    is included for completeness — that writer pre-dates this layer and is
    NOT migrated in Slice 4 (see STATUS.md Open Questions).

This module DOES NOT change the `events` schema (ADR-002 stays intact). It
is the conventioned writer over `insert_event` from `state.persistence`.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from enum import Enum
from typing import Any

import structlog

from ..state.models import Event, EventLevel
from ..state.persistence import insert_event, load_events

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Event-type taxonomy
# ---------------------------------------------------------------------------


class EventType(str, Enum):
    """Canonical event types written into `structured_data.event_type`.

    Add new types here rather than at call sites — keeps the taxonomy
    grep-able and prevents typos diverging across modules.
    """

    # Workflow lifecycle (orchestrator, Phase 2)
    WORKFLOW_STARTED = "workflow.started"
    WORKFLOW_COMPLETED = "workflow.completed"
    WORKFLOW_FAILED = "workflow.failed"
    WORKFLOW_CANCELLED = "workflow.cancelled"
    WORKFLOW_NOTE = "workflow.note"

    # Step lifecycle (orchestrator, Phase 2)
    STEP_STARTED = "step.started"
    STEP_COMPLETED = "step.completed"
    STEP_FAILED = "step.failed"

    # Budget — Slice 3 already emits this; included for taxonomy completeness.
    BUDGET_EXCEEDED = "budget.exceeded"

    # Human-in-the-loop (Slice 5)
    CHECKPOINT_REQUESTED = "checkpoint.requested"
    CHECKPOINT_APPROVED = "checkpoint.approved"
    CHECKPOINT_REJECTED = "checkpoint.rejected"

    # Fabrication-resistance (Phase 2 — cross-agent validation)
    FABRICATION_DETECTED = "agent.fabrication_detected"


# Operator-facing reference; mirrored in the module docstring for grep-ability.
EVENT_LEVEL_GUIDE: dict[EventLevel, str] = {
    EventLevel.INFO: "normal lifecycle progress; no operator attention needed",
    EventLevel.WARN: "recoverable anomaly; operator should know; workflow continues",
    EventLevel.ERROR: "halt-worthy; orchestrator must not schedule further work until reconciled",
    EventLevel.CHECKPOINT: "human-in-the-loop pause point requested (Slice 5)",
    EventLevel.APPROVAL: "human-in-the-loop decision recorded (approve/reject + reviewer)",
}


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------


def emit_event(
    conn: sqlite3.Connection,
    *,
    workflow_id: str,
    event_type: EventType | str,
    level: EventLevel,
    message: str,
    structured_data: dict[str, Any] | None = None,
    step_id: int | None = None,
    timestamp: datetime | str | None = None,
) -> int:
    """Conventioned writer for the `events` table.

    Always writes `structured_data.event_type` as the first key, merged with
    any caller-supplied additional keys. Caller-supplied `event_type` inside
    `structured_data` is rejected — pass it via the `event_type` parameter
    instead, so the canonical name is the single source of truth.

    Returns the new event row id.
    """
    etype = event_type.value if isinstance(event_type, EventType) else event_type
    if structured_data is not None and "event_type" in structured_data:
        raise ValueError(
            "do not pass 'event_type' inside structured_data; use the event_type parameter"
        )

    payload: dict[str, Any] = {"event_type": etype}
    if structured_data:
        payload.update(structured_data)

    if timestamp is None:
        ts_iso = datetime.now(UTC).isoformat()
    elif isinstance(timestamp, datetime):
        ts_iso = (
            timestamp.astimezone(UTC).isoformat()
            if timestamp.tzinfo
            else timestamp.replace(tzinfo=UTC).isoformat()
        )
    else:
        ts_iso = timestamp

    event = Event(
        workflow_id=workflow_id,
        step_id=step_id,
        timestamp=ts_iso,
        level=level,
        message=message,
        structured_data=payload,
    )
    event_id = insert_event(conn, event)

    log.info(
        "observability.event.emitted",
        workflow_id=workflow_id,
        event_type=etype,
        level=level.value,
        event_id=event_id,
        step_id=step_id,
    )
    return event_id


# ---------------------------------------------------------------------------
# Read primitive
# ---------------------------------------------------------------------------


def events_of_type(
    conn: sqlite3.Connection,
    workflow_id: str,
    event_type: EventType | str,
) -> list[Event]:
    """All events for a workflow matching `structured_data.event_type`.

    Uses SQLite's built-in `json_extract` — same pattern Slice 3 uses in
    `is_workflow_halted_by_budget`. This is the generic primitive; specific
    halt/checkpoint queries can be expressed in terms of it. Ordered by
    timestamp then id (matches `load_events`).

    DESIGN BOUNDARY: this is the only structured query the layer ships.
    More complex filtering (date ranges, multi-type joins, aggregations)
    should be answered by SQL written at the call site or — if it can be
    served by `rg` over JSONL — by ripgrep. We are NOT building a query
    engine here (CLAUDE.md deferred list).
    """
    etype = event_type.value if isinstance(event_type, EventType) else event_type
    rows = conn.execute(
        """
        SELECT * FROM events
         WHERE workflow_id = ?
           AND json_extract(structured_data, '$.event_type') = ?
         ORDER BY timestamp ASC, id ASC
        """,
        (workflow_id, etype),
    ).fetchall()
    # Reuse load_events' row → Event coercion by going through it for the
    # 0-row case below; for the populated case, inline the same shape to
    # avoid pulling private helpers.
    if not rows:
        return []
    # Filter in-Python: cheaper than duplicating _row_to_event from the
    # state module, and the result set for a single workflow+type is small
    # by Phase 1-2 sizing (worst case: a few budget.exceeded rows).
    all_events = load_events(conn, workflow_id)
    return [e for e in all_events if (e.structured_data or {}).get("event_type") == etype]
