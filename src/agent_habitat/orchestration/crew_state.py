"""CrewState — the shared LangGraph state for the Phase 2 lead-enrichment crew.

Per ADR-006 §1: a single TypedDict; additive writes; no upstream mutation.
Each node returns a partial dict; LangGraph merges it into the shared state.

Invariants (structural, enforced by node-implementation discipline rather
than runtime checks — ADR-006 §1):

  - Each node writes ONLY its own field plus the bookkeeping fields it owns.
  - All upstream agent outputs remain reachable from every downstream node;
    this is what makes ADR-006 §3's fabrication-resistance substring check
    mechanically possible from a downstream agent's perspective.

`workflow_id` is the audit-table primary key AND LangGraph's `thread_id`
(ADR-002 Option 1 + ADR-001's "shared identifier" claim). `company_name` is
the input. The four `<output>` fields below are written by the four Phase 2
agent adapters; `drafter_approved` is written by the checkpoint node;
`terminate_reason` is written by `terminate_no_draft` when the workflow
ends early.

The Slice 7 critic adds `critique` + `fabrication_retries` to this state
ADDITIVELY when it lands — Phase 2 Slice 6 does NOT pre-empt those keys.
"""

from __future__ import annotations

from typing import TypedDict

from ..agents.models import CompanyProfile, Critique, Draft, RawSignals, ScoredCompany


class CrewState(TypedDict, total=False):
    """Shared state for the lead-enrichment crew graph.

    `total=False` because LangGraph node functions return partial dicts;
    a key is present only after the node that produces it has run. The
    standard LangGraph reducer overwrites keys (no append semantics) —
    matching ADR-006 §1's "additive writes, no upstream mutation"
    discipline at the per-key granularity.

    Field ownership (which adapter writes which key):

      researcher_adapter            → raw_signals
      extractor_adapter             → profile
      scorer_adapter                → scored_company
      request_drafter_approval      → drafter_approved
      drafter_adapter               → draft
      critic_adapter                → critique, fabrication_retries
      terminate_no_draft            → terminate_reason
      terminate_with_critic_failure → terminate_reason

    `workflow_id` and `company_name` are set by the orchestrator entry
    point (`run_crew`) before the first `graph.invoke`.

    `critique` and `fabrication_retries` (Slice 7) are additive to the
    Slice 6 state shape — the bounded-retry edge per ADR-006 §1 lives
    here. `fabrication_retries` starts at 0 (implicit, `total=False`);
    the critic adapter increments it before routing back to the drafter.
    """

    workflow_id: str
    company_name: str
    raw_signals: RawSignals
    profile: CompanyProfile
    scored_company: ScoredCompany
    drafter_approved: bool
    draft: Draft
    critique: Critique
    fabrication_retries: int
    terminate_reason: str


# Reasons recorded on `CrewState.terminate_reason` when the workflow ends
# without producing a draft. These distinguish the four structurally-
# different empty-outcomes per ADR-006 §1 + §3: the gated-by-score case,
# the gated-by-coverage case, the human-rejection case, and the Slice 7
# critic-rejection case (fabrication-retry exhausted). All four finalise
# the workflow as COMPLETED in the COMPLETED-no-draft sense for the first
# three; the critic-failure case finalises FAILED per ADR-006 §1 (a
# persistent fabrication is a halt-worthy contract violation, not a
# benign empty-outcome).
TERMINATE_REASON_SCORE_GATED = "score_gated"
TERMINATE_REASON_COVERAGE_GATED = "coverage_gated"
TERMINATE_REASON_REJECTED = "rejected"
TERMINATE_REASON_CRITIC_FAILURE = "critic_failure"


__all__ = [
    "TERMINATE_REASON_COVERAGE_GATED",
    "TERMINATE_REASON_CRITIC_FAILURE",
    "TERMINATE_REASON_REJECTED",
    "TERMINATE_REASON_SCORE_GATED",
    "CrewState",
]
