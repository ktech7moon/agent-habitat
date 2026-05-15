# agent-habitat STATUS

## Current Phase
Phase 1 — Habitat Infrastructure (full plan: docs/ROADMAP.md)

## Current Slice
Slice 4 — ObservabilityLayer (unified event emission + structlog config + thin JSONL read) **— COMPLETE**

## Slice 1 Subtasks
- [x] Scaffold project skeleton + plan docs
- [x] ADR-001: LangGraph over CrewAI (Accepted 2026-05-13)
- [x] ADR-002: Persistence schema (Accepted 2026-05-13)
- [x] llm.py wrapper implementation (2026-05-13)
- [x] Slice 1 live smoke: single Haiku call through llm.py, JSONL telemetry write verified (2026-05-13)

## Slice 2 Subtasks
- [x] Step 0: `stop_reason` + derived `truncated` on LLMResult (committed separately)
- [x] Pydantic v2 models (`Workflow`, `WorkflowStep`, `Event`) + status/level enums
- [x] DDL bootstrap (idempotent `init_schema`) for `workflows`, `workflow_steps`, `events`
- [x] Typed CRUD: insert / update / load / query-by-status
- [x] Cost rollup: `recompute_cost_total` sums `workflow_steps.cost_usd` → `workflows.cost_total_usd`
- [x] Orphan reconciliation: `reconcile_orphan_steps` (startup sweep per ADR-002)
- [x] 33 deterministic tests in `tests/test_state.py`; full suite passes, ruff + mypy strict clean

