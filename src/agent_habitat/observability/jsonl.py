"""Thin read interface for per-LLM-call JSONL telemetry.

`llm.py` owns the JSONL writer (`_append_telemetry`) and is untouched in
Slice 4 — Slice 4 only adds the READ side. Records live at
`data/logs/YYYY-MM-DD/<workflow_id>.jsonl` (ADR-002 / Slice 1).

DESIGN INTENT — STAY THIN

CLAUDE.md's deferred list names "LangSmith / observability SaaS" as
gated on a single trigger: "telemetry queries outgrow ripgrep-on-JSONL".
That trigger is the design constraint. This module is deliberately small:

  - `iter_telemetry` — open a workflow's JSONL file(s) and yield decoded
    records in line order, decorated with `_path` and `_line` so callers
    can echo a usable output_ref back.
  - `resolve_output_ref` — load the single record at one ADR-002
    `path:line` reference.

That is the whole API. There is intentionally NO filtering, NO aggregation,
NO indexing here. If a question can be answered with
`rg '"agent_name":"researcher"' data/logs/`, prefer that.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

from ..state.models import validate_workflow_id_for_path


class TelemetryReadError(Exception):
    """Raised on malformed JSONL, missing files, or out-of-range refs."""


_DEFAULT_LOG_ROOT = Path("data/logs")


def iter_telemetry(
    workflow_id: str,
    log_root: Path | str = _DEFAULT_LOG_ROOT,
    day: date | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield each JSONL record written for `workflow_id`, in line order.

    Args:
      workflow_id: as supplied to `llm.complete()`. The file name is
        `<workflow_id>.jsonl` inside each UTC day subdirectory.
      log_root: telemetry root; default `data/logs`. Override in tests.
      day: if provided, read only that UTC calendar day's file. If None,
        iterate every day's file present for this workflow, in calendar
        order — workflows that span midnight have records under two
        dated directories.

    Each yielded dict is the parsed JSONL record (`timestamp`, `model`,
    `cost_usd`, …) plus two synthetic keys:
      - `_path`: the file the record came from (POSIX string).
      - `_line`: 1-indexed line number — recombine as
        `f"{rec['_path']}:{rec['_line']}"` to get an ADR-002 output_ref.

    Yields nothing if no telemetry exists for the workflow (or for the
    given day). Empty lines are silently skipped (mid-write tolerance).
    Malformed JSON raises `TelemetryReadError` with file:line context.
    """
    # Defensive path-traversal guard at the read boundary too — the writer
    # only accepts uuid4-hex workflow_ids, but the reader is called with
    # whatever the CLI / debug tooling passes in.
    validate_workflow_id_for_path(workflow_id)
    root = Path(log_root)
    if day is not None:
        path = root / day.strftime("%Y-%m-%d") / f"{workflow_id}.jsonl"
        if path.exists():
            yield from _iter_file(path)
        return

    if not root.exists():
        return
    day_dirs = sorted(d for d in root.iterdir() if d.is_dir())
    for d in day_dirs:
        path = d / f"{workflow_id}.jsonl"
        if path.exists():
            yield from _iter_file(path)


def _iter_file(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, start=1):
            stripped = raw.rstrip("\n")
            if not stripped:
                continue
            try:
                rec = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise TelemetryReadError(
                    f"malformed JSONL at {path.as_posix()}:{lineno}: {exc.msg}"
                ) from exc
            if not isinstance(rec, dict):
                raise TelemetryReadError(f"non-object JSONL at {path.as_posix()}:{lineno}")
            rec["_path"] = path.as_posix()
            rec["_line"] = lineno
            yield rec


def resolve_output_ref(ref: str) -> dict[str, Any]:
    """Load the single JSONL record at an ADR-002 `output_ref` (path:line).

    The output_ref format is `<path>:<line_number>` (1-indexed), produced
    by `llm.py` and stored in `workflow_steps.output_ref` / `input_ref`.
    This function is the canonical resolver.

    Raises TelemetryReadError for malformed refs, missing files,
    out-of-range line numbers, empty target lines, or non-object payloads.
    """
    path_str, sep, lineno_str = ref.rpartition(":")
    if not sep or not path_str or not lineno_str:
        raise TelemetryReadError(f"malformed output_ref: {ref!r}")
    try:
        target_line = int(lineno_str)
    except ValueError as exc:
        raise TelemetryReadError(f"malformed output_ref line number: {ref!r}") from exc
    if target_line < 1:
        raise TelemetryReadError(f"output_ref line must be 1-indexed: {ref!r}")

    path = Path(path_str)
    if not path.exists():
        raise TelemetryReadError(f"output_ref path does not exist: {path.as_posix()}")

    with path.open("r", encoding="utf-8") as f:
        for i, raw in enumerate(f, start=1):
            if i != target_line:
                continue
            stripped = raw.rstrip("\n")
            if not stripped:
                raise TelemetryReadError(f"empty line at {ref}")
            try:
                rec = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise TelemetryReadError(f"malformed JSON at {ref}: {exc.msg}") from exc
            if not isinstance(rec, dict):
                raise TelemetryReadError(f"non-object JSON at {ref}")
            return rec

    raise TelemetryReadError(f"line {target_line} not found in {path.as_posix()}")
