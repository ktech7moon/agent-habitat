"""ICP rubric loader: validate operator-tunable scoring config from TOML.

The rubric file (default: `config/rubric.toml`) is the operator's tuning
surface (CLAUDE.md rule 7: "operator tunes outcomes, developers tune
prompts"). It mirrors `config/budgets.toml`'s idiom — `[defaults]` for
global tunables, `[dimensions.<name>]` for per-dimension override sections.

Per ADR-004's "Forward dependencies handed to Slice 4", the loader must
validate at load time:

  - weights sum to 1.0 across all dimensions
  - each dimension's `field` is a member of `PROFILE_FIELD_NAMES`
  - tier thresholds form `tier_a_min > tier_b_min > tier_c_min >= floor`
  - required `[defaults]` keys present
  - `missing_data_policy = "renormalise"` (the only Phase 2 option)

Malformed rubrics fail with a clear, path-named error — same
operator-debuggability discipline as `budget.config.load_budget_config`.

Parsed via stdlib `tomllib` (Python 3.11+). No new dependency.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..agents.models import PROFILE_FIELD_NAMES

DEFAULT_RUBRIC_PATH = Path("config/rubric.toml")

#: The only Phase 2 missing-data policy. ADR-004 §2 explicitly defers
#: alternatives (penalty, collapsed-composite, gap=0) and §5 declares
#: `"renormalise"` the sole Phase 2 option. Listed here so the policy
#: string is one source of truth.
MISSING_DATA_POLICY_RENORMALISE = "renormalise"

#: Total dimension weight must sum to 1.0 (ADR-004 §1). Float arithmetic
#: gives an epsilon — TOML literals like `0.1 + 0.2 + 0.7` round to 1.0
#: but `0.30 + 0.20 + 0.20 + 0.20 + 0.10` may produce `1.0000000000000002`.
_WEIGHT_SUM_TOLERANCE = 1e-9


class DimensionConfig(BaseModel):
    """One scoring dimension as declared in `[dimensions.<name>]`.

    `field` MUST be a member of `PROFILE_FIELD_NAMES` (one dimension scores
    exactly one profile field — ADR-004 §1 explicitly defers multi-field
    dimensions). `weight` must be a positive float; total weight across all
    dimensions is validated at the `RubricConfig` level. `prose` is the
    operator-authored rubric the Scorer LLM applies to the field values to
    produce a per-dimension score in `[0, 5]`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(..., min_length=1, description="Section name, e.g. 'industry'.")
    field: str = Field(..., description="Must be a member of PROFILE_FIELD_NAMES.")
    weight: float = Field(..., gt=0.0, le=1.0)
    prose: str = Field(..., min_length=1, description="Operator-authored rubric text.")

    @field_validator("field")
    @classmethod
    def _field_in_profile_names(cls, value: str) -> str:
        if value not in PROFILE_FIELD_NAMES:
            raise ValueError(f"field must be one of {PROFILE_FIELD_NAMES}; got {value!r}")
        return value


