# agent-habitat STATUS

## Current Phase
Phase 1 — Habitat Infrastructure (full plan: docs/ROADMAP.md)

## Current Slice
Slice 2 — Persistence layer (Pydantic models + SQLite CRUD + reconciliation)  **— COMPLETE**

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

## Open Questions
- **Rate table needs verification.** `_RATES_USD_PER_MTOK` in `llm.py` uses best-known values (Haiku $1/$5, Sonnet $3/$15, Opus $15/$75 per MTok input/output) stamped 2026-05-13. Joseph: cross-check against the public Anthropic pricing page before relying on the cost numbers for any budget decision (Slice 3 will).
- **ADR-002 underspecification: `workflows.id` generation algorithm.** ADR-002 fixes the *relationship* (id is shared with LangGraph as `thread_id`) and the *type* (TEXT PRIMARY KEY) but does not name a generation method. Slice 2 defaults to `uuid.uuid4().hex` via `new_workflow_id()`; callers may override. Revisit with an ADR-002 addendum if Phase 2 needs sortable or time-prefixed ids (ULID, snowflake) for cheap range scans.
- **ADR-002 underspecification: orphan reconciliation target without LangGraph state.** ADR-002 says orphans reconcile to "failed with a synthesized event, or resume." The "or resume" branch needs LangGraph checkpoint state to decide whether resume is safe; that wiring lands with the Phase 2 orchestrator. Slice 2 implements the deterministic half: mark orphan FAILED, set `finished_at=now`, synthesize a WARN event. The resume branch can layer on top later without changing this contract.

## Last Session
Implemented `src/agent_habitat/state/` — `models.py` (Pydantic v2 models for `Workflow`, `WorkflowStep`, `Event` plus the three status/level enums and `new_workflow_id()`), `schema.py` (idempotent DDL exactly per ADR-002, plus a `connect()` helper that turns FKs on and sets `Row` factory), and `persistence.py` (the public CRUD surface listed above). All three audit tables are created against `data/state/agent_habitat.db` by default; tests use `tmp_path` only.

Aggregation primitive: `recompute_cost_total(conn, workflow_id)` reads `SUM(workflow_steps.cost_usd)` and writes it back to `workflows.cost_total_usd`. Per ADR-002, Slice 2 does aggregation; the budget cap + halt-on-exceed is Slice 3.

Reconciliation: `reconcile_orphan_steps(conn, now=None)` is the startup sweep ADR-002 named. It finds `workflow_steps WHERE status='running' AND finished_at IS NULL`, marks each one FAILED with a synthesized `events` row at level=WARN carrying `step_id` / `step_index` / `agent_name` / `reconciled_at` in `structured_data`. Idempotent (re-running on a swept DB is a no-op). Wiring it to actual startup is deferred until the orchestrator exists.

Step 0 (separate commit `4f9967c`): added `stop_reason: str | None` to `LLMResult` (pass-through from Anthropic's `Message.stop_reason`) and a derived `truncated` computed property (`stop_reason == "max_tokens"`). Also written to the JSONL telemetry record. Four new unit tests around the field.

Tests: 33 deterministic tests in `tests/test_state.py` covering idempotent DDL, FK enforcement, CRUD round-trip (Pydantic structural equality), status query, step-index uniqueness, ordered loads, JSON column round-trip (including unicode + nested + None), cost rollup (sum, zero-steps, isolation across workflows), full-round-trip with workflow + steps + events, and reconciliation (orphan → failed + event, completed/failed not touched, weird "running with finished_at" left alone, idempotent re-run, multi-workflow sweep). Full suite (49 tests including llm.py) passes cleanly. ruff check + ruff format + mypy strict all clean.

Two ADR-002 underspecifications were resolved with documented defaults (see Open Questions): id generation = `uuid4().hex`; orphan resolution = always FAILED for now, since LangGraph state for the "or resume" branch doesn't exist yet.

## Next Session Entry Point
Fresh session, Opus high. **Phase 1 Slice 3 — cost tracking module + budget caps.** Build on top of Slice 2's `recompute_cost_total`: a `CostTracker` (or equivalent) that observes per-call `LLMResult.cost_usd`, persists incrementally via `workflow_steps.cost_usd` + `recompute_cost_total`, and enforces a per-workflow cap (and optionally a daily aggregate cap) by raising before the next LLM call when the budget would be exceeded. Tests must cover the halt-on-exceed semantics deterministically (no live API). Verify the rate table in `llm.py:_RATES_USD_PER_MTOK` against the public Anthropic pricing page before any budget logic ships.
