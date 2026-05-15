# agent-habitat STATUS

## Current Phase
Phase 2 — 5-Agent Lead Enrichment Crew (full plan: docs/ROADMAP.md)

## Current Slice
Phase 2 Slice 1 — Crew-architecture ADR **— COMPLETE** (ADR-006 accepted 2026-05-14).
ADR-003 (web search tool) **— ACCEPTED 2026-05-14**. Phase 2 Slice 2 (Researcher
agent) is now fully unblocked. Phase 1 remains shippable; Slice 8 (optional
Phase 1 polish) is queued separately and can be picked up anytime without
blocking Phase 2.

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

## Slice 7 Subtasks
- [x] Live API smoke across 5 deliberately varied URLs (Wikipedia article w/ `<main>`, Python docs w/o semantic tags, PEP 20 w/ `<article>`, example.com sparse, httpbin 404 controlled failure)
- [x] Per-run dataset captured from persisted workflow + JSONL telemetry: status, cost USD, input/output tokens, wall-time per step, parse path, output_ref
- [x] Synthesis: cost-distribution analysis, fixed-floor confirmation, MAX_PROMPT_CHARS truncation discovery, parse-path coverage of all three branches, budget-cap calibration
- [x] Phase 1 README rewritten: hiring-manager-honest framing, real-numbers calibration table, what-the-live-runs-taught-us section, honest scope (what Phase 1 is NOT), verified setup/run commands
- [x] Full suite (174 deterministic) passes; ruff check + ruff format + mypy strict all clean

## Slice 7 Calibration Findings

Five live runs on 2026-05-14 with `claude-sonnet-4-6`. Full dataset is in README.md
("Live calibration" section); the findings that came out of it:

- **Cost spans 14× across page types** ($0.001212 → $0.016830). The fixed-cost-floor pattern (Slice 6 finding on example.com) holds and is now quantified at ~$0.0012 per minimal run. Above ~500 input tokens, cost is input-dominated; output tokens are bounded 60–238 by the "three to five sentences" instruction regardless of input size.
- **`MAX_PROMPT_CHARS=12_000` fires silently on heavy real-world pages.** Wikipedia (45,079 extracted chars) and Python docs (26,239 extracted chars) both got truncated to the first 12K before reaching the LLM. Slice 6's single trivial page (142 chars) could not have surfaced this. Recorded in README as an honest product behavior; queued below as an Open Question for Phase 2 (log a `WARN` event on truncation, or chunk-and-stitch — ADR-gated).
- **All three parse-strategy branches got real-world coverage.** `<main>` (Wikipedia), `<article>` (PEP 20 — first observation; Slice 6 only saw fallback), whole-soup fallback (Python docs + example.com). The fallback is load-bearing on the public web: Python's official docs use neither semantic tag.
- **Latency is LLM-bound at 92–94%** of wall time on completed runs. Fetch + parse total 0.2–0.5s; everything else is Sonnet. Wall time scales at ~1.5s per 1,000 input tokens. Implication for Phase 2: each agent in a five-agent chain adds 2–7s LLM wall time depending on payload — pipelining and Haiku-for-grunt-work matter for end-to-end latency, not just dollars.
- **example.com run-to-run variance is tight.** Slice 6: 104 in / 54 out / $0.001122. Slice 7: 104 in / 60 out / $0.001212. Input deterministic, output ±10%. Telemetry numbers are trustworthy signal, not noise.
- **Failure contract held in the wild.** The httpbin 404 produced exactly the expected four-event trail (`workflow.started`, `step.started`, `step.failed`, `workflow.failed`), `FAILED` workflow + step rows with `finished_at` stamped, fast exit (0.52s), zero LLM cost. No stuck-RUNNING workflow.

