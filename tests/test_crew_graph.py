"""Deterministic tests for the Phase 2 Slice 6 LangGraph crew orchestrator.

Coverage:

  TestCrewState              — TypedDict structural sanity.
  TestGraphCompile           — `build_crew_graph` compiles + has the
                               expected node set.
  TestRunCrewHappyPath       — initial run reaches PAUSED at the human
                               checkpoint; step rows for researcher /
                               extractor / scorer present, no drafter
                               row yet; pending CHECKPOINT_REQUESTED
                               event exists; workflow.status=PAUSED.
  TestRunCrewGated           — scorer below floor → terminate_no_draft
                               → COMPLETED with terminate_reason set;
                               no drafter LLM call; no checkpoint row.
  TestRunCrewApproved        — approve_checkpoint then resume_crew →
                               COMPLETED + draft + drafter step row.
                               Confirms resume idempotency (no second
                               CHECKPOINT_REQUESTED row written).
  TestRunCrewRejected        — reject_checkpoint then resume_crew →
                               CANCELLED; graph NOT re-invoked
                               (drafter LLM mock never called).
  TestInfrastructureError    — researcher raises → workflow FAILED with
                               error_step="researcher" and a
                               workflow.failed event.
  TestReconciliationGuard    — orphan step with live SqliteSaver
                               checkpoint NOT reconciled; orphan
                               without IS reconciled.
  TestCrossSessionResume     — file-backed DB, close all connections,
                               reopen fresh ones, approve + resume to
                               completion.
  TestProjectionAndAudit     — workflow.completed event carries
                               produced_draft / terminate_reason; step
                               rows carry the expected projections;
                               approve_drafter checkpoint payload
                               includes the ScoredCompany.

NO live API call here; the live smoke is at the bottom guarded by
`@pytest.mark.live` and `ANTHROPIC_API_KEY` (one happy-path crew run
end-to-end through approval + resume, one cross-session resume
re-using fresh connections).
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from langgraph.checkpoint.sqlite import SqliteSaver

from agent_habitat.agents import drafter as drafter_mod
from agent_habitat.agents import extractor as extractor_mod
from agent_habitat.agents import researcher as researcher_mod
from agent_habitat.agents import scorer as scorer_mod
from agent_habitat.agents.models import (
    PROFILE_FIELD_NAMES,
)
from agent_habitat.checkpoint import (
    CheckpointResolution,
    approve_checkpoint,
    list_pending_checkpoints,
    reject_checkpoint,
)
from agent_habitat.cli import main
from agent_habitat.llm import Citation, LLMResult
from agent_habitat.orchestration.crew_graph import (
    CHECKPOINT_ACTION_APPROVE_DRAFTER,
    STEP_INDEX_DRAFTER,
    STEP_INDEX_EXTRACTOR,
    STEP_INDEX_RESEARCHER,
    STEP_INDEX_SCORER,
    build_crew_graph,
    resume_crew,
    run_crew,
)
from agent_habitat.orchestration.crew_state import (
    TERMINATE_REASON_REJECTED,
    TERMINATE_REASON_SCORE_GATED,
    CrewState,
)
from agent_habitat.scoring import load_rubric
from agent_habitat.state import (
    StepStatus,
    Workflow,
    WorkflowStatus,
    has_langgraph_checkpoint,
    init_db,
    insert_step,
    insert_workflow,
    load_events,
    load_steps,
    load_workflow,
    new_workflow_id,
    reconcile_orphan_steps,
)
from agent_habitat.state.models import WorkflowStep


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
def log_root(tmp_path: Path) -> Path:
    return tmp_path / "logs"


@pytest.fixture
def saver_conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Separate sqlite3.Connection wired to SqliteSaver. Closes on teardown."""
    sc = sqlite3.connect(db_path, check_same_thread=False)
    try:
        yield sc
    finally:
        sc.close()


@pytest.fixture
def saver(saver_conn: sqlite3.Connection) -> SqliteSaver:
    s = SqliteSaver(saver_conn)
    s.setup()
    return s


# ---------------------------------------------------------------------------
# Stub LLM payloads — mirrors test_drafter.py's helpers so the crew graph
# can be exercised end-to-end against canned responses.
# ---------------------------------------------------------------------------


_SIGNAL_TEXT = (
    "Acme Corp is a fintech startup with about 500 employees. Their stack uses "
    "Python and AWS. They recently raised a $50M Series B. Jane Doe is the CTO."
)


def _researcher_llm() -> LLMResult:
    return LLMResult(
        content="A short narrative.",
        model="claude-haiku-4-5-20251001",
        input_tokens=300,
        output_tokens=80,
        cost_usd=0.001,
        jsonl_ref="data/logs/2026-05-14/wf-r.jsonl:1",
        stop_reason="end_turn",
        web_searches=1,
        citations=[
            Citation(
                cited_text=_SIGNAL_TEXT,
                source_url="https://example.com/acme",
                source_title="Acme overview",
            )
        ],
    )


