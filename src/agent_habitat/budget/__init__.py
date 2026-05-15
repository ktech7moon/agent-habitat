"""agent-habitat budget enforcement (Slice 3).

Daily UTC-calendar-day spend caps per workflow, configured by the operator
via `config/budgets.toml`. This package contributes:

  - Config loading (`load_budget_config`, `BudgetConfig`, `cap_for_workflow_type`).
  - Daily-window cost aggregation against Slice 2's `workflow_steps`.
  - A pure under/approaching/over evaluator.
  - Exceed detection that records a halt-signal event into the EXISTING
    events table (no ADR-002 schema change).
  - A halt-signal query the Phase 2 orchestrator will call before each step.

Per-call cost and per-workflow aggregation are NOT here — they live in
`llm.py` and `state.persistence.recompute_cost_total` respectively.
"""

from .config import (
    DEFAULT_BUDGET_CONFIG_PATH,
    BudgetConfig,
    BudgetConfigError,
    cap_for_workflow_type,
    load_budget_config,
)
from .tracker import (
    BUDGET_EXCEEDED_EVENT_TYPE,
    BUDGET_EXCEEDED_MESSAGE,
    BudgetCheck,
    BudgetStatus,
    check_workflow_budget,
    cost_within_window,
    evaluate_budget,
    is_workflow_halted_by_budget,
    record_budget_exceeded,
    utc_day_window,
)

__all__ = [
    "BUDGET_EXCEEDED_EVENT_TYPE",
    "BUDGET_EXCEEDED_MESSAGE",
    "DEFAULT_BUDGET_CONFIG_PATH",
    "BudgetCheck",
    "BudgetConfig",
    "BudgetConfigError",
    "BudgetStatus",
    "cap_for_workflow_type",
    "check_workflow_budget",
    "cost_within_window",
    "evaluate_budget",
    "is_workflow_halted_by_budget",
    "load_budget_config",
    "record_budget_exceeded",
    "utc_day_window",
]
