"""Single entry point for all LLM calls.

Every LLM call in agent-habitat flows through `complete()` — agent code MUST
NOT import the `anthropic` SDK directly (CLAUDE.md rule 9). This wrapper owns:

- Three-tier model dispatch (Haiku / Sonnet / Opus 4.7).
- Per-call cost computation from a date-stamped rate table.
- JSONL telemetry append to `data/logs/YYYY-MM-DD/<workflow_id>.jsonl`.
- The `LLMResult` return contract that ADR-002 specifies as the input to
  Slice 2's audit-row writes.

Scope (Slice 1): per-call cost + telemetry. Budget caps and cross-call
aggregation (Slice 3), the full ObservabilityLayer (Slice 4), retries, and
any agent code are out of scope here.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

import structlog
from anthropic import Anthropic
from anthropic.types import (
    CitationsWebSearchResultLocation,
    MessageParam,
    TextBlock,
    ToolUnionParam,
)
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, computed_field

from .state.models import validate_workflow_id_for_path

log = structlog.get_logger(__name__)


def _bootstrap() -> None:
    """Load environment variables from .env into ``os.environ``.

    Importing ``llm.py`` from a library context (tests, downstream
    framework integration) should NOT read .env from disk — library
    side-effects on import are surprising and break reproducible tests.
    Call this from CLI entrypoints (see ``cli.py::main``) so that
    ``ANTHROPIC_API_KEY`` is available when ``_get_client()`` first
    constructs the Anthropic client. Tests that exercise live calls set
    the env var directly via pytest fixtures.
    """
    load_dotenv()


class ModelTier(str, Enum):
    """Three-tier model routing. Default DOWN — escalate only when quality requires it."""

    HAIKU = "claude-haiku-4-5-20251001"
    SONNET = "claude-sonnet-4-6"
    OPUS = "claude-opus-4-7"


# ---------------------------------------------------------------------------
# RATE TABLE — values are USD per 1,000,000 tokens (input, output).
#
# Verified: 2026-05-15 against
# https://platform.claude.com/docs/en/about-claude/pricing
#   - Claude Haiku 4.5  : $1 / $5  per MTok (unchanged from 2026-05-13)
#   - Claude Sonnet 4.6 : $3 / $15 per MTok (unchanged from 2026-05-13)
#   - Claude Opus 4.7   : $5 / $25 per MTok (CHANGED — was $15/$75 at
#                         kickoff; Anthropic repriced Opus 4.7 to be 3×
#                         cheaper than Opus 4.1, the older rate. Slice 8's
#                         calibration cost numbers reflect the OLD rates as
#                         recorded in the JSONL telemetry; ADR-006 §1's
#                         calibration footnote names the rate change.
#                         Cost figures emitted post-this-stamp will be
#                         lower than the Slice 8 numbers for any
#                         Opus-bearing run.)
# When re-verifying, bump the date stamp above.
# ---------------------------------------------------------------------------
_RATES_USD_PER_MTOK: dict[ModelTier, tuple[float, float]] = {
    ModelTier.HAIKU: (1.00, 5.00),
    ModelTier.SONNET: (3.00, 15.00),
    ModelTier.OPUS: (5.00, 25.00),
}


# ---------------------------------------------------------------------------
# SERVER-TOOL FEE — $10 per 1,000 web_search requests = $0.01 per search,
# billed in addition to the token costs above (ADR-003).
#
# Verified: 2026-05-15 against
# https://platform.claude.com/docs/en/about-claude/pricing#web-search-tool
# (unchanged from the 2026-05-14 stamp). When re-verifying, bump the date.
# ---------------------------------------------------------------------------
_WEB_SEARCH_FEE_USD: float = 0.01


class Citation(BaseModel):
    """One `web_search_result_location` citation emitted by the model.

    `cited_text` is the verbatim source span the model grounded a claim
    against — surfaced by Anthropic's web_search tool, NOT the model's
    own narrative text. This is the corpus the fabrication-resistance
    substring check (ADR-006 §3) grounds against.

    Note (ADR-003 wrinkle, 2026-05-14): the raw `web_search_tool_result`
    block's per-result `encrypted_content` is opaque (designed for
    multi-turn round-trip). `cited_text` is the plain-readable equivalent
    — verbatim source prose tied to a source URL, surfaced by the model
    when it cites a span. Building Signals from these citations honours
    the fabrication-grounding invariant by construction.
    """

    model_config = ConfigDict(frozen=True)

    cited_text: str
    source_url: str
    source_title: str | None = None


class LLMResult(BaseModel):
    """Return contract for every LLM call. Slice 2 wires these fields into workflow_steps."""

    model_config = ConfigDict(frozen=True)

    content: str = Field(..., description="Concatenated text content from the response.")
    model: str = Field(..., description="Resolved model ID used for the call.")
    input_tokens: int
    output_tokens: int
    cost_usd: float
    jsonl_ref: str = Field(
        ...,
        description=(
            "ADR-002 output_ref format: '<path>:<line>' into "
            "data/logs/YYYY-MM-DD/<workflow_id>.jsonl, 1-indexed. "
            "Durable before complete() returns."
        ),
    )
    stop_reason: str | None = Field(
        default=None,
        description=(
            "Raw Anthropic Message.stop_reason — 'end_turn', 'max_tokens', "
            "'stop_sequence', 'tool_use', etc. None only if the API returned "
            "no stop_reason (should not happen for a successful call)."
        ),
    )
    web_searches: int = Field(
        default=0,
        description=(
            "Number of web_search server-tool requests the model issued. "
            "Zero for calls that did not invoke web_search. Additive on top "
            "of the existing contract."
        ),
    )
    citations: list[Citation] = Field(
        default_factory=list,
        description=(
            "Plain-readable `web_search_result_location` citations emitted "
            "by the model — each is a verbatim source span tagged with URL "
            "and title. Empty for calls that did not invoke web_search or "
            "did not produce citations. ADR-003 grounding corpus for the "
            "Researcher's `RawSignals` (see Citation docstring)."
        ),
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def truncated(self) -> bool:
        """Convenience: true iff the response was cut off by max_tokens."""
        return self.stop_reason == "max_tokens"


def compute_cost_usd(
    tier: ModelTier,
    input_tokens: int,
    output_tokens: int,
    *,
    web_search_requests: int = 0,
) -> float:
    """Per-call cost from the rate table plus any server-tool fees.

    Token cost is the input/output product against the dated rate table.
    `web_search_requests` adds `n * $0.01` (server-tool fee, ADR-003) so
    that `LLMResult.cost_usd` remains the single aggregated truth for
    budget primitives (Slice 3) and step-row cost (Slice 2 persistence).
    Pure function — easy to unit-test.
    """
    input_rate, output_rate = _RATES_USD_PER_MTOK[tier]
    token_cost = (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000
    tool_cost = web_search_requests * _WEB_SEARCH_FEE_USD
    return token_cost + tool_cost


def _telemetry_path(log_root: Path, workflow_id: str, now: datetime) -> Path:
    # Defensive path-traversal guard: workflow_id is always uuid4 hex today
    # (see state.models.new_workflow_id), but validate at the boundary so
    # any future caller passing arbitrary text can't escape the log root.
    validate_workflow_id_for_path(workflow_id)
    return log_root / now.strftime("%Y-%m-%d") / f"{workflow_id}.jsonl"


#: In-memory cache of the current 1-indexed line count for every JSONL
#: file written through ``_append_telemetry`` in this process. Keyed by
#: the file's POSIX path so the cache is unique per
#: (log_root, date, workflow_id) — tests that swap ``log_root`` get a
#: distinct key automatically. Bounded by the number of distinct telemetry
#: files a single process writes, which is the number of (date,
#: workflow_id) pairs it serves.
_LINE_COUNT_CACHE: dict[str, int] = {}


def _append_telemetry(path: Path, record: dict[str, Any]) -> int:
    """Append one JSONL line; return its 1-indexed line number.

    SINGLE-WRITER ASSUMPTION (Phase 1-2): workflows run synchronously per
    the roadmap. The in-memory line-count cache (``_LINE_COUNT_CACHE``)
    is correct under that assumption — every append goes through this
    function, so the cached count is the authoritative current count. If
    a second process appends to the same file concurrently the cache would
    drift; this is documented as out-of-scope until cross-process
    coordination is wired (CLAUDE.md deferred list).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    key = path.as_posix()
    cached = _LINE_COUNT_CACHE.get(key)
    if cached is not None:
        existing = cached
    else:
        existing = 0
        if path.exists():
            with path.open("rb") as f:
                for _ in f:
                    existing += 1
    line = json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())
    new_line_no = existing + 1
    _LINE_COUNT_CACHE[key] = new_line_no
    return new_line_no


