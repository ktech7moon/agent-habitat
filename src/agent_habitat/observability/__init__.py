"""ObservabilityLayer (Slice 4).

agent-habitat has three observability surfaces — this module unifies them
without consolidating their writers:

  1. The `events` table  — persistent, queryable audit trail. ADR-002's
     append-only narrative around workflow lifecycle, step transitions,
     budget exceedance, HITL checkpoints, and fabrication flags. Writers
     should go through `emit_event()` so every row carries the
     conventioned `structured_data.event_type` shape and a documented level.
  2. JSONL telemetry      — one line per LLM call at
     `data/logs/YYYY-MM-DD/<workflow_id>.jsonl`. `llm.py` owns the WRITER
     (`_append_telemetry`, untouched in Slice 4); this module owns the
     thin READER (`iter_telemetry`, `resolve_output_ref`). Stays
     ripgrep-friendly by design — the trigger to move OFF JSONL is
     "queries harder than ripgrep can handle" (CLAUDE.md deferred list).
  3. structlog operational logs — process-stdout structured logs for
     human/dev observation. `configure_logging()` is the single central
     config; `bind_workflow_context()` binds workflow_id/agent_name into
     contextvars so every `structlog.get_logger(__name__).info(...)` call
     downstream is automatically tagged.

These three surfaces answer different questions and live at different
durabilities. Slice 4 does not fuse them — it makes their relationship
explicit and their interfaces consistent.

Public API:

  Events (events.py):
    emit_event(conn, *, workflow_id, event_type, level, message, ...) → int
    events_of_type(conn, workflow_id, event_type) → list[Event]
    EventType                                — string-enum of canonical types
    EVENT_LEVEL_GUIDE                        — level-semantics docstring/constant

  Logging (logging.py):
    configure_logging(*, level=..., json_output=False, stream=None) → None
    bind_workflow_context(*, workflow_id, agent_name=None)          → None
    clear_log_context()                                              → None

  JSONL read (jsonl.py):
    iter_telemetry(workflow_id, log_root=..., day=None) → Iterator[dict]
    resolve_output_ref(ref, log_root_fallback=None)     → dict
    TelemetryReadError                                  — exception type
"""

from .events import (
    EVENT_LEVEL_GUIDE,
    EventType,
    emit_event,
    events_of_type,
)
from .jsonl import (
    TelemetryReadError,
    iter_telemetry,
    resolve_output_ref,
)
from .logging import (
    bind_workflow_context,
    clear_log_context,
    configure_logging,
)

__all__ = [
    "EVENT_LEVEL_GUIDE",
    "EventType",
    "TelemetryReadError",
    "bind_workflow_context",
    "clear_log_context",
    "configure_logging",
    "emit_event",
    "events_of_type",
    "iter_telemetry",
    "resolve_output_ref",
]
