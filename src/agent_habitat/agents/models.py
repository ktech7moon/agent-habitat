"""Cross-agent handoff models (Pydantic v2).

Per ADR-006 §1: each Phase 2 agent has a typed output model that downstream
agents receive over LangGraph state. Slice 2 lands the Researcher's output —
`RawSignals`. Later slices add `CompanyProfile`, `ScoredCompany`, `Draft`,
`Critique` here as they're built.

`RawSignals` is also the upstream half of ADR-006 §3's fabrication-resistance
contract: the drafter may cite only text that appears verbatim in one of
these signal records (after whitespace+case normalisation). See `Signal`'s
docstring for why `text` is sourced from web_search citation spans rather
than from the model's narrative summary.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


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
