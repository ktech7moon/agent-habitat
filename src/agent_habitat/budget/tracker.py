"""Daily budget enforcement: window + check + halt-signal.

Slice 3's actual new work. Built on Slice 2's persistence layer — this module
does not re-implement per-call cost (lives in `llm.py`) or per-workflow
aggregation (lives in `state.persistence.recompute_cost_total`). It adds:

  1. UTC-calendar-day window calculation.
  2. Per-workflow cost summed inside that window from `workflow_steps`.
  3. A pure `evaluate_budget` (under / approaching / over).
  4. `check_workflow_budget` — joins the three above.
  5. `record_budget_exceeded` — writes a structured event into the EXISTING
     `events` table at level=ERROR. No schema change required.
  6. `is_workflow_halted_by_budget` — query: has this workflow tripped its
     budget cap? Phase 2's orchestrator will call this before each step;
     Slice 3 just provides the primitive.

Actually stopping a running workflow is the orchestrator's job (Phase 2).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum

import structlog

from .config import BudgetConfig, cap_for_workflow_type

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Halt signal — represented as an event row in the existing events table.
# No new schema, no new status. The orchestrator (Phase 2) treats the
# presence of one of these events as a hard stop for the workflow.
# ---------------------------------------------------------------------------

BUDGET_EXCEEDED_EVENT_TYPE = "budget.exceeded"
BUDGET_EXCEEDED_MESSAGE = "workflow halted: daily cost cap exceeded"


class BudgetStatus(str, Enum):
    """Three-state budget state machine."""

    UNDER = "under"
    APPROACHING = "approaching"
    OVER = "over"


@dataclass(frozen=True)
class BudgetCheck:
    """Result of `check_workflow_budget`.

    Carries everything `record_budget_exceeded` needs to write a useful
    audit event — no second query required at exceed time.
    """

    workflow_id: str
    workflow_type: str
    status: BudgetStatus
    cost_usd: float
    cap_usd: float
    window_start: str  # ISO-8601 UTC
    window_end: str  # ISO-8601 UTC


# ---------------------------------------------------------------------------
# UTC calendar-day window
# ---------------------------------------------------------------------------


def utc_day_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Return the half-open UTC day window containing `now`: [start, end).

    "Daily" definition is UTC calendar day — see config/budgets.toml header
    and STATUS.md Open Questions. UTC matches the existing JSONL telemetry
    directory layout (`data/logs/YYYY-MM-DD/`).

    Both returned datetimes are tz-aware UTC.
    """
    instant = now if now is not None else datetime.now(UTC)
    if instant.tzinfo is None:
        # Defensive: treat naive datetimes as UTC rather than raising — calling
        # code in this project produces UTC-aware datetimes everywhere, but a
        # naive value would otherwise crash the comparison below.
        instant = instant.replace(tzinfo=UTC)
    else:
        instant = instant.astimezone(UTC)
    start = instant.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start, end


# ---------------------------------------------------------------------------
# Cost aggregation inside a window
# ---------------------------------------------------------------------------


def cost_within_window(
    conn: sqlite3.Connection,
    workflow_id: str,
    window_start: str,
    window_end: str,
) -> float:
    """Sum `workflow_steps.cost_usd` for steps whose `started_at` is in
    [window_start, window_end) for the given workflow.

    Attribution is by `started_at`: the day a step *began* is the day its
    cost counts against. This matches Slice 1's JSONL bucketing (the
    telemetry line was written shortly after `started_at`). Window
    boundaries are ISO-8601 UTC strings — they sort correctly as text,
    so SQL string comparison is sufficient (no datetime coercion needed).
    """
    row = conn.execute(
        """
        SELECT COALESCE(SUM(cost_usd), 0.0) AS total
          FROM workflow_steps
         WHERE workflow_id = ?
           AND started_at >= ?
           AND started_at <  ?
        """,
        (workflow_id, window_start, window_end),
    ).fetchone()
    return float(row["total"])


# ---------------------------------------------------------------------------
# Pure budget evaluation
# ---------------------------------------------------------------------------