**Budget cap recommendation — KEEP $2/day for `url_summarizer`.** Most expensive observed
run was $0.01683 (Python docs); $2/day = ~118 max-cost runs/day, or ~218 at the
mean of the four successful runs ($0.009187). The cap is now calibrated against
real heavy-page cost rather than extrapolated from the trivial-page floor —
generous headroom for operator-paced exploration without being so loose it
provides no safety signal. No config change needed; `config/budgets.toml` stays
at $2.00 daily for the `url_summarizer` override.

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
- **`MAX_PROMPT_CHARS` truncation — visibility resolved; chunk-and-stitch still ADR-gated.** Visibility half RESOLVED (2026-05-14, post-Slice-7 surgical fix): `SummarizerResult` now carries `input_truncated` + `original_chars`/`used_chars`/`dropped_chars`, and the summarize step's `step.completed` event records the same keys additively in `structured_data` (only when truncation actually fired — no false-positive keys on under-limit input). Live-smoke confirmed on https://en.wikipedia.org/wiki/Anthropic: 45,079 → 12,000 chars, dropped 33,079, run still COMPLETED, signal visible end-to-end. Same instinct as `LLMResult.stop_reason` (Slice 2) applied to the INPUT side. What remains: whether to chunk-and-stitch (summarise sections, then summarise the summaries) so a heavy page is actually fully covered rather than just transparently truncated — that is a behaviour/cost change, ADR-worthy, and queued for the Phase 2 Slice 1 crew-architecture ADR. The MAX_PROMPT_CHARS *value* (12,000) is a separate tuning question; the visibility data this fix generates is what should inform it. Source: Slice 7 live calibration finding #1, recorded in README; visibility commit references this entry.
- **`run_step()` utility — design RESOLVED in ADR-006 §2; implementation queued for Phase 2 Slice 2.** ADR-006 commits to a context-manager utility at `src/agent_habitat/orchestration/run_step.py` (the package exists but is empty; this is its first occupant) with explicit `record_cost` / `record_output_ref` / `record_structured_data` verbs on a yielded `StepRecorder`. Lifecycle = open RUNNING step row → emit step.started → yield → finalise COMPLETED with the recorder's accumulated cost/output_ref/structured_data on normal exit; on exception, finalise FAILED with `error_message` + emit step.failed, re-raise. No LangGraph dependency in the utility — it stays a pure habitat audit-lifecycle primitive. Implementation lands alongside Phase 2 Slice 2 (Researcher) since the researcher is the first new agent to need it; the summarizer is retrofitted onto the same utility in that same diff (proving the contract serves both call sites) and the ~40-45 lines of cosmetic trim ride along. Source: ADR-006 §2; alternatives (callable-with-work-fn, decorator, BaseAgent class, orchestrator-injected hooks) all rejected with rationale in ADR-006's Alternatives D.

## Last Session
ADR-003 — web search tool for the Researcher agent. Chose **Anthropic's built-in
`web_search` server-side tool**, enabled on the Researcher's single Haiku call
through `llm.py`. Three alternatives considered at their best and rejected for
THIS project (not in general): Tavily (LLM-tuned snippets but sits beside
`llm.py` as a second client; dual source of truth between API response and
`RawSignals` breaks the fabrication-resistance grounding invariant unless an
alignment check is added), Brave (cheapest but same structural objection PLUS
results are raw search-engine descriptions rather than LLM-ready spans —
requires a second LLM/`web_fetch` round-trip), and custom httpx+BS4 (has no
search backend; it's a fetch-and-parse layer, not a search-tool decision).

Architectural placement: `llm.complete()` gains a `tools=` passthrough (Slice 2
scope to design the exact surface); `compute_cost_usd` extends to add
`num_web_searches * $0.01` onto `LLMResult.cost_usd`; JSONL telemetry gains
additive `web_searches` / `web_search_fee_usd` keys. The Researcher captures
`search_result` content blocks from the response verbatim into
`RawSignals.signals[].text` — the exact prose the model read becomes the corpus
the drafter's substring check grounds against (ADR-006 §3 contract holds by
construction, not by reconciliation). Cost lands on `workflow_steps.cost_usd`
via the existing `run_step()` + `step.record_cost(result.cost_usd)` path; no
second writer, no new schema. Realistic per-Researcher-run cost: $0.035–$0.045
(~3 searches × $0.01 fee + Haiku tokens). Tradeoff accepted: vendor lock-in to
Anthropic's search backend — bounded blast radius because the Researcher is
encapsulated and its handoff contract is `RawSignals`, not the search tool.

