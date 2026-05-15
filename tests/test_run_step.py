"""Tests for the run_step() lifecycle utility (orchestration/run_step.py).

Coverage:
  TestNormalExit      — step row COMPLETED, standard events, no cost/ref defaults.
  TestRecorderVerbs   — record_cost / record_output_ref / record_structured_data:
                        values land on step row + step.completed event;
                        additive accumulation; last write wins.
  TestExceptionExit   — step row FAILED, step.failed event, exception re-raised,
                        error_message carries type+message, no cost written.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from agent_habitat.orchestration.run_step import StepRecorder, run_step
from agent_habitat.state import (
    StepStatus,
    Workflow,
    WorkflowStatus,
    init_db,
    insert_workflow,
    load_events,
    load_steps,
)

WF_ID = "wf-run-step-test-001"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = init_db(tmp_path / "test_run_step.db")
    insert_workflow(c, Workflow(id=WF_ID, workflow_type="test", status=WorkflowStatus.RUNNING))
    try:
        yield c
    finally:
        c.close()


def _event_types(conn: sqlite3.Connection) -> list[str | None]:
    return [(e.structured_data or {}).get("event_type") for e in load_events(conn, WF_ID)]


class TestNormalExit:
    def test_step_row_completed(self, conn: sqlite3.Connection) -> None:
        with run_step(conn, workflow_id=WF_ID, step_index=1, agent_name="fetch"):
            pass
        steps = load_steps(conn, WF_ID)
        assert len(steps) == 1
        s = steps[0]
        assert s.status is StepStatus.COMPLETED
        assert s.agent_name == "fetch"
        assert s.step_index == 1
        assert s.finished_at is not None
        assert s.error_message is None

    def test_events_started_and_completed(self, conn: sqlite3.Connection) -> None:
        with run_step(conn, workflow_id=WF_ID, step_index=1, agent_name="fetch"):
            pass
        types = _event_types(conn)
        assert types.count("step.started") == 1
        assert types.count("step.completed") == 1
        assert "step.failed" not in types

    def test_completed_event_has_standard_keys(self, conn: sqlite3.Connection) -> None:
        with run_step(conn, workflow_id=WF_ID, step_index=2, agent_name="parse"):
            pass
        events = load_events(conn, WF_ID)
        completed = next(
            e for e in events if (e.structured_data or {}).get("event_type") == "step.completed"
        )
        sd = completed.structured_data or {}
        assert sd.get("step_name") == "parse"
        assert sd.get("step_index") == 2
        assert "cost_usd" in sd
        assert "output_ref" not in sd  # not recorded → absent

    def test_no_cost_no_output_ref_defaults(self, conn: sqlite3.Connection) -> None:
        with run_step(conn, workflow_id=WF_ID, step_index=1, agent_name="fetch"):
            pass
        s = load_steps(conn, WF_ID)[0]
        assert s.cost_usd == 0.0
        assert s.output_ref is None

    def test_yields_recorder_instance(self, conn: sqlite3.Connection) -> None:
        with run_step(conn, workflow_id=WF_ID, step_index=1, agent_name="test") as step:
            assert isinstance(step, StepRecorder)
            assert step.agent_name == "test"
            assert step.step_index == 1
            assert step.workflow_id == WF_ID
            assert isinstance(step.step_id, int)


class TestRecorderVerbs:
    def test_record_cost_lands_on_step_row(self, conn: sqlite3.Connection) -> None:
        with run_step(conn, workflow_id=WF_ID, step_index=1, agent_name="summarize") as step:
            step.record_cost(0.00456)
        assert load_steps(conn, WF_ID)[0].cost_usd == pytest.approx(0.00456)

    def test_record_output_ref_lands_on_step_row(self, conn: sqlite3.Connection) -> None:
        ref = "data/logs/2026-05-14/wf.jsonl:3"
        with run_step(conn, workflow_id=WF_ID, step_index=1, agent_name="summarize") as step:
            step.record_output_ref(ref)
        assert load_steps(conn, WF_ID)[0].output_ref == ref

    def test_record_output_ref_in_completed_event(self, conn: sqlite3.Connection) -> None:
        ref = "data/logs/2026-05-14/wf.jsonl:3"
        with run_step(conn, workflow_id=WF_ID, step_index=1, agent_name="summarize") as step:
            step.record_output_ref(ref)
        events = load_events(conn, WF_ID)
        completed = next(
            e for e in events if (e.structured_data or {}).get("event_type") == "step.completed"
        )
        assert (completed.structured_data or {}).get("output_ref") == ref

    def test_record_structured_data_merged_in_completed_event(
        self, conn: sqlite3.Connection
    ) -> None:
        with run_step(conn, workflow_id=WF_ID, step_index=1, agent_name="researcher") as step:
            step.record_structured_data({"signal_count": 5, "source_count": 3})
        events = load_events(conn, WF_ID)
        completed = next(
            e for e in events if (e.structured_data or {}).get("event_type") == "step.completed"
        )
        sd = completed.structured_data or {}
        assert sd.get("signal_count") == 5
        assert sd.get("source_count") == 3

    def test_record_structured_data_additive(self, conn: sqlite3.Connection) -> None:
        with run_step(conn, workflow_id=WF_ID, step_index=1, agent_name="researcher") as step:
            step.record_structured_data({"key_a": 1})
            step.record_structured_data({"key_b": 2})
        events = load_events(conn, WF_ID)
        completed = next(
            e for e in events if (e.structured_data or {}).get("event_type") == "step.completed"
        )
        sd = completed.structured_data or {}
        assert sd.get("key_a") == 1
        assert sd.get("key_b") == 2

    def test_record_structured_data_last_write_wins(self, conn: sqlite3.Connection) -> None:
        with run_step(conn, workflow_id=WF_ID, step_index=1, agent_name="researcher") as step:
            step.record_structured_data({"count": 10})
            step.record_structured_data({"count": 20})
        events = load_events(conn, WF_ID)
        completed = next(
            e for e in events if (e.structured_data or {}).get("event_type") == "step.completed"
        )
        assert (completed.structured_data or {}).get("count") == 20

    def test_record_cost_last_write_wins(self, conn: sqlite3.Connection) -> None:
        with run_step(conn, workflow_id=WF_ID, step_index=1, agent_name="summarize") as step:
            step.record_cost(1.0)
            step.record_cost(2.5)
        assert load_steps(conn, WF_ID)[0].cost_usd == pytest.approx(2.5)

    def test_cost_in_completed_event_structured_data(self, conn: sqlite3.Connection) -> None:
        with run_step(conn, workflow_id=WF_ID, step_index=1, agent_name="summarize") as step:
            step.record_cost(0.009)
        events = load_events(conn, WF_ID)
        completed = next(
            e for e in events if (e.structured_data or {}).get("event_type") == "step.completed"
        )
        assert (completed.structured_data or {}).get("cost_usd") == pytest.approx(0.009)


class TestExceptionExit:
    def test_step_row_failed_on_exception(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(ValueError):
            with run_step(conn, workflow_id=WF_ID, step_index=1, agent_name="fetch"):
                raise ValueError("something went wrong")
        s = load_steps(conn, WF_ID)[0]
        assert s.status is StepStatus.FAILED
        assert s.finished_at is not None
        assert s.error_message is not None
        assert "ValueError" in s.error_message
        assert "something went wrong" in s.error_message

    def test_failed_event_emitted_not_completed(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(RuntimeError):
            with run_step(conn, workflow_id=WF_ID, step_index=1, agent_name="fetch"):
                raise RuntimeError("boom")
        types = _event_types(conn)
        assert types.count("step.started") == 1
        assert types.count("step.failed") == 1
        assert "step.completed" not in types

    def test_failed_event_has_step_name_and_error(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(RuntimeError):
            with run_step(conn, workflow_id=WF_ID, step_index=1, agent_name="parse"):
                raise RuntimeError("parse exploded")
        events = load_events(conn, WF_ID)
        failed = next(
            e for e in events if (e.structured_data or {}).get("event_type") == "step.failed"
        )
        sd = failed.structured_data or {}
        assert sd.get("step_name") == "parse"
        assert "parse exploded" in (sd.get("error") or "")

    def test_exception_is_reraised(self, conn: sqlite3.Connection) -> None:
        class CustomError(Exception):
            pass

        with pytest.raises(CustomError, match="specific message"):
            with run_step(conn, workflow_id=WF_ID, step_index=1, agent_name="test"):
                raise CustomError("specific message")

    def test_exception_type_in_step_error_message(self, conn: sqlite3.Connection) -> None:
        class BoomError(RuntimeError):
            pass

        with pytest.raises(BoomError):
            with run_step(conn, workflow_id=WF_ID, step_index=1, agent_name="test"):
                raise BoomError("exploded")
        s = load_steps(conn, WF_ID)[0]
        assert "BoomError" in (s.error_message or "")

    def test_cost_not_written_on_exception(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(RuntimeError):
            with run_step(conn, workflow_id=WF_ID, step_index=1, agent_name="test") as step:
                step.record_cost(5.0)
                raise RuntimeError("abort")
        # FAILED path skips the COMPLETED update; step row keeps default cost=0.0.
        assert load_steps(conn, WF_ID)[0].cost_usd == 0.0

    def test_failed_event_step_index_present(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(KeyError):
            with run_step(conn, workflow_id=WF_ID, step_index=7, agent_name="scorer"):
                raise KeyError("missing key")
        events = load_events(conn, WF_ID)
        failed = next(
            e for e in events if (e.structured_data or {}).get("event_type") == "step.failed"
        )
        assert (failed.structured_data or {}).get("step_index") == 7
