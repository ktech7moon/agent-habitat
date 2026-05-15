"""Pydantic v2 models for ADR-002's audit schema.

Three tables, three models — `Workflow`, `WorkflowStep`, `Event` — plus the
string enums for their constrained columns. The models mirror the DDL exactly:
TEXT columns are `str`, REAL columns are `float`, AUTOINCREMENT INTEGER
primary keys are `int | None` (None until SQLite assigns one on insert), and
the `metadata` / `structured_data` JSON columns are typed as `dict[str, Any]`
deserialized on load.

These models are the boundary contract between Python and SQLite. They are
not LangGraph's `State` — that's a separate runtime object owned by the
orchestrator. ADR-002 keeps the two schemas independent on purpose.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Status / level enums — exact match to the SQLite CHECK constraints in
# ADR-002's DDL. Keep these in sync with `schema.py`.
# ---------------------------------------------------------------------------


class WorkflowStatus(str, Enum):
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class EventLevel(str, Enum):
    INFO = "info"
    WARN = "warn"
    ERROR = "error"
    CHECKPOINT = "checkpoint"
    APPROVAL = "approval"


# ---------------------------------------------------------------------------
# ID generation
#
# FLAG (ADR-002 underspecified): ADR-002 fixes `workflows.id` as TEXT
# PRIMARY KEY and the shared identifier passed to LangGraph as `thread_id`,
# but does not name a generation algorithm. We default to uuid4 hex (32 chars,
# globally unique, URL-safe, no time leakage). Callers may supply their own
# id when creating a `Workflow`. If Phase 2 needs sortable / time-prefixed
# ids (e.g. for cheap range scans), revisit with an addendum to ADR-002.
# ---------------------------------------------------------------------------


def new_workflow_id() -> str:
    """Default workflow id factory. Uuid4 hex — opaque, unique, sortable
    only by insertion order (which we track separately via started_at).
    """
    return uuid.uuid4().hex


def _utcnow_iso() -> str:
    """ISO-8601 UTC timestamp, the wire format for every TEXT timestamp column."""
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Domain models — one per audit table.
# ---------------------------------------------------------------------------


class Workflow(BaseModel):
    """One row of the `workflows` table.

    Lifecycle: created with `status=RUNNING` and `started_at=now`. As steps
    complete, `cost_total_usd` is rolled up from `workflow_steps.cost_usd`
    (see `persistence.recompute_cost_total`). `finished_at` is set when the
    workflow terminates (completed / failed / cancelled).
    """

    model_config = ConfigDict(use_enum_values=False, validate_assignment=True)

    id: str = Field(default_factory=new_workflow_id)
    workflow_type: str
    status: WorkflowStatus = WorkflowStatus.RUNNING
    started_at: str = Field(default_factory=_utcnow_iso)
    finished_at: str | None = None
    cost_total_usd: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkflowStep(BaseModel):
    """One row of the `workflow_steps` table.

    `id` is None until SQLite assigns one on insert. `step_index` is a
    monotonic counter within the workflow (`UNIQUE (workflow_id, step_index)`
    enforced at the DB level). `output_ref` / `input_ref` follow ADR-002's
    `data/logs/YYYY-MM-DD/<workflow_id>.jsonl:<line>` format — those refs
    are produced by `llm.py`; Slice 2 only stores them.
    """

    model_config = ConfigDict(use_enum_values=False, validate_assignment=True)

    id: int | None = None
    workflow_id: str
    step_index: int
    agent_name: str
    status: StepStatus = StepStatus.RUNNING
    started_at: str = Field(default_factory=_utcnow_iso)
    finished_at: str | None = None
    input_ref: str | None = None
    output_ref: str | None = None
    cost_usd: float = 0.0
    error_message: str | None = None


class Event(BaseModel):
    """One row of the `events` table.

    Events are the append-only audit narrative around steps — INFO for normal
    progress, WARN/ERROR for issues, CHECKPOINT/APPROVAL for human-in-the-loop
    pauses (Slice 5). `step_id` is nullable because some events (workflow
    start/end, reconciliation sweeps) aren't tied to a specific step.
    """

    model_config = ConfigDict(use_enum_values=False, validate_assignment=True)

    id: int | None = None
    workflow_id: str
    step_id: int | None = None
    timestamp: str = Field(default_factory=_utcnow_iso)
    level: EventLevel
    message: str
    structured_data: dict[str, Any] | None = None
