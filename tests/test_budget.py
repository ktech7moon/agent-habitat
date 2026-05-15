"""Tests for the Slice 3 budget module.

Deterministic — no live API, no production DB. Every DB-touching test uses
a tmp_path SQLite file plus the Slice 2 persistence layer to seed rows.

Coverage:

  TestEvaluateBudget       — pure under / approaching / over classification.
  TestUtcDayWindow         — calendar-day boundaries; tz handling; default-now path.
  TestCostWithinWindow     — inclusion / exclusion at boundaries; isolation by workflow.
  TestCheckWorkflowBudget  — end-to-end: cap resolution + window cost + status.
  TestRecordExceedEvent    — exceed writes the right event row; non-OVER raises.
  TestIsHaltedByBudget     — halt-signal query before/after record.
  TestConfigLoading        — TOML parse: defaults, overrides, missing, malformed.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_habitat.budget import (
    BUDGET_EXCEEDED_EVENT_TYPE,
    BudgetCheck,
    BudgetConfig,
    BudgetConfigError,
    BudgetStatus,
    cap_for_workflow_type,
    check_workflow_budget,
    cost_within_window,
    evaluate_budget,
    is_workflow_halted_by_budget,
    load_budget_config,
    record_budget_exceeded,
    utc_day_window,
)
from agent_habitat.state import (
    StepStatus,
    Workflow,
    WorkflowStep,
    init_db,
    insert_step,
    insert_workflow,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    """Fresh SQLite DB per test, fully initialised with Slice 2's schema."""
    c = init_db(tmp_path / "test.db")
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def config() -> BudgetConfig:
    """A small in-memory BudgetConfig — avoids touching the TOML file."""
    return BudgetConfig(
        default_cap_usd=5.00,
        approaching_threshold=0.80,
        overrides={"lead_enrichment": 10.00, "url_summarizer": 2.00},
    )


def _seed_workflow(
    conn: sqlite3.Connection,
    *,
    workflow_id: str = "wf-test",
    workflow_type: str = "lead_enrichment",
) -> Workflow:
    wf = Workflow(id=workflow_id, workflow_type=workflow_type)
    insert_workflow(conn, wf)
    return wf


def _seed_step(
    conn: sqlite3.Connection,
    workflow_id: str,
    *,
    step_index: int,
    cost: float,
    started_at: datetime,
) -> int:
    step = WorkflowStep(
        workflow_id=workflow_id,
        step_index=step_index,
        agent_name="researcher",
        status=StepStatus.COMPLETED,
        started_at=started_at.isoformat(),
        finished_at=started_at.isoformat(),
        cost_usd=cost,
    )
    return insert_step(conn, step)


# ---------------------------------------------------------------------------
# Pure evaluator
# ---------------------------------------------------------------------------


class TestEvaluateBudget:
    def test_zero_spend_is_under(self) -> None:
        assert evaluate_budget(0.0, 5.0, 0.80) is BudgetStatus.UNDER

    def test_below_threshold_is_under(self) -> None:
        # 0.80 * 5.0 = 4.0 → 3.99 is still UNDER
        assert evaluate_budget(3.99, 5.0, 0.80) is BudgetStatus.UNDER

    def test_at_threshold_is_approaching(self) -> None:
        # exactly cap * threshold → APPROACHING (inclusive lower bound)
        assert evaluate_budget(4.00, 5.0, 0.80) is BudgetStatus.APPROACHING

    def test_above_threshold_below_cap_is_approaching(self) -> None:
        assert evaluate_budget(4.99, 5.0, 0.80) is BudgetStatus.APPROACHING

    def test_at_cap_is_over(self) -> None:
        # cost == cap → OVER (cap is the hard ceiling, not a half-open bound)
        assert evaluate_budget(5.00, 5.0, 0.80) is BudgetStatus.OVER

    def test_above_cap_is_over(self) -> None:
        assert evaluate_budget(7.50, 5.0, 0.80) is BudgetStatus.OVER

    def test_zero_cap_any_spend_is_over(self) -> None:
        assert evaluate_budget(0.01, 0.0, 0.80) is BudgetStatus.OVER

    def test_zero_cap_zero_spend_is_under(self) -> None:
        # No cap, no spend → no halt. Avoids spurious halts on freshly-created workflows.
        assert evaluate_budget(0.0, 0.0, 0.80) is BudgetStatus.UNDER

    def test_threshold_one_skips_approaching_band(self) -> None:
        assert evaluate_budget(4.99, 5.0, 1.0) is BudgetStatus.UNDER
        assert evaluate_budget(5.00, 5.0, 1.0) is BudgetStatus.OVER

    def test_threshold_zero_makes_any_positive_spend_approaching(self) -> None:
        assert evaluate_budget(0.01, 5.0, 0.0) is BudgetStatus.APPROACHING