## Slice 4 Subtasks
- [x] `observability/events.py` — `emit_event()` conventioned writer over the existing `events` table; `EventType` taxonomy enum; `EVENT_LEVEL_GUIDE` semantics; `events_of_type()` `json_extract` query primitive
- [x] `observability/logging.py` — central `configure_logging()` (console renderer default, JSON output opt-in); `bind_workflow_context()` / `clear_log_context()` via structlog contextvars; idempotent
- [x] `observability/jsonl.py` — `iter_telemetry(workflow_id, log_root, day=None)` over `data/logs/YYYY-MM-DD/<wf>.jsonl`; `resolve_output_ref(path:line)` canonical resolver; `TelemetryReadError` for malformed/missing
- [x] Module-level coherence docstring spelling out the three surfaces (`events` table, JSONL, structlog) and what each is for
- [x] `llm.py` untouched (no ad-hoc structlog config existed to align; rule #14 leaves the working JSONL writer in place)
- [x] 29 deterministic tests in `tests/test_observability.py`; full suite (119 tests) passes, ruff + ruff format + mypy strict clean

## Slice 3 Subtasks
- [x] Budget config: `config/budgets.toml` (operator-tunable, stdlib `tomllib`, no new dep)
- [x] `BudgetConfig` pydantic model + `load_budget_config()` with operator-debuggable errors
- [x] `cap_for_workflow_type()` resolution: override wins, else default
- [x] UTC calendar-day window helper (`utc_day_window`) — decision recorded under Open Questions
- [x] `cost_within_window` — SUM over `workflow_steps.cost_usd` for steps with `started_at` in window
- [x] Pure `evaluate_budget(cost, cap, threshold) → UNDER | APPROACHING | OVER`
- [x] `check_workflow_budget()` — composed end-to-end check returning a `BudgetCheck` dataclass
- [x] `record_budget_exceeded()` — writes a structured `budget.exceeded` event into the EXISTING `events` table (no ADR-002 schema change)
- [x] `is_workflow_halted_by_budget()` — `json_extract`-backed halt-signal query for the Phase 2 orchestrator
- [x] 40 deterministic tests in `tests/test_budget.py`; full suite (90 tests) passes, ruff + mypy strict clean

## Open Questions
- **Consolidate `llm.py`'s JSONL telemetry writer through the ObservabilityLayer.** Today `llm.py._append_telemetry` writes JSONL directly; Slice 4 added the conventioned READ side (`iter_telemetry`, `resolve_output_ref`) but did NOT touch the writer — `LLMResult` is a load-bearing contract and rule #14 forbids broad refactors without an ADR. Future work: either (a) route llm.py's writer through an ObservabilityLayer writer module so the path/line/format conventions live in one place, or (b) explicitly document the writer-stays-in-llm.py boundary as the chosen architecture. Trigger: any second writer of JSONL telemetry (Slice 5 checkpoint payloads? Phase 2 agent intermediate artefacts?) — that's the moment to centralise.
- **Rate table needs verification.** `_RATES_USD_PER_MTOK` in `llm.py` uses best-known values (Haiku $1/$5, Sonnet $3/$15, Opus $15/$75 per MTok input/output) stamped 2026-05-13. Joseph: cross-check against the public Anthropic pricing page before relying on the cost numbers for any budget decision (Slice 3 enforcement is now wired but reads the same rate table).
- **ADR-002 underspecification: `workflows.id` generation algorithm.** ADR-002 fixes the *relationship* (id is shared with LangGraph as `thread_id`) and the *type* (TEXT PRIMARY KEY) but does not name a generation method. Slice 2 defaults to `uuid.uuid4().hex` via `new_workflow_id()`; callers may override. Revisit with an ADR-002 addendum if Phase 2 needs sortable or time-prefixed ids (ULID, snowflake) for cheap range scans.
- **ADR-002 underspecification: orphan reconciliation target without LangGraph state.** ADR-002 says orphans reconcile to "failed with a synthesized event, or resume." The "or resume" branch needs LangGraph checkpoint state to decide whether resume is safe; that wiring lands with the Phase 2 orchestrator. Slice 2 implements the deterministic half: mark orphan FAILED, set `finished_at=now`, synthesize a WARN event. The resume branch can layer on top later without changing this contract.
- **Slice 3 "daily" definition resolved: UTC calendar day.** "Daily budget cap" = the half-open interval `[today 00:00:00 UTC, tomorrow 00:00:00 UTC)`. Caps reset at UTC midnight. Why UTC over rolling-24h or local-tz: aligns with the JSONL telemetry directory layout (`data/logs/YYYY-MM-DD/` already UTC), is trivially auditable, and makes window queries simple ISO-string range comparisons. Revisit if a workload needs per-tenant local-tz semantics.

## Last Session
Implemented `src/agent_habitat/observability/` — the ObservabilityLayer. Three files, deliberately proportionate:

`events.py` — unified writer over the existing ADR-002 `events` table. `emit_event(conn, *, workflow_id, event_type, level, message, structured_data=None, step_id=None, timestamp=None)` always stamps `structured_data.event_type` as the first key (rejects caller-supplied `event_type` inside the payload to prevent two-source-of-truth bugs). `EventType` enum is the canonical taxonomy: workflow.{started,completed,failed,cancelled,note}, step.{started,completed,failed}, budget.exceeded (Slice 3 owns the writer, listed here for completeness), checkpoint.{requested,approved,rejected} (Slice 5), agent.fabrication_detected (Phase 2). `EVENT_LEVEL_GUIDE` documents INFO/WARN/ERROR/CHECKPOINT/APPROVAL semantics — the contract every future emitter inherits. `events_of_type()` is the generic `json_extract`-backed read primitive (same pattern Slice 3 uses in `is_workflow_halted_by_budget`).

`logging.py` — central structlog configuration. `configure_logging(level, json_output, stream)` sets processors (merge_contextvars, add_log_level, ISO UTC timestamper, stack/exc renderers) and dispatches to either `ConsoleRenderer` (dev default, no colours) or `JSONRenderer(sort_keys=True)` (production / log shipping). Idempotent — re-call to reconfigure cleanly in tests. `bind_workflow_context(workflow_id, agent_name=None)` and `clear_log_context()` thin-wrap structlog contextvars so every downstream `structlog.get_logger(__name__).info(...)` is auto-tagged. `llm.py` was NOT touched: it already does `log = structlog.get_logger(__name__)` only — no ad-hoc `structlog.configure()` call existed to delete, so "alignment" was a no-op.

`jsonl.py` — thin reader over `llm.py`'s telemetry. `iter_telemetry(workflow_id, log_root, day=None)` yields decoded records in line order, decorating each with `_path` and `_line` so callers can echo a usable output_ref. Day=None walks every dated subdir in calendar order (workflows that span midnight have records under two dirs). `resolve_output_ref(ref)` is the canonical `path:line` resolver. Empty lines silently skipped (mid-write tolerance); malformed JSON, missing files, out-of-range lines, zero-indexed refs, and non-object payloads all raise `TelemetryReadError` with file:line context. Intentionally NO filtering / aggregation / indexing — the trigger to move OFF JSONL (CLAUDE.md deferred list) is "queries harder than ripgrep can handle", so the layer stays grep-friendly by design.

`__init__.py` carries a module-level docstring spelling out the three surfaces and how they relate (`events` table = persistent queryable audit; JSONL = per-LLM-call detail; structlog = operational logs) — the coherence statement the prompt asked for.

Tests: 29 deterministic in `tests/test_observability.py` covering (1) `emit_event` row shape, payload merge, caller-supplied-event_type rejection, string event_type acceptance, explicit step_id/timestamp persistence, `events_of_type` json_extract filtering across multiple workflows and event types, `EVENT_LEVEL_GUIDE` coverage of every `EventLevel`; (2) structlog config: JSON output, console renderer default, contextvars binding, context clearing, level filtering, idempotent reconfigure with autouse `structlog.reset_defaults()` teardown; (3) JSONL reader: single-day + multi-day iteration, missing-workflow/missing-root empty cases, blank-line skipping with correct `_line` numbers, malformed-JSON and non-object-payload errors, `resolve_output_ref` success + every documented failure mode (malformed no-colon, non-integer line, missing path, line-out-of-range, zero-indexed rejected, empty target line) and a round-trip against the `path:line` format `llm.py` actually writes.

Full suite (119 tests including llm.py, state, budget, observability) passes. ruff check + ruff format + mypy strict all clean.

Implemented `src/agent_habitat/budget/` — `config.py` (Pydantic v2 `BudgetConfig`, `load_budget_config()` over stdlib `tomllib`, `cap_for_workflow_type()`, and a `BudgetConfigError` with path-named messages) and `tracker.py` (`utc_day_window`, `cost_within_window`, pure `evaluate_budget`, `check_workflow_budget`, `record_budget_exceeded`, `is_workflow_halted_by_budget`, plus `BudgetCheck` dataclass and `BudgetStatus` enum). Bundled `config/budgets.toml` ships default daily cap = $5.00 with `approaching_threshold = 0.80`, and overrides for `lead_enrichment` ($10) and `url_summarizer` ($2). No new dependency — Python 3.11+ `tomllib`.

Halt-signal representation: an ERROR-level row in the EXISTING `events` table with `structured_data` carrying `{event_type: "budget.exceeded", workflow_type, cost_usd, cap_usd, window_start, window_end}`. The halt query uses SQLite's built-in `json_extract` against `$.event_type`. No ADR-002 schema change was required — the events table's structured_data column was designed for exactly this kind of additive use.

Cost attribution by `workflow_steps.started_at` (the day the step *began* is the day its cost counts). ISO-8601 UTC strings sort correctly as text, so the window query is a direct string range scan — no datetime coercion. Boundaries are inclusive lower / exclusive upper (`>= start AND < end`).

Slice 3's actual new work (per the kickoff prompt) was the budget-check + halt-signal LOGIC; actually stopping a running workflow is the orchestrator's job in Phase 2 and not built here. `is_workflow_halted_by_budget` is the primitive the orchestrator will call before scheduling each step.

Tests: 40 deterministic tests in `tests/test_budget.py` — pure evaluator across boundary/edge cases (zero cap, threshold=0, threshold=1, at-cap, at-threshold); UTC window correctness including tz conversion and naive-datetime defensive path; `cost_within_window` inclusion/exclusion at boundaries and isolation across workflows; end-to-end check with override resolution; exceed-event row shape; halt-signal query including the "unrelated error event must not trip the halt" anti-confusion case; TOML config loading including missing file, malformed TOML, missing required key, missing [defaults] section, override without `daily_cap_usd`, threshold out of range, and a sanity-check that the bundled `config/budgets.toml` loads. Full suite (90 tests including llm.py and state) passes cleanly. ruff check + ruff format + mypy strict all clean.

## Next Session Entry Point
Fresh session, Opus high. **Phase 1 Slice 5 — CheckpointSystem.** Human-in-the-loop pause/resume primitive for flagged actions (sending outreach, publishing, irreversible state changes). LangGraph's native `interrupt`/`Command(resume=...)` is the runtime mechanism (ADR-001/ADR-002 already named this); Slice 5's job is the audit half — writing `checkpoint.requested` / `checkpoint.approved` / `checkpoint.rejected` events through `observability.emit_event` (taxonomy and level semantics already in place from Slice 4), the approval-decision payload contract, and the resume-on-approval primitive the Phase 2 orchestrator will call. The actual orchestrator wiring + a CLI approval surface remain Phase 2. Do not build a web UI (still deferred).
