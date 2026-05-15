"""Tests for the ObservabilityLayer (Slice 4).

Three subjects, three test classes:

  TestEmitEvent       — emit_event() writes correct rows with the conventioned
                        structured_data shape; events_of_type() finds them.
  TestConfigureLogging — configure_logging() produces the expected structured
                        output; bind_workflow_context() tags subsequent lines.
  TestJsonlReader     — iter_telemetry() yields decoded records with path/line
                        decorations; resolve_output_ref() handles success and
                        every documented failure mode.

Deterministic, no live API. tmp_path used for both DB and log_root.
"""

from __future__ import annotations

import io
import json
import logging
import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import structlog

from agent_habitat.observability import (
    EVENT_LEVEL_GUIDE,
    EventType,
    TelemetryReadError,
    bind_workflow_context,
    clear_log_context,
    configure_logging,
    emit_event,
    events_of_type,
    iter_telemetry,
    resolve_output_ref,
)
from agent_habitat.state import (
    EventLevel,
    Workflow,
    WorkflowStatus,
    init_db,
    insert_workflow,
    load_events,
    new_workflow_id,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "agent_habitat.db"


@pytest.fixture
def conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    c = init_db(db_path)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def workflow_id(conn: sqlite3.Connection) -> str:
    wf = Workflow(
        id=new_workflow_id(),
        workflow_type="url_summarizer",
        status=WorkflowStatus.RUNNING,
        started_at="2026-05-14T00:00:00+00:00",
    )
    insert_workflow(conn, wf)
    return wf.id


@pytest.fixture
def log_root(tmp_path: Path) -> Path:
    root = tmp_path / "logs"
    root.mkdir()
    return root


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")


# ---------------------------------------------------------------------------
# Event emission
# ---------------------------------------------------------------------------


class TestEmitEvent:
    def test_emits_with_event_type_in_structured_data(
        self, conn: sqlite3.Connection, workflow_id: str
    ) -> None:
        event_id = emit_event(
            conn,
            workflow_id=workflow_id,
            event_type=EventType.WORKFLOW_STARTED,
            level=EventLevel.INFO,
            message="workflow started",
        )
        assert event_id > 0
        events = load_events(conn, workflow_id)
        assert len(events) == 1
        ev = events[0]
        assert ev.level is EventLevel.INFO
        assert ev.message == "workflow started"
        assert ev.structured_data == {"event_type": "workflow.started"}

    def test_extra_structured_data_merges_with_event_type_first(
        self, conn: sqlite3.Connection, workflow_id: str
    ) -> None:
        emit_event(
            conn,
            workflow_id=workflow_id,
            event_type=EventType.STEP_COMPLETED,
            level=EventLevel.INFO,
            message="step done",
            structured_data={"agent_name": "researcher", "cost_usd": 0.0123},
        )
        ev = load_events(conn, workflow_id)[0]
        assert ev.structured_data is not None
        assert ev.structured_data["event_type"] == "step.completed"
        assert ev.structured_data["agent_name"] == "researcher"
        assert ev.structured_data["cost_usd"] == 0.0123

    def test_caller_supplied_event_type_in_payload_is_rejected(
        self, conn: sqlite3.Connection, workflow_id: str
    ) -> None:
        with pytest.raises(ValueError, match="event_type"):
            emit_event(
                conn,
                workflow_id=workflow_id,
                event_type=EventType.WORKFLOW_NOTE,
                level=EventLevel.INFO,
                message="bad",
                structured_data={"event_type": "should-not-pass"},
            )

    def test_accepts_string_event_type(self, conn: sqlite3.Connection, workflow_id: str) -> None:
        # Future event types may not yet be in the enum; allow strings too.
        emit_event(
            conn,
            workflow_id=workflow_id,
            event_type="custom.new_type",
            level=EventLevel.WARN,
            message="ad-hoc",
        )
        ev = load_events(conn, workflow_id)[0]
        assert ev.structured_data == {"event_type": "custom.new_type"}

    def test_step_id_and_explicit_timestamp_are_persisted(
        self, conn: sqlite3.Connection, workflow_id: str
    ) -> None:
        ts = datetime(2026, 5, 14, 12, 30, 0, tzinfo=UTC)
        emit_event(
            conn,
            workflow_id=workflow_id,
            event_type=EventType.STEP_STARTED,
            level=EventLevel.INFO,
            message="started",
            step_id=None,
            timestamp=ts,
        )
        ev = load_events(conn, workflow_id)[0]
        assert ev.timestamp == "2026-05-14T12:30:00+00:00"

    def test_events_of_type_filters_with_json_extract(
        self, conn: sqlite3.Connection, workflow_id: str
    ) -> None:
        emit_event(
            conn,
            workflow_id=workflow_id,
            event_type=EventType.WORKFLOW_STARTED,
            level=EventLevel.INFO,
            message="a",
        )
        emit_event(
            conn,
            workflow_id=workflow_id,
            event_type=EventType.STEP_FAILED,
            level=EventLevel.ERROR,
            message="b",
        )
        emit_event(
            conn,
            workflow_id=workflow_id,
            event_type=EventType.STEP_FAILED,
            level=EventLevel.ERROR,
            message="c",
        )
        failures = events_of_type(conn, workflow_id, EventType.STEP_FAILED)
        assert [e.message for e in failures] == ["b", "c"]

        none_match = events_of_type(conn, workflow_id, EventType.BUDGET_EXCEEDED)
        assert none_match == []

    def test_events_of_type_accepts_string(
        self, conn: sqlite3.Connection, workflow_id: str
    ) -> None:
        emit_event(
            conn,
            workflow_id=workflow_id,
            event_type="custom.thing",
            level=EventLevel.INFO,
            message="x",
        )
        assert len(events_of_type(conn, workflow_id, "custom.thing")) == 1

    def test_level_guide_covers_every_event_level(self) -> None:
        # Every EventLevel must have a documented semantic in EVENT_LEVEL_GUIDE.
        assert set(EVENT_LEVEL_GUIDE.keys()) == set(EventLevel)


# ---------------------------------------------------------------------------
# structlog configuration
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _restore_structlog() -> Iterator[None]:
    """Don't let test-specific configure_logging leak across tests."""
    yield
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()


class TestConfigureLogging:
    def test_json_output_writes_structured_record(self) -> None:
        buf = io.StringIO()
        configure_logging(level=logging.INFO, json_output=True, stream=buf)
        structlog.get_logger("test").info("hello", k=1)
        line = buf.getvalue().strip()
        rec = json.loads(line)
        assert rec["event"] == "hello"
        assert rec["k"] == 1
        assert rec["level"] == "info"
        assert "timestamp" in rec

    def test_console_renderer_is_default_and_omits_json_braces(self) -> None:
        buf = io.StringIO()
        configure_logging(stream=buf)
        structlog.get_logger("test").info("hello", k=1)
        out = buf.getvalue()
        assert "hello" in out
        assert "k=1" in out
        # Console renderer is not JSON — no leading '{'.
        assert not out.lstrip().startswith("{")

    def test_bind_workflow_context_tags_subsequent_lines(self) -> None:
        buf = io.StringIO()
        configure_logging(json_output=True, stream=buf)
        bind_workflow_context(workflow_id="wf-abc", agent_name="researcher")
        structlog.get_logger("test").info("step")
        rec = json.loads(buf.getvalue().strip())
        assert rec["workflow_id"] == "wf-abc"
        assert rec["agent_name"] == "researcher"

    def test_clear_log_context_removes_bindings(self) -> None:
        buf = io.StringIO()
        configure_logging(json_output=True, stream=buf)
        bind_workflow_context(workflow_id="wf-1")
        clear_log_context()
        structlog.get_logger("test").info("step")
        rec = json.loads(buf.getvalue().strip())
        assert "workflow_id" not in rec

    def test_level_filtering_drops_below_threshold(self) -> None:
        buf = io.StringIO()
        configure_logging(level=logging.WARNING, json_output=True, stream=buf)
        log = structlog.get_logger("test")
        log.info("dropped")
        log.warning("kept")
        lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
        assert len(lines) == 1
        assert json.loads(lines[0])["event"] == "kept"

    def test_reconfigure_is_idempotent(self) -> None:
        buf1 = io.StringIO()
        configure_logging(json_output=True, stream=buf1)
        buf2 = io.StringIO()
        configure_logging(json_output=True, stream=buf2)
        structlog.get_logger("test").info("after-reconfigure")
        assert buf1.getvalue() == ""
        assert "after-reconfigure" in buf2.getvalue()


# ---------------------------------------------------------------------------
# JSONL read interface
# ---------------------------------------------------------------------------


class TestJsonlReader:
    def test_iter_telemetry_single_day(self, log_root: Path) -> None:
        path = log_root / "2026-05-14" / "wf-1.jsonl"
        _write_jsonl(
            path,
            [
                {"timestamp": "2026-05-14T00:00:01+00:00", "cost_usd": 0.001},
                {"timestamp": "2026-05-14T00:00:02+00:00", "cost_usd": 0.002},
            ],
        )
        recs = list(iter_telemetry("wf-1", log_root=log_root, day=date(2026, 5, 14)))
        assert len(recs) == 2
        assert recs[0]["cost_usd"] == 0.001
        assert recs[0]["_line"] == 1
        assert recs[1]["_line"] == 2
        assert recs[0]["_path"].endswith("2026-05-14/wf-1.jsonl")

    def test_iter_telemetry_all_days_in_calendar_order(self, log_root: Path) -> None:
        _write_jsonl(
            log_root / "2026-05-13" / "wf-1.jsonl",
            [{"cost_usd": 0.1, "day": 13}],
        )
        _write_jsonl(
            log_root / "2026-05-14" / "wf-1.jsonl",
            [{"cost_usd": 0.2, "day": 14}, {"cost_usd": 0.3, "day": 14}],
        )
        recs = list(iter_telemetry("wf-1", log_root=log_root))
        assert [r["day"] for r in recs] == [13, 14, 14]

    def test_iter_telemetry_missing_workflow_returns_empty(self, log_root: Path) -> None:
        assert list(iter_telemetry("does-not-exist", log_root=log_root)) == []

    def test_iter_telemetry_skips_blank_lines(self, log_root: Path) -> None:
        path = log_root / "2026-05-14" / "wf-1.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text('{"cost_usd":0.1}\n\n{"cost_usd":0.2}\n', encoding="utf-8")
        recs = list(iter_telemetry("wf-1", log_root=log_root, day=date(2026, 5, 14)))
        assert [r["cost_usd"] for r in recs] == [0.1, 0.2]
        # _line numbers still reflect the true file line.
        assert [r["_line"] for r in recs] == [1, 3]

    def test_iter_telemetry_raises_on_malformed_json(self, log_root: Path) -> None:
        path = log_root / "2026-05-14" / "wf-1.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text('{"cost_usd":0.1}\nnot-json\n', encoding="utf-8")
        gen = iter_telemetry("wf-1", log_root=log_root, day=date(2026, 5, 14))
        next(gen)  # first record OK
        with pytest.raises(TelemetryReadError, match="malformed JSONL"):
            next(gen)

    def test_iter_telemetry_raises_on_non_object_payload(self, log_root: Path) -> None:
        path = log_root / "2026-05-14" / "wf-1.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text("[1,2,3]\n", encoding="utf-8")
        with pytest.raises(TelemetryReadError, match="non-object"):
            list(iter_telemetry("wf-1", log_root=log_root, day=date(2026, 5, 14)))

    def test_iter_telemetry_missing_log_root_is_empty(self, tmp_path: Path) -> None:
        assert list(iter_telemetry("wf-1", log_root=tmp_path / "nope")) == []

    def test_resolve_output_ref_success(self, log_root: Path) -> None:
        path = log_root / "2026-05-14" / "wf-1.jsonl"
        _write_jsonl(
            path,
            [
                {"cost_usd": 0.001, "model": "haiku"},
                {"cost_usd": 0.002, "model": "sonnet"},
                {"cost_usd": 0.003, "model": "opus"},
            ],
        )
        ref = f"{path.as_posix()}:2"
        rec = resolve_output_ref(ref)
        assert rec["model"] == "sonnet"
        assert rec["cost_usd"] == 0.002

    def test_resolve_output_ref_malformed_no_colon(self) -> None:
        with pytest.raises(TelemetryReadError, match="malformed"):
            resolve_output_ref("no-colon-here")

    def test_resolve_output_ref_non_integer_line(self) -> None:
        with pytest.raises(TelemetryReadError, match="line number"):
            resolve_output_ref("/tmp/x.jsonl:abc")

    def test_resolve_output_ref_missing_path(self, tmp_path: Path) -> None:
        with pytest.raises(TelemetryReadError, match="does not exist"):
            resolve_output_ref(f"{(tmp_path / 'missing.jsonl').as_posix()}:1")

    def test_resolve_output_ref_line_out_of_range(self, log_root: Path) -> None:
        path = log_root / "2026-05-14" / "wf-1.jsonl"
        _write_jsonl(path, [{"a": 1}])
        with pytest.raises(TelemetryReadError, match="not found"):
            resolve_output_ref(f"{path.as_posix()}:99")

    def test_resolve_output_ref_zero_line_rejected(self, log_root: Path) -> None:
        path = log_root / "2026-05-14" / "wf-1.jsonl"
        _write_jsonl(path, [{"a": 1}])
        with pytest.raises(TelemetryReadError, match="1-indexed"):
            resolve_output_ref(f"{path.as_posix()}:0")

    def test_resolve_output_ref_empty_target_line(self, log_root: Path) -> None:
        path = log_root / "2026-05-14" / "wf-1.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text('{"a":1}\n\n{"a":2}\n', encoding="utf-8")
        with pytest.raises(TelemetryReadError, match="empty line"):
            resolve_output_ref(f"{path.as_posix()}:2")

    def test_resolve_output_ref_matches_llm_py_format(self, log_root: Path) -> None:
        # llm.py writes paths with `.as_posix()` and 1-indexed lines via
        # `_append_telemetry`. Reproduce that shape and round-trip it.
        path = log_root / "2026-05-14" / "wf-1.jsonl"
        _write_jsonl(path, [{"line_number": 1}, {"line_number": 2}])
        ref_format = f"{path.as_posix()}:1"
        assert resolve_output_ref(ref_format)["line_number"] == 1