# ---------------------------------------------------------------------------
# UTC day window
# ---------------------------------------------------------------------------


class TestUtcDayWindow:
    def test_window_is_calendar_day_in_utc(self) -> None:
        instant = datetime(2026, 5, 14, 13, 27, 4, tzinfo=UTC)
        start, end = utc_day_window(instant)
        assert start == datetime(2026, 5, 14, 0, 0, 0, tzinfo=UTC)
        assert end == datetime(2026, 5, 15, 0, 0, 0, tzinfo=UTC)

    def test_window_is_half_open_one_day_long(self) -> None:
        start, end = utc_day_window(datetime(2026, 5, 14, 23, 59, 59, tzinfo=UTC))
        assert end - start == timedelta(days=1)

    def test_non_utc_tz_converted_to_utc(self) -> None:
        # 2026-05-14 02:00 in a +05:00 zone is 2026-05-13 21:00 UTC → day = 5/13.
        from datetime import timezone

        plus5 = timezone(timedelta(hours=5))
        instant = datetime(2026, 5, 14, 2, 0, 0, tzinfo=plus5)
        start, _ = utc_day_window(instant)
        assert start == datetime(2026, 5, 13, 0, 0, 0, tzinfo=UTC)

    def test_naive_datetime_treated_as_utc(self) -> None:
        # Defensive path: naive in, UTC interpretation out. No exception.
        instant = datetime(2026, 5, 14, 13, 0, 0)
        start, end = utc_day_window(instant)
        assert start == datetime(2026, 5, 14, 0, 0, 0, tzinfo=UTC)
        assert end == datetime(2026, 5, 15, 0, 0, 0, tzinfo=UTC)

    def test_default_now_returns_today_window(self) -> None:
        start, end = utc_day_window()
        now = datetime.now(UTC)
        assert start <= now < end


# ---------------------------------------------------------------------------
# cost_within_window
# ---------------------------------------------------------------------------


class TestCostWithinWindow:
    def test_sums_only_steps_inside_window(self, conn: sqlite3.Connection) -> None:
        wf = _seed_workflow(conn)
        # Window: 2026-05-14 UTC.
        inside_morning = datetime(2026, 5, 14, 6, 0, 0, tzinfo=UTC)
        inside_evening = datetime(2026, 5, 14, 22, 30, 0, tzinfo=UTC)
        before_window = datetime(2026, 5, 13, 23, 59, 0, tzinfo=UTC)
        after_window = datetime(2026, 5, 15, 0, 0, 1, tzinfo=UTC)

        _seed_step(conn, wf.id, step_index=1, cost=0.10, started_at=inside_morning)
        _seed_step(conn, wf.id, step_index=2, cost=0.25, started_at=inside_evening)
        _seed_step(conn, wf.id, step_index=3, cost=99.00, started_at=before_window)
        _seed_step(conn, wf.id, step_index=4, cost=99.00, started_at=after_window)

        start, end = utc_day_window(datetime(2026, 5, 14, 12, tzinfo=UTC))
        total = cost_within_window(conn, wf.id, start.isoformat(), end.isoformat())
        assert total == pytest.approx(0.35)

    def test_lower_bound_inclusive_upper_bound_exclusive(self, conn: sqlite3.Connection) -> None:
        wf = _seed_workflow(conn)
        start_of_day = datetime(2026, 5, 14, 0, 0, 0, tzinfo=UTC)
        end_of_day = datetime(2026, 5, 15, 0, 0, 0, tzinfo=UTC)

        _seed_step(conn, wf.id, step_index=1, cost=1.00, started_at=start_of_day)
        # Exactly at upper bound → excluded (half-open).
        _seed_step(conn, wf.id, step_index=2, cost=99.00, started_at=end_of_day)

        total = cost_within_window(conn, wf.id, start_of_day.isoformat(), end_of_day.isoformat())
        assert total == pytest.approx(1.00)

    def test_isolation_across_workflows(self, conn: sqlite3.Connection) -> None:
        wf_a = _seed_workflow(conn, workflow_id="wf-a")
        wf_b = _seed_workflow(conn, workflow_id="wf-b")
        t = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
        _seed_step(conn, wf_a.id, step_index=1, cost=1.00, started_at=t)
        _seed_step(conn, wf_b.id, step_index=1, cost=2.00, started_at=t)

        start, end = utc_day_window(t)
        assert cost_within_window(
            conn, wf_a.id, start.isoformat(), end.isoformat()
        ) == pytest.approx(1.00)
        assert cost_within_window(
            conn, wf_b.id, start.isoformat(), end.isoformat()
        ) == pytest.approx(2.00)

    def test_empty_workflow_is_zero(self, conn: sqlite3.Connection) -> None:
        wf = _seed_workflow(conn)
        start, end = utc_day_window(datetime(2026, 5, 14, 12, tzinfo=UTC))
        assert cost_within_window(conn, wf.id, start.isoformat(), end.isoformat()) == 0.0


