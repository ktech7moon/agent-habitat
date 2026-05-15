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
    Citation,
    LLMResult,
    ModelTier,
    _append_telemetry,
    _extract_web_search_citations,
    _telemetry_path,
    _web_search_request_count,
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

    def test_web_search_fee_added(self) -> None:
        # Three searches @ $0.01 on top of zero token cost = $0.03.
        assert compute_cost_usd(ModelTier.HAIKU, 0, 0, web_search_requests=3) == pytest.approx(0.03)

    def test_web_search_fee_combines_with_tokens(self) -> None:
        # 1000 input + 500 output on Haiku = $0.0035; + 2 searches = $0.0035 + $0.02 = $0.0235.
        assert compute_cost_usd(ModelTier.HAIKU, 1000, 500, web_search_requests=2) == pytest.approx(
            0.0235
        )

    def test_web_search_default_is_zero(self) -> None:
        # No keyword arg → identical to the old contract; ordinary calls unaffected.
        assert compute_cost_usd(ModelTier.HAIKU, 1000, 500) == compute_cost_usd(
            ModelTier.HAIKU, 1000, 500, web_search_requests=0
        )


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
            stop_reason="end_turn",
        )
        assert r.content == "hello"
        assert r.input_tokens == 10
        assert r.output_tokens == 5
        assert r.cost_usd == pytest.approx(0.000035)
        assert r.jsonl_ref == "data/logs/2026-05-13/wf.jsonl:1"
        assert r.stop_reason == "end_turn"
        assert r.truncated is False

    def test_stop_reason_defaults_none(self) -> None:
        r = LLMResult(
            content="x",
            model="claude-haiku-4-5-20251001",
            input_tokens=1,
            output_tokens=1,
            cost_usd=0.0,
            jsonl_ref="p:1",
        )
        assert r.stop_reason is None
        assert r.truncated is False

    def test_truncated_when_max_tokens(self) -> None:
        r = LLMResult(
            content="x",
            model="claude-haiku-4-5-20251001",
            input_tokens=1,
            output_tokens=32,
            cost_usd=0.0,
            jsonl_ref="p:1",
            stop_reason="max_tokens",
        )
        assert r.truncated is True

    def test_truncated_false_for_other_stop_reasons(self) -> None:
        for reason in ("end_turn", "stop_sequence", "tool_use", None):
            r = LLMResult(
                content="x",
                model="claude-haiku-4-5-20251001",
                input_tokens=1,
                output_tokens=1,
                cost_usd=0.0,
                jsonl_ref="p:1",
                stop_reason=reason,
            )
            assert r.truncated is False, f"reason={reason!r} should not be truncated"

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
# tools= passthrough + server-tool fee + citations + additive JSONL keys
# (no network — the anthropic client is mocked).
# ---------------------------------------------------------------------------


from types import SimpleNamespace  # noqa: E402
from typing import Any  # noqa: E402
from unittest.mock import patch  # noqa: E402

from agent_habitat import llm as llm_mod  # noqa: E402


def _fake_text_block(text: str, citations: list[object] | None = None) -> SimpleNamespace:
    """Stand-in for anthropic.types.TextBlock that survives isinstance() checks.

    `complete()` uses `isinstance(block, TextBlock)` to filter text content;
    we monkey-patch that name to `SimpleNamespace` in the relevant tests so
    these fakes pass the check.
    """
    return SimpleNamespace(type="text", text=text, citations=citations)


def _fake_web_citation(*, cited_text: str, url: str, title: str | None = None) -> SimpleNamespace:
    """Stand-in for CitationsWebSearchResultLocation."""
    return SimpleNamespace(
        type="web_search_result_location",
        cited_text=cited_text,
        url=url,
        title=title,
        encrypted_index="opaque",
    )


def _fake_usage(
    *, input_tokens: int = 100, output_tokens: int = 20, web_searches: int | None = None
) -> SimpleNamespace:
    usage = SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)
    if web_searches is not None:
        usage.server_tool_use = SimpleNamespace(web_search_requests=web_searches)
    return usage


def _fake_response(
    *,
    content: list[object],
    input_tokens: int = 100,
    output_tokens: int = 20,
    stop_reason: str = "end_turn",
    web_searches: int | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        usage=_fake_usage(
            input_tokens=input_tokens, output_tokens=output_tokens, web_searches=web_searches
        ),
        stop_reason=stop_reason,
    )


