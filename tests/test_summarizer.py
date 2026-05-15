"""Tests for the Slice 6 URL summarizer agent.

Deterministic units (no API, no network) plus one guarded live smoke that
runs at most once when ANTHROPIC_API_KEY is set.

Coverage map:

  TestFetch              — httpx-mocked fetch: success, redirect, 404, network
                           error, oversize body, empty body, bad scheme.
  TestParse              — readable-text extraction: normal HTML, <main> /
                           <article> preference, script/style stripping,
                           too-short page rejected.
  TestRunHappy           — run_summarizer with mocked LLM: workflow row,
                           three step rows, events emitted, cost rolled up,
                           output_ref written, summary returned, telemetry
                           jsonl_ref resolves.
  TestRunFailurePaths    — bad URL, fetch error, empty page, LLM error:
                           workflow ends FAILED, finished_at stamped, right
                           events, no uncaught crash, no stuck RUNNING.
  TestCLI                — run-summarizer CLI happy + failure paths.
  test_live_summarizer_round_trip
                         — one live smoke against a stable URL; skipped
                           without ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from click.testing import CliRunner

from agent_habitat.agents import summarizer as summarizer_mod
from agent_habitat.agents.summarizer import (
    MAX_HTML_BYTES,
    MIN_READABLE_CHARS,
    SummarizerError,
    extract_readable_text,
    fetch_url,
    run_summarizer,
)
from agent_habitat.cli import main
from agent_habitat.llm import LLMResult
from agent_habitat.state import (
    StepStatus,
    WorkflowStatus,
    init_db,
    load_events,
    load_steps,
    load_workflow,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "agent_habitat.db"


@pytest.fixture
def conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    c = init_db(db_path)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def log_root(tmp_path: Path) -> Path:
    return tmp_path / "logs"


SAMPLE_HTML = """
<!doctype html>
<html>
<head>
  <title>Demo Page</title>
  <style>.x { color: red; }</style>
  <script>console.log('ignored');</script>
</head>
<body>
  <header>Site Header</header>
  <main>
    <h1>Demo Page</h1>
    <p>This is a demonstration page used to test the summarizer agent.</p>
    <p>It contains two paragraphs of perfectly cromulent prose for parsing.</p>
  </main>
  <footer>Site Footer</footer>