def _extractor_llm() -> LLMResult:
    body = json.dumps(
        {
            "size": {
                "values": ["~500 employees"],
                "source_spans": [{"signal_index": 0, "quote": _SIGNAL_TEXT}],
            },
            "industry": {
                "values": ["Fintech"],
                "source_spans": [{"signal_index": 0, "quote": _SIGNAL_TEXT}],
            },
            "tech_stack": {
                "values": ["Python", "AWS"],
                "source_spans": [{"signal_index": 0, "quote": _SIGNAL_TEXT}],
            },
            "recent_news": {
                "values": ["$50M Series B"],
                "source_spans": [{"signal_index": 0, "quote": _SIGNAL_TEXT}],
            },
            "decision_makers": {
                "values": ["Jane Doe (CTO)"],
                "source_spans": [{"signal_index": 0, "quote": _SIGNAL_TEXT}],
            },
        }
    )
    return LLMResult(
        content=body,
        model="claude-sonnet-4-6",
        input_tokens=400,
        output_tokens=200,
        cost_usd=0.005,
        jsonl_ref="data/logs/2026-05-14/wf-e.jsonl:1",
        stop_reason="end_turn",
        web_searches=0,
        citations=[],
    )


def _scorer_llm(score: float = 5.0) -> LLMResult:
    body = json.dumps(
        {
            "dimensions": [
                {
                    "field": name,
                    "score": score,
                    "grounded_quote": _SIGNAL_TEXT,
                    "reasoning": f"Strong signal for {name}.",
                }
                for name in PROFILE_FIELD_NAMES
            ]
        }
    )
    return LLMResult(
        content=body,
        model="claude-sonnet-4-6",
        input_tokens=600,
        output_tokens=300,
        cost_usd=0.008,
        jsonl_ref="data/logs/2026-05-14/wf-s.jsonl:1",
        stop_reason="end_turn",
        web_searches=0,
        citations=[],
    )


_GOOD_PROSE = (
    "Hi there,\n\n"
    "I noticed your work as a fintech startup, particularly the recent "
    "$50M Series B. Your stack on Python and AWS is a strong overlap with "
    "the kind of compliance-grade integrations my team builds.\n\n"
    "If a 30-minute conversation about regulated-industry agent patterns "
    "would be useful, I'd be happy to set one up."
)


def _drafter_llm() -> LLMResult:
    body = json.dumps(
        {
            "prose": _GOOD_PROSE,
            "claims": [
                {"text": "fintech startup", "supporting_dimension": "industry"},
                {"text": "Python and AWS", "supporting_dimension": "tech_stack"},
                {"text": "$50M Series B", "supporting_dimension": "recent_news"},
            ],
        }
    )
    return LLMResult(
        content=body,
        model="claude-opus-4-7",
        input_tokens=1500,
        output_tokens=400,
        cost_usd=0.054,
        jsonl_ref="data/logs/2026-05-14/wf-d.jsonl:1",
        stop_reason="end_turn",
        web_searches=0,
        citations=[],
    )


_RUBRIC_TOML_PASSING = (
    "[defaults]\n"
    "floor = 0.0\n"
    "min_coverage = 0.0\n"
    "tier_a_min = 80.0\n"
    "tier_b_min = 65.0\n"
    "tier_c_min = 0.0\n"
    'missing_data_policy = "renormalise"\n'
    "\n"
    "[dimensions.size]\n"
    'field = "size"\nweight = 0.30\nprose = "Score size."\n\n'
    "[dimensions.industry]\n"
    'field = "industry"\nweight = 0.20\nprose = "Score industry."\n\n'
    "[dimensions.tech_stack]\n"
    'field = "tech_stack"\nweight = 0.20\nprose = "Score stack."\n\n'
    "[dimensions.recent_news]\n"
    'field = "recent_news"\nweight = 0.20\nprose = "Score news."\n\n'
    "[dimensions.decision_makers]\n"
    'field = "decision_makers"\nweight = 0.10\nprose = "Score people."\n'
)


_RUBRIC_TOML_HIGH_FLOOR = (
    _RUBRIC_TOML_PASSING.replace("floor = 0.0", "floor = 99.0")
    .replace("tier_a_min = 80.0", "tier_a_min = 99.9")
    .replace("tier_b_min = 65.0", "tier_b_min = 99.5")
    .replace("tier_c_min = 0.0", "tier_c_min = 99.0")
)


def _write_rubric(tmp_path: Path, body: str = _RUBRIC_TOML_PASSING) -> Path:
    p = tmp_path / "rubric.toml"
    p.write_text(body)
    return p


def _patch_all_agents(
    *,
    researcher_result: LLMResult | Exception | None = None,
    extractor_result: LLMResult | Exception | None = None,
    scorer_result: LLMResult | Exception | None = None,
    drafter_result: LLMResult | Exception | None = None,
) -> Any:
    """Context manager stacking patches on all four agent modules' `complete`.

    `*_result` items are returned as the value; if `Exception` instance, raised.
    `None` means: don't patch (test should not exercise that agent).
    """
    import contextlib

    @contextlib.contextmanager
    def _stack() -> Iterator[None]:
        with contextlib.ExitStack() as st:
            for mod, val in (
                (researcher_mod, researcher_result),
                (extractor_mod, extractor_result),
                (scorer_mod, scorer_result),
                (drafter_mod, drafter_result),
            ):
                if val is None:
                    continue
                if isinstance(val, Exception):
                    st.enter_context(patch.object(mod, "complete", side_effect=val))
                else:
                    st.enter_context(patch.object(mod, "complete", return_value=val))
            yield

    return _stack()


# ---------------------------------------------------------------------------
# TestCrewState
# ---------------------------------------------------------------------------


class TestCrewState:
    def test_total_false_allows_empty_initial_state(self) -> None:
        # CrewState is total=False, so {} is a valid CrewState value at the
        # type-checker level. We just construct one and confirm dict-style
        # access works (the orchestrator entry point relies on this).
        state: CrewState = {}
        assert state.get("workflow_id") is None
        state2: CrewState = {"workflow_id": "abc", "company_name": "Acme"}
        assert state2["workflow_id"] == "abc"
        assert state2["company_name"] == "Acme"

    def test_terminate_reason_constants_distinct(self) -> None:
        from agent_habitat.orchestration.crew_state import (
            TERMINATE_REASON_COVERAGE_GATED,
            TERMINATE_REASON_REJECTED as REJECTED,
            TERMINATE_REASON_SCORE_GATED as SCORE,
        )

        assert len({TERMINATE_REASON_COVERAGE_GATED, REJECTED, SCORE}) == 3


