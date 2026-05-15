"""Researcher agent — Phase 2 Slice 2.

Given a company name, makes one Haiku call with Anthropic's `web_search`
server-side tool enabled (ADR-003), captures the model's `cited_text`
spans into a `RawSignals` payload, and persists the full audit chain
through `run_step()` (ADR-006 §2).

Empty-signals outcome is a VALID result, not an error (ADR-006 §1): a
COMPLETED workflow with `signal_count == 0` is the structured "not worth
drafting" answer the orchestrator will later route to `terminate_no_draft`.
Infrastructure errors (API failure, etc.) propagate from `run_step` and
finalise the workflow FAILED — no retries (ADR-006 §1, Slice 1).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import structlog
from anthropic.types import ToolUnionParam, WebSearchTool20250305Param

from ..llm import ModelTier, complete
from ..observability import EventType, emit_event
from ..orchestration.run_step import run_step
from ..state import (
    EventLevel,
    Workflow,
    WorkflowStatus,
    insert_workflow,
    new_workflow_id,
    recompute_cost_total,
    update_workflow,
)
from .models import RawSignals, Signal

log = structlog.get_logger(__name__)

WORKFLOW_TYPE = "lead_enrichment_researcher"
AGENT_NAME = "researcher"

#: Per-run search budget knob. The model decides how many to issue up to
#: this cap; ADR-003 estimates ~3 searches at $0.01 each as the typical
#: shape, with Haiku tokens bringing the realistic per-run cost to
#: ~$0.035-$0.045. Operator-tunable per call.
DEFAULT_MAX_SEARCHES = 3

#: Output token cap on the researcher's narrative. The narrative is NOT
#: what RawSignals.text grounds against (the cited_text spans are) — we
#: just need enough room for the model to finish coherent citations.
RESEARCHER_MAX_TOKENS = 1024

SYSTEM_PROMPT = (
    "You are a research assistant gathering recent, concrete signals about a "
    "company so a downstream agent can decide whether to draft outreach. Use "
    "the web_search tool to find specific facts: recent funding events, "
    "product launches, hiring announcements, regulatory filings, leadership "
    "changes, partnerships, or notable press. Cite each claim against the "
    "source you found it in — your job is to surface signals, not to "
    "summarise from memory. If nothing concrete surfaces, say so plainly; "
    "do not fabricate signals."
)


def _user_prompt(company_name: str) -> str:
    return (
        f"Find one to three recent, concrete signals about the company "
        f'"{company_name}". Cite each claim against its source. Keep your '
        f"answer brief."
    )


def _web_search_tool(*, max_searches: int) -> list[ToolUnionParam]:
    """Anthropic web_search tool config per ADR-003."""
    tool: WebSearchTool20250305Param = {
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": max_searches,
    }
    # TypedDicts in the SDK union don't auto-narrow as list elements; cast is
    # the minimal fix and the runtime shape is exactly WebSearchTool20250305Param.
    return [cast(ToolUnionParam, tool)]


@dataclass(frozen=True)
class ResearcherResult:
    """What `run_researcher` returns. Mirrors the workflow it just wrote.

    Empty `raw_signals.signals` with `status=COMPLETED` is a VALID outcome
    (see module docstring) — `error_step` / `error_message` are populated
    only when the run actually failed.
    """

    workflow_id: str
    status: WorkflowStatus
    company_name: str
    raw_signals: RawSignals
    cost_usd: float
    error_step: str | None = None
    error_message: str | None = None


def _utcnow() -> datetime:
    return datetime.now(UTC)


def run_researcher(
    conn: sqlite3.Connection,
    *,
    company_name: str,
    workflow_id: str | None = None,
    max_searches: int = DEFAULT_MAX_SEARCHES,
    log_root: Path | None = None,
    now: Callable[[], datetime] | None = None,
) -> ResearcherResult:
    """Execute one researcher run end-to-end through the habitat.

    Returns a `ResearcherResult`. Infrastructure errors (LLM API failure,
    SQLite errors) propagate from `run_step` — the step is finalised FAILED,
    the workflow is finalised FAILED with `finished_at` stamped, and the
    exception is re-raised. There are no retries (ADR-006 §1, Slice 1).

    Empty signals are NOT an error: the workflow finalises COMPLETED with
    a populated-but-empty `RawSignals` (ADR-006 §1 empty-outcome contract).
    """
    clock = now or _utcnow
    wf_id = workflow_id or new_workflow_id()

    workflow = Workflow(
        id=wf_id,
        workflow_type=WORKFLOW_TYPE,
        status=WorkflowStatus.RUNNING,
        started_at=clock().isoformat(),
        metadata={"company_name": company_name},
    )
    insert_workflow(conn, workflow)
    emit_event(
        conn,
        workflow_id=wf_id,
        event_type=EventType.WORKFLOW_STARTED,
        level=EventLevel.INFO,
        message=f"researcher workflow started for {company_name!r}",
        structured_data={"company_name": company_name, "workflow_type": WORKFLOW_TYPE},
        timestamp=clock(),
    )
    log.info("researcher.run.start", workflow_id=wf_id, company_name=company_name)

    raw_signals = RawSignals(company_name=company_name, signals=[])
    llm_ref = ""

    try:
        with run_step(
            conn,
            workflow_id=wf_id,
            step_index=1,
            agent_name=AGENT_NAME,
            now=clock,
        ) as step:
            llm_result = complete(
                model_tier=ModelTier.HAIKU,
                messages=[{"role": "user", "content": _user_prompt(company_name)}],
                workflow_id=wf_id,
                agent_name=AGENT_NAME,
                system=SYSTEM_PROMPT,
                max_tokens=RESEARCHER_MAX_TOKENS,
                tools=_web_search_tool(max_searches=max_searches),
                log_root=log_root,
            )

            retrieved_at = clock()
            signals = [
                Signal(
                    text=c.cited_text,
                    source_url=c.source_url,
                    source_title=c.source_title,
                    retrieved_at=retrieved_at,
                )
                for c in llm_result.citations
            ]
            raw_signals = RawSignals(company_name=company_name, signals=signals)

            step.record_cost(llm_result.cost_usd)
            step.record_output_ref(llm_result.jsonl_ref)
            step.record_structured_data(
                {
                    "signal_count": raw_signals.signal_count,
                    "source_count": raw_signals.source_count,
                    "web_searches": llm_result.web_searches,
                }
            )
            llm_ref = llm_result.jsonl_ref
    except Exception as exc:
        finished = clock().isoformat()
        updated = workflow.model_copy(
            update={"status": WorkflowStatus.FAILED, "finished_at": finished}
        )
        update_workflow(conn, updated)
        error_msg = f"{type(exc).__name__}: {exc}"
        emit_event(
            conn,
            workflow_id=wf_id,
            event_type=EventType.WORKFLOW_FAILED,
            level=EventLevel.ERROR,
            message=f"researcher workflow failed: {error_msg}",
            structured_data={"failed_step": AGENT_NAME, "error": error_msg},
            timestamp=clock(),
        )
        cost_total = recompute_cost_total(conn, wf_id)
        log.warning(
            "researcher.run.failed",
            workflow_id=wf_id,
            company_name=company_name,
            error=error_msg,
        )
        return ResearcherResult(
            workflow_id=wf_id,
            status=WorkflowStatus.FAILED,
            company_name=company_name,
            raw_signals=RawSignals(company_name=company_name, signals=[]),
            cost_usd=cost_total,
            error_step=AGENT_NAME,
            error_message=error_msg,
        )

    cost_total = recompute_cost_total(conn, wf_id)
    finished = clock().isoformat()
    completed = workflow.model_copy(
        update={
            "status": WorkflowStatus.COMPLETED,
            "finished_at": finished,
            "cost_total_usd": cost_total,
        }
    )
    update_workflow(conn, completed)
    emit_event(
        conn,
        workflow_id=wf_id,
        event_type=EventType.WORKFLOW_COMPLETED,
        level=EventLevel.INFO,
        message=f"researcher workflow completed for {company_name!r}",
        structured_data={
            "company_name": company_name,
            "cost_total_usd": cost_total,
            "signal_count": raw_signals.signal_count,
            "source_count": raw_signals.source_count,
            "researcher_jsonl_ref": llm_ref,
        },
        timestamp=clock(),
    )
    log.info(
        "researcher.run.complete",
        workflow_id=wf_id,
        company_name=company_name,
        signal_count=raw_signals.signal_count,
        source_count=raw_signals.source_count,
        cost_usd=cost_total,
    )

    return ResearcherResult(
        workflow_id=wf_id,
        status=WorkflowStatus.COMPLETED,
        company_name=company_name,
        raw_signals=raw_signals,
        cost_usd=cost_total,
    )