</body>
</html>
"""


def _mock_client(handler: object) -> httpx.Client:
    """Build a real httpx.Client wired to a MockTransport handler."""
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return httpx.Client(transport=transport, follow_redirects=True)


def _fake_llm_result(
    *,
    content: str = "A demonstration page with two cromulent paragraphs.",
    cost_usd: float = 0.00123,
    jsonl_ref: str = "data/logs/2026-05-14/wf.jsonl:1",
    input_tokens: int = 250,
    output_tokens: int = 40,
) -> LLMResult:
    return LLMResult(
        content=content,
        model="claude-sonnet-4-6",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        jsonl_ref=jsonl_ref,
        stop_reason="end_turn",
    )


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


class TestFetch:
    def test_success_returns_body_text(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="hello world")

        body = fetch_url("https://example.com/", http_client=_mock_client(handler))
        assert body == "hello world"

    def test_follows_redirect(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/start":
                return httpx.Response(302, headers={"location": "/end"})
            return httpx.Response(200, text="final")

        body = fetch_url("https://example.com/start", http_client=_mock_client(handler))
        assert body == "final"

    def test_non_2xx_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, text="not found")

        with pytest.raises(SummarizerError) as exc_info:
            fetch_url("https://example.com/", http_client=_mock_client(handler))
        assert exc_info.value.step_name == "fetch"
        assert "404" in exc_info.value.message

    def test_network_error_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("simulated network failure")

        with pytest.raises(SummarizerError) as exc_info:
            fetch_url("https://example.com/", http_client=_mock_client(handler))
        assert exc_info.value.step_name == "fetch"
        assert "simulated network failure" in exc_info.value.message

    def test_empty_body_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"")

        with pytest.raises(SummarizerError) as exc_info:
            fetch_url("https://example.com/", http_client=_mock_client(handler))
        assert exc_info.value.step_name == "fetch"
        assert "empty response body" in exc_info.value.message

    def test_oversize_body_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"x" * (MAX_HTML_BYTES + 1))

        with pytest.raises(SummarizerError) as exc_info:
            fetch_url("https://example.com/", http_client=_mock_client(handler))
        assert exc_info.value.step_name == "fetch"
        assert "too large" in exc_info.value.message

    def test_bad_scheme_raises(self) -> None:
        with pytest.raises(SummarizerError) as exc_info:
            fetch_url("ftp://example.com/file")
        assert exc_info.value.step_name == "fetch"
        assert "unsupported URL scheme" in exc_info.value.message

    def test_empty_url_raises(self) -> None:
        with pytest.raises(SummarizerError) as exc_info:
            fetch_url("")
        assert exc_info.value.step_name == "fetch"


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------


class TestParse:
    def test_extracts_visible_text(self) -> None:
        text = extract_readable_text(SAMPLE_HTML)
        assert "demonstration page" in text
        assert "cromulent prose" in text

    def test_drops_script_and_style(self) -> None:
        text = extract_readable_text(SAMPLE_HTML)
        assert "console.log" not in text
        assert "color: red" not in text

    def test_prefers_main_tag(self) -> None:
        text = extract_readable_text(SAMPLE_HTML)
        # <header>/<footer> are outside <main> and should be skipped.
        assert "Site Header" not in text
        assert "Site Footer" not in text

    def test_falls_back_when_no_main_tag(self) -> None:
        html = (
            "<html><body><p>"
            + ("Some real content with enough length to clear the threshold. " * 4)
            + "</p></body></html>"
        )
        text = extract_readable_text(html)
        assert "real content" in text

    def test_too_short_page_raises(self) -> None:
        with pytest.raises(SummarizerError) as exc_info:
            extract_readable_text("<html><body>hi</body></html>")
        assert exc_info.value.step_name == "parse"
        assert "too short" in exc_info.value.message

    def test_empty_input_raises(self) -> None:
        with pytest.raises(SummarizerError) as exc_info:
            extract_readable_text("")
        assert exc_info.value.step_name == "parse"

    def test_min_threshold_constant_sane(self) -> None:
        # Anti-regression: someone moves the threshold to 0 and our short-page
        # contract evaporates. Assert it stays meaningful.
        assert MIN_READABLE_CHARS >= 20


# ---------------------------------------------------------------------------
# Happy-path run
# ---------------------------------------------------------------------------


def _ok_handler(body: str = SAMPLE_HTML) -> object:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body)

    return handler


class TestRunHappy:
    def test_returns_completed_result(self, conn: sqlite3.Connection, log_root: Path) -> None:
        http = _mock_client(_ok_handler())
        with patch.object(summarizer_mod, "complete", return_value=_fake_llm_result()):
            result = run_summarizer(
                conn,
                url="https://example.com/",
                http_client=http,
                log_root=log_root,
            )

        assert result.status is WorkflowStatus.COMPLETED
        assert result.error_step is None
        assert result.error_message is None
        assert result.summary is not None
        assert "demonstration" in result.summary.lower()
        assert result.cost_usd == pytest.approx(0.00123)

    def test_persists_workflow_steps_events(self, conn: sqlite3.Connection, log_root: Path) -> None:
        http = _mock_client(_ok_handler())
        with patch.object(summarizer_mod, "complete", return_value=_fake_llm_result()):
            result = run_summarizer(
                conn,
                url="https://example.com/",
                http_client=http,
                log_root=log_root,
            )

        wf = load_workflow(conn, result.workflow_id)
        assert wf is not None
        assert wf.status is WorkflowStatus.COMPLETED
        assert wf.finished_at is not None
        assert wf.workflow_type == "url_summarizer"
        assert wf.metadata.get("url") == "https://example.com/"
        assert wf.cost_total_usd == pytest.approx(0.00123)

        steps = load_steps(conn, result.workflow_id)
        assert [s.agent_name for s in steps] == ["fetch", "parse", "summarize"]
        for s in steps:
            assert s.status is StepStatus.COMPLETED
            assert s.finished_at is not None

        summarize_step = steps[2]
        assert summarize_step.cost_usd == pytest.approx(0.00123)
        assert summarize_step.output_ref == "data/logs/2026-05-14/wf.jsonl:1"

        events = load_events(conn, result.workflow_id)
        types = [(e.structured_data or {}).get("event_type") for e in events]
        # 1 workflow.started, 3 * (step.started + step.completed), 1 workflow.completed
        assert types.count("workflow.started") == 1
        assert types.count("workflow.completed") == 1
        assert types.count("step.started") == 3
        assert types.count("step.completed") == 3
        # None of the failure event types fired.
        assert "step.failed" not in types
        assert "workflow.failed" not in types

    def test_uses_sonnet_tier(self, conn: sqlite3.Connection, log_root: Path) -> None:
        http = _mock_client(_ok_handler())
        with patch.object(
            summarizer_mod, "complete", return_value=_fake_llm_result()
        ) as mock_complete:
            run_summarizer(
                conn,
                url="https://example.com/",
                http_client=http,
                log_root=log_root,
            )
        kwargs = mock_complete.call_args.kwargs
        assert kwargs["model_tier"].value == "claude-sonnet-4-6"
        assert kwargs["system"] is not None

    def test_custom_workflow_id_honoured(self, conn: sqlite3.Connection, log_root: Path) -> None:
        http = _mock_client(_ok_handler())
        with patch.object(summarizer_mod, "complete", return_value=_fake_llm_result()):
            result = run_summarizer(
                conn,
                url="https://example.com/",
                workflow_id="wf-test-001",
                http_client=http,
                log_root=log_root,
            )
        assert result.workflow_id == "wf-test-001"


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


class TestRunFailurePaths:
    def _assert_failed_and_not_running(
        self,
        conn: sqlite3.Connection,
        workflow_id: str,
        *,
        expected_failed_step: str,
    ) -> None:
        wf = load_workflow(conn, workflow_id)
        assert wf is not None
        assert wf.status is WorkflowStatus.FAILED, f"workflow must be FAILED, got {wf.status}"
        assert wf.finished_at is not None, "FAILED workflow must have finished_at stamped"

        events = load_events(conn, workflow_id)
        types = [(e.structured_data or {}).get("event_type") for e in events]
        assert "workflow.failed" in types
        assert "workflow.completed" not in types
        # Exactly one step.failed for the failing step
        assert types.count("step.failed") == 1
        failure_evt = next(
            e for e in events if (e.structured_data or {}).get("event_type") == "step.failed"
        )
        sd = failure_evt.structured_data or {}
        assert sd.get("step_name") == expected_failed_step

        # The failing step row itself is FAILED with error_message populated.
        steps = load_steps(conn, workflow_id)
        failed_step = next(s for s in steps if s.agent_name == expected_failed_step)
        assert failed_step.status is StepStatus.FAILED
        assert failed_step.error_message is not None
        assert failed_step.finished_at is not None

    def test_bad_url_scheme(self, conn: sqlite3.Connection, log_root: Path) -> None:
        result = run_summarizer(conn, url="ftp://example.com/", log_root=log_root)
        assert result.status is WorkflowStatus.FAILED
        assert result.error_step == "fetch"
        assert result.summary is None
        self._assert_failed_and_not_running(conn, result.workflow_id, expected_failed_step="fetch")

    def test_fetch_404(self, conn: sqlite3.Connection, log_root: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        http = _mock_client(handler)
        result = run_summarizer(
            conn,
            url="https://example.com/missing",
            http_client=http,
            log_root=log_root,
        )
        assert result.status is WorkflowStatus.FAILED
        assert result.error_step == "fetch"
        self._assert_failed_and_not_running(conn, result.workflow_id, expected_failed_step="fetch")

    def test_fetch_network_error(self, conn: sqlite3.Connection, log_root: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("dns blew up")

        http = _mock_client(handler)
        result = run_summarizer(
            conn,
            url="https://example.com/",
            http_client=http,
            log_root=log_root,
        )
        assert result.status is WorkflowStatus.FAILED
        assert result.error_step == "fetch"
        self._assert_failed_and_not_running(conn, result.workflow_id, expected_failed_step="fetch")

    def test_empty_page(self, conn: sqlite3.Connection, log_root: Path) -> None:
        # 200 OK but with a body too thin to summarize.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html><body><p>hi</p></body></html>")

        http = _mock_client(handler)
        result = run_summarizer(
            conn,
            url="https://example.com/spa",
            http_client=http,
            log_root=log_root,
        )
        assert result.status is WorkflowStatus.FAILED
        assert result.error_step == "parse"
        self._assert_failed_and_not_running(conn, result.workflow_id, expected_failed_step="parse")
        # Fetch step succeeded; only parse failed.
        steps = load_steps(conn, result.workflow_id)
        assert [s.agent_name for s in steps] == ["fetch", "parse"]
        assert steps[0].status is StepStatus.COMPLETED
        assert steps[1].status is StepStatus.FAILED

    def test_llm_error(self, conn: sqlite3.Connection, log_root: Path) -> None:
        http = _mock_client(_ok_handler())

        class BoomError(RuntimeError):
            pass

        with patch.object(
            summarizer_mod,
            "complete",
            side_effect=BoomError("anthropic exploded"),
        ):
            result = run_summarizer(
                conn,
                url="https://example.com/",
                http_client=http,
                log_root=log_root,
            )

        assert result.status is WorkflowStatus.FAILED
        assert result.error_step == "summarize"
        assert "anthropic exploded" in (result.error_message or "")
        self._assert_failed_and_not_running(
            conn, result.workflow_id, expected_failed_step="summarize"
        )
        # fetch + parse should have completed; summarize is the one that failed.
        steps = load_steps(conn, result.workflow_id)
        assert [s.agent_name for s in steps] == ["fetch", "parse", "summarize"]
        assert steps[0].status is StepStatus.COMPLETED
        assert steps[1].status is StepStatus.COMPLETED
        assert steps[2].status is StepStatus.FAILED


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCLI:
    def test_run_summarizer_happy(self, tmp_path: Path) -> None:
        db = tmp_path / "wf.db"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=SAMPLE_HTML)

        # Monkey-patch the agent's httpx.Client constructor to use the mock,
        # and the LLM call to return a fake result.
        http = _mock_client(handler)

        runner = CliRunner()
        with (
            patch.object(summarizer_mod, "httpx", new=httpx),
            patch.object(summarizer_mod, "complete", return_value=_fake_llm_result()),
            patch.object(httpx, "Client", return_value=http),
        ):
            result = runner.invoke(
                main,
                ["run-summarizer", "--db", str(db), "https://example.com/"],
            )

        assert result.exit_code == 0, result.output
        assert "status: COMPLETED" in result.output
        assert "Automated summary" in result.output  # decision-support footer

    def test_run_summarizer_failure_exits_nonzero(self, tmp_path: Path) -> None:
        db = tmp_path / "wf.db"
        runner = CliRunner()
        # Bad scheme — fails before any network call.
        result = runner.invoke(
            main,
            ["run-summarizer", "--db", str(db), "ftp://example.com/"],
        )
        assert result.exit_code != 0
        assert "status: FAILED" in result.output
        assert "fetch" in result.output


# ---------------------------------------------------------------------------
# Live smoke — one real call. Skipped without ANTHROPIC_API_KEY.
# ---------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY")
    or os.environ["ANTHROPIC_API_KEY"].startswith("sk-ant-REPLACE"),
    reason="ANTHROPIC_API_KEY not set; live smoke skipped.",
)
def test_live_summarizer_round_trip(conn: sqlite3.Connection, log_root: Path) -> None:
    """End-to-end live: fetch + parse + Sonnet summarize against example.com.

    Verifies the entire habitat round-trip with real network + real API:
    workflow COMPLETED, three steps recorded, events emitted, cost_total > 0,
    summarize output_ref resolves to a real JSONL line.
    """
    start = datetime.now(UTC)
    result = run_summarizer(
        conn,
        url="https://example.com/",
        log_root=log_root,
    )
    duration_s = (datetime.now(UTC) - start).total_seconds()

    # Print observations to stdout so calibration data shows up in pytest -s.
    print(f"\n[live-smoke] duration_s={duration_s:.2f}")
    print(f"[live-smoke] status={result.status.value}")
    print(f"[live-smoke] cost_usd={result.cost_usd:.6f}")
    print(f"[live-smoke] summary={result.summary!r}")

    assert result.status is WorkflowStatus.COMPLETED
    assert result.summary is not None and result.summary.strip()
    assert result.cost_usd > 0.0

    wf = load_workflow(conn, result.workflow_id)
    assert wf is not None
    assert wf.status is WorkflowStatus.COMPLETED
    assert wf.cost_total_usd > 0.0

    steps = load_steps(conn, result.workflow_id)
    assert [s.agent_name for s in steps] == ["fetch", "parse", "summarize"]
    assert all(s.status is StepStatus.COMPLETED for s in steps)

    summarize_step = steps[2]
    assert summarize_step.output_ref is not None
    # Resolve the output_ref to confirm it points at a real JSONL line.
    path_str, _, line_str = summarize_step.output_ref.rpartition(":")
    jsonl_path = Path(path_str)
    assert jsonl_path.exists(), f"output_ref path missing: {jsonl_path}"
    line_no = int(line_str)
    lines = jsonl_path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[line_no - 1])
    assert record["workflow_id"] == result.workflow_id
    assert record["agent_name"] == "url_summarizer"
    assert record["model"] == "claude-sonnet-4-6"
    print(
        f"[live-smoke] input_tokens={record['input_tokens']} "
        f"output_tokens={record['output_tokens']}"
    )

    events = load_events(conn, result.workflow_id)
    types = [(e.structured_data or {}).get("event_type") for e in events]
    assert "workflow.started" in types
    assert "workflow.completed" in types
    assert types.count("step.completed") == 3