class TestWebSearchRequestCount:
    def test_missing_server_tool_use(self) -> None:
        usage = SimpleNamespace(input_tokens=1, output_tokens=1)
        assert _web_search_request_count(usage) == 0

    def test_present(self) -> None:
        usage = SimpleNamespace(server_tool_use=SimpleNamespace(web_search_requests=3))
        assert _web_search_request_count(usage) == 3

    def test_none_value_treated_as_zero(self) -> None:
        usage = SimpleNamespace(server_tool_use=SimpleNamespace(web_search_requests=None))
        assert _web_search_request_count(usage) == 0


class TestExtractCitations:
    def test_no_text_blocks(self) -> None:
        # A bare server_tool_use block emits no citations.
        non_text = SimpleNamespace(type="server_tool_use")
        assert _extract_web_search_citations([non_text]) == []

    def test_text_block_without_citations(self) -> None:
        with patch.object(llm_mod, "TextBlock", new=SimpleNamespace):
            block = _fake_text_block("hello", citations=None)
            assert _extract_web_search_citations([block]) == []

    def test_text_block_with_web_search_citations(self) -> None:
        with (
            patch.object(llm_mod, "TextBlock", new=SimpleNamespace),
            patch.object(llm_mod, "CitationsWebSearchResultLocation", new=SimpleNamespace),
        ):
            c = _fake_web_citation(
                cited_text="Acme raised $50M",
                url="https://example.com/news",
                title="Acme Funding",
            )
            block = _fake_text_block("text", citations=[c])
            out = _extract_web_search_citations([block])
            assert len(out) == 1
            assert isinstance(out[0], Citation)
            assert out[0].cited_text == "Acme raised $50M"
            assert out[0].source_url == "https://example.com/news"
            assert out[0].source_title == "Acme Funding"


