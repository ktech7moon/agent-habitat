"""agent-habitat scoring — operator-tunable ICP rubric loading (Slice 4).

This package mirrors `budget/`'s shape: an operator-tunable TOML config
loaded through stdlib `tomllib` with operator-debuggable error messages.
The bundled file at `config/rubric.toml` is a format template (ADR-004 §1);
operators replace its values with their actual Ideal Customer Profile.

The rubric's validated shape is consumed by `agents/scorer.py` (Slice 4):
load once at scorer entry, apply per-dimension across a `CompanyProfile`,
emit a `ScoredCompany` with the renormalise-with-coverage composite.

Per-call scoring math, the grounding check, and the agent's LLM call live
in `agents/scorer.py` — this package owns ONLY the configuration surface
(load + validate). That keeps the operator-vs-developer split honest:
operators touch this file's input (the TOML); developers touch the agent.
"""

from .rubric import (
    DEFAULT_RUBRIC_PATH,
    MISSING_DATA_POLICY_RENORMALISE,
    DimensionConfig,
    RubricConfig,
    RubricConfigError,
    load_rubric,
)

__all__ = [
    "DEFAULT_RUBRIC_PATH",
    "MISSING_DATA_POLICY_RENORMALISE",
    "DimensionConfig",
    "RubricConfig",
    "RubricConfigError",
    "load_rubric",
]
