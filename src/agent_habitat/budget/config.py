"""Budget configuration: load operator-tunable daily caps from TOML.

The config file (default: `config/budgets.toml`) is the operator's tuning
surface — daily spend caps per workflow_type, plus a global approaching
threshold. No code change required to adjust caps.

Parsed via stdlib `tomllib` (Python 3.11+). No new dependency.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

DEFAULT_BUDGET_CONFIG_PATH = Path("config/budgets.toml")


class BudgetConfig(BaseModel):
    """Validated budget configuration.

    `default_cap_usd` is the daily cap applied to any workflow_type without
    an explicit entry in `overrides`. `approaching_threshold` is a single
    global fraction in [0, 1] used to flip status from UNDER to APPROACHING.
    """

    model_config = ConfigDict(frozen=True)

    default_cap_usd: float = Field(..., ge=0.0)
    approaching_threshold: float = Field(..., ge=0.0, le=1.0)
    overrides: dict[str, float] = Field(default_factory=dict)

    @field_validator("overrides")
    @classmethod
    def _caps_non_negative(cls, value: dict[str, float]) -> dict[str, float]:
        for wf_type, cap in value.items():
            if cap < 0:
                raise ValueError(f"override cap for {wf_type!r} must be >= 0, got {cap}")
        return value


class BudgetConfigError(Exception):
    """Raised when the budget config file is missing or malformed."""


def load_budget_config(path: Path | str = DEFAULT_BUDGET_CONFIG_PATH) -> BudgetConfig:
    """Read and validate a `budgets.toml`.

    Expected shape:

        [defaults]
        daily_cap_usd = 5.00
        approaching_threshold = 0.80

        [workflow_types.<name>]
        daily_cap_usd = <float>

    Raises `BudgetConfigError` for missing file, malformed TOML, or schema
    violations. The error message names the path so misconfiguration is
    operator-debuggable.
    """
    path = Path(path)
    if not path.exists():
        raise BudgetConfigError(f"budget config not found: {path}")

    try:
        with path.open("rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise BudgetConfigError(f"malformed TOML in {path}: {exc}") from exc

    defaults = raw.get("defaults")
    if not isinstance(defaults, dict):
        raise BudgetConfigError(f"{path}: missing or non-table [defaults] section")

    try:
        default_cap = float(defaults["daily_cap_usd"])
        threshold = float(defaults["approaching_threshold"])
    except KeyError as exc:
        raise BudgetConfigError(f"{path}: [defaults] missing required key {exc.args[0]!r}") from exc
    except (TypeError, ValueError) as exc:
        raise BudgetConfigError(f"{path}: [defaults] has non-numeric value: {exc}") from exc

    overrides: dict[str, float] = {}
    wf_types_section = raw.get("workflow_types", {})
    if not isinstance(wf_types_section, dict):
        raise BudgetConfigError(f"{path}: [workflow_types] must be a table")
    for wf_type, body in wf_types_section.items():
        if not isinstance(body, dict) or "daily_cap_usd" not in body:
            raise BudgetConfigError(f"{path}: [workflow_types.{wf_type}] missing 'daily_cap_usd'")
        try:
            overrides[wf_type] = float(body["daily_cap_usd"])
        except (TypeError, ValueError) as exc:
            raise BudgetConfigError(
                f"{path}: [workflow_types.{wf_type}].daily_cap_usd not numeric: {exc}"
            ) from exc

    try:
        return BudgetConfig(
            default_cap_usd=default_cap,
            approaching_threshold=threshold,
            overrides=overrides,
        )
    except ValueError as exc:
        raise BudgetConfigError(f"{path}: {exc}") from exc


def cap_for_workflow_type(config: BudgetConfig, workflow_type: str) -> float:
    """Resolve the daily cap for a workflow_type: override wins, else default."""
    return config.overrides.get(workflow_type, config.default_cap_usd)