class TestCompleteToolsPassthrough:
    """End-to-end behaviour of `complete()` with the tools= extension.

    Mocks the anthropic client so no network call happens; verifies that
    tools is forwarded, server-tool fees fold into cost, citations land on
    LLMResult, JSONL keys are present, and ordinary no-tools calls are
    unaffected.
    """

    def _patch_client(self, response: object) -> Any:
        client = SimpleNamespace(
            messages=SimpleNamespace(create=lambda **kw: response),
        )
        return patch.object(llm_mod, "_get_client", return_value=client)

    def test_tools_forwarded_to_messages_create(self, tmp_path: Path) -> None:
        seen_kwargs: dict[str, Any] = {}

        def _capture(**kw: Any) -> object:
            seen_kwargs.update(kw)
            return _fake_response(
                content=[],
                input_tokens=10,
                output_tokens=5,
                web_searches=0,
            )

        client = SimpleNamespace(messages=SimpleNamespace(create=_capture))
        tools_cfg = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 2}]

        with (
            patch.object(llm_mod, "_get_client", return_value=client),
            patch.object(llm_mod, "TextBlock", new=SimpleNamespace),
        ):
            complete(
                model_tier=ModelTier.HAIKU,
                messages=[{"role": "user", "content": "hi"}],
                workflow_id="wf-tools-1",
                agent_name="tester",
                tools=tools_cfg,  # type: ignore[arg-type]
                log_root=tmp_path,
            )

        assert seen_kwargs.get("tools") == tools_cfg

    def test_no_tools_means_no_tools_kwarg(self, tmp_path: Path) -> None:
        seen_kwargs: dict[str, Any] = {}

        def _capture(**kw: Any) -> object:
            seen_kwargs.update(kw)
            return _fake_response(content=[], input_tokens=10, output_tokens=5)

        client = SimpleNamespace(messages=SimpleNamespace(create=_capture))

        with (
            patch.object(llm_mod, "_get_client", return_value=client),
            patch.object(llm_mod, "TextBlock", new=SimpleNamespace),
        ):
            complete(
                model_tier=ModelTier.HAIKU,
                messages=[{"role": "user", "content": "hi"}],
                workflow_id="wf-no-tools-1",
                agent_name="tester",
                log_root=tmp_path,
            )

        assert "tools" not in seen_kwargs

    def test_server_tool_fee_in_cost(self, tmp_path: Path) -> None:
        response = _fake_response(
            content=[],
            input_tokens=0,
            output_tokens=0,
            web_searches=3,
        )
        with (
            self._patch_client(response),
            patch.object(llm_mod, "TextBlock", new=SimpleNamespace),
        ):
            result = complete(
                model_tier=ModelTier.HAIKU,
                messages=[{"role": "user", "content": "hi"}],
                workflow_id="wf-fee-1",
                agent_name="tester",
                tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
                log_root=tmp_path,
            )
        # Zero tokens, 3 searches → $0.03 exactly.
        assert result.cost_usd == pytest.approx(0.03)
        assert result.web_searches == 3

    def test_citations_extracted_onto_llm_result(self, tmp_path: Path) -> None:
        c1 = _fake_web_citation(
            cited_text="Acme raised $50M",
            url="https://example.com/a",
            title="Acme",
        )
        c2 = _fake_web_citation(
            cited_text="Hired CTO Jane",
            url="https://example.com/b",
            title=None,
        )
        response = _fake_response(
            content=[
                _fake_text_block("Background ", citations=None),
                _fake_text_block("with funding context.", citations=[c1]),
                _fake_text_block(" Also hiring news:", citations=[c2]),
            ],
            input_tokens=100,
            output_tokens=20,
            web_searches=2,
        )
        with (
            self._patch_client(response),
            patch.object(llm_mod, "TextBlock", new=SimpleNamespace),
            patch.object(llm_mod, "CitationsWebSearchResultLocation", new=SimpleNamespace),
        ):
            result = complete(
                model_tier=ModelTier.HAIKU,
                messages=[{"role": "user", "content": "hi"}],
                workflow_id="wf-cit-1",
                agent_name="tester",
                tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
                log_root=tmp_path,
            )
        assert len(result.citations) == 2
        assert result.citations[0].cited_text == "Acme raised $50M"
        assert result.citations[1].source_url == "https://example.com/b"
        assert result.web_searches == 2

    def test_jsonl_gains_additive_keys_when_tools_used(self, tmp_path: Path) -> None:
        response = _fake_response(content=[], input_tokens=100, output_tokens=20, web_searches=2)
        with (
            self._patch_client(response),
            patch.object(llm_mod, "TextBlock", new=SimpleNamespace),
        ):
            result = complete(
                model_tier=ModelTier.HAIKU,
                messages=[{"role": "user", "content": "hi"}],
                workflow_id="wf-jsonl-1",
                agent_name="tester",
                tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
                log_root=tmp_path,
            )
        path_str, _, line_str = result.jsonl_ref.rpartition(":")
        record = json.loads(
            Path(path_str).read_text(encoding="utf-8").splitlines()[int(line_str) - 1]
        )
        assert record["web_searches"] == 2
        assert record["web_search_fee_usd"] == pytest.approx(0.02)
        # Existing keys still present.
        for k in (
            "timestamp",
            "workflow_id",
            "agent_name",
            "model",
            "input_tokens",
            "output_tokens",
            "cost_usd",
            "stop_reason",
            "response_text",
        ):
            assert k in record

    def test_no_tools_call_jsonl_record_unchanged(self, tmp_path: Path) -> None:
        response = _fake_response(content=[], input_tokens=10, output_tokens=5)
        with (
            self._patch_client(response),
            patch.object(llm_mod, "TextBlock", new=SimpleNamespace),
        ):
            result = complete(
                model_tier=ModelTier.HAIKU,
                messages=[{"role": "user", "content": "hi"}],
                workflow_id="wf-no-tools-2",
                agent_name="tester",
                log_root=tmp_path,
            )
        path_str, _, line_str = result.jsonl_ref.rpartition(":")
        record = json.loads(
            Path(path_str).read_text(encoding="utf-8").splitlines()[int(line_str) - 1]
        )
        # The additive keys must NOT appear on an ordinary call — that's the
        # "ordinary no-tools call is unaffected" contract.
        assert "web_searches" not in record
        assert "web_search_fee_usd" not in record
        # And the LLMResult fields default cleanly.
        assert result.web_searches == 0
        assert result.citations == []


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
