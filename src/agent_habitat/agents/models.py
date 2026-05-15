"""Cross-agent handoff models (Pydantic v2).

Per ADR-006 §1: each Phase 2 agent has a typed output model that downstream
agents receive over LangGraph state. Slice 2 landed the Researcher's output —
`RawSignals`. Slice 3 landed the Extractor's output — `CompanyProfile` plus
the supporting `ProfileField` / `SourceSpan` / `ExtractionGap` shapes. Slice 4
lands the Scorer's output — `ScoredCompany` and its per-dimension carrier
`DimensionScore`. Later slices add `Draft`, `Critique` here as they're built.

`RawSignals` is also the upstream half of ADR-006 §3's fabrication-resistance
contract: the drafter may cite only text that appears verbatim in one of
these signal records (after whitespace+case normalisation). See `Signal`'s
docstring for why `text` is sourced from web_search citation spans rather
than from the model's narrative summary.

`CompanyProfile` extends the same discipline one hop downstream: every
extracted field carries a `SourceSpan` (signal index + verbatim quote) back
into the upstream `RawSignals`. The Extractor enforces substring grounding
on every quote; an over-reaching quote (text not present in the cited
signal) is downgraded to an `ExtractionGap` rather than allowed through.

`ScoredCompany` extends the chain again: each per-dimension reasoning carries
a `grounded_quote` that must substring-match (same normalisation rule) one of
the cited `ProfileField`'s `source_spans`. The Scorer downgrades over-reach
to an excluded dimension, mirroring Slice 3's `_ground_field` shape. The
five-hop grounding chain (ADR-004 §3) now runs `Citation.cited_text →
Signal.text → ProfileField.source_spans → ScoredCompany.dimensions[].grounded_quote
→ Draft claim`, each a substring of the previous.
"""

from __future__ import annotations

from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Signal(BaseModel):
    """One grounded signal about a company — verbatim source span + URL.

    `text` MUST be a `cited_text` span from a `web_search_result_location`
    citation in the researcher's API response — verbatim source content
    Anthropic's web_search tool returned and the model chose to ground a
    claim against. It is NEVER the model's own narrative phrasing.

    Why this discipline matters: the drafter's substring check (ADR-006 §3)
    grounds against the concatenated text of upstream Signal records. If
    `text` were the model's summary, the substring check would be checking
    against paraphrase rather than source — exactly the dual-source-of-truth
    failure ADR-003 and ADR-006 §3 exist to prevent.
    """

    model_config = ConfigDict(frozen=True)

    text: str = Field(..., description="Verbatim source span from a web_search citation.")
    source_url: str = Field(..., description="The URL the citation points at.")
    source_title: str | None = Field(
        default=None,
        description="Page title from the citation, if the tool returned one.",
    )
    retrieved_at: datetime = Field(
        ...,
        description="Wall-clock UTC time the researcher's API call returned this citation.",
    )


class RawSignals(BaseModel):
    """Researcher's handoff payload — a list of grounded signals about a company.

    Empty `signals` is a VALID result (ADR-006 §1: a researcher that finds
    no usable signals returns a structured-but-empty `RawSignals`; the
    orchestrator routes that workflow to `terminate_no_draft`). It is NOT
    an error. Callers should treat `signal_count == 0` as a legitimate
    early-termination signal, not a failure to report.

    `signal_count` and `source_count` are the projection ADR-006 §1 names
    for the researcher's `step.completed` event — computed from `signals`
    so they cannot drift.
    """

    model_config = ConfigDict(frozen=True)

    company_name: str = Field(..., description="The company the researcher was asked about.")
    signals: list[Signal] = Field(default_factory=list)

    @property
    def signal_count(self) -> int:
        """Total signal records, including those from the same source URL."""
        return len(self.signals)

    @property
    def source_count(self) -> int:
        """Distinct source URLs across all signals."""
        return len({s.source_url for s in self.signals})


# ---------------------------------------------------------------------------
# Extractor output — CompanyProfile + ExtractionGap + source-span refs.
# Slice 3 (Phase 2). Each field is either an extracted value+grounding or
# an explicit gap; ExtractionGap is the PATTERNS.md #2 pattern made typed.
# ---------------------------------------------------------------------------