_client: Anthropic | None = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key or api_key.startswith("sk-ant-REPLACE"):
            raise RuntimeError("ANTHROPIC_API_KEY not set; populate .env from .env.example.")
        _client = Anthropic(api_key=api_key)
    return _client


def _extract_web_search_citations(content: list[Any]) -> list[Citation]:
    """Pull every `web_search_result_location` citation off the response.

    The model emits one TextBlock per cited span; each block's `.citations`
    is a list of typed citation objects. Only `CitationsWebSearchResultLocation`
    citations are relevant here — other citation kinds (file search, doc
    page locations) are out of scope for the Researcher.
    """
    citations: list[Citation] = []
    for block in content:
        if not isinstance(block, TextBlock):
            continue
        block_citations = block.citations or []
        for c in block_citations:
            if not isinstance(c, CitationsWebSearchResultLocation):
                continue
            citations.append(
                Citation(
                    cited_text=c.cited_text,
                    source_url=c.url,
                    source_title=c.title,
                )
            )
    return citations


def _web_search_request_count(usage: Any) -> int:
    """Pull `server_tool_use.web_search_requests` off the response usage.

    Returns 0 when the field is missing (no-tools call, older SDK, etc.).
    """
    server_tool_use = getattr(usage, "server_tool_use", None)
    if server_tool_use is None:
        return 0
    return int(getattr(server_tool_use, "web_search_requests", 0) or 0)


