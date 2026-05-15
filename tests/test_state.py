"""Tests for the SQLite persistence layer (ADR-002 Option 1).

Every test uses a tmp_path DB — production `data/state/agent_habitat.db`
is never touched. No network, no API calls; persistence is fully
deterministic so there's no live smoke for this slice.

Coverage:

  TestSchema         — idempotent DDL bootstrap, FK enforcement.
  TestWorkflowsCRUD  — insert / load / update / status query round-trip.
  TestStepsCRUD      — insert assigns id, update by id, ordered load.
  TestEventsCRUD     — insert / ordered load with structured_data JSON.
  TestFullRoundTrip  — save a workflow with steps + events, reload,
                       assert structural equality through Pydantic models.
  TestCostRollup     — recompute_cost_total sums step costs into workflow.
  TestReconcile      — orphan in-progress steps reconcile to failed +
                       synthesized event; idempotent re-run is a no-op.
  TestIdHelper       — new_workflow_id returns unique 32-char hex strings.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_habitat.state import (
    Event,
    EventLevel,
    StepStatus,
    Workflow,
    WorkflowStatus,
    WorkflowStep,
    init_db,
    init_schema,
    insert_event,
    insert_step,
    insert_workflow,
    load_events,
    load_full,
    load_steps,
    load_workflow,
    new_workflow_id,
    reconcile_orphan_steps,
    recompute_cost_total,
    update_step,
    update_workflow,
    workflows_by_status,
)
from agent_habitat.state.persistence import (
    ORPHAN_ERROR_MESSAGE,
    ORPHAN_EVENT_MESSAGE,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "agent_habitat.db"


@pytest.fixture
def conn(db_path: Path) -> sqlite3.Connection:
    c = init_db(db_path)
    try:
        yield c
    finally:
        c.close()


def _new_wf(workflow_type: str = "url_summarizer", **overrides: object) -> Workflow:
    base: dict[str, object] = {
        "id": new_workflow_id(),
        "workflow_type": workflow_type,
        "status": WorkflowStatus.RUNNING,
        "started_at": "2026-05-13T12:00:00+00:00",
        "metadata": {"source": "test"},
    }
    base.update(overrides)
    return Workflow(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class TestSchema:
    def test_init_db_creates_tables(self, db_path: Path) -> None:
        c = init_db(db_path)
        try:
            tables = {
                row["name"]
                for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
        finally:
            c.close()
        assert {"workflows", "workflow_steps", "events"}.issubset(tables)

    def test_init_db_is_idempotent(self, db_path: Path) -> None:
        c1 = init_db(db_path)
        c1.close()
        # Insert a workflow on the first init.
        c2 = init_db(db_path)
        try:
            insert_workflow(c2, _new_wf())
            count_after_first = c2.execute("SELECT COUNT(*) FROM workflows").fetchone()[0]
        finally:
            c2.close()
        # A third init_schema must not wipe the row.
        c3 = init_db(db_path)
        try:
            init_schema(c3)  # explicit second call
            count_after_third = c3.execute("SELECT COUNT(*) FROM workflows").fetchone()[0]
        finally:
            c3.close()
        assert count_after_first == 1
        assert count_after_third == 1

    def test_foreign_keys_enforced(self, conn: sqlite3.Connection) -> None:
        # Inserting a step pointing at a non-existent workflow must fail.
        bad_step = WorkflowStep(
            workflow_id="does-not-exist",
            step_index=0,
            agent_name="researcher",
        )
        with pytest.raises(sqlite3.IntegrityError):
            insert_step(conn, bad_step)


# ---------------------------------------------------------------------------
# Workflow CRUD
# ---------------------------------------------------------------------------


class TestWorkflowsCRUD:
    def test_insert_then_load(self, conn: sqlite3.Connection) -> None:
        wf = _new_wf()
        insert_workflow(conn, wf)
        loaded = load_workflow(conn, wf.id)
        assert loaded is not None
        assert loaded == wf

    def test_load_missing_returns_none(self, conn: sqlite3.Connection) -> None:
        assert load_workflow(conn, "no-such-id") is None

    def test_update_changes_status_and_finished_at(self, conn: sqlite3.Connection) -> None:
        wf = _new_wf()
        insert_workflow(conn, wf)
        wf.status = WorkflowStatus.COMPLETED
        wf.finished_at = "2026-05-13T13:00:00+00:00"
        update_workflow(conn, wf)
        loaded = load_workflow(conn, wf.id)
        assert loaded is not None
        assert loaded.status is WorkflowStatus.COMPLETED
        assert loaded.finished_at == "2026-05-13T13:00:00+00:00"

    def test_update_missing_raises(self, conn: sqlite3.Connection) -> None:
        wf = _new_wf(id="ghost")
        with pytest.raises(KeyError):
            update_workflow(conn, wf)

    def test_query_by_status_returns_only_matching(self, conn: sqlite3.Connection) -> None:
        running_a = _new_wf(started_at="2026-05-13T10:00:00+00:00")
        running_b = _new_wf(started_at="2026-05-13T11:00:00+00:00")
        completed = _new_wf(
            status=WorkflowStatus.COMPLETED,
            finished_at="2026-05-13T11:30:00+00:00",
        )
        for w in (running_a, running_b, completed):
            insert_workflow(conn, w)

        running = workflows_by_status(conn, WorkflowStatus.RUNNING)
        done = workflows_by_status(conn, WorkflowStatus.COMPLETED)
        cancelled = workflows_by_status(conn, WorkflowStatus.CANCELLED)

        assert {w.id for w in running} == {running_a.id, running_b.id}
        # ORDER BY started_at — earlier first.
        assert [w.id for w in running] == [running_a.id, running_b.id]
        assert [w.id for w in done] == [completed.id]
        assert cancelled == []

    def test_metadata_roundtrips_unicode_and_nested(self, conn: sqlite3.Connection) -> None:
        meta = {"k": "vä", "nested": {"a": [1, 2, 3]}, "n": 7}
        wf = _new_wf(metadata=meta)
        insert_workflow(conn, wf)
        loaded = load_workflow(conn, wf.id)
        assert loaded is not None
        assert loaded.metadata == meta

    def test_empty_metadata_roundtrips_as_empty_dict(self, conn: sqlite3.Connection) -> None:
        wf = _new_wf(metadata={})
        insert_workflow(conn, wf)
        loaded = load_workflow(conn, wf.id)
        assert loaded is not None
        assert loaded.metadata == {}

    def test_status_check_constraint_rejects_bogus_value(self, conn: sqlite3.Connection) -> None:
        # We can't construct an invalid enum via Pydantic, so go around it.
        wf = _new_wf()
        with pytest.raises(sqlite3.IntegrityError):
            with conn:
                conn.execute(
                    """
                    INSERT INTO workflows (id, workflow_type, status, started_at)
                    VALUES (?, ?, 'not-a-real-status', ?)
                    """,
                    (wf.id, wf.workflow_type, wf.started_at),
                )


# ---------------------------------------------------------------------------
# WorkflowStep CRUD
# ---------------------------------------------------------------------------


class TestStepsCRUD:
    def test_insert_assigns_id_back_onto_object(self, conn: sqlite3.Connection) -> None:
        wf = _new_wf()
        insert_workflow(conn, wf)
        step = WorkflowStep(
            workflow_id=wf.id,
            step_index=0,
            agent_name="researcher",
        )
        assert step.id is None
        assigned = insert_step(conn, step)
        assert assigned > 0
        assert step.id == assigned

    def test_update_step_requires_id(self, conn: sqlite3.Connection) -> None:
        wf = _new_wf()
        insert_workflow(conn, wf)
        step = WorkflowStep(workflow_id=wf.id, step_index=0, agent_name="x")
        with pytest.raises(ValueError):
            update_step(conn, step)

    def test_step_lifecycle_roundtrips(self, conn: sqlite3.Connection) -> None:
        wf = _new_wf()
        insert_workflow(conn, wf)
        step = WorkflowStep(
            workflow_id=wf.id,
            step_index=0,
            agent_name="researcher",
            started_at="2026-05-13T12:00:00+00:00",
        )
        insert_step(conn, step)
        # Finish the step.
        step.status = StepStatus.COMPLETED
        step.finished_at = "2026-05-13T12:00:05+00:00"
        step.output_ref = "data/logs/2026-05-13/wf.jsonl:1"
        step.cost_usd = 0.00123
        update_step(conn, step)

        loaded = load_steps(conn, wf.id)
        assert len(loaded) == 1
        assert loaded[0] == step

    def test_step_index_uniqueness_enforced(self, conn: sqlite3.Connection) -> None:
        wf = _new_wf()
        insert_workflow(conn, wf)
        insert_step(conn, WorkflowStep(workflow_id=wf.id, step_index=0, agent_name="a"))
        with pytest.raises(sqlite3.IntegrityError):
            insert_step(conn, WorkflowStep(workflow_id=wf.id, step_index=0, agent_name="b"))

    def test_steps_load_in_step_index_order(self, conn: sqlite3.Connection) -> None:
        wf = _new_wf()
        insert_workflow(conn, wf)
        for idx in (2, 0, 1):
            insert_step(
                conn,
                WorkflowStep(workflow_id=wf.id, step_index=idx, agent_name=f"a{idx}"),
            )
        loaded = load_steps(conn, wf.id)
        assert [s.step_index for s in loaded] == [0, 1, 2]


# ---------------------------------------------------------------------------
# Event CRUD
# ---------------------------------------------------------------------------


class TestEventsCRUD:
    def test_insert_assigns_id_back(self, conn: sqlite3.Connection) -> None:
        wf = _new_wf()
        insert_workflow(conn, wf)
        ev = Event(
            workflow_id=wf.id,
            level=EventLevel.INFO,
            message="workflow started",
            structured_data={"k": "v"},
        )
        assigned = insert_event(conn, ev)
        assert assigned > 0
        assert ev.id == assigned

    def test_events_ordered_by_timestamp_then_id(self, conn: sqlite3.Connection) -> None:
        wf = _new_wf()
        insert_workflow(conn, wf)
        # Three events at the same timestamp — tiebreak should be id ASC,
        # which matches insertion order.
        ts = "2026-05-13T12:00:00+00:00"
        for msg in ("a", "b", "c"):
            insert_event(
                conn,
                Event(
                    workflow_id=wf.id,
                    timestamp=ts,
                    level=EventLevel.INFO,
                    message=msg,
                ),
            )
        # An earlier event added last must sort first.
        insert_event(
            conn,
            Event(
                workflow_id=wf.id,
                timestamp="2026-05-13T11:00:00+00:00",
                level=EventLevel.INFO,
                message="earlier",
            ),
        )
        loaded = load_events(conn, wf.id)
        assert [e.message for e in loaded] == ["earlier", "a", "b", "c"]

    def test_structured_data_none_roundtrips_as_none(self, conn: sqlite3.Connection) -> None:
        wf = _new_wf()
        insert_workflow(conn, wf)
        ev = Event(
            workflow_id=wf.id,
            level=EventLevel.WARN,
            message="bare",
            structured_data=None,
        )
        insert_event(conn, ev)
        loaded = load_events(conn, wf.id)
        assert loaded[0].structured_data is None


# ---------------------------------------------------------------------------
# Full round-trip
# ---------------------------------------------------------------------------


class TestFullRoundTrip:
    def test_save_then_load_full_structural_equality(self, conn: sqlite3.Connection) -> None:
        wf = _new_wf(metadata={"target_url": "https://example.com"})
        insert_workflow(conn, wf)

        step_a = WorkflowStep(
            workflow_id=wf.id,
            step_index=0,
            agent_name="researcher",
            status=StepStatus.COMPLETED,
            started_at="2026-05-13T12:00:00+00:00",
            finished_at="2026-05-13T12:00:03+00:00",
            output_ref="data/logs/2026-05-13/wf.jsonl:1",
            cost_usd=0.001,
        )
        step_b = WorkflowStep(
            workflow_id=wf.id,
            step_index=1,
            agent_name="extractor",
            status=StepStatus.COMPLETED,
            started_at="2026-05-13T12:00:04+00:00",
            finished_at="2026-05-13T12:00:09+00:00",
            input_ref="data/logs/2026-05-13/wf.jsonl:1",
            output_ref="data/logs/2026-05-13/wf.jsonl:2",
            cost_usd=0.005,
        )
        insert_step(conn, step_a)
        insert_step(conn, step_b)

        ev_start = Event(
            workflow_id=wf.id,
            timestamp="2026-05-13T12:00:00+00:00",
            level=EventLevel.INFO,
            message="workflow started",
        )
        ev_step = Event(
            workflow_id=wf.id,
            step_id=step_a.id,
            timestamp="2026-05-13T12:00:03+00:00",
            level=EventLevel.INFO,
            message="researcher complete",
            structured_data={"tokens": 1234},
        )
        insert_event(conn, ev_start)
        insert_event(conn, ev_step)

        # Roll up costs into the workflow row.
        recompute_cost_total(conn, wf.id)

        loaded = load_full(conn, wf.id)
        assert loaded is not None
        loaded_wf, loaded_steps, loaded_events = loaded

        # Workflow equality (after cost rollup).
        assert loaded_wf.id == wf.id
        assert loaded_wf.workflow_type == wf.workflow_type
        assert loaded_wf.metadata == wf.metadata
        assert loaded_wf.cost_total_usd == pytest.approx(0.006)

        # Steps round-trip exactly.
        assert loaded_steps == [step_a, step_b]

        # Events round-trip exactly.
        assert loaded_events == [ev_start, ev_step]

    def test_load_full_returns_none_for_missing(self, conn: sqlite3.Connection) -> None:
        assert load_full(conn, "no-such-id") is None


# ---------------------------------------------------------------------------
# Cost rollup
# ---------------------------------------------------------------------------


class TestCostRollup:
    def test_recompute_sums_step_costs(self, conn: sqlite3.Connection) -> None:
        wf = _new_wf()
        insert_workflow(conn, wf)
        for i, cost in enumerate([0.001, 0.002, 0.003, 0.004]):
            insert_step(
                conn,
                WorkflowStep(
                    workflow_id=wf.id,
                    step_index=i,
                    agent_name=f"a{i}",
                    cost_usd=cost,
                ),
            )
        total = recompute_cost_total(conn, wf.id)
        assert total == pytest.approx(0.010)
        loaded = load_workflow(conn, wf.id)
        assert loaded is not None
        assert loaded.cost_total_usd == pytest.approx(0.010)

    def test_recompute_with_no_steps_is_zero(self, conn: sqlite3.Connection) -> None:
        wf = _new_wf()
        insert_workflow(conn, wf)
        assert recompute_cost_total(conn, wf.id) == 0.0

    def test_recompute_missing_workflow_raises(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(KeyError):
            recompute_cost_total(conn, "ghost")

    def test_recompute_is_isolated_per_workflow(self, conn: sqlite3.Connection) -> None:
        wf1 = _new_wf()
        wf2 = _new_wf()
        insert_workflow(conn, wf1)
        insert_workflow(conn, wf2)
        insert_step(
            conn,
            WorkflowStep(workflow_id=wf1.id, step_index=0, agent_name="a", cost_usd=0.5),
        )
        insert_step(
            conn,
            WorkflowStep(workflow_id=wf2.id, step_index=0, agent_name="b", cost_usd=0.25),
        )
        recompute_cost_total(conn, wf1.id)
        recompute_cost_total(conn, wf2.id)
        loaded1 = load_workflow(conn, wf1.id)
        loaded2 = load_workflow(conn, wf2.id)
        assert loaded1 is not None and loaded2 is not None
        assert loaded1.cost_total_usd == pytest.approx(0.5)
        assert loaded2.cost_total_usd == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


class TestReconcile:
    def test_reconciles_orphan_to_failed(self, conn: sqlite3.Connection) -> None:
        wf = _new_wf()
        insert_workflow(conn, wf)
        orphan = WorkflowStep(
            workflow_id=wf.id,
            step_index=0,
            agent_name="researcher",
            status=StepStatus.RUNNING,
            started_at="2026-05-13T12:00:00+00:00",
        )
        insert_step(conn, orphan)
        assert orphan.id is not None

        fixed_now = datetime(2026, 5, 13, 13, 0, 0, tzinfo=UTC)
        reconciled = reconcile_orphan_steps(conn, now=fixed_now)
        assert reconciled == [orphan.id]

        loaded = load_steps(conn, wf.id)
        assert len(loaded) == 1
        s = loaded[0]
        assert s.status is StepStatus.FAILED
        assert s.finished_at == fixed_now.isoformat()
        assert s.error_message == ORPHAN_ERROR_MESSAGE

        # One synthesized event at level=warn, attached to this step.
        events = load_events(conn, wf.id)
        assert len(events) == 1
        ev = events[0]
        assert ev.level is EventLevel.WARN
        assert ev.message == ORPHAN_EVENT_MESSAGE
        assert ev.step_id == orphan.id
        assert ev.structured_data is not None
        assert ev.structured_data["step_id"] == orphan.id
        assert ev.structured_data["agent_name"] == "researcher"
        assert ev.structured_data["reconciled_at"] == fixed_now.isoformat()

    def test_does_not_touch_completed_or_failed_steps(self, conn: sqlite3.Connection) -> None:
        wf = _new_wf()
        insert_workflow(conn, wf)
        done = WorkflowStep(
            workflow_id=wf.id,
            step_index=0,
            agent_name="a",
            status=StepStatus.COMPLETED,
            started_at="2026-05-13T12:00:00+00:00",
            finished_at="2026-05-13T12:00:01+00:00",
        )
        already_failed = WorkflowStep(
            workflow_id=wf.id,
            step_index=1,
            agent_name="b",
            status=StepStatus.FAILED,
            started_at="2026-05-13T12:00:02+00:00",
            finished_at="2026-05-13T12:00:03+00:00",
            error_message="prior failure",
        )
        insert_step(conn, done)
        insert_step(conn, already_failed)

        reconciled = reconcile_orphan_steps(conn)
        assert reconciled == []
        loaded = load_steps(conn, wf.id)
        # Both rows unchanged.
        assert loaded[0].status is StepStatus.COMPLETED
        assert loaded[1].status is StepStatus.FAILED
        assert loaded[1].error_message == "prior failure"

    def test_running_with_finished_at_set_is_not_orphan(self, conn: sqlite3.Connection) -> None:
        # Edge: status='running' but finished_at IS NOT NULL — anomalous but
        # the sweep predicate requires both, so this row is left alone for
        # an operator to investigate.
        wf = _new_wf()
        insert_workflow(conn, wf)
        with conn:
            conn.execute(
                """
                INSERT INTO workflow_steps (
                    workflow_id, step_index, agent_name, status,
                    started_at, finished_at, cost_usd
                ) VALUES (?, ?, ?, 'running', ?, ?, ?)
                """,
                (wf.id, 0, "weird", "2026-05-13T12:00:00+00:00", "2026-05-13T12:00:01+00:00", 0.0),
            )
        reconciled = reconcile_orphan_steps(conn)
        assert reconciled == []

    def test_idempotent_second_call_is_noop(self, conn: sqlite3.Connection) -> None:
        wf = _new_wf()
        insert_workflow(conn, wf)
        insert_step(
            conn,
            WorkflowStep(
                workflow_id=wf.id,
                step_index=0,
                agent_name="x",
                status=StepStatus.RUNNING,
            ),
        )
        first = reconcile_orphan_steps(conn)
        assert len(first) == 1
        second = reconcile_orphan_steps(conn)
        assert second == []
        # No duplicate event emitted.
        events = load_events(conn, wf.id)
        assert len(events) == 1

    def test_reconciles_orphans_across_multiple_workflows(self, conn: sqlite3.Connection) -> None:
        wf1 = _new_wf()
        wf2 = _new_wf()
        insert_workflow(conn, wf1)
        insert_workflow(conn, wf2)
        s1 = WorkflowStep(workflow_id=wf1.id, step_index=0, agent_name="a")
        s2 = WorkflowStep(workflow_id=wf2.id, step_index=0, agent_name="b")
        insert_step(conn, s1)
        insert_step(conn, s2)
        reconciled = reconcile_orphan_steps(conn)
        assert set(reconciled) == {s1.id, s2.id}

    def test_no_orphans_returns_empty_list(self, conn: sqlite3.Connection) -> None:
        assert reconcile_orphan_steps(conn) == []


# ---------------------------------------------------------------------------
# ID helper
# ---------------------------------------------------------------------------


class TestIdHelper:
    def test_returns_32_char_hex(self) -> None:
        wf_id = new_workflow_id()
        assert len(wf_id) == 32
        # uuid4().hex is lowercase hex.
        int(wf_id, 16)  # must parse as hex

    def test_collisions_are_implausible(self) -> None:
        ids = {new_workflow_id() for _ in range(1000)}
        assert len(ids) == 1000
