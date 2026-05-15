# agent-habitat STATUS

## Current Phase
Phase 1 — Habitat Infrastructure (full plan: docs/ROADMAP.md)

## Current Slice
Slice 6 — Demo agent: URL summarizer exercising the full habitat stack **— COMPLETE**

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

## Slice 6 Subtasks
- [x] `src/agent_habitat/agents/summarizer.py` — `run_summarizer`, `fetch_url`, `extract_readable_text`, `SummarizerResult`, `SummarizerError`; three synchronous steps (fetch / parse / summarize), Sonnet tier via `llm.complete()`, decision-support framing in the CLI output
- [x] `src/agent_habitat/cli.py` — `run-summarizer URL` command reusing the `--db` pattern; non-zero exit on FAILED workflow
- [x] Habitat integration: one workflow row + three step rows + the conventioned event sequence (`workflow.started`, `step.{started,completed}` ×3, `workflow.completed`) on success; FAILED workflow + `workflow.failed` + matching `step.failed` on any step failure, `finished_at` always stamped, no stuck-RUNNING workflows
- [x] Cost path wired end to end: `LLMResult.cost_usd` → `workflow_steps.cost_usd` (summarize step) → `recompute_cost_total` → `workflows.cost_total_usd`; `output_ref` on the summarize step points at the JSONL line `llm.py` wrote
- [x] 26 deterministic tests in `tests/test_summarizer.py` (httpx `MockTransport` for fetch, `unittest.mock.patch` for the LLM): fetch happy + 404 + network error + oversize + empty + bad scheme + bad URL; parse happy + script/style stripped + `<main>` preference + fallback + too-short rejected; happy run persists everything correctly; four failure paths (bad scheme, 404, network error, empty page, LLM error) end the workflow FAILED with the right step row + events; CLI happy + failure exits non-zero
- [x] One live smoke (`@pytest.mark.live`) against `https://example.com/`: full round-trip verified, calibration observations captured (see "Slice 6 Live Smoke Calibration" below)
- [x] Full suite (175 tests, including the live smoke) passes; ruff check + ruff format + mypy strict clean

## Slice 6 Live Smoke Calibration

One live run against `https://example.com/` on 2026-05-14 with Sonnet 4.6.
Genuine observations a mocked test could not have surfaced — these feed the
Slice 7 calibration story / README:

- **Real cost: $0.001122** for the whole run (104 input tokens, 54 output tokens). That's ~0.06% of the configured `url_summarizer` $2/day cap. Even ~1800 trivial-page runs/day would not trip the cap — the cap is generous for short pages and only meaningful for longer documents or many calls.
- **Input-token floor is dominated by the system prompt + boilerplate, not the page.** example.com's readable text is ~140 chars; the prompt still came in at 104 input tokens. Useful sizing intuition: cost-per-run has a non-trivial fixed floor regardless of how short the page is.
- **Sonnet self-paced to ~3 sentences without truncation** (54 output tokens vs 512 cap, `stop_reason=end_turn`). The system-prompt instruction "three to five sentences in plain prose" held; no markdown leaked, no preamble. Encouraging for a prose contract that has to survive without an explicit JSON schema.
- **`<main>`/`<article>` preference did NOT fire on the simplest real page.** example.com has neither tag, so the parser fell back to whole-soup extraction. The mocks asserted the *preference path* works; the *real* path on the simplest production URL is the fallback. Implication for Slice 7: invest more in the fallback's quality, since real-world pages skew toward unsemantic markup.
- **Latency: 2.84s total**, dominated by the Sonnet call. The httpx fetch to example.com was sub-100ms; parse is negligible. LLM time is the binding latency — useful when reasoning about Phase 2 multi-agent pipelines (each agent call adds ~2s of LLM wall time even on trivial input).
- **Eight events emitted in the expected order** — workflow.started → step.started/completed × 3 → workflow.completed. The taxonomy survived first contact with a real workload; no convention drift versus what Slice 4 documented.
- **Telemetry round-trip is real.** The summarize step's `output_ref` (a path + line number) resolved to a JSONL record carrying `workflow_id`, `agent_name=url_summarizer`, `model=claude-sonnet-4-6`, both token counts, cost, and the full response text. This is the audit story working end-to-end on a real call rather than a fixture.