def complete(
    *,
    model_tier: ModelTier,
    messages: list[MessageParam],
    workflow_id: str,
    agent_name: str,
    system: str | None = None,
    max_tokens: int = 1024,
    tools: list[ToolUnionParam] | None = None,
    log_root: Path | None = None,
) -> LLMResult:
    """Single LLM entry point.

    ORDERING INVARIANT (ADR-002): the JSONL telemetry append completes — and
    its line number is captured — before this function returns. The returned
    `jsonl_ref` is therefore durable; Slice 2 can copy it into
    `workflow_steps.output_ref` without race.

    `tools` is forwarded unchanged to `messages.create(tools=...)` — Anthropic
    server-tool configs (e.g. web_search) live here. When tools are used the
    response's `server_tool_use.web_search_requests` count is folded into
    `cost_usd` via `compute_cost_usd` and surfaced on `LLMResult.web_searches`
    plus additive JSONL telemetry keys (`web_searches`, `web_search_fee_usd`).
    Calls without tools are unaffected — the new keys are absent.

    Error handling is intentionally minimal: API exceptions surface unmodified.
    Retry policy is a later concern.
    """
    root = log_root if log_root is not None else Path("data/logs")
    client = _get_client()
    model_id = model_tier.value

    log.info(
        "llm.call.start",
        model=model_id,
        workflow_id=workflow_id,
        agent_name=agent_name,
        with_tools=tools is not None,
    )

    kwargs: dict[str, Any] = {
        "model": model_id,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if system is not None:
        kwargs["system"] = system
    if tools is not None:
        kwargs["tools"] = tools

    try:
        response = client.messages.create(**kwargs)
    except Exception:
        log.exception(
            "llm.call.failed",
            model=model_id,
            workflow_id=workflow_id,
            agent_name=agent_name,
        )
        raise

    content_parts: list[str] = []
    for block in response.content:
        if isinstance(block, TextBlock):
            content_parts.append(block.text)
    content = "".join(content_parts)

    input_tokens = response.usage.input_tokens
    output_tokens = response.usage.output_tokens
    web_searches = _web_search_request_count(response.usage)
    cost_usd = compute_cost_usd(
        model_tier,
        input_tokens,
        output_tokens,
        web_search_requests=web_searches,
    )
    citations = _extract_web_search_citations(list(response.content))
    stop_reason = response.stop_reason

    now = datetime.now(UTC)
    record: dict[str, Any] = {
        "timestamp": now.isoformat(),
        "workflow_id": workflow_id,
        "agent_name": agent_name,
        "model": model_id,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost_usd,
        "stop_reason": stop_reason,
        "response_text": content,
    }
    if tools is not None:
        # Additive keys — only present when the call actually used tools.
        # Ordinary no-tools calls keep their record shape unchanged.
        record["web_searches"] = web_searches
        record["web_search_fee_usd"] = web_searches * _WEB_SEARCH_FEE_USD
    path = _telemetry_path(root, workflow_id, now)
    line_no = _append_telemetry(path, record)
    jsonl_ref = f"{path.as_posix()}:{line_no}"

    log.info(
        "llm.call.complete",
        model=model_id,
        workflow_id=workflow_id,
        agent_name=agent_name,
        cost_usd=cost_usd,
        jsonl_ref=jsonl_ref,
        web_searches=web_searches,
        citation_count=len(citations),
    )

    return LLMResult(
        content=content,
        model=model_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        jsonl_ref=jsonl_ref,
        stop_reason=stop_reason,
        web_searches=web_searches,
        citations=citations,
    )
