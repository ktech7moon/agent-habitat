"""Tests for the Slice 5 CheckpointSystem.

Deterministic — tmp_path SQLite, no live API. Click commands exercised via
`click.testing.CliRunner`. Coverage:

  TestRequest               — request writes the right event + transitions
                              the workflow to PAUSED; preconditions (unknown
                              workflow, terminal workflow, already-pending)
                              raise CheckpointError.
  TestResolveApprove        — approve writes the approval event with a
                              checkpoint_id back-reference and transitions
                              workflow PAUSED -> RUNNING.
  TestResolveReject         — reject writes the rejection event and
                              transitions workflow PAUSED -> CANCELLED;
                              finished_at is stamped.
  TestQueries               — list_pending filters resolved out;
                              is_workflow_paused_for_checkpoint mirrors;
                              get_checkpoint returns resolved or None.
  TestResolveErrors         — approve/reject of unknown or already-resolved
                              ids raise CheckpointError.
  TestCLI                   — list / show / approve / reject end to end;
                              unknown / already-resolved ids surface as a
                              ClickException with a clean non-zero exit;
                              decision-support footer present on pending
                              show output.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner

from agent_habitat.checkpoint import (
    Checkpoint,
    CheckpointError,
    CheckpointResolution,
    approve_checkpoint,
    get_checkpoint,
    is_workflow_paused_for_checkpoint,
    list_pending_checkpoints,
    reject_checkpoint,
    request_checkpoint,
)
from agent_habitat.cli import DECISION_SUPPORT_FOOTER, main
from agent_habitat.observability.events import EventType
from agent_habitat.state import (
    EventLevel,
    Workflow,
    WorkflowStatus,
    init_db,
    insert_workflow,
    load_events,
    load_workflow,
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


def _seed_workflow(
    conn: sqlite3.Connection,
    *,
    status: WorkflowStatus = WorkflowStatus.RUNNING,
    workflow_type: str = "lead_enrichment",
) -> Workflow:
    wf = Workflow(
        id=new_workflow_id(),
        workflow_type=workflow_type,
        status=status,
        started_at="2026-05-14T00:00:00+00:00",
    )
    insert_workflow(conn, wf)
    return wf


def _request(
    conn: sqlite3.Connection,
    wf_id: str,
    **overrides: object,
) -> Checkpoint:
    kwargs: dict[str, object] = {
        "workflow_id": wf_id,
        "action": "send_outreach",
        "summary": "Send the drafted outreach email to lead #42.",
    }
    kwargs.update(overrides)
    return request_checkpoint(conn, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------


class TestRequest:
    def test_writes_checkpoint_event_and_pauses_workflow(self, conn: sqlite3.Connection) -> None:
        wf = _seed_workflow(conn)
        cp = _request(
            conn,
            wf.id,
            proposed_payload={"to": "lead@example.com", "body": "..."},
            requested_by="drafter",
            step_id=None,
        )

        assert cp.is_pending
        assert cp.workflow_id == wf.id
        assert cp.action == "send_outreach"
        assert cp.requested_by == "drafter"
        assert cp.proposed_payload == {"to": "lead@example.com", "body": "..."}
        assert cp.resolution is None

        # Workflow flipped to PAUSED.
        reloaded = load_workflow(conn, wf.id)
        assert reloaded is not None
        assert reloaded.status is WorkflowStatus.PAUSED

        # Event row carries the canonical taxonomy + CHECKPOINT level.
        events = load_events(conn, wf.id)
        assert len(events) == 1
        evt = events[0]
        assert evt.level is EventLevel.CHECKPOINT
        assert evt.id == cp.id
        assert evt.structured_data is not None
        assert evt.structured_data["event_type"] == EventType.CHECKPOINT_REQUESTED.value
        assert evt.structured_data["action"] == "send_outreach"
        assert evt.structured_data["requested_by"] == "drafter"
        assert "checkpoint requested" in evt.message

    def test_optional_fields_omitted_when_unset(self, conn: sqlite3.Connection) -> None:
        wf = _seed_workflow(conn)
        cp = _request(conn, wf.id)
        events = load_events(conn, wf.id)
        payload = events[0].structured_data or {}
        assert "proposed_payload" not in payload
        assert "requested_by" not in payload
        assert cp.proposed_payload is None
        assert cp.requested_by is None

    def test_request_on_unknown_workflow_raises(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(CheckpointError, match="unknown workflow"):
            _request(conn, "does-not-exist")

    @pytest.mark.parametrize(
        "terminal",
        [WorkflowStatus.COMPLETED, WorkflowStatus.FAILED, WorkflowStatus.CANCELLED],
    )
    def test_request_on_terminal_workflow_raises(
        self, conn: sqlite3.Connection, terminal: WorkflowStatus
    ) -> None:
        wf = _seed_workflow(conn, status=terminal)
        with pytest.raises(CheckpointError, match="terminal workflow"):
            _request(conn, wf.id)

    def test_second_pending_request_is_rejected(self, conn: sqlite3.Connection) -> None:
        wf = _seed_workflow(conn)
        _request(conn, wf.id)
        with pytest.raises(CheckpointError, match="already has a pending checkpoint"):
            _request(conn, wf.id, action="send_other")


# ---------------------------------------------------------------------------
# Approve
# ---------------------------------------------------------------------------


class TestResolveApprove:
    def test_approve_transitions_workflow_back_to_running(self, conn: sqlite3.Connection) -> None:
        wf = _seed_workflow(conn)
        cp = _request(conn, wf.id)

        resolved = approve_checkpoint(conn, cp.id, reviewer="joe", note="lead profile checks out")

        assert resolved.resolution is CheckpointResolution.APPROVED
        assert resolved.reviewer == "joe"
        assert resolved.note == "lead profile checks out"
        assert resolved.resolved_at is not None

        reloaded = load_workflow(conn, wf.id)
        assert reloaded is not None
        assert reloaded.status is WorkflowStatus.RUNNING
        # finished_at remains None — approval is NOT terminal.
        assert reloaded.finished_at is None

        events = load_events(conn, wf.id)
        # Request + approval.
        assert len(events) == 2
        approval = events[-1]
        assert approval.level is EventLevel.APPROVAL
        assert approval.structured_data is not None
        assert approval.structured_data["event_type"] == EventType.CHECKPOINT_APPROVED.value
        assert approval.structured_data["checkpoint_id"] == cp.id
        assert approval.structured_data["reviewer"] == "joe"
        assert approval.structured_data["note"] == "lead profile checks out"


# ---------------------------------------------------------------------------
# Reject
# ---------------------------------------------------------------------------


class TestResolveReject:
    def test_reject_cancels_workflow_and_stamps_finished_at(self, conn: sqlite3.Connection) -> None:
        wf = _seed_workflow(conn)
        cp = _request(conn, wf.id)

        resolved = reject_checkpoint(conn, cp.id, reviewer="joe", reason="lead does not match ICP")

        assert resolved.resolution is CheckpointResolution.REJECTED
        assert resolved.note == "lead does not match ICP"

        reloaded = load_workflow(conn, wf.id)
        assert reloaded is not None
        assert reloaded.status is WorkflowStatus.CANCELLED
        assert reloaded.finished_at is not None  # terminal stamp
        assert reloaded.finished_at == resolved.resolved_at

        events = load_events(conn, wf.id)
        assert len(events) == 2
        rejection = events[-1]
        assert rejection.level is EventLevel.APPROVAL
        assert rejection.structured_data is not None
        assert rejection.structured_data["event_type"] == EventType.CHECKPOINT_REJECTED.value
        assert rejection.structured_data["checkpoint_id"] == cp.id
        assert rejection.structured_data["note"] == "lead does not match ICP"


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


class TestQueries:
    def test_list_pending_returns_open_only(self, conn: sqlite3.Connection) -> None:
        wf_a = _seed_workflow(conn, workflow_type="lead_enrichment")
        wf_b = _seed_workflow(conn, workflow_type="url_summarizer")

        cp_a = _request(conn, wf_a.id, action="send_outreach_a")
        cp_b = _request(conn, wf_b.id, action="send_outreach_b")
        # Resolve A; B remains pending.
        approve_checkpoint(conn, cp_a.id, reviewer="joe")

        pending = list_pending_checkpoints(conn)
        assert [c.id for c in pending] == [cp_b.id]

    def test_list_pending_filters_by_workflow(self, conn: sqlite3.Connection) -> None:
        wf_a = _seed_workflow(conn)
        wf_b = _seed_workflow(conn)
        cp_a = _request(conn, wf_a.id)
        _request(conn, wf_b.id)
        result = list_pending_checkpoints(conn, wf_a.id)
        assert [c.id for c in result] == [cp_a.id]

    def test_is_workflow_paused_mirrors_pending(self, conn: sqlite3.Connection) -> None:
        wf = _seed_workflow(conn)
        assert is_workflow_paused_for_checkpoint(conn, wf.id) is False

        cp = _request(conn, wf.id)
        assert is_workflow_paused_for_checkpoint(conn, wf.id) is True

        approve_checkpoint(conn, cp.id, reviewer="joe")
        assert is_workflow_paused_for_checkpoint(conn, wf.id) is False

    def test_get_checkpoint_unknown_id_returns_none(self, conn: sqlite3.Connection) -> None:
        assert get_checkpoint(conn, 9999) is None

    def test_get_checkpoint_returns_resolved_record(self, conn: sqlite3.Connection) -> None:
        wf = _seed_workflow(conn)
        cp = _request(conn, wf.id)
        reject_checkpoint(conn, cp.id, reviewer="joe", reason="not now")
        loaded = get_checkpoint(conn, cp.id)
        assert loaded is not None
        assert loaded.is_pending is False
        assert loaded.resolution is CheckpointResolution.REJECTED
        assert loaded.reviewer == "joe"
        assert loaded.note == "not now"

    def test_get_checkpoint_ignores_non_checkpoint_event_id(self, conn: sqlite3.Connection) -> None:
        # An events row that isn't a checkpoint.requested must not be
        # returned by get_checkpoint (defends against id collisions if a
        # caller passes a wrong number).
        wf = _seed_workflow(conn)
        from agent_habitat.observability.events import emit_event

        unrelated_id = emit_event(
            conn,
            workflow_id=wf.id,
            event_type=EventType.WORKFLOW_NOTE,
            level=EventLevel.INFO,
            message="hello",
        )
        assert get_checkpoint(conn, unrelated_id) is None


# ---------------------------------------------------------------------------
# Resolve errors
# ---------------------------------------------------------------------------


class TestResolveErrors:
    def test_approve_unknown_id_raises(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(CheckpointError, match="unknown checkpoint id"):
            approve_checkpoint(conn, 9999, reviewer="joe")

    def test_reject_unknown_id_raises(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(CheckpointError, match="unknown checkpoint id"):
            reject_checkpoint(conn, 9999, reviewer="joe")

    def test_double_approve_raises(self, conn: sqlite3.Connection) -> None:
        wf = _seed_workflow(conn)
        cp = _request(conn, wf.id)
        approve_checkpoint(conn, cp.id, reviewer="joe")
        with pytest.raises(CheckpointError, match="already resolved"):
            approve_checkpoint(conn, cp.id, reviewer="joe")

    def test_approve_after_reject_raises(self, conn: sqlite3.Connection) -> None:
        wf = _seed_workflow(conn)
        cp = _request(conn, wf.id)
        reject_checkpoint(conn, cp.id, reviewer="joe")
        with pytest.raises(CheckpointError, match="already resolved"):
            approve_checkpoint(conn, cp.id, reviewer="joe")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCLI:
    def test_list_empty(self, db_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["checkpoint", "--db", str(db_path), "list"])
        assert result.exit_code == 0, result.output
        assert "No pending checkpoints." in result.output

    def test_list_shows_pending(self, db_path: Path) -> None:
        conn = init_db(db_path)
        try:
            wf = _seed_workflow(conn)
            cp = _request(
                conn,
                wf.id,
                action="send_outreach",
                summary="Send the drafted email to lead #42.",
                requested_by="drafter",
            )
        finally:
            conn.close()

        runner = CliRunner()
        result = runner.invoke(main, ["checkpoint", "--db", str(db_path), "list"])
        assert result.exit_code == 0, result.output
        assert "1 pending checkpoint" in result.output
        assert f"[{cp.id}]" in result.output
        assert "send_outreach" in result.output
        assert wf.id in result.output
        assert "drafter" in result.output

    def test_show_pending_renders_decision_support_footer(self, db_path: Path) -> None:
        conn = init_db(db_path)
        try:
            wf = _seed_workflow(conn)
            cp = _request(
                conn,
                wf.id,
                proposed_payload={"to": "lead@example.com"},
            )
        finally:
            conn.close()

        runner = CliRunner()
        result = runner.invoke(main, ["checkpoint", "--db", str(db_path), "show", str(cp.id)])
        assert result.exit_code == 0, result.output
        assert f"Checkpoint #{cp.id}" in result.output
        assert "PENDING" in result.output
        # Proposed payload rendered as readable JSON.
        assert '"to": "lead@example.com"' in result.output
        assert DECISION_SUPPORT_FOOTER in result.output

    def test_show_unknown_id_fails_cleanly(self, db_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["checkpoint", "--db", str(db_path), "show", "9999"])
        assert result.exit_code != 0
        assert "unknown checkpoint id: 9999" in result.output

    def test_approve_command(self, db_path: Path) -> None:
        conn = init_db(db_path)
        try:
            wf = _seed_workflow(conn)
            cp = _request(conn, wf.id)
        finally:
            conn.close()

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "checkpoint",
                "--db",
                str(db_path),
                "approve",
                str(cp.id),
                "--reviewer",
                "joe",
                "--note",
                "looks good",
            ],
        )
        assert result.exit_code == 0, result.output
        assert f"Approved checkpoint {cp.id}" in result.output

        conn = init_db(db_path)
        try:
            reloaded = load_workflow(conn, wf.id)
            assert reloaded is not None
            assert reloaded.status is WorkflowStatus.RUNNING
            loaded = get_checkpoint(conn, cp.id)
            assert loaded is not None
            assert loaded.resolution is CheckpointResolution.APPROVED
            assert loaded.reviewer == "joe"
            assert loaded.note == "looks good"
        finally:
            conn.close()

    def test_reject_command(self, db_path: Path) -> None:
        conn = init_db(db_path)
        try:
            wf = _seed_workflow(conn)
            cp = _request(conn, wf.id)
        finally:
            conn.close()

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "checkpoint",
                "--db",
                str(db_path),
                "reject",
                str(cp.id),
                "--reviewer",
                "joe",
                "--reason",
                "not ICP",
            ],
        )
        assert result.exit_code == 0, result.output
        assert f"Rejected checkpoint {cp.id}" in result.output

        conn = init_db(db_path)
        try:
            reloaded = load_workflow(conn, wf.id)
            assert reloaded is not None
            assert reloaded.status is WorkflowStatus.CANCELLED
            loaded = get_checkpoint(conn, cp.id)
            assert loaded is not None
            assert loaded.resolution is CheckpointResolution.REJECTED
            assert loaded.note == "not ICP"
        finally:
            conn.close()

    def test_approve_unknown_id_fails_cleanly(self, db_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "checkpoint",
                "--db",
                str(db_path),
                "approve",
                "9999",
                "--reviewer",
                "joe",
            ],
        )
        assert result.exit_code != 0
        assert "unknown checkpoint id: 9999" in result.output

    def test_approve_already_resolved_fails_cleanly(self, db_path: Path) -> None:
        conn = init_db(db_path)
        try:
            wf = _seed_workflow(conn)
            cp = _request(conn, wf.id)
            approve_checkpoint(conn, cp.id, reviewer="joe")
        finally:
            conn.close()

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "checkpoint",
                "--db",
                str(db_path),
                "reject",
                str(cp.id),
                "--reviewer",
                "joe",
            ],
        )
        assert result.exit_code != 0
        assert "already resolved" in result.output

    def test_approve_requires_reviewer(self, db_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["checkpoint", "--db", str(db_path), "approve", "1"],
        )
        assert result.exit_code != 0
        assert "reviewer" in result.output.lower()

    def test_show_resolved_renders_resolution_block(self, db_path: Path) -> None:
        conn = init_db(db_path)
        try:
            wf = _seed_workflow(conn)
            cp = _request(conn, wf.id)
            approve_checkpoint(conn, cp.id, reviewer="joe", note="approved after review")
        finally:
            conn.close()

        runner = CliRunner()
        result = runner.invoke(main, ["checkpoint", "--db", str(db_path), "show", str(cp.id)])
        assert result.exit_code == 0, result.output
        assert "APPROVED" in result.output
        assert "joe" in result.output
        assert "approved after review" in result.output
        # Decision-support footer is for pending only.
        assert DECISION_SUPPORT_FOOTER not in result.output


# ---------------------------------------------------------------------------
# Sanity: payload round-trips through SQLite JSON exactly
# ---------------------------------------------------------------------------


def test_proposed_payload_json_roundtrip(conn: sqlite3.Connection) -> None:
    wf = _seed_workflow(conn)
    payload = {
        "to": "lead@example.com",
        "subject": "intro",
        "tags": ["icp-match", "warm"],
        "metadata": {"score": 0.87},
    }
    cp = _request(conn, wf.id, proposed_payload=payload)
    events = load_events(conn, wf.id)
    raw = events[0].structured_data or {}
    assert raw.get("proposed_payload") == payload
    # Re-loading via get_checkpoint reconstitutes identically.
    loaded = get_checkpoint(conn, cp.id)
    assert loaded is not None
    assert loaded.proposed_payload == payload
    # And the dump is valid JSON (no exotic encoding).
    json.dumps(loaded.proposed_payload)