# ---------------------------------------------------------------------------
# TestGraphCompile
# ---------------------------------------------------------------------------


class TestGraphCompile:
    def test_graph_has_expected_nodes(
        self,
        conn: sqlite3.Connection,
        tmp_path: Path,
        saver: SqliteSaver,
    ) -> None:
        rubric = load_rubric(_write_rubric(tmp_path))
        graph = build_crew_graph(conn, rubric, saver=saver)
        # The compiled graph exposes its node names via `get_graph().nodes`.
        node_names = set(graph.get_graph().nodes.keys())
        for required in {
            "researcher",
            "extractor",
            "scorer",
            "request_drafter_approval",
            "drafter",
            "terminate_no_draft",
        }:
            assert required in node_names, f"missing node {required!r}; got {node_names}"


# ---------------------------------------------------------------------------
# TestRunCrewHappyPath — initial run reaches PAUSED at the human checkpoint.
# ---------------------------------------------------------------------------


class TestRunCrewHappyPath:
    def test_initial_run_pauses_at_checkpoint(
        self,
        conn: sqlite3.Connection,
        saver: SqliteSaver,
        tmp_path: Path,
        log_root: Path,
    ) -> None:
        rubric = load_rubric(_write_rubric(tmp_path))
        with _patch_all_agents(
            researcher_result=_researcher_llm(),
            extractor_result=_extractor_llm(),
            scorer_result=_scorer_llm(),
            # drafter NOT patched — the graph should pause before drafter runs.
        ):
            result = run_crew(
                conn,
                company_name="Acme Corp",
                rubric=rubric,
                saver=saver,
                log_root=log_root,
                workflow_id="wf-crew-pause",
            )

        assert result.status is WorkflowStatus.PAUSED
        assert result.pending_checkpoint_id is not None
        assert result.draft is None
        # Workflow row status is PAUSED (moved by CheckpointSystem).
        wf = load_workflow(conn, "wf-crew-pause")
        assert wf is not None
        assert wf.status is WorkflowStatus.PAUSED
        # Three step rows: researcher, extractor, scorer (no drafter yet).
        steps = load_steps(conn, "wf-crew-pause")
        agent_names = sorted(s.agent_name for s in steps)
        assert agent_names == ["extractor", "researcher", "scorer"]
        assert all(s.status is StepStatus.COMPLETED for s in steps)
        # A pending checkpoint row exists.
        pending = list_pending_checkpoints(conn, "wf-crew-pause")
        assert len(pending) == 1
        assert pending[0].action == CHECKPOINT_ACTION_APPROVE_DRAFTER

    def test_step_indices_are_monotonic_and_match_constants(
        self,
        conn: sqlite3.Connection,
        saver: SqliteSaver,
        tmp_path: Path,
        log_root: Path,
    ) -> None:
        rubric = load_rubric(_write_rubric(tmp_path))
        with _patch_all_agents(
            researcher_result=_researcher_llm(),
            extractor_result=_extractor_llm(),
            scorer_result=_scorer_llm(),
        ):
            run_crew(
                conn,
                company_name="Acme Corp",
                rubric=rubric,
                saver=saver,
                log_root=log_root,
                workflow_id="wf-crew-indices",
            )
        steps = sorted(load_steps(conn, "wf-crew-indices"), key=lambda s: s.step_index)
        idx_by_agent = {s.agent_name: s.step_index for s in steps}
        assert idx_by_agent["researcher"] == STEP_INDEX_RESEARCHER
        assert idx_by_agent["extractor"] == STEP_INDEX_EXTRACTOR
        assert idx_by_agent["scorer"] == STEP_INDEX_SCORER


# ---------------------------------------------------------------------------
# TestRunCrewGated — score-gated path → terminate_no_draft.
# ---------------------------------------------------------------------------


class TestRunCrewGated:
    def test_below_floor_routes_to_terminate_no_draft(
        self,
        conn: sqlite3.Connection,
        saver: SqliteSaver,
        tmp_path: Path,
        log_root: Path,
    ) -> None:
        rubric_path = _write_rubric(tmp_path, _RUBRIC_TOML_HIGH_FLOOR)
        rubric = load_rubric(rubric_path)
        drafter_call_count = {"n": 0}

        def _drafter_should_not_be_called(*args: Any, **kwargs: Any) -> LLMResult:
            drafter_call_count["n"] += 1
            return _drafter_llm()

        with (
            patch.object(researcher_mod, "complete", return_value=_researcher_llm()),
            patch.object(extractor_mod, "complete", return_value=_extractor_llm()),
            # Scorer returns LOW score (1.0 per dimension); rubric floor=99.
            patch.object(scorer_mod, "complete", return_value=_scorer_llm(score=1.0)),
            patch.object(drafter_mod, "complete", side_effect=_drafter_should_not_be_called),
        ):
            result = run_crew(
                conn,
                company_name="Acme Corp",
                rubric=rubric,
                saver=saver,
                log_root=log_root,
                workflow_id="wf-crew-gated",
            )

        assert result.status is WorkflowStatus.COMPLETED
        assert result.draft is None
        assert result.terminate_reason == TERMINATE_REASON_SCORE_GATED
        # Drafter LLM never called.
        assert drafter_call_count["n"] == 0
        # Workflow row finalised COMPLETED.
        wf = load_workflow(conn, "wf-crew-gated")
        assert wf is not None
        assert wf.status is WorkflowStatus.COMPLETED
        assert wf.finished_at is not None
        # No pending checkpoint, no drafter step row.
        assert list_pending_checkpoints(conn, "wf-crew-gated") == []
        steps = load_steps(conn, "wf-crew-gated")
        assert "drafter" not in {s.agent_name for s in steps}
        # workflow.note event names the gating reason.
        events = load_events(conn, "wf-crew-gated")
        note = next(
            (e for e in events if (e.structured_data or {}).get("event_type") == "workflow.note"),
            None,
        )
        assert note is not None
        assert (note.structured_data or {})["terminate_reason"] == TERMINATE_REASON_SCORE_GATED