def evaluate_budget(
    cost_usd: float,
    cap_usd: float,
    approaching_threshold: float,
) -> BudgetStatus:
    """Pure function: classify cost against cap.

    Boundary semantics:
      - cost >= cap                                 → OVER
      - cap > cost >= cap * approaching_threshold   → APPROACHING
      - otherwise                                   → UNDER

    Edge case — `cap_usd == 0`: any positive spend is OVER; zero spend is
    UNDER. Negative caps are coerced to 0 (defensive — the config layer
    already rejects negatives).
    """
    cap = max(cap_usd, 0.0)
    if cap == 0.0:
        return BudgetStatus.OVER if cost_usd > 0.0 else BudgetStatus.UNDER
    if cost_usd >= cap:
        return BudgetStatus.OVER
    if cost_usd >= cap * approaching_threshold:
        return BudgetStatus.APPROACHING
    return BudgetStatus.UNDER


# ---------------------------------------------------------------------------
# End-to-end check
# ---------------------------------------------------------------------------


def check_workflow_budget(
    conn: sqlite3.Connection,
    workflow_id: str,
    workflow_type: str,
    config: BudgetConfig,
    now: datetime | None = None,
) -> BudgetCheck:
    """Resolve cap, compute today's cost for the workflow, classify status."""
    cap = cap_for_workflow_type(config, workflow_type)
    start, end = utc_day_window(now)
    start_iso, end_iso = start.isoformat(), end.isoformat()
    cost = cost_within_window(conn, workflow_id, start_iso, end_iso)
    status = evaluate_budget(cost, cap, config.approaching_threshold)
    return BudgetCheck(
        workflow_id=workflow_id,
        workflow_type=workflow_type,
        status=status,
        cost_usd=cost,
        cap_usd=cap,
        window_start=start_iso,
        window_end=end_iso,
    )


# ---------------------------------------------------------------------------
# Exceed detection — record the halt signal
# ---------------------------------------------------------------------------


def record_budget_exceeded(
    conn: sqlite3.Connection,
    check: BudgetCheck,
    now: datetime | None = None,
) -> int:
    """Write an ERROR-level `budget.exceeded` event for this workflow.

    Returns the new event row's id. Idempotency is the caller's job — if you
    call this twice for the same exceed, you get two rows. The halt-signal
    query (`is_workflow_halted_by_budget`) only needs one.
    """
    if check.status is not BudgetStatus.OVER:
        raise ValueError(
            f"record_budget_exceeded called with non-OVER status: {check.status.value}"
        )
    ts = (now or datetime.now(UTC)).isoformat()
    structured = json.dumps(
        {
            "event_type": BUDGET_EXCEEDED_EVENT_TYPE,
            "workflow_type": check.workflow_type,
            "cost_usd": check.cost_usd,
            "cap_usd": check.cap_usd,
            "window_start": check.window_start,
            "window_end": check.window_end,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    with conn:
        cur = conn.execute(
            """
            INSERT INTO events (
                workflow_id, step_id, timestamp, level, message, structured_data
            ) VALUES (?, NULL, ?, 'error', ?, ?)
            """,
            (check.workflow_id, ts, BUDGET_EXCEEDED_MESSAGE, structured),
        )
        event_id = int(cur.lastrowid) if cur.lastrowid is not None else 0

    log.warning(
        "budget.exceeded",
        workflow_id=check.workflow_id,
        workflow_type=check.workflow_type,
        cost_usd=check.cost_usd,
        cap_usd=check.cap_usd,
        event_id=event_id,
    )
    return event_id


def is_workflow_halted_by_budget(
    conn: sqlite3.Connection,
    workflow_id: str,
) -> bool:
    """True iff at least one `budget.exceeded` event exists for the workflow.

    Phase 2's orchestrator will call this before scheduling the next step.
    Uses SQLite's built-in `json_extract` against the `structured_data`
    column — no app-side JSON parsing per row.
    """
    row = conn.execute(
        """
        SELECT 1
          FROM events
         WHERE workflow_id = ?
           AND level = 'error'
           AND json_extract(structured_data, '$.event_type') = ?
         LIMIT 1
        """,
        (workflow_id, BUDGET_EXCEEDED_EVENT_TYPE),
    ).fetchone()
    return row is not None
