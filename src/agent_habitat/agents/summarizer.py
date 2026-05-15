"""URL summarizer demo agent — Phase 1 Slice 6.

First end-to-end workload riding the habitat. Three synchronous steps:

  1. fetch     — httpx GET of the URL, with a size cap on the response body.
  2. parse     — BeautifulSoup readable-text extraction (lean; no scrapy-grade
                 boilerplate stripping).
  3. summarize — `llm.complete()` (Sonnet tier) over the extracted text.

The point of this slice is not the summarizer; the summarizer is the load.
A single run exercises every Phase 1 habitat primitive:

  - state.persistence  — workflow + step rows, lifecycle status transitions.
  - observability      — `emit_event()` for workflow.started/completed/failed
                         and step.started/completed/failed.
  - llm.py             — every LLM call routes through `complete()`; the
                         JSONL telemetry line is referenced from the
                         summarize step's `output_ref`.
  - budget             — the summarize step writes real `cost_usd`;
                         `recompute_cost_total` rolls it up onto
                         `workflows.cost_total_usd`, where the existing
                         daily-cap query primitive can sum it.

Explicitly out of scope (Phase 2):
  - No LangGraph; the orchestrator does not exist yet.
  - No checkpoint invocation; the summarizer has no flagged actions.
  - No active budget enforcement; cost is *recorded* so the existing budget
    primitives can find it. Halting on exceed is the orchestrator's job.

Failure contract: any step error transitions the workflow to FAILED with
`finished_at` stamped, emits a step.failed and workflow.failed event, and
returns a `SummarizerResult` with `status=FAILED`. The run never crashes
uncaught from the caller's perspective, and it never leaves a workflow
stuck in RUNNING.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

import httpx
import structlog
from bs4 import BeautifulSoup

from ..llm import ModelTier, complete
from ..observability import EventType, emit_event
from ..state import (
    EventLevel,
    StepStatus,
    Workflow,
    WorkflowStatus,
    WorkflowStep,
    insert_step,
    insert_workflow,
    new_workflow_id,
    recompute_cost_total,
    update_step,
    update_workflow,
)

log = structlog.get_logger(__name__)

_T = TypeVar("_T")


# ---------------------------------------------------------------------------
# Constants — tuning surface for the agent without touching call sites.
# ---------------------------------------------------------------------------

WORKFLOW_TYPE = "url_summarizer"
AGENT_NAME = "url_summarizer"

#: Max bytes of HTTP response body we will download before bailing out.
#: A 1 MB cap is comfortable headroom for normal web pages and a hard stop
#: against pathological responses (huge PDFs, infinite streams).
MAX_HTML_BYTES = 1_000_000

#: Network + read timeout. The whole point of this agent is observability;
#: a hung fetch should fail loudly rather than stall the workflow.
HTTP_TIMEOUT_SECONDS = 20.0

#: User-Agent we present. Plain, identifiable, no spoofing.
USER_AGENT = "agent-habitat-url-summarizer/0.1 (+https://github.com/agent-habitat)"

#: Minimum extracted-text length we consider "summarizable". Below this the
#: parse step fails: most likely a JS-only SPA, a paywall, or a 200-OK error
#: page. Tuned conservatively — short legitimate pages exist, but for a
#: summarizer below ~80 chars there is nothing useful to summarize.
MIN_READABLE_CHARS = 80

#: Cap on text passed to the LLM. Keeps cost and latency predictable; the
#: Sonnet context window is much larger but we are not paying to summarize
#: appendices for a demo agent.
MAX_PROMPT_CHARS = 12_000

#: Output cap on the summary itself. Sonnet at 512 tokens easily produces
#: 3-5 dense sentences. We track `truncated` via `LLMResult.truncated`.
SUMMARIZE_MAX_TOKENS = 512

SYSTEM_PROMPT = (
    "You write tight, faithful summaries of web pages. "
    "Produce three to five sentences in plain prose — no markdown, no bullet "
    "points, no preamble. Stick to claims the page actually makes; do not "
    "infer beyond the text. If the page is essentially empty or unintelligible, "
    "say so in one sentence."
)


# ---------------------------------------------------------------------------
# Result contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SummarizerResult:
    """What `run_summarizer` returns. Mirrors the workflow it just wrote."""

    workflow_id: str
    status: WorkflowStatus
    url: str
    summary: str | None
    cost_usd: float
    error_step: str | None = None
    error_message: str | None = None


class SummarizerError(Exception):
    """Internal control-flow signal for a step failure.

    Carries the (step_name, message) so the orchestrating function can record
    them onto the failing step row and emit the right events. Never escapes
    `run_summarizer` — callers see a `SummarizerResult` with status=FAILED.
    """

    def __init__(self, step_name: str, message: str) -> None:
        super().__init__(message)
        self.step_name = step_name
        self.message = message


# ---------------------------------------------------------------------------
# Step 1: fetch
# ---------------------------------------------------------------------------


def fetch_url(url: str, *, http_client: httpx.Client | None = None) -> str:
    """GET the URL and return the decoded body, capped at MAX_HTML_BYTES.

    Raises `SummarizerError("fetch", ...)` for any failure mode: bad URL
    scheme, network error, non-2xx response, oversize body, or empty body.
    The caller (`run_summarizer`) turns that into a FAILED step + event.

    Injection point: `http_client` lets tests pass an `httpx.Client` built
    against a `MockTransport`. In production we open one per call.
    """
    if not url or not isinstance(url, str):
        raise SummarizerError("fetch", "url must be a non-empty string")
    parsed_scheme = url.split(":", 1)[0].lower() if ":" in url else ""
    if parsed_scheme not in {"http", "https"}:
        raise SummarizerError(
            "fetch", f"unsupported URL scheme: {parsed_scheme or '(none)'} (need http/https)"
        )

    owns_client = http_client is None
    client = http_client or httpx.Client(
        timeout=HTTP_TIMEOUT_SECONDS,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    )
    try:
        try:
            response = client.get(url)
        except httpx.HTTPError as exc:
            raise SummarizerError("fetch", f"http error fetching {url}: {exc}") from exc

        if response.status_code >= 400:
            raise SummarizerError("fetch", f"non-success status {response.status_code} for {url}")

        body = response.content
        if len(body) > MAX_HTML_BYTES:
            raise SummarizerError(
                "fetch",
                f"response body too large ({len(body)} bytes > cap {MAX_HTML_BYTES})",
            )
        if not body:
            raise SummarizerError("fetch", f"empty response body from {url}")

        # response.text uses the server-declared encoding when present and
        # falls back to charset-detection; either way we get a str.
        return response.text
    finally:
        if owns_client:
            client.close()


# ---------------------------------------------------------------------------
# Step 2: parse
# ---------------------------------------------------------------------------


def extract_readable_text(html: str) -> str:
    """Pull readable text from HTML; lean approach, not scrapy-grade.

    Strategy: drop <script>, <style>, and obvious non-content tags; pull
    text with whitespace-collapsed newlines; if a <main> or <article> tag
    exists, prefer that subtree as the primary content. Returns a single
    cleaned string. Raises `SummarizerError("parse", ...)` if the result
    is below `MIN_READABLE_CHARS` — too thin to summarize.
    """
    if not html:
        raise SummarizerError("parse", "empty html input")

    soup = BeautifulSoup(html, "html.parser")

    for tag_name in ("script", "style", "noscript", "template", "svg"):
        for tag in soup.find_all(tag_name):
            tag.decompose()

    # Prefer the page's main content region when one is explicitly tagged.
    main = soup.find("main") or soup.find("article")
    root = main if main is not None else soup

    text = root.get_text(separator="\n", strip=True)
    # Collapse runs of blank lines: BeautifulSoup separator="\n" emits one
    # per child, which gets noisy.
    lines = [ln for ln in (line.strip() for line in text.splitlines()) if ln]
    cleaned = "\n".join(lines)

    if len(cleaned) < MIN_READABLE_CHARS:
        raise SummarizerError(
            "parse",
            f"extracted text too short ({len(cleaned)} chars < {MIN_READABLE_CHARS}); "
            "page may be JS-only, paywalled, or an error page",
        )
    return cleaned


# ---------------------------------------------------------------------------
# Orchestration — wire the three steps through the habitat
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(UTC)


def run_summarizer(
    conn: sqlite3.Connection,
    *,
    url: str,
    workflow_id: str | None = None,
    log_root: Path | None = None,
    http_client: httpx.Client | None = None,
    now: Callable[[], datetime] | None = None,
) -> SummarizerResult:
    """Execute one URL summarization end-to-end through the habitat.

    Returns a `SummarizerResult`. Does not raise on summarizer-domain errors
    (bad URL, fetch failure, empty page, LLM error); instead, the workflow
    is recorded as FAILED with the offending step's `error_message` and the
    matching events. SQLite errors and programmer errors still propagate.

    Args:
      conn: an open connection to an initialised agent-habitat DB.
      url: the page to summarize.
      workflow_id: optional override; defaults to a fresh uuid4 hex.
      log_root: optional override for the JSONL telemetry root (used by tests).
      http_client: optional injected `httpx.Client` (used by tests).
      now: optional clock factory (used by tests for deterministic timestamps).
    """
    clock = now or _utcnow
    wf_id = workflow_id or new_workflow_id()

    workflow = Workflow(
        id=wf_id,
        workflow_type=WORKFLOW_TYPE,
        status=WorkflowStatus.RUNNING,
        started_at=clock().isoformat(),
        metadata={"url": url},
    )
    insert_workflow(conn, workflow)
    emit_event(
        conn,
        workflow_id=wf_id,
        event_type=EventType.WORKFLOW_STARTED,
        level=EventLevel.INFO,
        message=f"url_summarizer workflow started for {url}",
        structured_data={"url": url, "workflow_type": WORKFLOW_TYPE},
        timestamp=clock(),
    )

    log.info("summarizer.run.start", workflow_id=wf_id, url=url)

    try:
        html = _run_step(
            conn,
            workflow_id=wf_id,
            step_index=1,
            step_name="fetch",
            now=clock,
            work=lambda: fetch_url(url, http_client=http_client),
        )

        readable = _run_step(
            conn,
            workflow_id=wf_id,
            step_index=2,
            step_name="parse",
            now=clock,
            work=lambda: extract_readable_text(html),
        )

        summary, llm_cost, llm_ref = _run_summarize_step(
            conn,
            workflow_id=wf_id,
            step_index=3,
            readable_text=readable,
            log_root=log_root,
            now=clock,
        )

    except SummarizerError as exc:
        # Step row + step.failed event have already been written by the step
        # helper. Close out the workflow.
        finished = clock().isoformat()
        updated = workflow.model_copy(
            update={"status": WorkflowStatus.FAILED, "finished_at": finished}
        )
        update_workflow(conn, updated)
        emit_event(
            conn,
            workflow_id=wf_id,
            event_type=EventType.WORKFLOW_FAILED,
            level=EventLevel.ERROR,
            message=f"url_summarizer workflow failed at {exc.step_name}: {exc.message}",
            structured_data={"failed_step": exc.step_name, "error": exc.message},
            timestamp=clock(),
        )
        cost_total = recompute_cost_total(conn, wf_id)
        log.warning(
            "summarizer.run.failed",
            workflow_id=wf_id,
            url=url,
            failed_step=exc.step_name,
            error=exc.message,
        )
        return SummarizerResult(
            workflow_id=wf_id,
            status=WorkflowStatus.FAILED,
            url=url,
            summary=None,
            cost_usd=cost_total,
            error_step=exc.step_name,
            error_message=exc.message,
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
        message=f"url_summarizer workflow completed for {url}",
        structured_data={
            "url": url,
            "cost_total_usd": cost_total,
            "summarize_jsonl_ref": llm_ref,
        },
        timestamp=clock(),
    )
    log.info(
        "summarizer.run.complete",
        workflow_id=wf_id,
        url=url,
        cost_usd=cost_total,
        jsonl_ref=llm_ref,
    )

    return SummarizerResult(
        workflow_id=wf_id,
        status=WorkflowStatus.COMPLETED,
        url=url,
        summary=summary,
        cost_usd=cost_total,
    )


# ---------------------------------------------------------------------------
# Step helpers
# ---------------------------------------------------------------------------


def _run_step(
    conn: sqlite3.Connection,
    *,
    workflow_id: str,
    step_index: int,
    step_name: str,
    now: Callable[[], datetime],
    work: Callable[[], _T],
) -> _T:
    """Run a non-LLM step: insert RUNNING row, emit step.started, do work,
    finalize the row + emit step.completed/failed.

    Raises `SummarizerError` for `work` failures after persisting the
    FAILED step + step.failed event.
    """
    started = now().isoformat()
    step = WorkflowStep(
        workflow_id=workflow_id,
        step_index=step_index,
        agent_name=step_name,
        status=StepStatus.RUNNING,
        started_at=started,
    )
    insert_step(conn, step)
    emit_event(
        conn,
        workflow_id=workflow_id,
        event_type=EventType.STEP_STARTED,
        level=EventLevel.INFO,
        message=f"step started: {step_name}",
        structured_data={"step_name": step_name, "step_index": step_index},
        step_id=step.id,
        timestamp=now(),
    )

    try:
        result = work()
    except SummarizerError as exc:
        finished_at = now().isoformat()
        update_step(
            conn,
            step.model_copy(
                update={
                    "status": StepStatus.FAILED,
                    "finished_at": finished_at,
                    "error_message": exc.message,
                }
            ),
        )
        emit_event(
            conn,
            workflow_id=workflow_id,
            event_type=EventType.STEP_FAILED,
            level=EventLevel.ERROR,
            message=f"step failed: {step_name}: {exc.message}",
            structured_data={
                "step_name": step_name,
                "step_index": step_index,
                "error": exc.message,
            },
            step_id=step.id,
            timestamp=now(),
        )
        raise

    finished_at = now().isoformat()
    update_step(
        conn,
        step.model_copy(update={"status": StepStatus.COMPLETED, "finished_at": finished_at}),
    )
    emit_event(
        conn,
        workflow_id=workflow_id,
        event_type=EventType.STEP_COMPLETED,
        level=EventLevel.INFO,
        message=f"step completed: {step_name}",
        structured_data={"step_name": step_name, "step_index": step_index},
        step_id=step.id,
        timestamp=now(),
    )
    return result


def _run_summarize_step(
    conn: sqlite3.Connection,
    *,
    workflow_id: str,
    step_index: int,
    readable_text: str,
    log_root: Path | None,
    now: Callable[[], datetime],
) -> tuple[str, float, str]:
    """The LLM-bearing step. Records `cost_usd` + `output_ref` onto the row.

    Returns `(summary, cost_usd, jsonl_ref)`. Raises `SummarizerError("summarize",
    ...)` on any LLM/API exception, after persisting the FAILED step + event.
    """
    started = now().isoformat()
    step = WorkflowStep(
        workflow_id=workflow_id,
        step_index=step_index,
        agent_name="summarize",
        status=StepStatus.RUNNING,
        started_at=started,
    )
    insert_step(conn, step)
    emit_event(
        conn,
        workflow_id=workflow_id,
        event_type=EventType.STEP_STARTED,
        level=EventLevel.INFO,
        message="step started: summarize",
        structured_data={"step_name": "summarize", "step_index": step_index},
        step_id=step.id,
        timestamp=now(),
    )

    prompt_text = readable_text[:MAX_PROMPT_CHARS]

    try:
        result = complete(
            model_tier=ModelTier.SONNET,
            messages=[{"role": "user", "content": prompt_text}],
            workflow_id=workflow_id,
            agent_name=AGENT_NAME,
            system=SYSTEM_PROMPT,
            max_tokens=SUMMARIZE_MAX_TOKENS,
            log_root=log_root,
        )
    except Exception as exc:
        finished_at = now().isoformat()
        message = f"{type(exc).__name__}: {exc}"
        update_step(
            conn,
            step.model_copy(
                update={
                    "status": StepStatus.FAILED,
                    "finished_at": finished_at,
                    "error_message": message,
                }
            ),
        )
        emit_event(
            conn,
            workflow_id=workflow_id,
            event_type=EventType.STEP_FAILED,
            level=EventLevel.ERROR,
            message=f"step failed: summarize: {message}",
            structured_data={
                "step_name": "summarize",
                "step_index": step_index,
                "error": message,
            },
            step_id=step.id,
            timestamp=now(),
        )
        raise SummarizerError("summarize", message) from exc

    finished_at = now().isoformat()
    update_step(
        conn,
        step.model_copy(
            update={
                "status": StepStatus.COMPLETED,
                "finished_at": finished_at,
                "output_ref": result.jsonl_ref,
                "cost_usd": result.cost_usd,
            }
        ),
    )
    emit_event(
        conn,
        workflow_id=workflow_id,
        event_type=EventType.STEP_COMPLETED,
        level=EventLevel.INFO,
        message="step completed: summarize",
        structured_data={
            "step_name": "summarize",
            "step_index": step_index,
            "cost_usd": result.cost_usd,
            "output_ref": result.jsonl_ref,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "truncated": result.truncated,
        },
        step_id=step.id,
        timestamp=now(),
    )

    return result.content.strip(), result.cost_usd, result.jsonl_ref
