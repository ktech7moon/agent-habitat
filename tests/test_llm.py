"""Tests for llm.py — deterministic units + one guarded live smoke.

Run unit tests only (default):       pytest tests/test_llm.py
Skip the live test explicitly:       pytest tests/test_llm.py -m "not live"
Include only the live test:          pytest tests/test_llm.py -m live

The live test costs real money (~one Haiku call); it must run at most once
per session and is skipped automatically when ANTHROPIC_API_KEY is unset.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_habitat.llm import (
    LLMResult,
    ModelTier,
    _append_telemetry,
    _telemetry_path,
    complete,
    compute_cost_usd,
)


# ---------------------------------------------------------------------------
# Pure unit tests — no network, no API key required.
# ---------------------------------------------------------------------------


class TestCostMath:
    """Cost-per-call against the rate table. Pure function."""

    def test_haiku_cost(self) -> None:
        # 1M input @ $1, 1M output @ $5 → $6.00
        assert compute_cost_usd(ModelTier.HAIKU, 1_000_000, 1_000_000) == pytest.approx(6.00)

    def test_sonnet_cost(self) -> None:
        # 1M input @ $3, 1M output @ $15 → $18.00
        assert compute_cost_usd(ModelTier.SONNET, 1_000_000, 1_000_000) == pytest.approx(18.00)

    def test_opus_cost(self) -> None:
        # 1M input @ $15, 1M output @ $75 → $90.00
        assert compute_cost_usd(ModelTier.OPUS, 1_000_000, 1_000_000) == pytest.approx(90.00)

    def test_small_call_haiku(self) -> None:
        # 1000 input + 500 output on Haiku → 1000*1 + 500*5 = 3500 / 1e6 = $0.0035
        assert compute_cost_usd(ModelTier.HAIKU, 1000, 500) == pytest.approx(0.0035)

    def test_zero_tokens(self) -> None:
        assert compute_cost_usd(ModelTier.HAIKU, 0, 0) == 0.0


class TestTelemetryPath:
    """Path layout from ADR-002: data/logs/YYYY-MM-DD/<workflow_id>.jsonl."""

    def test_path_layout(self, tmp_path: Path) -> None:
        now = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)
        p = _telemetry_path(tmp_path, "wf-abc-123", now)
        assert p == tmp_path / "2026-05-13" / "wf-abc-123.jsonl"


class TestAppendTelemetry:
    """JSONL append + line-number capture. Single-writer assumption holds in tests."""

    def test_first_append_returns_line_1(self, tmp_path: Path) -> None:
        path = tmp_path / "wf.jsonl"
        line_no = _append_telemetry(path, {"a": 1})
        assert line_no == 1
        assert path.read_text(encoding="utf-8") == '{"a":1}\n'

    def test_second_append_returns_line_2(self, tmp_path: Path) -> None:
        path = tmp_path / "wf.jsonl"
        _append_telemetry(path, {"a": 1})
        line_no = _append_telemetry(path, {"a": 2})
        assert line_no == 2
        lines = path.read_text(encoding="utf-8").splitlines()
        assert json.loads(lines[0]) == {"a": 1}
        assert json.loads(lines[1]) == {"a": 2}

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        path = tmp_path / "2026-05-13" / "wf.jsonl"
        line_no = _append_telemetry(path, {"x": "y"})
        assert line_no == 1
        assert path.exists()
        assert path.parent.is_dir()

    def test_each_line_is_one_json_object(self, tmp_path: Path) -> None:
        path = tmp_path / "wf.jsonl"
        for i in range(5):
            _append_telemetry(path, {"i": i})
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 5
        assert [json.loads(line)["i"] for line in lines] == [0, 1, 2, 3, 4]


class TestLLMResultModel:
    """Pydantic v2 return contract — the shape Slice 2 will wire against."""

    def test_required_fields(self) -> None:
        r = LLMResult(
            content="hello",
            model="claude-haiku-4-5-20251001",
            input_tokens=10,
            output_tokens=5,
            cost_usd=0.000035,
            jsonl_ref="data/logs/2026-05-13/wf.jsonl:1",
        )
        assert r.content == "hello"
        assert r.input_tokens == 10
        assert r.output_tokens == 5
        assert r.cost_usd == pytest.approx(0.000035)
        assert r.jsonl_ref == "data/logs/2026-05-13/wf.jsonl:1"

    def test_frozen(self) -> None:
        r = LLMResult(
            content="x",
            model="claude-haiku-4-5-20251001",
            input_tokens=1,
            output_tokens=1,
            cost_usd=0.0,
            jsonl_ref="p:1",
        )
        with pytest.raises(Exception):  # pydantic ValidationError on frozen mutation
            r.content = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Live smoke — one real Haiku call. Skipped when no key. Costs real money.
# Mark with `live` so CI / repeated runs can deselect via `-m "not live"`.
# ---------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY")
    or os.environ["ANTHROPIC_API_KEY"].startswith("sk-ant-REPLACE"),
    reason="ANTHROPIC_API_KEY not set; live smoke skipped.",
)
def test_live_haiku_round_trip(tmp_path: Path) -> None:
    """Single live Haiku call. Verifies the full LLMResult contract round-trips."""
    workflow_id = "live-smoke-001"
    result = complete(
        model_tier=ModelTier.HAIKU,
        messages=[{"role": "user", "content": "Reply with exactly the word: pong"}],
        workflow_id=workflow_id,
        agent_name="smoke-tester",
        max_tokens=32,
        log_root=tmp_path,
    )

    assert result.content.strip(), "response content was empty"
    assert result.model == ModelTier.HAIKU.value
    assert result.input_tokens > 0
    assert result.output_tokens > 0
    assert result.cost_usd > 0.0

    # jsonl_ref must point at an existing path:line that parses.
    path_str, _, line_str = result.jsonl_ref.rpartition(":")
    line_no = int(line_str)
    assert line_no >= 1
    path = Path(path_str)
    assert path.exists(), f"jsonl_ref path does not exist: {path}"
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[line_no - 1])
    for field in (
        "timestamp",
        "workflow_id",
        "agent_name",
        "model",
        "input_tokens",
        "output_tokens",
        "cost_usd",
        "response_text",
    ):
        assert field in record, f"telemetry record missing field: {field}"
    assert record["workflow_id"] == workflow_id
    assert record["model"] == ModelTier.HAIKU.value