# ---------------------------------------------------------------------------
# End-to-end check
# ---------------------------------------------------------------------------


class TestCheckWorkflowBudget:
    def test_under(self, conn: sqlite3.Connection, config: BudgetConfig) -> None:
        wf = _seed_workflow(conn, workflow_type="lead_enrichment")  # cap = 10
        now = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
        _seed_step(conn, wf.id, step_index=1, cost=1.00, started_at=now)

        check = check_workflow_budget(conn, wf.id, wf.workflow_type, config, now=now)
        assert check.status is BudgetStatus.UNDER
        assert check.cap_usd == 10.00
        assert check.cost_usd == pytest.approx(1.00)

    def test_approaching_uses_threshold(
        self, conn: sqlite3.Connection, config: BudgetConfig
    ) -> None:
        # url_summarizer cap = 2.00, threshold = 0.80 → 1.60 trips APPROACHING.
        wf = _seed_workflow(conn, workflow_type="url_summarizer")
        now = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
        _seed_step(conn, wf.id, step_index=1, cost=1.60, started_at=now)

        check = check_workflow_budget(conn, wf.id, wf.workflow_type, config, now=now)
        assert check.status is BudgetStatus.APPROACHING
        assert check.cap_usd == 2.00

    def test_over(self, conn: sqlite3.Connection, config: BudgetConfig) -> None:
        wf = _seed_workflow(conn, workflow_type="url_summarizer")  # cap = 2
        now = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
        _seed_step(conn, wf.id, step_index=1, cost=2.50, started_at=now)

        check = check_workflow_budget(conn, wf.id, wf.workflow_type, config, now=now)
        assert check.status is BudgetStatus.OVER
        assert check.cap_usd == 2.00

    def test_unknown_workflow_type_falls_back_to_default(
        self, conn: sqlite3.Connection, config: BudgetConfig
    ) -> None:
        wf = _seed_workflow(conn, workflow_type="brand_new_workflow")
        now = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
        _seed_step(conn, wf.id, step_index=1, cost=4.00, started_at=now)

        check = check_workflow_budget(conn, wf.id, wf.workflow_type, config, now=now)
        # default cap is 5.00; 4.00 / 5.00 = 0.80 → APPROACHING
        assert check.cap_usd == 5.00
        assert check.status is BudgetStatus.APPROACHING

    def test_cost_outside_today_not_counted(
        self, conn: sqlite3.Connection, config: BudgetConfig
    ) -> None:
        wf = _seed_workflow(conn, workflow_type="url_summarizer")  # cap 2.00
        # A spendy step from yesterday must not bleed into today's budget.
        yesterday = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)
        today = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
        _seed_step(conn, wf.id, step_index=1, cost=99.00, started_at=yesterday)
        _seed_step(conn, wf.id, step_index=2, cost=0.50, started_at=today)

        check = check_workflow_budget(conn, wf.id, wf.workflow_type, config, now=today)
        assert check.cost_usd == pytest.approx(0.50)
        assert check.status is BudgetStatus.UNDER