PROFILE_FIELD_NAMES: tuple[str, ...] = (
    "size",
    "industry",
    "tech_stack",
    "recent_news",
    "decision_makers",
)
"""The CompanyProfile fields the Extractor populates. Order is the canonical
prompt + projection order; downstream agents iterate via this tuple rather
than relying on dict insertion order."""


class SourceSpan(BaseModel):
    """A verbatim substring of a `Signal.text` the extractor grounded a field against.

    `quote` MUST be a verbatim substring of
    `raw_signals.signals[signal_index].text` after whitespace+case
    normalisation. The Extractor validates this on every extracted field; a
    quote that does not substring-match is downgraded to `ExtractionGap`
    (reason="span_not_grounded") rather than allowed through. This is the
    upstream half of ADR-006 §3's substring-check discipline — the extractor
    can ground only what the researcher actually surfaced as `cited_text`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    signal_index: int = Field(
        ...,
        ge=0,
        description="Index into the originating RawSignals.signals list.",
    )
    quote: str = Field(
        ...,
        min_length=1,
        description="Verbatim substring of signals[signal_index].text (normalised).",
    )


class ExtractionGap(BaseModel):
    """Audit-grade record that a profile field could not be filled.

    Per PATTERNS.md #2, the absence of data is itself data — a gap is
    structurally distinct from a silent null. Each gap carries a
    human/machine-readable `reason` so an auditor can distinguish "the
    signals did not cover this field" from "the cited span was too short
    to support extraction" from "the model attempted extraction but the
    quote did not substring-match the cited signal" (over-reach).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: str = Field(
        ...,
        min_length=1,
        description=(
            "Why this field was not extracted. Conventional values: "
            "'no_signals' (RawSignals was empty), "
            "'field_not_in_signals' (signals did not mention this field), "
            "'insufficient_source_span' (cited spans too narrow to support extraction), "
            "'span_not_grounded' (model proposed a quote that is not a substring of "
            "the cited signal — over-reach, downgraded by the substring validator)."
        ),
    )


class ProfileField(BaseModel):
    """One profile field — EITHER extracted values + grounding OR an explicit gap.

    Mutually exclusive: exactly one of (`values` + `source_spans`) or `gap`
    is populated. Validator enforces this — there is no silent third state
    where a field is both empty and ungapped. `values` is a list to permit
    naturally-multi-valued fields (tech_stack, decision_makers); single-
    valued fields use a one-item list.

    Audit shape: `source_spans` is non-empty whenever `values` is — every
    extracted value lands with at least one verbatim grounding span. This
    is the data ADR-006 §3's substring check will later consume.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    values: list[str] = Field(
        default_factory=list,
        description="Extracted values for this field. Empty iff this is a gap.",
    )
    source_spans: list[SourceSpan] = Field(
        default_factory=list,
        description="Grounding spans, one or more. Empty iff this is a gap.",
    )
    gap: ExtractionGap | None = Field(
        default=None,
        description="Set iff this field was not extracted. None iff `values` is populated.",
    )

    @model_validator(mode="after")
    def _check_exclusive(self) -> Self:
        has_values = bool(self.values)
        has_gap = self.gap is not None
        if has_values == has_gap:
            raise ValueError(
                "ProfileField must be either extracted (values+source_spans) "
                "or a gap — not both, not neither."
            )
        if has_values and len(self.source_spans) == 0:
            raise ValueError("ProfileField with values must have at least one SourceSpan.")
        if has_gap and (self.values or self.source_spans):
            raise ValueError("ProfileField with gap must have empty values and source_spans.")
        return self

    @property
    def is_gap(self) -> bool:
        return self.gap is not None

    @classmethod
    def as_gap(cls, reason: str) -> ProfileField:
        """Construct a gap-shaped ProfileField. Convenience for empty-outcome paths."""
        return cls(gap=ExtractionGap(reason=reason))


class CompanyProfile(BaseModel):
    """Extractor's handoff payload — structured fields about a company.

    Per ADR-006 §1 the field set covers company attributes — size, industry,
    tech_stack, recent_news, decision_makers. Each is a `ProfileField`:
    either an extracted value with `SourceSpan` refs back into the
    originating `RawSignals`, or an `ExtractionGap` surfacing why the field
    could not be filled.

    Empty signals (`RawSignals.signal_count == 0`) is a VALID input and
    produces an all-gaps profile, not a failure — the workflow completes
    successfully (ADR-006 §1 empty-outcome contract carried one hop
    downstream).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    company_name: str = Field(..., description="The company the profile describes.")
    size: ProfileField
    industry: ProfileField
    tech_stack: ProfileField
    recent_news: ProfileField
    decision_makers: ProfileField

    def field(self, name: str) -> ProfileField:
        """Look up a field by name from `PROFILE_FIELD_NAMES`."""
        if name not in PROFILE_FIELD_NAMES:
            raise KeyError(f"unknown profile field: {name!r}")
        return getattr(self, name)  # type: ignore[no-any-return]

    @property
    def gap_count(self) -> int:
        """Number of fields that are gaps rather than extracted values."""
        return sum(1 for name in PROFILE_FIELD_NAMES if self.field(name).is_gap)

    @property
    def extracted_count(self) -> int:
        """Number of fields that were successfully extracted."""
        return len(PROFILE_FIELD_NAMES) - self.gap_count