# ---------------------------------------------------------------------------
# TestRunCrewApproved — approve_checkpoint + resume_crew → COMPLETED + draft.
# Confirms resume idempotency (no duplicate CHECKPOINT_REQUESTED row).
# ---------------------------------------------------------------------------


class TestRunCrewApproved:
    def test_resume_after_approve_runs_drafter_and_completes(
        self,
        conn: sqlite3.Connection,
        saver: SqliteSaver,
        tmp_path: Path,
        log_root: Path,
    ) -> None:
        rubric = load_rubric(_write_rubric(tmp_path))
        with _patch_all_agents(
            researcher_result=_researcher_llm(),
            extractor_result=_extractor_llm(),
            scorer_result=_scorer_llm(),
        ):
            paused = run_crew(
                conn,
                company_name="Acme Corp",
                rubric=rubric,
                saver=saver,
                log_root=log_root,
                workflow_id="wf-crew-approve",
            )
        assert paused.status is WorkflowStatus.PAUSED
        assert paused.pending_checkpoint_id is not None

        # Approve via the existing CheckpointSystem CLI surface.
        cp = approve_checkpoint(conn, paused.pending_checkpoint_id, reviewer="test-runner")
        assert cp.resolution is CheckpointResolution.APPROVED

        # Resume — drafter runs.
        with _patch_all_agents(
            drafter_result=_drafter_llm(),
        ):
            resumed = resume_crew(
                conn,
                workflow_id="wf-crew-approve",
                rubric=rubric,
                saver=saver,
                log_root=log_root,
            )

        assert resumed.status is WorkflowStatus.COMPLETED
        assert resumed.draft is not None
        assert resumed.draft.claim_count == 3
        wf = load_workflow(conn, "wf-crew-approve")
        assert wf is not None
        assert wf.status is WorkflowStatus.COMPLETED
        # All four step rows present.
        steps = sorted(load_steps(conn, "wf-crew-approve"), key=lambda s: s.step_index)
        assert [s.agent_name for s in steps] == [
            "researcher",
            "extractor",
            "scorer",
            "drafter",
        ]
        assert steps[-1].step_index == STEP_INDEX_DRAFTER
        # Resume idempotency: exactly one CHECKPOINT_REQUESTED row for this
        # workflow's approve_drafter action (the short-circuit prevented
        # a second `request_checkpoint` call on resume re-entry).
        events = load_events(conn, "wf-crew-approve")
        requested = [
            e
            for e in events
            if (e.structured_data or {}).get("event_type") == "checkpoint.requested"
            and (e.structured_data or {}).get("action") == CHECKPOINT_ACTION_APPROVE_DRAFTER
        ]
        assert len(requested) == 1


# ---------------------------------------------------------------------------
# TestRunCrewRejected — reject_checkpoint then resume_crew → CANCELLED.
# Graph not re-invoked (drafter mock never called).
# ---------------------------------------------------------------------------


class TestRunCrewRejected:
    def test_resume_after_reject_returns_cancelled_without_invoking_graph(
        self,
        conn: sqlite3.Connection,
        saver: SqliteSaver,
        tmp_path: Path,
        log_root: Path,
    ) -> None:
        rubric = load_rubric(_write_rubric(tmp_path))
        with _patch_all_agents(
            researcher_result=_researcher_llm(),
            extractor_result=_extractor_llm(),
            scorer_result=_scorer_llm(),
        ):
            paused = run_crew(
                conn,
                company_name="Acme Corp",
                rubric=rubric,
                saver=saver,
                log_root=log_root,
                workflow_id="wf-crew-reject",
            )
        assert paused.status is WorkflowStatus.PAUSED
        assert paused.pending_checkpoint_id is not None

        cp = reject_checkpoint(
            conn,
            paused.pending_checkpoint_id,
            reviewer="test-runner",
            reason="not a fit right now",
        )
        assert cp.resolution is CheckpointResolution.REJECTED
        # CheckpointSystem moves the workflow to CANCELLED.
        wf = load_workflow(conn, "wf-crew-reject")
        assert wf is not None
        assert wf.status is WorkflowStatus.CANCELLED

        # Resume should NOT re-invoke the graph (drafter mock would explode).
        def _drafter_must_not_be_called(*args: Any, **kwargs: Any) -> LLMResult:
            raise AssertionError("drafter LLM should not be called after rejection")

        with patch.object(drafter_mod, "complete", side_effect=_drafter_must_not_be_called):
            resumed = resume_crew(
                conn,
                workflow_id="wf-crew-reject",
                rubric=rubric,
                saver=saver,
                log_root=log_root,
            )

        assert resumed.status is WorkflowStatus.CANCELLED
        assert resumed.terminate_reason == TERMINATE_REASON_REJECTED
        # No drafter step row was added.
        steps = load_steps(conn, "wf-crew-reject")
        assert "drafter" not in {s.agent_name for s in steps}