class RubricConfig(BaseModel):
    """Validated rubric — `[defaults]` plus one `DimensionConfig` per dimension.

    Invariants enforced on construction (ADR-004 §1, §2, Forward deps):
      - Weights sum to 1.0 within tolerance.
      - `tier_a_min > tier_b_min > tier_c_min >= floor`.
      - `missing_data_policy == "renormalise"` (only Phase 2 option).
      - Dimension `field` values are unique (one dimension per profile field).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    floor: float = Field(..., ge=0.0, le=100.0)
    min_coverage: float = Field(..., ge=0.0, le=1.0)
    tier_a_min: float = Field(..., ge=0.0, le=100.0)
    tier_b_min: float = Field(..., ge=0.0, le=100.0)
    tier_c_min: float = Field(..., ge=0.0, le=100.0)
    missing_data_policy: str = Field(...)
    dimensions: list[DimensionConfig] = Field(..., min_length=1)

    @model_validator(mode="after")
    def _check_invariants(self) -> Self:
        if self.missing_data_policy != MISSING_DATA_POLICY_RENORMALISE:
            raise ValueError(
                f"missing_data_policy must be {MISSING_DATA_POLICY_RENORMALISE!r}; "
                f"got {self.missing_data_policy!r} "
                "(Phase 2 supports only 'renormalise' — see ADR-004 §2)"
            )
        if not (self.tier_a_min > self.tier_b_min > self.tier_c_min >= self.floor):
            raise ValueError(
                "tier thresholds must satisfy tier_a_min > tier_b_min > tier_c_min >= floor; "
                f"got tier_a_min={self.tier_a_min}, tier_b_min={self.tier_b_min}, "
                f"tier_c_min={self.tier_c_min}, floor={self.floor}"
            )

        total = sum(d.weight for d in self.dimensions)
        if abs(total - 1.0) > _WEIGHT_SUM_TOLERANCE:
            raise ValueError(
                f"dimension weights must sum to 1.0; got {total} "
                f"({len(self.dimensions)} dimensions)"
            )

        fields_seen: set[str] = set()
        for d in self.dimensions:
            if d.field in fields_seen:
                raise ValueError(
                    f"duplicate dimension field {d.field!r}; each PROFILE_FIELD_NAMES "
                    "member may back at most one dimension"
                )
            fields_seen.add(d.field)
        return self

    def dimension_for_field(self, field: str) -> DimensionConfig | None:
        """Return the `DimensionConfig` whose `field` matches, or None.

        Used by the Scorer to look up the rubric for one `CompanyProfile`
        field. A rubric does not have to score every `PROFILE_FIELD_NAMES`
        member; a missing dimension is silently ignored at scoring time
        (the profile field simply does not contribute to the composite).
        """
        for d in self.dimensions:
            if d.field == field:
                return d
        return None


class RubricConfigError(Exception):
    """Raised when the rubric config file is missing or malformed."""


def load_rubric(path: Path | str = DEFAULT_RUBRIC_PATH) -> RubricConfig:
    """Read and validate a `rubric.toml`.

    Expected shape (mirrors `budgets.toml`'s idiom):

        [defaults]
        floor = 55.0
        min_coverage = 0.0
        tier_a_min = 80.0
        tier_b_min = 65.0
        tier_c_min = 55.0
        missing_data_policy = "renormalise"

        [dimensions.<name>]
        field = "<PROFILE_FIELD_NAMES member>"
        weight = <float in (0, 1]>
        prose = "<operator-authored rubric>"

    Raises `RubricConfigError` for missing file, malformed TOML, schema
    violations, weight-sum violations, tier-ordering violations, or
    unknown profile field names. The error message names the path so
    misconfiguration is operator-debuggable.
    """
    path = Path(path)
    if not path.exists():
        raise RubricConfigError(f"rubric config not found: {path}")

    try:
        with path.open("rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise RubricConfigError(f"malformed TOML in {path}: {exc}") from exc

    defaults = raw.get("defaults")
    if not isinstance(defaults, dict):
        raise RubricConfigError(f"{path}: missing or non-table [defaults] section")

    required_default_keys = (
        "floor",
        "min_coverage",
        "tier_a_min",
        "tier_b_min",
        "tier_c_min",
        "missing_data_policy",
    )
    for key in required_default_keys:
        if key not in defaults:
            raise RubricConfigError(f"{path}: [defaults] missing required key {key!r}")

    dims_section = raw.get("dimensions")
    if not isinstance(dims_section, dict) or not dims_section:
        raise RubricConfigError(
            f"{path}: missing or empty [dimensions] section "
            "(at least one [dimensions.<name>] required)"
        )

    dim_configs: list[DimensionConfig] = []
    for dim_name, body in dims_section.items():
        if not isinstance(body, dict):
            raise RubricConfigError(f"{path}: [dimensions.{dim_name}] must be a table")
        for required in ("field", "weight", "prose"):
            if required not in body:
                raise RubricConfigError(
                    f"{path}: [dimensions.{dim_name}] missing required key {required!r}"
                )
        try:
            dim_configs.append(
                DimensionConfig(
                    name=dim_name,
                    field=str(body["field"]),
                    weight=float(body["weight"]),
                    prose=str(body["prose"]),
                )
            )
        except ValueError as exc:
            raise RubricConfigError(f"{path}: [dimensions.{dim_name}] {exc}") from exc

    try:
        return RubricConfig(
            floor=float(defaults["floor"]),
            min_coverage=float(defaults["min_coverage"]),
            tier_a_min=float(defaults["tier_a_min"]),
            tier_b_min=float(defaults["tier_b_min"]),
            tier_c_min=float(defaults["tier_c_min"]),
            missing_data_policy=str(defaults["missing_data_policy"]),
            dimensions=dim_configs,
        )
    except (TypeError, ValueError) as exc:
        raise RubricConfigError(f"{path}: {exc}") from exc