# ---------------------------------------------------------------------------
# Exceed event
# ---------------------------------------------------------------------------


class TestRecordExceedEvent:
    def test_writes_event_with_expected_shape(self, conn: sqlite3.Connection) -> None:
        wf = _seed_workflow(conn, workflow_type="url_summarizer")
        check = BudgetCheck(
            workflow_id=wf.id,
            workflow_type=wf.workflow_type,
            status=BudgetStatus.OVER,
            cost_usd=3.00,
            cap_usd=2.00,
            window_start="2026-05-14T00:00:00+00:00",
            window_end="2026-05-15T00:00:00+00:00",
        )
        recorded_at = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
        event_id = record_budget_exceeded(conn, check, now=recorded_at)
        assert event_id > 0

        row = conn.execute(
            "SELECT workflow_id, step_id, timestamp, level, message, structured_data "
            "FROM events WHERE id = ?",
            (event_id,),
        ).fetchone()
        assert row["workflow_id"] == wf.id
        assert row["step_id"] is None
        assert row["timestamp"] == recorded_at.isoformat()
        assert row["level"] == "error"
        assert "halted" in row["message"]
        payload = json.loads(row["structured_data"])
        assert payload["event_type"] == BUDGET_EXCEEDED_EVENT_TYPE
        assert payload["workflow_type"] == "url_summarizer"
        assert payload["cost_usd"] == pytest.approx(3.00)
        assert payload["cap_usd"] == pytest.approx(2.00)
        assert payload["window_start"] == "2026-05-14T00:00:00+00:00"
        assert payload["window_end"] == "2026-05-15T00:00:00+00:00"

    def test_non_over_status_raises(self, conn: sqlite3.Connection) -> None:
        check = BudgetCheck(
            workflow_id="wf-x",
            workflow_type="url_summarizer",
            status=BudgetStatus.APPROACHING,
            cost_usd=1.80,
            cap_usd=2.00,
            window_start="2026-05-14T00:00:00+00:00",
            window_end="2026-05-15T00:00:00+00:00",
        )
        with pytest.raises(ValueError, match="non-OVER status"):
            record_budget_exceeded(conn, check)


# ---------------------------------------------------------------------------
# Halt-signal query
# ---------------------------------------------------------------------------


class TestIsHaltedByBudget:
    def test_false_before_any_exceed(self, conn: sqlite3.Connection) -> None:
        wf = _seed_workflow(conn)
        assert is_workflow_halted_by_budget(conn, wf.id) is False

    def test_true_after_exceed_recorded(self, conn: sqlite3.Connection) -> None:
        wf = _seed_workflow(conn, workflow_type="url_summarizer")
        check = BudgetCheck(
            workflow_id=wf.id,
            workflow_type=wf.workflow_type,
            status=BudgetStatus.OVER,
            cost_usd=3.00,
            cap_usd=2.00,
            window_start="2026-05-14T00:00:00+00:00",
            window_end="2026-05-15T00:00:00+00:00",
        )
        record_budget_exceeded(conn, check)
        assert is_workflow_halted_by_budget(conn, wf.id) is True

    def test_isolated_per_workflow(self, conn: sqlite3.Connection) -> None:
        wf_a = _seed_workflow(conn, workflow_id="wf-a", workflow_type="url_summarizer")
        wf_b = _seed_workflow(conn, workflow_id="wf-b", workflow_type="url_summarizer")
        check = BudgetCheck(
            workflow_id=wf_a.id,
            workflow_type=wf_a.workflow_type,
            status=BudgetStatus.OVER,
            cost_usd=3.00,
            cap_usd=2.00,
            window_start="2026-05-14T00:00:00+00:00",
            window_end="2026-05-15T00:00:00+00:00",
        )
        record_budget_exceeded(conn, check)
        assert is_workflow_halted_by_budget(conn, wf_a.id) is True
        assert is_workflow_halted_by_budget(conn, wf_b.id) is False

    def test_unrelated_error_event_is_not_a_halt_signal(self, conn: sqlite3.Connection) -> None:
        # A generic level=error event for the same workflow must not trip the
        # halt-signal query — the query keys on event_type, not just level.
        wf = _seed_workflow(conn)
        conn.execute(
            "INSERT INTO events (workflow_id, step_id, timestamp, level, message, structured_data) "
            "VALUES (?, NULL, ?, 'error', ?, ?)",
            (
                wf.id,
                "2026-05-14T12:00:00+00:00",
                "some unrelated failure",
                json.dumps({"event_type": "agent.failure"}),
            ),
        )
        conn.commit()
        assert is_workflow_halted_by_budget(conn, wf.id) is False


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