# ---------------------------------------------------------------------------
# Scorer output — DimensionScore + ScoredCompany. Slice 4 (Phase 2).
# Per ADR-004: renormalise-with-coverage scoring + grounding-quote substring
# check. A dimension whose `field` is a gap on the `CompanyProfile` is
# EXCLUDED (`score=None`, `grounded_quote=None`); its weight is dropped from
# the renormalisation denominator. Grounding-failure (the LLM's
# `grounded_quote` is not a substring of the cited ProfileField's
# `source_spans`) also downgrades the dimension to excluded — same shape as
# Slice 3's `_ground_field`.
# ---------------------------------------------------------------------------


TIER_VALUES: tuple[str, ...] = ("A", "B", "C", "below_c")
"""Tier labels assigned by the Scorer. `below_c` means the composite score
fell below `tier_c_min` (and therefore also below the operator's floor, since
ADR-004 §1 requires `tier_c_min >= floor`). `tier` on `ScoredCompany` is None
when `score` is None (all-gaps profile)."""


GATED_BY_VALUES: tuple[str, ...] = ("score", "coverage")
"""Which gate routed a workflow to `terminate_no_draft`, per ADR-004 §2:
- "score"    — `score < floor`.
- "coverage" — `coverage < min_coverage` (or all-gaps with `min_coverage > 0`).
Coverage takes precedence when both fail: a low-coverage run is the more
informative empty-outcome (the score is not meaningfully comparable). `gated_by`
is None when the run passed both gates (the orchestrator routes to the drafter)."""