# ---------------------------------------------------------------------------
# TestInfrastructureError — researcher raises → workflow FAILED + error_step.
# ---------------------------------------------------------------------------


class TestInfrastructureError:
    def test_researcher_raise_finalises_workflow_failed(
        self,
        conn: sqlite3.Connection,
        saver: SqliteSaver,
        tmp_path: Path,
        log_root: Path,
    ) -> None:
        rubric = load_rubric(_write_rubric(tmp_path))
        with patch.object(
            researcher_mod,
            "complete",
            side_effect=RuntimeError("API 500"),
        ):
            result = run_crew(
                conn,
                company_name="Acme Corp",
                rubric=rubric,
                saver=saver,
                log_root=log_root,
                workflow_id="wf-crew-fail",
            )
        assert result.status is WorkflowStatus.FAILED
        assert result.error_step == "researcher"
        assert "API 500" in (result.error_message or "")
        wf = load_workflow(conn, "wf-crew-fail")
        assert wf is not None
        assert wf.status is WorkflowStatus.FAILED
        assert wf.finished_at is not None
        # The researcher's step row is FAILED (run_step finalised it).
        steps = load_steps(conn, "wf-crew-fail")
        assert len(steps) == 1
        assert steps[0].agent_name == "researcher"
        assert steps[0].status is StepStatus.FAILED
        # workflow.failed event emitted.
        events = load_events(conn, "wf-crew-fail")
        assert any((e.structured_data or {}).get("event_type") == "workflow.failed" for e in events)

    def test_drafter_raise_after_resume_finalises_workflow_failed(
        self,
        conn: sqlite3.Connection,
        saver: SqliteSaver,
        tmp_path: Path,
        log_root: Path,
    ) -> None:
        rubric = load_rubric(_write_rubric(tmp_path))
        with _patch_all_agents(
            researcher_result=_researcher_llm(),
            extractor_result=_extractor_llm(),
            scorer_result=_scorer_llm(),
        ):
            paused = run_crew(
                conn,
                company_name="Acme Corp",
                rubric=rubric,
                saver=saver,
                log_root=log_root,
                workflow_id="wf-crew-drafter-fail",
            )
        assert paused.status is WorkflowStatus.PAUSED
        assert paused.pending_checkpoint_id is not None
        approve_checkpoint(conn, paused.pending_checkpoint_id, reviewer="test-runner")

        with patch.object(drafter_mod, "complete", side_effect=RuntimeError("Opus 500")):
            result = resume_crew(
                conn,
                workflow_id="wf-crew-drafter-fail",
                rubric=rubric,
                saver=saver,
                log_root=log_root,
            )
        assert result.status is WorkflowStatus.FAILED
        assert result.error_step == "drafter"
        wf = load_workflow(conn, "wf-crew-drafter-fail")
        assert wf is not None
        assert wf.status is WorkflowStatus.FAILED


# ---------------------------------------------------------------------------
# TestReconciliationGuard — STOP #3c.
# Orphan step with live SqliteSaver checkpoint NOT reconciled; without IS.
# ---------------------------------------------------------------------------


class TestReconciliationGuard:
    def test_orphan_with_live_checkpoint_is_skipped(
        self,
        conn: sqlite3.Connection,
        saver: SqliteSaver,
    ) -> None:
        # Create a workflow with a running step.
        wf_id = new_workflow_id()
        insert_workflow(
            conn,
            Workflow(id=wf_id, workflow_type="crew-test", status=WorkflowStatus.RUNNING),
        )
        insert_step(
            conn,
            WorkflowStep(
                workflow_id=wf_id,
                step_index=1,
                agent_name="researcher",
                status=StepStatus.RUNNING,
            ),
        )
        # Inject a fake checkpoint row so has_langgraph_checkpoint returns True.
        with conn:
            conn.execute(
                "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id) "
                "VALUES (?, '', 'cp-stub')",
                (wf_id,),
            )
        assert has_langgraph_checkpoint(conn, wf_id) is True

        reconciled = reconcile_orphan_steps(conn)
        assert reconciled == []
        # Step row still RUNNING.
        steps = load_steps(conn, wf_id)
        assert steps[0].status is StepStatus.RUNNING
        assert steps[0].finished_at is None

    def test_orphan_without_checkpoint_is_failed(
        self,
        conn: sqlite3.Connection,
        saver: SqliteSaver,
    ) -> None:
        wf_id = new_workflow_id()
        insert_workflow(
            conn,
            Workflow(id=wf_id, workflow_type="crew-test", status=WorkflowStatus.RUNNING),
        )
        insert_step(
            conn,
            WorkflowStep(
                workflow_id=wf_id,
                step_index=1,
                agent_name="researcher",
                status=StepStatus.RUNNING,
            ),
        )
        # No fake checkpoint row — has_langgraph_checkpoint returns False.
        assert has_langgraph_checkpoint(conn, wf_id) is False

        reconciled = reconcile_orphan_steps(conn)
        assert len(reconciled) == 1
        steps = load_steps(conn, wf_id)
        assert steps[0].status is StepStatus.FAILED
        assert steps[0].finished_at is not None
        assert "orphaned-on-startup" in (steps[0].error_message or "")

    def test_has_langgraph_checkpoint_on_fresh_db_returns_false(
        self,
        db_path: Path,
    ) -> None:
        """The `checkpoints` table doesn't exist until SqliteSaver.setup() is
        called. On a DB that has never seen the orchestrator, the guard must
        return False rather than raising — matching Slice 2 behaviour exactly."""
        # Open a fresh DB without ever wiring SqliteSaver.
        c = init_db(db_path)
        try:
            assert has_langgraph_checkpoint(c, "any-id") is False
        finally:
            c.close()


