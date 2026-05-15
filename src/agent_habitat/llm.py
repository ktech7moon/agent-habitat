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
from anthropic.types import MessageParam, TextBlock
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, computed_field

load_dotenv()

log = structlog.get_logger(__name__)


class ModelTier(str, Enum):
    """Three-tier model routing. Default DOWN — escalate only when quality requires it."""

    HAIKU = "claude-haiku-4-5-20251001"
    SONNET = "claude-sonnet-4-6"
    OPUS = "claude-opus-4-7"


# ---------------------------------------------------------------------------
# RATE TABLE — NEEDS VERIFICATION against current Anthropic pricing.
#
# Last reviewed: 2026-05-13. Values are USD per 1,000,000 tokens (input, output).
# These are best-known values at time of writing; confirm against the public
# Anthropic pricing page before trusting cost figures for budget decisions.
# When verified or updated, bump the date stamp above.
# ---------------------------------------------------------------------------
_RATES_USD_PER_MTOK: dict[ModelTier, tuple[float, float]] = {
    ModelTier.HAIKU: (1.00, 5.00),
    ModelTier.SONNET: (3.00, 15.00),
    ModelTier.OPUS: (15.00, 75.00),
}


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

    @computed_field  # type: ignore[prop-decorator]
    @property
    def truncated(self) -> bool:
        """Convenience: true iff the response was cut off by max_tokens."""
        return self.stop_reason == "max_tokens"


def compute_cost_usd(tier: ModelTier, input_tokens: int, output_tokens: int) -> float:
    """Per-call cost from the rate table. Pure function — easy to unit-test."""
    input_rate, output_rate = _RATES_USD_PER_MTOK[tier]
    return (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000


def _telemetry_path(log_root: Path, workflow_id: str, now: datetime) -> Path:
    return log_root / now.strftime("%Y-%m-%d") / f"{workflow_id}.jsonl"


def _append_telemetry(path: Path, record: dict[str, Any]) -> int:
    """Append one JSONL line; return its 1-indexed line number.

    SINGLE-WRITER ASSUMPTION (Phase 1-2): workflows run synchronously per the
    roadmap. No file lock, no async coordination. Re-evaluate when multi-process
    orchestration triggers the Postgres migration (CLAUDE.md deferred list).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
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
    return existing + 1


_client: Anthropic | None = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key or api_key.startswith("sk-ant-REPLACE"):
            raise RuntimeError("ANTHROPIC_API_KEY not set; populate .env from .env.example.")
        _client = Anthropic(api_key=api_key)
    return _client


def complete(
    *,
    model_tier: ModelTier,
    messages: list[MessageParam],
    workflow_id: str,
    agent_name: str,
    system: str | None = None,
    max_tokens: int = 1024,
    log_root: Path | None = None,
) -> LLMResult:
    """Single LLM entry point.

    ORDERING INVARIANT (ADR-002): the JSONL telemetry append completes — and
    its line number is captured — before this function returns. The returned
    `jsonl_ref` is therefore durable; Slice 2 can copy it into
    `workflow_steps.output_ref` without race.

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
    )

    kwargs: dict[str, Any] = {
        "model": model_id,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if system is not None:
        kwargs["system"] = system

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
    cost_usd = compute_cost_usd(model_tier, input_tokens, output_tokens)
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
    )

    return LLMResult(
        content=content,
        model=model_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        jsonl_ref=jsonl_ref,
        stop_reason=stop_reason,
    )