Verified current pricing 2026-05-14: Anthropic `web_search` $10/1K searches +
token costs; Tavily $0.008/search PAYG (1K/mo free); Brave $5/1K ($5/mo credit,
legacy free tier deprecated Feb 2026). Numbers stamped in ADR-003 with the same
"verify before trusting" discipline the existing rate-table Open Question
applies. Forward dependency handed to Slice 2: extend `llm.py` `tools=`
passthrough + cost aggregation; build `RawSignals` from `search_result` content
blocks (not from the model's narrative text); land
`orchestration/run_step.py` and retrofit the summarizer through it in the same
diff (ADR-006 §2 forward dependency).

No product code changed. ADR-003 promoted from Proposed to Accepted in
`docs/adr/README.md`. STATUS.md updated to reflect Phase 2 Slice 2 fully
unblocked.

## Prior Session
Slice 7 — multi-URL live calibration + Phase 1 README. Ran the URL summarizer
against five deliberately varied real URLs (Wikipedia article with `<main>`,
Python docs with no semantic tags, PEP 20 with `<article>`, sparse example.com,
controlled httpbin 404), captured a clean per-run dataset from the persisted
workflow + JSONL telemetry, synthesised findings, and rewrote `README.md` as a
hiring-manager-facing Phase 1 showcase grounded in those real numbers — full
dataset table + the calibration-story section (what live runs taught us that
mocks couldn't). No product code changed; all quality gates rerun clean (174
deterministic tests, ruff check, ruff format, mypy strict). Total live-API
spend for the sweep: ~$0.037 across five runs.

Key calibration outcomes: (1) cost spans 14× across page types and is
input-dominated above ~500 tokens — fixed-cost floor pattern from Slice 6
holds and is now quantified at ~$0.0012/run; (2) `MAX_PROMPT_CHARS=12_000`
truncation fires silently on heavy real-world pages (Wikipedia, Python docs)
— recorded as a new Open Question; (3) all three parse-strategy branches got
real coverage including the previously-unseen `<article>` branch; (4) latency
is LLM-bound at 92–94% of wall time; (5) example.com run-to-run variance is
tight (input deterministic, output ±10%); (6) failure contract held in the
wild on the 404; (7) budget cap of $2/day for `url_summarizer` is recommended
to STAY — now calibrated against real heavy-page cost ($0.01683 max) rather
than extrapolated from the trivial-page floor.

Phase 1 is shippable. Five subsystems (LLM wrapper, state, observability,
budget, checkpoint) each independently exercised by tests and live runs; one
demo agent threads them end-to-end; the audit chain `workflow → step →
telemetry record` resolves on every successful run; the failure path holds.

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
**Phase 2 Slice 2 — Researcher agent.** Fresh session, Opus high (slice
implementation). ADR-003 is now accepted: the Researcher uses Anthropic's
server-side `web_search` tool routed through `llm.py`, with the per-search fee
aggregated into `LLMResult.cost_usd` and `search_result` content blocks
captured verbatim into `RawSignals`. Build, in order:

1. **`src/agent_habitat/orchestration/run_step.py`** — the context-manager
   utility per ADR-006 §2: `run_step(conn, *, workflow_id, step_index,
   agent_name, now=None)` yielding a `StepRecorder` with `record_cost` /
   `record_output_ref` / `record_structured_data` verbs. Tests cover normal
   exit (step row → COMPLETED + step.completed event with merged structured
   data), exception exit (step row → FAILED + step.failed + re-raise), and
   the no-cost / no-output_ref non-LLM step shape.
2. **Retrofit the summarizer onto `run_step()`** in the same diff — proves
   the contract serves a real existing agent. Fold in the ~40-45 lines of
   cosmetic trim (over-long docstring, section dividers, WORKFLOW_TYPE
   constants) noted in the (now-resolved) Open Question. Quality gates must
   stay clean (175+ deterministic tests, ruff, mypy strict).
3. **`src/agent_habitat/agents/researcher.py`** — `run_researcher(conn,
   *, company_name, ...)` per ADR-006 §1 handoff contract and ADR-003's
   tool decision. Concrete shape:
   - Extend `llm.complete()` with a `tools=` passthrough (forwarded to
     `messages.create`); extend `compute_cost_usd` to add `num_web_searches *
     $0.01` onto `cost_usd`; add `web_searches` + `web_search_fee_usd`
     additive keys to the JSONL telemetry record. Stamp the per-search rate
     constant in `llm.py` with a verify-against-public-pricing date.
   - Build the `RawSignals` Pydantic v2 model: each `Signal` carries
     `text: str` + `source_url: str` + `retrieved_at: datetime`. Construct
     signals **directly from `search_result` content blocks** in the response
     — NOT from the model's narrative text. The block text is what the
     fabrication-resistance substring check (ADR-006 §3) will ground against.
   - One Haiku call (per the model-routing table) with
     `tools=[{"type": "web_search_*", "max_uses": N}]`. Mirror
     `{signal_count, source_count, web_searches}` onto `step.completed`.
   - No LangGraph yet (that's Slice 6 — the orchestrator). The researcher is
     callable as a standalone agent now; the orchestrator will wrap it later.

Phase 2 Slice 1 is COMPLETE (ADR-006 sets the crew topology, the `run_step()`
contract, and the fabrication-resistance contract — ADR-005 folded in).
ADR-003 (this session) closes the last blueprint gap before Phase 2 Slice 2.
Phase 1 Slice 8 (optional polish) remains available anytime and is not on the
critical path.