class DimensionScore(BaseModel):
    """One dimension's contribution to the composite score.

    A dimension is in one of two structurally-distinct states:

    - **Scored**: `score in [0, 5]`, `grounded_quote` is a verbatim substring
      (after whitespace+case normalisation) of one of the cited
      `ProfileField`'s `source_spans`. `reasoning` explains the score.
    - **Excluded**: `score is None`, `grounded_quote is None`. The weight is
      dropped from the renormalisation denominator. Two reasons reach this
      state: the source field was an `ExtractionGap`, or the LLM's
      grounded_quote failed the substring check (downgraded over-reach, same
      shape as Slice 3's `_ground_field`). `reasoning` distinguishes the two.

    `field` MUST be a member of `PROFILE_FIELD_NAMES`. `weight` is copied from
    the operator's rubric and is in `[0, 1]`; it is preserved on excluded
    dimensions for audit (so the projection can show "this dimension carried
    20% of the rubric, but it was excluded for reason X").
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    field: str = Field(
        ...,
        description="The PROFILE_FIELD_NAMES member this dimension scored.",
    )
    weight: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Operator-authored weight from the rubric; preserved on excluded dimensions for audit.",
    )
    score: float | None = Field(
        default=None,
        ge=0.0,
        le=5.0,
        description="Per-dimension score in [0, 5]; None iff the dimension was excluded.",
    )
    grounded_quote: str | None = Field(
        default=None,
        description=(
            "Verbatim substring (after normalisation) of one of the cited ProfileField's "
            "source_spans; None iff the dimension was excluded."
        ),
    )
    reasoning: str = Field(
        ...,
        min_length=1,
        description="Operator-readable prose explaining the score or the exclusion.",
    )

    @field_validator("field")
    @classmethod
    def _field_in_profile_names(cls, value: str) -> str:
        if value not in PROFILE_FIELD_NAMES:
            raise ValueError(f"field must be one of {PROFILE_FIELD_NAMES}; got {value!r}")
        return value

    @model_validator(mode="after")
    def _check_excluded_consistency(self) -> Self:
        score_is_none = self.score is None
        quote_is_none = self.grounded_quote is None
        if score_is_none != quote_is_none:
            raise ValueError(
                "DimensionScore must have score and grounded_quote both set "
                "(scored) or both None (excluded) — not mixed."
            )
        return self

    @property
    def is_excluded(self) -> bool:
        return self.score is None


class ScoredCompany(BaseModel):
    """Scorer's handoff payload — composite score + per-dimension records.

    ADR-004 §2's renormalise-with-coverage policy travels on this shape:

    - `score` is the renormalised composite in `[0, 100]`, or `None` when
      every dimension was excluded (the all-gaps `CompanyProfile` path).
    - `coverage` is `sum(weight for present dimensions)` in `[0, 1]`. A
      run with `coverage = 0.0` has no scorable dimensions; one with
      `coverage = 1.0` covered the whole rubric.
    - `floor` and `min_coverage` are echoed from the operator's rubric
      onto the ScoredCompany so an auditor inspecting one row sees both
      the produced score and the bar it was judged against without
      resolving a separate config file.
    - `passed_floor` is `score is not None and score >= floor`.
    - `passed_coverage` is `coverage >= min_coverage` (always True when
      `min_coverage = 0`).
    - `tier` is `"A" | "B" | "C" | "below_c"` per the rubric thresholds,
      or None iff `score` is None.
    - `gated_by` names the gate that would route this workflow to
      `terminate_no_draft`: `"coverage"` if the coverage gate failed
      (takes precedence), else `"score"` if the floor gate failed, else
      None (both gates passed; the orchestrator routes to the drafter).

    The Scorer itself does NOT do the routing — it produces this honest
    record and the orchestrator (Slice 6) reads `gated_by` to decide.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    company_name: str = Field(..., description="The company the score describes.")
    score: float | None = Field(
        default=None,
        ge=0.0,
        le=100.0,
        description="Composite renormalised score in [0, 100]; None iff all dimensions were excluded.",
    )
    coverage: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Fraction of total rubric weight that was actually scorable, in [0, 1].",
    )
    floor: float = Field(
        ...,
        ge=0.0,
        le=100.0,
        description="Operator's floor (from the rubric) — score below this routes to terminate_no_draft.",
    )
    min_coverage: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Operator's coverage gate (from the rubric); 0.0 disables the second gate.",
    )
    passed_floor: bool = Field(
        ...,
        description="True iff score is not None and score >= floor.",
    )
    passed_coverage: bool = Field(
        ...,
        description="True iff coverage >= min_coverage.",
    )
    tier: str | None = Field(
        default=None,
        description='"A" | "B" | "C" | "below_c"; None iff score is None.',
    )
    gated_by: str | None = Field(
        default=None,
        description='"score" | "coverage" | None — which gate (if any) failed.',
    )
    dimensions: list[DimensionScore] = Field(
        ...,
        description="One DimensionScore per rubric dimension, in PROFILE_FIELD_NAMES canonical order.",
    )

    @field_validator("tier")
    @classmethod
    def _tier_in_allowed(cls, value: str | None) -> str | None:
        if value is not None and value not in TIER_VALUES:
            raise ValueError(f"tier must be one of {TIER_VALUES} or None; got {value!r}")
        return value

    @field_validator("gated_by")
    @classmethod
    def _gated_by_in_allowed(cls, value: str | None) -> str | None:
        if value is not None and value not in GATED_BY_VALUES:
            raise ValueError(f"gated_by must be one of {GATED_BY_VALUES} or None; got {value!r}")
        return value

    @model_validator(mode="after")
    def _check_invariants(self) -> Self:
        # tier is None iff score is None — they travel together.
        if (self.tier is None) != (self.score is None):
            raise ValueError(
                "tier must be None iff score is None — "
                f"got tier={self.tier!r}, score={self.score!r}"
            )
        return self

    @property
    def routes_to_draft(self) -> bool:
        """True iff this workflow should advance to the drafter (both gates passed)."""
        return self.gated_by is None
