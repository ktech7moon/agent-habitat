"""URL summarizer demo agent — Phase 1 Slice 6.

Three steps (fetch / parse / summarize) threaded through the habitat via
run_step(): state.persistence, observability, llm.py, and budget tracking.

Failure contract: any step error records a FAILED workflow with
finished_at stamped, emits step.failed + workflow.failed, and returns
SummarizerResult(status=FAILED). Never leaves a workflow stuck in RUNNING.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import structlog
from bs4 import BeautifulSoup

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

log = structlog.get_logger(__name__)

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


@dataclass(frozen=True)
class SummarizerResult:
    """What `run_summarizer` returns. Mirrors the workflow it just wrote.

    `input_truncated` plus the three char counts surface the MAX_PROMPT_CHARS
    truncation so a caller can see "this summary was of truncated input"
    without digging through the event log — the input-side analogue of
    `LLMResult.truncated` for output-side max_tokens cutoff. The counts are
    char counts of the extracted readable text, not byte counts.
    """

    workflow_id: str
    status: WorkflowStatus
    url: str
    summary: str | None
    cost_usd: float
    error_step: str | None = None
    error_message: str | None = None
    input_truncated: bool = False
    original_chars: int = 0
    used_chars: int = 0
    dropped_chars: int = 0


@dataclass(frozen=True)
class _TruncationInfo:
    """Internal: what the summarize step did to its input before the LLM call."""

    truncated: bool
    original_chars: int
    used_chars: int
    dropped_chars: int


@dataclass(frozen=True)
class SummarizerNodeOutput:
    """Layer A return shape for the summarize step.

    The wrapper (`run_summarizer`) applies `cost_usd` / `output_ref` /
    `structured_data` (and the truncation extras when present) onto the
    `run_step` recorder.

    The summarizer is a 3-step intra-agent workflow (fetch / parse /
    summarize); the first two pure layers already exist as `fetch_url`
    and `extract_readable_text`. This dataclass is the third step's
    pure-output carrier, kept consistent with the Phase 2 agents'
    `*NodeOutput` shape.
    """

    summary: str
    cost_usd: float
    output_ref: str
    structured_data: dict[str, Any]
    truncation: _TruncationInfo


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


def _utcnow() -> datetime:
    return datetime.now(UTC)


def summarize_text(
    readable: str,
    *,
    workflow_id: str,
    log_root: Path | None = None,
) -> SummarizerNodeOutput:
    """Layer A: pure summarize-step logic.

    Applies the MAX_PROMPT_CHARS truncation contract, calls
    `llm.complete()` (Sonnet), and returns the typed summary plus the
    telemetry the wrapper records onto its step. Wraps LLM exceptions
    in `SummarizerError("summarize", ...)` so the caller's audit shape
    (step `agent_name="summarize"` finalised FAILED) is preserved.

    Does NOT touch the database, does NOT call `run_step`. `workflow_id`
    flows through to `llm.complete()` for telemetry attribution only.
    """
    original_chars = len(readable)
    used_chars = min(original_chars, MAX_PROMPT_CHARS)
    dropped_chars = original_chars - used_chars
    truncation = _TruncationInfo(
        truncated=dropped_chars > 0,
        original_chars=original_chars,
        used_chars=used_chars,
        dropped_chars=dropped_chars,
    )
    try:
        llm_result = complete(
            model_tier=ModelTier.SONNET,
            messages=[{"role": "user", "content": readable[:MAX_PROMPT_CHARS]}],
            workflow_id=workflow_id,
            agent_name="url_summarizer",
            system=SYSTEM_PROMPT,
            max_tokens=SUMMARIZE_MAX_TOKENS,
            log_root=log_root,
        )
    except Exception as exc:
        raise SummarizerError("summarize", f"{type(exc).__name__}: {exc}") from exc

    structured: dict[str, Any] = {
        "input_tokens": llm_result.input_tokens,
        "output_tokens": llm_result.output_tokens,
        "truncated": llm_result.truncated,
    }
    if truncation.truncated:
        structured.update(
            {
                "input_truncated": True,
                "original_chars": truncation.original_chars,
                "used_chars": truncation.used_chars,
                "dropped_chars": truncation.dropped_chars,
            }
        )
    return SummarizerNodeOutput(
        summary=llm_result.content.strip(),
        cost_usd=llm_result.cost_usd,
        output_ref=llm_result.jsonl_ref,
        structured_data=structured,
        truncation=truncation,
    )


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
        workflow_type="url_summarizer",
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
        structured_data={"url": url, "workflow_type": "url_summarizer"},
        timestamp=clock(),
    )
    log.info("summarizer.run.start", workflow_id=wf_id, url=url)

    summary: str | None = None
    llm_ref = ""
    truncation = _TruncationInfo(truncated=False, original_chars=0, used_chars=0, dropped_chars=0)
    html = ""
    readable = ""

    try:
        with run_step(conn, workflow_id=wf_id, step_index=1, agent_name="fetch", now=clock):
            html = fetch_url(url, http_client=http_client)

        with run_step(conn, workflow_id=wf_id, step_index=2, agent_name="parse", now=clock):
            readable = extract_readable_text(html)

        with run_step(
            conn, workflow_id=wf_id, step_index=3, agent_name="summarize", now=clock
        ) as step:
            node_out = summarize_text(readable, workflow_id=wf_id, log_root=log_root)
            step.record_cost(node_out.cost_usd)
            step.record_output_ref(node_out.output_ref)
            step.record_structured_data(node_out.structured_data)
            summary = node_out.summary
            llm_ref = node_out.output_ref
            truncation = node_out.truncation

    except SummarizerError as exc:
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
        input_truncated=truncation.truncated,
        original_chars=truncation.original_chars,
        used_chars=truncation.used_chars,
        dropped_chars=truncation.dropped_chars,
    )
