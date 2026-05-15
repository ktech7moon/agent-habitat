"""agent-habitat persistence layer (ADR-002 Option 1).

Public API for Slice 2: typed CRUD over `workflows` / `workflow_steps` /
`events` plus the startup-sweep reconciliation function. LangGraph's own
checkpoint tables (created by `SqliteSaver`) share the same .db file but
are not managed here — that wiring lands with the Phase 2 orchestrator.
"""

from .models import (
    Event,
    EventLevel,
    StepStatus,
    Workflow,
    WorkflowStatus,
    WorkflowStep,
    new_workflow_id,
)
from .persistence import (
    init_db,
    insert_event,
    insert_step,
    insert_workflow,
    load_events,
    load_full,
    load_steps,
    load_workflow,
    reconcile_orphan_steps,
    recompute_cost_total,
    update_step,
    update_workflow,
    workflows_by_status,
)
from .schema import DEFAULT_DB_PATH, connect, init_schema

__all__ = [
    "DEFAULT_DB_PATH",
    "Event",
    "EventLevel",
    "StepStatus",
    "Workflow",
    "WorkflowStatus",
    "WorkflowStep",
    "connect",
    "init_db",
    "init_schema",
    "insert_event",
    "insert_step",
    "insert_workflow",
    "load_events",
    "load_full",
    "load_steps",
    "load_workflow",
    "new_workflow_id",
    "reconcile_orphan_steps",
    "recompute_cost_total",
    "update_step",
    "update_workflow",
    "workflows_by_status",
]