class TestConfigLoading:
    def test_parses_valid_file(self, tmp_path: Path) -> None:
        p = tmp_path / "budgets.toml"
        p.write_text(
            "[defaults]\n"
            "daily_cap_usd = 7.50\n"
            "approaching_threshold = 0.60\n"
            "\n"
            "[workflow_types.lead_enrichment]\n"
            "daily_cap_usd = 25.0\n"
            "\n"
            "[workflow_types.url_summarizer]\n"
            "daily_cap_usd = 1.0\n"
        )
        cfg = load_budget_config(p)
        assert cfg.default_cap_usd == 7.50
        assert cfg.approaching_threshold == 0.60
        assert cfg.overrides == {"lead_enrichment": 25.0, "url_summarizer": 1.0}

    def test_no_overrides_section_is_allowed(self, tmp_path: Path) -> None:
        p = tmp_path / "budgets.toml"
        p.write_text("[defaults]\ndaily_cap_usd = 5.0\napproaching_threshold = 0.80\n")
        cfg = load_budget_config(p)
        assert cfg.overrides == {}

    def test_cap_resolution_prefers_override_then_default(self) -> None:
        cfg = BudgetConfig(
            default_cap_usd=5.00,
            approaching_threshold=0.80,
            overrides={"lead_enrichment": 10.00},
        )
        assert cap_for_workflow_type(cfg, "lead_enrichment") == 10.00
        assert cap_for_workflow_type(cfg, "anything_else") == 5.00

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(BudgetConfigError, match="not found"):
            load_budget_config(tmp_path / "nope.toml")

    def test_malformed_toml_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "budgets.toml"
        p.write_text("[defaults\nthis is not toml")
        with pytest.raises(BudgetConfigError, match="malformed TOML"):
            load_budget_config(p)

    def test_missing_required_key_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "budgets.toml"
        p.write_text("[defaults]\ndaily_cap_usd = 5.0\n")  # missing approaching_threshold
        with pytest.raises(BudgetConfigError, match="approaching_threshold"):
            load_budget_config(p)

    def test_missing_defaults_section_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "budgets.toml"
        p.write_text("[workflow_types.x]\ndaily_cap_usd = 1.0\n")
        with pytest.raises(BudgetConfigError, match="defaults"):
            load_budget_config(p)

    def test_override_without_daily_cap_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "budgets.toml"
        p.write_text(
            "[defaults]\n"
            "daily_cap_usd = 5.0\n"
            "approaching_threshold = 0.80\n"
            "[workflow_types.broken]\n"
            "some_other_key = 1\n"
        )
        with pytest.raises(BudgetConfigError, match="daily_cap_usd"):
            load_budget_config(p)

    def test_threshold_out_of_range_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "budgets.toml"
        p.write_text("[defaults]\ndaily_cap_usd = 5.0\napproaching_threshold = 1.5\n")
        with pytest.raises(BudgetConfigError):
            load_budget_config(p)

    def test_bundled_config_file_loads(self) -> None:
        # Sanity-check the file we ship — guards against accidental breakage.
        cfg = load_budget_config(Path("config/budgets.toml"))
        assert cfg.default_cap_usd > 0
        assert 0.0 <= cfg.approaching_threshold <= 1.0