## Slice 5 Subtasks
- [x] `checkpoint/system.py` — `request_checkpoint`, `approve_checkpoint`, `reject_checkpoint`, `get_checkpoint`, `list_pending_checkpoints`, `is_workflow_paused_for_checkpoint`; `Checkpoint` frozen dataclass, `CheckpointResolution` enum, `CheckpointError`
- [x] Pending-approval record is additive on ADR-002's events table (request event id IS the checkpoint id; resolution events back-reference via `structured_data.checkpoint_id`) — no schema change
- [x] Workflow state transitions: RUNNING → PAUSED on request; PAUSED → RUNNING on approve; PAUSED → CANCELLED (terminal, `finished_at` stamped) on reject
- [x] One pending checkpoint per workflow — a second request on a still-paused workflow raises
- [x] `cli.py` extended with `checkpoint {list,show,approve,reject}` group; `--db` option on the group; decision-support footer on pending `show` output; `CheckpointError` surfaces as a clean `click.ClickException`
- [x] 30 deterministic tests in `tests/test_checkpoint.py`; full suite (149 tests) passes, ruff check + ruff format + mypy strict clean

## Open Questions
- **Consolidate `llm.py`'s JSONL telemetry writer through the ObservabilityLayer.** Today `llm.py._append_telemetry` writes JSONL directly; Slice 4 added the conventioned READ side (`iter_telemetry`, `resolve_output_ref`) but did NOT touch the writer — `LLMResult` is a load-bearing contract and rule #14 forbids broad refactors without an ADR. Future work: either (a) route llm.py's writer through an ObservabilityLayer writer module so the path/line/format conventions live in one place, or (b) explicitly document the writer-stays-in-llm.py boundary as the chosen architecture. Trigger: any second writer of JSONL telemetry (Slice 5 checkpoint payloads? Phase 2 agent intermediate artefacts?) — that's the moment to centralise.
- **Rate table needs verification.** `_RATES_USD_PER_MTOK` in `llm.py` uses best-known values (Haiku $1/$5, Sonnet $3/$15, Opus $15/$75 per MTok input/output) stamped 2026-05-13. Joseph: cross-check against the public Anthropic pricing page before relying on the cost numbers for any budget decision (Slice 3 enforcement is now wired but reads the same rate table).
- **ADR-002 underspecification: `workflows.id` generation algorithm.** ADR-002 fixes the *relationship* (id is shared with LangGraph as `thread_id`) and the *type* (TEXT PRIMARY KEY) but does not name a generation method. Slice 2 defaults to `uuid.uuid4().hex` via `new_workflow_id()`; callers may override. Revisit with an ADR-002 addendum if Phase 2 needs sortable or time-prefixed ids (ULID, snowflake) for cheap range scans.
- **ADR-002 underspecification: orphan reconciliation target without LangGraph state.** ADR-002 says orphans reconcile to "failed with a synthesized event, or resume." The "or resume" branch needs LangGraph checkpoint state to decide whether resume is safe; that wiring lands with the Phase 2 orchestrator. Slice 2 implements the deterministic half: mark orphan FAILED, set `finished_at=now`, synthesize a WARN event. The resume branch can layer on top later without changing this contract.
- **Slice 3 "daily" definition resolved: UTC calendar day.** "Daily budget cap" = the half-open interval `[today 00:00:00 UTC, tomorrow 00:00:00 UTC)`. Caps reset at UTC midnight. Why UTC over rolling-24h or local-tz: aligns with the JSONL telemetry directory layout (`data/logs/YYYY-MM-DD/` already UTC), is trivially auditable, and makes window queries simple ISO-string range comparisons. Revisit if a workload needs per-tenant local-tz semantics.
- **Promote a habitat-level `run_step()` utility before Phase 2 agents are built (ADR-gated).** The URL summarizer's step-lifecycle code (`_run_step` / `_run_summarize_step` in `src/agent_habitat/agents/summarizer.py` — ~188 lines) implements the open-RUNNING-row → emit step.started → do work → close COMPLETED/FAILED → emit result-event sequence. Every Phase 2 agent (researcher, extractor, scorer, drafter, critic) needs this exact lifecycle. If each copies it, that is ~940 lines of duplicated boilerplate and five independent copies of the audit-grade guarantee that can drift. The fix is to promote a habitat-level `run_step()` utility that agents call instead of reimplementing — a new shared abstraction, so it is ADR-gated per Working Agreement rule 14. Decide it as part of (or alongside) the Phase 2 Slice 1 ADR, before Phase 2 agents are built. Source: the Slice 6 summarizer.py audit. A separate, smaller cleanup also rides along whenever `summarizer.py` is next rewritten: ~40-45 lines of cosmetic trim (over-long module docstring, five section dividers, WORKFLOW_TYPE/AGENT_NAME constants that could be literals) — not worth its own session, fold it into the `run_step` extraction diff.

