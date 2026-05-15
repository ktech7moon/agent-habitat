"""Tests for the Phase 2 Slice 2 Researcher agent + RawSignals model.

Deterministic units (no API, no network — the LLM call is mocked) plus
one guarded live smoke that runs at most once when ANTHROPIC_API_KEY
is set.

Coverage:
  TestRawSignalsModel         — Signal/RawSignals validate; signal_count
                                / source_count derived; empty round-trip.
  TestRunHappy                — run_researcher with mocked LLM persists
                                workflow + step + events; cost rolled up;
                                output_ref set; projection mirrored.
  TestEmptyOutcome            — empty signals → COMPLETED (not FAILED), a
                                valid empty-but-typed RawSignals returned.
  TestInfrastructureFailure   — LLM raises → workflow FAILED, step FAILED,
                                events emitted, exception NOT escaped, no
                                stuck-RUNNING workflow.
  TestCLI                     — run-researcher happy + failure exits.
  test_live_researcher_round_trip — one real Haiku web_search call.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from agent_habitat.agents import researcher as researcher_mod
from agent_habitat.agents.models import RawSignals, Signal
from agent_habitat.agents.researcher import (
    AGENT_NAME,
    WORKFLOW_TYPE,
    ResearcherResult,
    run_researcher,
)
from agent_habitat.cli import main
from agent_habitat.llm import Citation, LLMResult
from agent_habitat.state import (
    StepStatus,
    WorkflowStatus,
    init_db,
    load_events,
    load_steps,
    load_workflow,
)


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


def _llm_result(
    *,
    citations: list[Citation] | None = None,
    cost_usd: float = 0.03245,
    web_searches: int = 2,
    jsonl_ref: str = "data/logs/2026-05-14/wf-res.jsonl:1",
) -> LLMResult:
    return LLMResult(
        content="Research summary text.",
        model="claude-haiku-4-5-20251001",
        input_tokens=1500,
        output_tokens=120,
        cost_usd=cost_usd,
        jsonl_ref=jsonl_ref,
        stop_reason="end_turn",
        web_searches=web_searches,
        citations=citations or [],
    )


def _citation(*, text: str, url: str, title: str | None = None) -> Citation:
    return Citation(cited_text=text, source_url=url, source_title=title)


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class TestRawSignalsModel:
    def test_signal_validates(self) -> None:
        s = Signal(
            text="Acme raised $50M Series B led by Sequoia.",
            source_url="https://example.com/news/acme",
            source_title="Acme Funding Round",
            retrieved_at=datetime(2026, 5, 14, 12, 0, tzinfo=UTC),
        )
        assert s.text.startswith("Acme")
        assert s.source_url == "https://example.com/news/acme"

    def test_signal_frozen(self) -> None:
        s = Signal(
            text="x",
            source_url="https://x",
            retrieved_at=datetime(2026, 5, 14, tzinfo=UTC),
        )
        with pytest.raises(Exception):  # pydantic ValidationError on frozen mutation
            s.text = "y"  # type: ignore[misc]

    def test_empty_signals_is_valid(self) -> None:
        rs = RawSignals(company_name="Acme")
        assert rs.signals == []
        assert rs.signal_count == 0
        assert rs.source_count == 0

    def test_signal_count_and_source_count_distinct(self) -> None:
        ts = datetime(2026, 5, 14, tzinfo=UTC)
        rs = RawSignals(
            company_name="Acme",
            signals=[
                Signal(text="a", source_url="https://x", retrieved_at=ts),
                Signal(text="b", source_url="https://x", retrieved_at=ts),
                Signal(text="c", source_url="https://y", retrieved_at=ts),
            ],
        )
        assert rs.signal_count == 3
        # source_count is distinct URLs, not signals.
        assert rs.source_count == 2

    def test_model_round_trip(self) -> None:
        ts = datetime(2026, 5, 14, tzinfo=UTC)
        rs = RawSignals(
            company_name="Acme",
            signals=[Signal(text="a", source_url="https://x", retrieved_at=ts)],
        )
        dumped = rs.model_dump_json()
        loaded = RawSignals.model_validate_json(dumped)
        assert loaded == rs


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestRunHappy:
    def _setup(
        self,
        conn: sqlite3.Connection,
        log_root: Path,
        *,
        citations: list[Citation],
    ) -> ResearcherResult:
        with patch.object(
            researcher_mod,
            "complete",
            return_value=_llm_result(citations=citations, web_searches=2),
        ):
            return run_researcher(
                conn,
                company_name="Acme Corp",
                log_root=log_root,
            )

    def test_returns_completed_with_signals(self, conn: sqlite3.Connection, log_root: Path) -> None:
        cits = [
            _citation(text="Acme raised $50M.", url="https://example.com/a", title="A"),
            _citation(text="Hired CTO Jane.", url="https://example.com/b", title="B"),
        ]
        result = self._setup(conn, log_root, citations=cits)
        assert result.status is WorkflowStatus.COMPLETED
        assert result.error_step is None
        assert result.error_message is None
        assert result.raw_signals.signal_count == 2
        assert result.raw_signals.source_count == 2
        assert result.cost_usd == pytest.approx(0.03245)
        # Signals retain verbatim cited text and URL.
        assert result.raw_signals.signals[0].text == "Acme raised $50M."
        assert result.raw_signals.signals[0].source_url == "https://example.com/a"

    def test_persists_workflow_step_events(self, conn: sqlite3.Connection, log_root: Path) -> None:
        cits = [_citation(text="x", url="https://example.com/a")]
        result = self._setup(conn, log_root, citations=cits)

        wf = load_workflow(conn, result.workflow_id)
        assert wf is not None
        assert wf.status is WorkflowStatus.COMPLETED
        assert wf.finished_at is not None
        assert wf.workflow_type == WORKFLOW_TYPE
        assert wf.metadata.get("company_name") == "Acme Corp"
        assert wf.cost_total_usd == pytest.approx(0.03245)

        steps = load_steps(conn, result.workflow_id)
        assert [s.agent_name for s in steps] == [AGENT_NAME]
        step = steps[0]
        assert step.status is StepStatus.COMPLETED
        assert step.finished_at is not None
        assert step.cost_usd == pytest.approx(0.03245)
        assert step.output_ref == "data/logs/2026-05-14/wf-res.jsonl:1"

        events = load_events(conn, result.workflow_id)
        types = [(e.structured_data or {}).get("event_type") for e in events]
        assert types.count("workflow.started") == 1
        assert types.count("workflow.completed") == 1
        assert types.count("step.started") == 1
        assert types.count("step.completed") == 1
        assert "step.failed" not in types
        assert "workflow.failed" not in types

    def test_step_completed_projection(self, conn: sqlite3.Connection, log_root: Path) -> None:
        cits = [
            _citation(text="a", url="https://example.com/x"),
            _citation(text="b", url="https://example.com/x"),
            _citation(text="c", url="https://example.com/y"),
        ]
        result = self._setup(conn, log_root, citations=cits)
        events = load_events(conn, result.workflow_id)
        completed = next(
            e for e in events if (e.structured_data or {}).get("event_type") == "step.completed"
        )
        sd = completed.structured_data or {}
        assert sd.get("step_name") == AGENT_NAME
        assert sd.get("signal_count") == 3
        assert sd.get("source_count") == 2
        assert sd.get("web_searches") == 2
        assert sd.get("cost_usd") == pytest.approx(0.03245)
        assert sd.get("output_ref") == "data/logs/2026-05-14/wf-res.jsonl:1"

    def test_uses_haiku_tier_with_web_search_tool(
        self, conn: sqlite3.Connection, log_root: Path
    ) -> None:
        with patch.object(researcher_mod, "complete", return_value=_llm_result()) as mock_complete:
            run_researcher(conn, company_name="Acme", log_root=log_root)
        kwargs = mock_complete.call_args.kwargs
        assert kwargs["model_tier"].value == "claude-haiku-4-5-20251001"
        assert kwargs["system"] is not None
        tools = kwargs["tools"]
        assert isinstance(tools, list) and len(tools) == 1
        assert tools[0]["type"] == "web_search_20250305"
        assert tools[0]["name"] == "web_search"
        # Default DEFAULT_MAX_SEARCHES is 3.
        assert tools[0]["max_uses"] == 3

    def test_max_searches_override_forwarded(
        self, conn: sqlite3.Connection, log_root: Path
    ) -> None:
        with patch.object(researcher_mod, "complete", return_value=_llm_result()) as mock_complete:
            run_researcher(
                conn,
                company_name="Acme",
                max_searches=7,
                log_root=log_root,
            )
        tools = mock_complete.call_args.kwargs["tools"]
        assert tools[0]["max_uses"] == 7

    def test_custom_workflow_id_honoured(self, conn: sqlite3.Connection, log_root: Path) -> None:
        with patch.object(researcher_mod, "complete", return_value=_llm_result()):
            result = run_researcher(
                conn,
                company_name="Acme",
                workflow_id="wf-research-1",
                log_root=log_root,
            )
        assert result.workflow_id == "wf-research-1"


# ---------------------------------------------------------------------------
# Empty-outcome — VALID result, not a failure (ADR-006 §1)
# ---------------------------------------------------------------------------


class TestEmptyOutcome:
    def test_no_citations_yields_completed_empty_signals(
        self, conn: sqlite3.Connection, log_root: Path
    ) -> None:
        # web_searches > 0 — the model searched but cited nothing.
        result_llm = _llm_result(citations=[], web_searches=2, cost_usd=0.025)
        with patch.object(researcher_mod, "complete", return_value=result_llm):
            result = run_researcher(
                conn,
                company_name="Obscure Co",
                log_root=log_root,
            )

        assert result.status is WorkflowStatus.COMPLETED
        assert result.raw_signals.signal_count == 0
        assert result.raw_signals.source_count == 0
        assert result.raw_signals.company_name == "Obscure Co"

        wf = load_workflow(conn, result.workflow_id)
        assert wf is not None
        assert wf.status is WorkflowStatus.COMPLETED, "empty signals must NOT mark workflow FAILED"
        events = load_events(conn, result.workflow_id)
        types = [(e.structured_data or {}).get("event_type") for e in events]
        assert "workflow.completed" in types
        assert "workflow.failed" not in types

        # The projection still records the zero counts (auditability).
        completed = next(
            e for e in events if (e.structured_data or {}).get("event_type") == "step.completed"
        )
        sd = completed.structured_data or {}
        assert sd.get("signal_count") == 0
        assert sd.get("source_count") == 0


# ---------------------------------------------------------------------------
# Infrastructure failure — workflow FAILED, exception propagation honoured
# ---------------------------------------------------------------------------


class TestInfrastructureFailure:
    def test_llm_error_marks_workflow_failed(
        self, conn: sqlite3.Connection, log_root: Path
    ) -> None:
        class APIBoom(RuntimeError):
            pass

        with patch.object(researcher_mod, "complete", side_effect=APIBoom("anthropic 5xx")):
            result = run_researcher(
                conn,
                company_name="Acme",
                log_root=log_root,
            )

        # No uncaught crash from the caller's perspective.
        assert result.status is WorkflowStatus.FAILED
        assert result.error_step == AGENT_NAME
        assert "anthropic 5xx" in (result.error_message or "")
        assert "APIBoom" in (result.error_message or "")
        # Empty-but-typed RawSignals returned even on failure.
        assert result.raw_signals.signals == []

        wf = load_workflow(conn, result.workflow_id)
        assert wf is not None
        assert wf.status is WorkflowStatus.FAILED
        assert wf.finished_at is not None, "FAILED workflow must have finished_at stamped"

        # Step row is FAILED with error_message.
        steps = load_steps(conn, result.workflow_id)
        assert len(steps) == 1
        assert steps[0].status is StepStatus.FAILED
        assert steps[0].finished_at is not None
        assert "APIBoom" in (steps[0].error_message or "")

        events = load_events(conn, result.workflow_id)
        types = [(e.structured_data or {}).get("event_type") for e in events]
        assert "step.failed" in types
        assert "workflow.failed" in types
        assert "step.completed" not in types
        assert "workflow.completed" not in types


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCLI:
    def test_run_researcher_happy(self, tmp_path: Path) -> None:
        db = tmp_path / "wf.db"
        cits = [_citation(text="Funding signal.", url="https://example.com/x", title="X")]
        runner = CliRunner()
        with patch.object(researcher_mod, "complete", return_value=_llm_result(citations=cits)):
            result = runner.invoke(
                main,
                ["run-researcher", "--db", str(db), "Acme Corp"],
            )
        assert result.exit_code == 0, result.output
        assert "status: COMPLETED" in result.output
        assert "Automated research signals" in result.output  # decision-support footer
        assert "Funding signal." in result.output
        assert "https://example.com/x" in result.output

    def test_run_researcher_failure_exits_nonzero(self, tmp_path: Path) -> None:
        db = tmp_path / "wf.db"

        class APIBoom(RuntimeError):
            pass

        runner = CliRunner()
        with patch.object(researcher_mod, "complete", side_effect=APIBoom("anthropic exploded")):
            result = runner.invoke(
                main,
                ["run-researcher", "--db", str(db), "Acme"],
            )
        assert result.exit_code != 0
        assert "status: FAILED" in result.output
        assert "anthropic exploded" in result.output

    def test_run_researcher_max_searches_option(self, tmp_path: Path) -> None:
        db = tmp_path / "wf.db"
        runner = CliRunner()
        with patch.object(researcher_mod, "complete", return_value=_llm_result()) as mock_complete:
            result = runner.invoke(
                main,
                ["run-researcher", "--db", str(db), "--max-searches", "5", "Acme"],
            )
        assert result.exit_code == 0, result.output
        tools = mock_complete.call_args.kwargs["tools"]
        assert tools[0]["max_uses"] == 5


# ---------------------------------------------------------------------------
# Live smoke — one real Haiku call with web_search. Skipped without key.
# ---------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY")
    or os.environ["ANTHROPIC_API_KEY"].startswith("sk-ant-REPLACE"),
    reason="ANTHROPIC_API_KEY not set; live smoke skipped.",
)
def test_live_researcher_round_trip(conn: sqlite3.Connection, log_root: Path) -> None:
    """End-to-end live: one Haiku call with web_search against a well-known company.

    Verifies the full habitat round-trip with real network + real API:
    workflow COMPLETED, step recorded, events emitted, cost > 0 (incl. fee),
    output_ref resolves, RawSignals populated with real readable cited spans.
    """
    start = datetime.now(UTC)
    result = run_researcher(
        conn,
        company_name="Anthropic",
        log_root=log_root,
    )
    duration_s = (datetime.now(UTC) - start).total_seconds()

    # Calibration data printed for pytest -s capture.
    print(f"\n[live-researcher] duration_s={duration_s:.2f}")
    print(f"[live-researcher] status={result.status.value}")
    print(f"[live-researcher] cost_usd={result.cost_usd:.6f}")
    print(f"[live-researcher] signal_count={result.raw_signals.signal_count}")
    print(f"[live-researcher] source_count={result.raw_signals.source_count}")
    for i, sig in enumerate(result.raw_signals.signals[:3], start=1):
        print(f"[live-researcher] signal[{i}] url={sig.source_url}")
        print(f"[live-researcher] signal[{i}] text={sig.text[:200]!r}")

    assert result.status is WorkflowStatus.COMPLETED
    assert result.cost_usd > 0.0

    wf = load_workflow(conn, result.workflow_id)
    assert wf is not None
    assert wf.status is WorkflowStatus.COMPLETED
    assert wf.cost_total_usd > 0.0

    steps = load_steps(conn, result.workflow_id)
    assert [s.agent_name for s in steps] == [AGENT_NAME]
    assert steps[0].status is StepStatus.COMPLETED
    assert steps[0].output_ref is not None

    # output_ref resolves to a real JSONL line.
    path_str, _, line_str = steps[0].output_ref.rpartition(":")
    jsonl_path = Path(path_str)
    assert jsonl_path.exists(), f"output_ref path missing: {jsonl_path}"
    line_no = int(line_str)
    record = json.loads(jsonl_path.read_text(encoding="utf-8").splitlines()[line_no - 1])
    assert record["workflow_id"] == result.workflow_id
    assert record["agent_name"] == AGENT_NAME
    assert record["model"] == "claude-haiku-4-5-20251001"
    # Additive web_search keys must be present on a tools= call.
    assert "web_searches" in record
    assert "web_search_fee_usd" in record
    print(f"[live-researcher] record_web_searches={record['web_searches']}")
    print(f"[live-researcher] record_web_search_fee_usd={record['web_search_fee_usd']}")
    print(f"[live-researcher] record_input_tokens={record['input_tokens']}")
    print(f"[live-researcher] record_output_tokens={record['output_tokens']}")

    events = load_events(conn, result.workflow_id)
    types = [(e.structured_data or {}).get("event_type") for e in events]
    assert "workflow.started" in types
    assert "workflow.completed" in types
    assert types.count("step.completed") == 1