# ---------------------------------------------------------------------------
# TestCrossSessionResume — file-backed DB; close + reopen connections.
# This is the load-bearing STOP #3 validation: the SqliteSaver state survives
# a process death and the orchestrator can RESUME from cold storage.
# ---------------------------------------------------------------------------


class TestCrossSessionResume:
    def test_resume_after_closing_all_connections(
        self,
        db_path: Path,
        tmp_path: Path,
    ) -> None:
        rubric = load_rubric(_write_rubric(tmp_path))
        wf_id = "wf-crew-cross-session"

        # Session 1 — run until PAUSED, then close everything.
        conn1 = init_db(db_path)
        saver_conn1 = sqlite3.connect(db_path, check_same_thread=False)
        saver1 = SqliteSaver(saver_conn1)
        saver1.setup()
        try:
            with _patch_all_agents(
                researcher_result=_researcher_llm(),
                extractor_result=_extractor_llm(),
                scorer_result=_scorer_llm(),
            ):
                paused = run_crew(
                    conn1,
                    company_name="Acme Corp",
                    rubric=rubric,
                    saver=saver1,
                    workflow_id=wf_id,
                )
            assert paused.status is WorkflowStatus.PAUSED
            pending_id = paused.pending_checkpoint_id
            assert pending_id is not None
        finally:
            conn1.close()
            saver_conn1.close()

        # Session 2 — fresh connections, approve, then resume.
        conn2 = init_db(db_path)
        try:
            approve_checkpoint(conn2, pending_id, reviewer="test-runner")
        finally:
            conn2.close()

        # Session 3 — fresh connections again, resume.
        conn3 = init_db(db_path)
        saver_conn3 = sqlite3.connect(db_path, check_same_thread=False)
        saver3 = SqliteSaver(saver_conn3)
        saver3.setup()
        try:
            with _patch_all_agents(drafter_result=_drafter_llm()):
                completed = resume_crew(
                    conn3,
                    workflow_id=wf_id,
                    rubric=rubric,
                    saver=saver3,
                )
            assert completed.status is WorkflowStatus.COMPLETED
            assert completed.draft is not None
            assert completed.draft.claim_count == 3
            wf = load_workflow(conn3, wf_id)
            assert wf is not None
            assert wf.status is WorkflowStatus.COMPLETED
        finally:
            conn3.close()
            saver_conn3.close()


# ---------------------------------------------------------------------------
# TestProjectionAndAudit — audit-row shape per ADR-002 + ADR-006 §1.3.
# ---------------------------------------------------------------------------


class TestProjectionAndAudit:
    def test_completed_event_carries_produced_draft_and_terminate_reason(
        self,
        conn: sqlite3.Connection,
        saver: SqliteSaver,
        tmp_path: Path,
        log_root: Path,
    ) -> None:
        rubric_path = _write_rubric(tmp_path, _RUBRIC_TOML_HIGH_FLOOR)
        rubric = load_rubric(rubric_path)
        with _patch_all_agents(
            researcher_result=_researcher_llm(),
            extractor_result=_extractor_llm(),
            scorer_result=_scorer_llm(score=1.0),
        ):
            run_crew(
                conn,
                company_name="Acme Corp",
                rubric=rubric,
                saver=saver,
                log_root=log_root,
                workflow_id="wf-crew-projection",
            )
        events = load_events(conn, "wf-crew-projection")
        completed = next(
            (
                e
                for e in events
                if (e.structured_data or {}).get("event_type") == "workflow.completed"
            ),
            None,
        )
        assert completed is not None
        sd = completed.structured_data or {}
        assert sd["produced_draft"] is False
        assert sd["terminate_reason"] == TERMINATE_REASON_SCORE_GATED
        assert sd["company_name"] == "Acme Corp"

    def test_checkpoint_payload_includes_scored_company(
        self,
        conn: sqlite3.Connection,
        saver: SqliteSaver,
        tmp_path: Path,
        log_root: Path,
    ) -> None:
        rubric = load_rubric(_write_rubric(tmp_path))
        with _patch_all_agents(
            researcher_result=_researcher_llm(),
            extractor_result=_extractor_llm(),
            scorer_result=_scorer_llm(),
        ):
            run_crew(
                conn,
                company_name="Acme Corp",
                rubric=rubric,
                saver=saver,
                log_root=log_root,
                workflow_id="wf-crew-cp-payload",
            )
        pending = list_pending_checkpoints(conn, "wf-crew-cp-payload")
        assert len(pending) == 1
        cp = pending[0]
        assert cp.proposed_payload is not None
        payload = cp.proposed_payload
        assert "scored_company" in payload
        sc_dict = payload["scored_company"]
        assert sc_dict["company_name"] == "Acme Corp"
        # Score + coverage echoed onto the payload so the operator's review
        # sees the same numbers the eventual Drafter footer would.
        assert "score" in sc_dict
        assert "coverage" in sc_dict


# ---------------------------------------------------------------------------
# CLI surface tests
# ---------------------------------------------------------------------------