## Last Session
Implemented `src/agent_habitat/agents/summarizer.py` — the Slice 6 demo agent. Three synchronous steps (fetch / parse / summarize) hand-wired through the existing habitat: workflow + step rows via Slice 2's persistence, lifecycle and step events via Slice 4's `emit_event` over the canonical `EventType` taxonomy, the LLM call routed through `llm.py` (Sonnet tier — the kickoff prompt overrode STATUS.md's earlier "Haiku" note), and `recompute_cost_total` rolling the summarize step's real cost up onto `workflows.cost_total_usd`. The summarize step's `output_ref` points at the JSONL line `llm.py` wrote, so the audit chain `workflow → step → telemetry record` resolves end-to-end on the first real workload.

Failure-path contract: any step error transitions the workflow to `FAILED` with `finished_at` stamped, emits a `step.failed` + `workflow.failed` pair, and returns a `SummarizerResult(status=FAILED, error_step, error_message)`. The agent never crashes uncaught from the caller's perspective, never leaves a workflow stuck in `RUNNING`. Exercised in tests across four real failure modes (bad URL scheme, 404, httpx network error, empty/SPA page) plus an injected LLM error — each one verifies the workflow row, the step row, and the event sequence.

CLI surface: `agent-habitat run-summarizer URL [--db PATH] [--workflow-id ID]`. Reuses the Slice 5 `--db` pattern. Prints workflow id + status + cost + the summary, footer-disclaimed for decision-support framing (CLAUDE.md non-obvious constraint). Failed runs exit non-zero.

Explicitly out of scope: no LangGraph (that's Phase 2), no checkpoint invocation (the summarizer has no flagged actions; Slice 5's system exists but isn't called), no active budget enforcement (cost is *recorded* so the existing budget primitives can find it — actually halting on exceed is the orchestrator's job). Per the kickoff prompt's hard stops, no new orchestration machinery was invented to make pieces fit.

Tests: 26 deterministic in `tests/test_summarizer.py` (httpx `MockTransport` for fetch, `unittest.mock.patch` for the LLM call); full suite (175 tests including the new live smoke) passes; ruff check + ruff format + mypy strict all clean. One live smoke ran successfully against https://example.com/ — observations recorded under "Slice 6 Live Smoke Calibration" above.

Implemented `src/agent_habitat/checkpoint/` — the CheckpointSystem. One package, one core module (`system.py`) plus the public-API `__init__.py`. The Slice 5 surface is six functions, one frozen dataclass, one enum, one exception:

  `request_checkpoint(conn, *, workflow_id, action, summary, proposed_payload=None, step_id=None, requested_by=None, now=None) → Checkpoint`
  `approve_checkpoint(conn, checkpoint_id, *, reviewer, note=None, now=None) → Checkpoint`
  `reject_checkpoint(conn, checkpoint_id, *, reviewer, reason=None, now=None) → Checkpoint`
  `get_checkpoint(conn, checkpoint_id) → Checkpoint | None`
  `list_pending_checkpoints(conn, workflow_id=None) → list[Checkpoint]`
  `is_workflow_paused_for_checkpoint(conn, workflow_id) → bool`

Pending-approval storage decision: ADDITIVE on ADR-002's events table — no schema change. The `checkpoint.requested` event's `events.id` IS the checkpoint id; resolution events (`checkpoint.approved` / `checkpoint.rejected`) carry `structured_data.checkpoint_id` back-referencing the request. A checkpoint is pending iff no resolution row references it — expressed in SQL via `NOT EXISTS (SELECT 1 FROM events e2 WHERE json_extract(...) = e1.id)`. Same idiom Slice 3 uses for `is_workflow_halted_by_budget`. ADR-002 already supports this shape (Consequences section explicitly named "Slice 5 (CheckpointSystem)" as the prototype for `level='approval'` rows over the events table), so no addendum was needed.

Workflow state machine: `request` flips `workflows.status` to PAUSED; `approve` flips it back to RUNNING; `reject` flips it to CANCELLED with `finished_at` stamped (terminal). Halt-signal query (`is_workflow_paused_for_checkpoint`) consults only the events table, not `workflows.status` — same pattern as the budget halt-signal, so the signal stays correct even if a status update lags. One pending checkpoint per workflow is enforced at the API boundary: a second request on a still-paused workflow raises `CheckpointError`. Multi-pending semantics would force a design choice about which resolution drives the workflow status transition; serialised resolution is the simpler, audit-clearer shape and matches the "halt the workflow until the human decides" framing.

Slice 5's new work is the checkpoint LOGIC + the CLI; actually pausing or resuming a *running* graph mid-execution remains the orchestrator's job (Phase 2). A workflow with an unresolved checkpoint reads as not-runnable — the orchestrator (when it lands) will call `is_workflow_paused_for_checkpoint` before scheduling each step and obey it. Slice 5 establishes the fact; the orchestrator obeys it later.

CLI surface in `src/agent_habitat/cli.py`:

  `agent-habitat checkpoint [--db PATH] list [--workflow ID]`
  `agent-habitat checkpoint [--db PATH] show CHECKPOINT_ID`
  `agent-habitat checkpoint [--db PATH] approve CHECKPOINT_ID --reviewer NAME [--note TEXT]`
  `agent-habitat checkpoint [--db PATH] reject  CHECKPOINT_ID --reviewer NAME [--reason TEXT]`

`--db` defaults to `DEFAULT_DB_PATH` (the production `data/state/agent_habitat.db`). `--reviewer` is required on approve/reject — every decision lands with a name on it for audit. `list` renders a per-checkpoint two-line block (id + workflow + action + requested-at + requester, then the summary). `show` renders a full detail card with the proposed payload pretty-printed as JSON; pending checkpoints include the project's decision-support footer ("operational context for your approval decision, not legal/medical/financial advice — verify before approving"). Resolved checkpoints render the resolution + reviewer + resolved_at + note. `CheckpointError` (unknown id, terminal workflow, already-resolved, already-pending) surfaces as a clean `click.ClickException` with a non-zero exit and no traceback noise.

Audit shape on the events table — every checkpoint emits two or three rows total:
- `checkpoint.requested` at level CHECKPOINT, payload `{event_type, action, summary, proposed_payload?, requested_by?}`.
- `checkpoint.approved` OR `checkpoint.rejected` at level APPROVAL, payload `{event_type, checkpoint_id, reviewer, note?}`.

This is the first time the CHECKPOINT and APPROVAL `EventLevel`s are written (Slice 4 added the taxonomy + level guide; Slice 5 is the first writer). The `EventType.CHECKPOINT_REQUESTED/APPROVED/REJECTED` members from Slice 4 are used verbatim — not redefined.

Tests: 30 deterministic in `tests/test_checkpoint.py`. Six test classes plus one round-trip:
- `TestRequest` — event-row shape, level, taxonomy; workflow → PAUSED; optional fields omitted when unset; unknown workflow / terminal workflow (parametrised over completed/failed/cancelled) / already-pending all raise.
- `TestResolveApprove` — workflow PAUSED → RUNNING with `finished_at` unchanged; approval event back-references checkpoint id and records reviewer + note.
- `TestResolveReject` — workflow PAUSED → CANCELLED with `finished_at` stamped; rejection event carries checkpoint_id + reason.
- `TestQueries` — `list_pending_checkpoints` filters resolved out and supports workflow filtering; `is_workflow_paused_for_checkpoint` mirrors the pending flag across request/resolve; `get_checkpoint` returns None for unknown ids and ignores non-checkpoint event ids (defends against passing a `workflow.note` row id).
- `TestResolveErrors` — approve/reject of unknown id raises; double-approve and approve-after-reject raise.
- `TestCLI` (click.testing.CliRunner over a tmp_path DB):
    - `list` empty + `list` with one pending
    - `show` pending renders decision-support footer; `show` resolved renders the resolution block (no footer)
    - `approve` + `reject` happy paths through the CLI, then re-open the DB and verify workflow status + resolution
    - `show` unknown id, `approve` unknown id, `reject` already-resolved id all exit non-zero with the right message
    - `approve` without `--reviewer` exits non-zero (click's own required-option enforcement)
- `test_proposed_payload_json_roundtrip` — nested dicts + lists in `proposed_payload` survive emit_event → SQLite TEXT(json) → load_events → get_checkpoint.

Full suite (149 tests including llm.py, state, budget, observability, checkpoint) passes. ruff check + ruff format --check + mypy strict all clean.

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
Fresh session, Opus high. **Phase 1 Slice 7 — live API smoke across 3-5 URLs + Phase 1 README.** Slice 6 landed one live calibration data point (example.com); Slice 7 broadens it: pick 3-5 stable, varied URLs (a news article with semantic `<article>` markup, a long-form blog post, a corporate marketing page, a docs page, optionally one paywall/SPA to confirm the FAILED path) and run `run-summarizer` against each, capturing real input-token / output-token / cost / latency numbers per page-type. Roll the calibration table into a `README.md` for Phase 1 — the operator-facing pitch ("habitat infrastructure for production agents") plus the calibration story (what the live runs revealed that mocks couldn't, what's surprisingly cheap, where the `<main>` preference fires vs. falls back). Audit the Phase 1 surface for any rough edges the multi-URL run exposes. No new code unless a calibration finding demands it. After Slice 7: Phase 1 is shippable; Phase 2 begins.
