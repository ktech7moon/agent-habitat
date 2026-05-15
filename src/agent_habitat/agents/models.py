"""Cross-agent handoff models (Pydantic v2).

Per ADR-006 §1: each Phase 2 agent has a typed output model that downstream
agents receive over LangGraph state. Slice 2 landed the Researcher's output —
`RawSignals`. Slice 3 lands the Extractor's output — `CompanyProfile` plus
the supporting `ProfileField` / `SourceSpan` / `ExtractionGap` shapes. Later
slices add `ScoredCompany`, `Draft`, `Critique` here as they're built.

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
"""

from __future__ import annotations

from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