class TestCli:
    def test_run_crew_initial_run_prints_paused_block(
        self,
        db_path: Path,
        tmp_path: Path,
    ) -> None:
        rubric_path = _write_rubric(tmp_path)
        with _patch_all_agents(
            researcher_result=_researcher_llm(),
            extractor_result=_extractor_llm(),
            scorer_result=_scorer_llm(),
        ):
            runner = CliRunner()
            res = runner.invoke(
                main,
                [
                    "run-crew",
                    "Acme Corp",
                    "--db",
                    str(db_path),
                    "--rubric",
                    str(rubric_path),
                    "--workflow-id",
                    "wf-crew-cli-pause",
                ],
            )
        assert res.exit_code == 0, res.output
        assert "PAUSED" in res.output
        assert "checkpoint approve" in res.output
        assert "--resume wf-crew-cli-pause" in res.output

    def test_run_crew_gated_prints_no_draft_block(
        self,
        db_path: Path,
        tmp_path: Path,
    ) -> None:
        rubric_path = _write_rubric(tmp_path, _RUBRIC_TOML_HIGH_FLOOR)
        with _patch_all_agents(
            researcher_result=_researcher_llm(),
            extractor_result=_extractor_llm(),
            scorer_result=_scorer_llm(score=1.0),
        ):
            runner = CliRunner()
            res = runner.invoke(
                main,
                [
                    "run-crew",
                    "Acme Corp",
                    "--db",
                    str(db_path),
                    "--rubric",
                    str(rubric_path),
                ],
            )
        assert res.exit_code == 0, res.output
        assert "NO DRAFT" in res.output
        assert "score_gated" in res.output

    def test_run_crew_requires_company_or_resume(
        self,
        db_path: Path,
        tmp_path: Path,
    ) -> None:
        rubric_path = _write_rubric(tmp_path)
        runner = CliRunner()
        res = runner.invoke(
            main,
            ["run-crew", "--db", str(db_path), "--rubric", str(rubric_path)],
        )
        assert res.exit_code != 0
        assert "COMPANY_NAME" in res.output or "supply" in res.output.lower()

    def test_run_crew_resume_and_company_are_mutually_exclusive(
        self,
        db_path: Path,
        tmp_path: Path,
    ) -> None:
        rubric_path = _write_rubric(tmp_path)
        runner = CliRunner()
        res = runner.invoke(
            main,
            [
                "run-crew",
                "Acme",
                "--resume",
                "wf-xyz",
                "--db",
                str(db_path),
                "--rubric",
                str(rubric_path),
            ],
        )
        assert res.exit_code != 0
        assert "mutually exclusive" in res.output.lower()

    def test_run_crew_resume_after_approve_prints_draft(
        self,
        db_path: Path,
        tmp_path: Path,
    ) -> None:
        rubric_path = _write_rubric(tmp_path)
        # Step 1 — initial run pauses.
        with _patch_all_agents(
            researcher_result=_researcher_llm(),
            extractor_result=_extractor_llm(),
            scorer_result=_scorer_llm(),
        ):
            runner = CliRunner()
            res1 = runner.invoke(
                main,
                [
                    "run-crew",
                    "Acme Corp",
                    "--db",
                    str(db_path),
                    "--rubric",
                    str(rubric_path),
                    "--workflow-id",
                    "wf-crew-cli-resume",
                ],
            )
        assert res1.exit_code == 0, res1.output
        # Step 2 — approve checkpoint via CLI.
        # Pull the pending checkpoint id out of the previous output.
        conn = init_db(db_path)
        try:
            pending = list_pending_checkpoints(conn, "wf-crew-cli-resume")
        finally:
            conn.close()
        assert len(pending) == 1
        cp_id = str(pending[0].id)
        res2 = runner.invoke(
            main,
            [
                "checkpoint",
                "--db",
                str(db_path),
                "approve",
                cp_id,
                "--reviewer",
                "test-runner",
            ],
        )
        assert res2.exit_code == 0, res2.output
        # Step 3 — resume run-crew.
        with _patch_all_agents(drafter_result=_drafter_llm()):
            res3 = runner.invoke(
                main,
                [
                    "run-crew",
                    "--resume",
                    "wf-crew-cli-resume",
                    "--db",
                    str(db_path),
                    "--rubric",
                    str(rubric_path),
                ],
            )
        assert res3.exit_code == 0, res3.output
        assert "COMPLETED" in res3.output
        assert "Draft prose:" in res3.output
        assert "$50M Series B" in res3.output


