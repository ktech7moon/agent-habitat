"""Step lifecycle context manager for agent-habitat.

`run_step()` wraps one agent step's audit lifecycle:
open a RUNNING step row, emit step.started, yield a StepRecorder for
accumulating cost/output_ref/structured_data, then close on exit.

Normal exit   → COMPLETED with recorder values + step.completed event.
Exception exit → FAILED with error_message + step.failed event + re-raise.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog

from ..observability.events import EventType, emit_event
from ..state.models import EventLevel, StepStatus, WorkflowStep
from ..state.persistence import insert_step, update_step

log = structlog.get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class StepRecorder:
    """Accumulates cost/output_ref/structured_data for a step.

    Yielded by run_step(). All record_* calls are additive; last write wins.
    """

    workflow_id: str
    step_id: int
    step_index: int
    agent_name: str
    _cost_usd: float = field(default=0.0)
    _output_ref: str | None = field(default=None)
    _structured: dict[str, Any] = field(default_factory=dict)

    def record_cost(self, usd: float) -> None:
        self._cost_usd = usd

    def record_output_ref(self, ref: str) -> None:
        self._output_ref = ref

    def record_structured_data(self, data: dict[str, Any]) -> None:
        self._structured.update(data)


@contextmanager
def run_step(
    conn: sqlite3.Connection,
    *,
    workflow_id: str,
    step_index: int,
    agent_name: str,
    now: Callable[[], datetime] | None = None,
) -> Iterator[StepRecorder]:
    """Open a RUNNING step row, emit step.started, yield a StepRecorder.

    Normal exit: finalise COMPLETED with accumulated cost/output_ref +
    emit step.completed (standard keys + recorder structured_data merged in).
    Exception exit: finalise FAILED with error_message, emit step.failed, re-raise.
    """
    clock = now or _utcnow

    step = WorkflowStep(
        workflow_id=workflow_id,
        step_index=step_index,
        agent_name=agent_name,
        status=StepStatus.RUNNING,
        started_at=clock().isoformat(),
    )
    step_id = insert_step(conn, step)
    emit_event(
        conn,
        workflow_id=workflow_id,
        event_type=EventType.STEP_STARTED,
        level=EventLevel.INFO,
        message=f"step started: {agent_name}",
        structured_data={"step_name": agent_name, "step_index": step_index},
        step_id=step_id,
        timestamp=clock(),
    )

    recorder = StepRecorder(
        workflow_id=workflow_id,
        step_id=step_id,
        step_index=step_index,
        agent_name=agent_name,
    )
    try:
        yield recorder
    except Exception as exc:
        error_msg = f"{type(exc).__name__}: {exc}"
        update_step(
            conn,
            step.model_copy(
                update={
                    "status": StepStatus.FAILED,
                    "finished_at": clock().isoformat(),
                    "error_message": error_msg,
                }
            ),
        )
        emit_event(
            conn,
            workflow_id=workflow_id,
            event_type=EventType.STEP_FAILED,
            level=EventLevel.ERROR,
            message=f"step failed: {agent_name}: {error_msg}",
            structured_data={
                "step_name": agent_name,
                "step_index": step_index,
                "error": error_msg,
            },
            step_id=step_id,
            timestamp=clock(),
        )
        raise
    else:
        structured: dict[str, Any] = {
            "step_name": agent_name,
            "step_index": step_index,
            "cost_usd": recorder._cost_usd,
        }
        if recorder._output_ref is not None:
            structured["output_ref"] = recorder._output_ref
        structured.update(recorder._structured)
        update_step(
            conn,
            step.model_copy(
                update={
                    "status": StepStatus.COMPLETED,
                    "finished_at": clock().isoformat(),
                    "cost_usd": recorder._cost_usd,
                    "output_ref": recorder._output_ref,
                }
            ),
        )
        emit_event(
            conn,
            workflow_id=workflow_id,
            event_type=EventType.STEP_COMPLETED,
            level=EventLevel.INFO,
            message=f"step completed: {agent_name}",
            structured_data=structured,
            step_id=step_id,
            timestamp=clock(),
        )
        log.info(
            "run_step.completed",
            workflow_id=workflow_id,
            agent_name=agent_name,
            step_index=step_index,
            cost_usd=recorder._cost_usd,
        )
