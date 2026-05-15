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

from ..agents.models import CompanyProfile, Draft, RawSignals, ScoredCompany


class CrewState(TypedDict, total=False):
    """Shared state for the lead-enrichment crew graph.

    `total=False` because LangGraph node functions return partial dicts;
    a key is present only after the node that produces it has run. The
    standard LangGraph reducer overwrites keys (no append semantics) —
    matching ADR-006 §1's "additive writes, no upstream mutation"
    discipline at the per-key granularity.

    Field ownership (which adapter writes which key):

      researcher_adapter        → raw_signals
      extractor_adapter         → profile
      scorer_adapter            → scored_company
      request_drafter_approval  → drafter_approved
      drafter_adapter           → draft
      terminate_no_draft        → terminate_reason

    `workflow_id` and `company_name` are set by the orchestrator entry
    point (`run_crew`) before the first `graph.invoke`.
    """

    workflow_id: str
    company_name: str
    raw_signals: RawSignals
    profile: CompanyProfile
    scored_company: ScoredCompany
    drafter_approved: bool
    draft: Draft
    terminate_reason: str


# Reasons recorded on `CrewState.terminate_reason` when the workflow ends
# without producing a draft. These distinguish the three structurally-
# different empty-outcomes per ADR-006 §1: the gated-by-score case, the
# gated-by-coverage case, and the human-rejection case. All three finalise
# the workflow as COMPLETED (gating is not a failure); the audit row's
# `workflow.note` event names the reason.
TERMINATE_REASON_SCORE_GATED = "score_gated"
TERMINATE_REASON_COVERAGE_GATED = "coverage_gated"
TERMINATE_REASON_REJECTED = "rejected"


__all__ = [
    "TERMINATE_REASON_COVERAGE_GATED",
    "TERMINATE_REASON_REJECTED",
    "TERMINATE_REASON_SCORE_GATED",
    "CrewState",
]