# ---------------------------------------------------------------------------
# LIVE SMOKE — guarded; skip without ANTHROPIC_API_KEY.
# ---------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set",
)
def test_live_crew_full_chain(db_path: Path, tmp_path: Path) -> None:
    """Smoke 1 — one full crew run end-to-end against a real company,
    all four agents live.

    Uses a relaxed rubric (floor=0, tier_c_min=0) so a passing
    ScoredCompany is possible. Accepts either of the two valid live
    outcomes:

      (a) PAUSED at the human checkpoint → approve + resume in the same
          process and assert a Draft is produced.
      (b) COMPLETED with `terminate_reason="score_gated"` → the live
          extractor produced enough gaps that the scorer's coverage or
          score gate fired. This IS a valid Slice 6 outcome (ADR-006 §1
          empty-outcome contract); the audit chain still has to be
          correct. The variability between (a) and (b) is itself
          calibration data for the live LLMs' extraction reliability
          — recorded in STATUS.md.

    Cross-session resume mechanics are validated separately in
    `test_live_crew_cross_session_resume`.
    """
    rubric_path = _write_rubric(tmp_path)
    rubric = load_rubric(rubric_path)
    wf_id = "wf-crew-live-full"

    conn = init_db(db_path)
    saver_conn = sqlite3.connect(db_path, check_same_thread=False)
    saver = SqliteSaver(saver_conn)
    saver.setup()
    try:
        result = run_crew(
            conn,
            company_name="Anthropic",
            rubric=rubric,
            saver=saver,
            workflow_id=wf_id,
        )

        # Three step rows always: researcher + extractor + scorer. If we
        # paused at the checkpoint there is no drafter row yet.
        steps = load_steps(conn, wf_id)
        agent_names = {s.agent_name for s in steps}
        assert {"researcher", "extractor", "scorer"} <= agent_names

        if result.status is WorkflowStatus.PAUSED:
            assert result.pending_checkpoint_id is not None
            assert result.cost_usd > 0.0
            approve_checkpoint(
                conn,
                result.pending_checkpoint_id,
                reviewer="live-smoke",
            )
            completed = resume_crew(
                conn,
                workflow_id=wf_id,
                rubric=rubric,
                saver=saver,
            )
            assert completed.status is WorkflowStatus.COMPLETED
            assert completed.draft is not None
            assert completed.draft.claim_count >= 1
            assert completed.cost_usd > result.cost_usd  # drafter cost added
            print(
                f"\n[live crew full PAUSED→Draft] "
                f"cost_usd={completed.cost_usd:.6f} "
                f"claims={completed.draft.claim_count} "
                f"chars={completed.draft.char_count}"
            )
        else:
            # Empty-outcome branch — workflow COMPLETED, no Draft.
            assert result.status is WorkflowStatus.COMPLETED
            assert result.draft is None
            assert result.terminate_reason is not None
            assert result.cost_usd > 0.0
            print(
                f"\n[live crew full gated] "
                f"terminate_reason={result.terminate_reason!r} "
                f"cost_usd={result.cost_usd:.6f} "
                f"(live extractor/scorer variability — see STATUS.md)"
            )

        wf = load_workflow(conn, wf_id)
        assert wf is not None
        assert wf.status is WorkflowStatus.COMPLETED
        assert wf.cost_total_usd > 0.0
    finally:
        conn.close()
        saver_conn.close()


@pytest.mark.live
@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set",
)
def test_live_crew_cross_session_resume(db_path: Path, tmp_path: Path) -> None:
    """Smoke 2 — cross-session resume mechanics, with a real Opus 4.7 drafter.

    The upstream chain (researcher / extractor / scorer) is MOCKED so that
    PAUSED is reached deterministically — the live-LLM variability that
    Smoke 1 captures shouldn't gate Slice 6's resume-mechanism test. The
    drafter is LIVE so this smoke validates one real Opus 4.7 call lands
    after a cross-session resume.

    Flow:
      Session 1: run_crew with mocked upstream → PAUSED. Close all
                 connections (simulated process death).
      Session 2: fresh conn → approve_checkpoint via CheckpointSystem.
                 Close.
      Session 3: fresh conn + fresh SqliteSaver → resume_crew with live
                 drafter → COMPLETED + real Opus Draft.

    This is the load-bearing STOP #3 validation: the SqliteSaver state
    plus the audit tables both survive a process death, and the
    orchestrator can pick up exactly where it left off.
    """
    rubric_path = _write_rubric(tmp_path)
    rubric = load_rubric(rubric_path)
    wf_id = "wf-crew-live-resume"

    # Session 1 — mocked upstream, pause at checkpoint.
    conn1 = init_db(db_path)
    saver_conn1 = sqlite3.connect(db_path, check_same_thread=False)
    saver1 = SqliteSaver(saver_conn1)
    saver1.setup()
    try:
        with _patch_all_agents(
            researcher_result=_researcher_llm(),
            extractor_result=_extractor_llm(),
            scorer_result=_scorer_llm(),
        ):
            paused = run_crew(
                conn1,
                company_name="Acme Corp (live-smoke)",
                rubric=rubric,
                saver=saver1,
                workflow_id=wf_id,
            )
        assert paused.status is WorkflowStatus.PAUSED
        pending_id = paused.pending_checkpoint_id
        assert pending_id is not None
    finally:
        conn1.close()
        saver_conn1.close()

    # Session 2 — fresh conn, approve, close.
    conn2 = init_db(db_path)
    try:
        approve_checkpoint(conn2, pending_id, reviewer="live-smoke")
    finally:
        conn2.close()

    # Session 3 — fresh conn + fresh saver_conn; live drafter, resume.
    conn3 = init_db(db_path)
    saver_conn3 = sqlite3.connect(db_path, check_same_thread=False)
    saver3 = SqliteSaver(saver_conn3)
    saver3.setup()
    try:
        completed = resume_crew(
            conn3,
            workflow_id=wf_id,
            rubric=rubric,
            saver=saver3,
        )
        assert completed.status is WorkflowStatus.COMPLETED
        assert completed.draft is not None
        assert completed.draft.claim_count >= 1
        assert completed.cost_usd > 0.0
        wf = load_workflow(conn3, wf_id)
        assert wf is not None
        assert wf.status is WorkflowStatus.COMPLETED
        # All four step rows survived the cross-session boundary.
        steps = sorted(load_steps(conn3, wf_id), key=lambda s: s.step_index)
        assert [s.agent_name for s in steps] == [
            "researcher",
            "extractor",
            "scorer",
            "drafter",
        ]
        print(
            f"\n[live crew resume] cost_usd={completed.cost_usd:.6f} "
            f"claims={completed.draft.claim_count} "
            f"chars={completed.draft.char_count}"
        )
    finally:
        conn3.close()
        saver_conn3.close()
