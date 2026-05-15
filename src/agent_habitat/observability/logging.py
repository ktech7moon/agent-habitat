"""Central structlog configuration for the project.

Every module in agent-habitat does `log = structlog.get_logger(__name__)`
at import time. Until Slice 4 there was no central `structlog.configure()`
call — modules used structlog's default config, which is "lightly" useful
but inconsistent across environments (dev vs. CI vs. production).

This module is the single point of structlog configuration. Call
`configure_logging()` once at process startup (CLI entry, test fixture,
interactive REPL). It is idempotent — re-calling replaces the active
configuration cleanly.

Workflow context binding uses structlog's contextvars processor: a single
`bind_workflow_context(workflow_id=..., agent_name=...)` call at the start
of a workflow tags every subsequent log line inside the same async/thread
context. `clear_log_context()` resets — call it at workflow boundary.

This module does NOT touch the JSONL telemetry writer in `llm.py`. Those
are different surfaces: structlog is operational logs for humans; the
JSONL file is the durable per-call audit record.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, TextIO

import structlog


def configure_logging(
    *,
    level: int = logging.INFO,
    json_output: bool = False,
    stream: TextIO | None = None,
) -> None:
    """Configure structlog + the stdlib root logger for the project.

    Args:
      level: filtering level for structlog (and the stdlib root logger).
        Default INFO. Use DEBUG for verbose runs.
      json_output: if True, render each log line as a single JSON object
        (one record per line) — appropriate for production / log shipping.
        If False (default), render as a colourless console line — easier
        to skim during development.
      stream: where to write log lines. Defaults to stderr.

    Processors (shared across both renderers):
      - merge_contextvars        — pulls workflow_id/agent_name bindings.
      - add_log_level            — emits `level` field.
      - TimeStamper(iso, utc)    — emits `timestamp` field in ISO-8601 UTC.
      - StackInfoRenderer        — formats stack_info= kwarg.
      - format_exc_info          — formats exc_info= kwarg.

    Idempotent: re-calling replaces the active configuration. Safe in tests
    that need to assert specific output.
    """
    target: TextIO = stream if stream is not None else sys.stderr

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer: Any
    if json_output:
        renderer = structlog.processors.JSONRenderer(sort_keys=True)
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=False)

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=target),
        cache_logger_on_first_use=False,
    )

    # Stdlib loggers (used transitively by some deps) inherit the level so
    # that filtering at the structlog wrapper isn't undercut by stdlib
    # leaking DEBUG noise. The handler is intentionally a no-op formatter —
    # this project's first-party code routes through structlog.
    root = logging.getLogger()
    root.setLevel(level)


def bind_workflow_context(
    *,
    workflow_id: str,
    agent_name: str | None = None,
) -> None:
    """Bind workflow_id (and optionally agent_name) into structlog contextvars.

    After this call, every `structlog.get_logger(...).info(...)` invocation
    on the same async/thread context will include `workflow_id` (and
    `agent_name` when provided) as fields, without each call site needing
    to pass them explicitly. Pairs with `clear_log_context()` at workflow
    boundary.
    """
    structlog.contextvars.bind_contextvars(workflow_id=workflow_id)
    if agent_name is not None:
        structlog.contextvars.bind_contextvars(agent_name=agent_name)


def clear_log_context() -> None:
    """Clear all structlog contextvars bindings.

    Call at workflow boundary (start of a fresh workflow, end of a run, or
    on test teardown) to prevent state from leaking between workflows.
    """
    structlog.contextvars.clear_contextvars()
