# agent-habitat STATUS

## Current Phase
Phase 1 — Habitat Infrastructure (full plan: docs/ROADMAP.md)

## Current Slice
Slice 1 — Scaffold + ADR-001 + ADR-002 + initial llm.py wrapper  **— COMPLETE**

## Slice 1 Subtasks
- [x] Scaffold project skeleton + plan docs
- [x] ADR-001: LangGraph over CrewAI (Accepted 2026-05-13)
- [x] ADR-002: Persistence schema (Accepted 2026-05-13)
- [x] llm.py wrapper implementation (2026-05-13)
- [x] Slice 1 live smoke: single Haiku call through llm.py, JSONL telemetry write verified (2026-05-13)

## Open Questions
- **Rate table needs verification.** `_RATES_USD_PER_MTOK` in `llm.py` uses best-known values (Haiku $1/$5, Sonnet $3/$15, Opus $15/$75 per MTok input/output) stamped 2026-05-13. Joseph: cross-check against the public Anthropic pricing page before relying on the cost numbers for any budget decision (Slice 3 will).

## Last Session
Implemented `src/agent_habitat/llm.py` and `tests/test_llm.py`. Single entry point: `complete(*, model_tier, messages, workflow_id, agent_name, system=None, max_tokens=1024, log_root=None) -> LLMResult`. `LLMResult` is a frozen Pydantic v2 model carrying `content`, `model`, `input_tokens`, `output_tokens`, `cost_usd`, `jsonl_ref` — exactly the contract ADR-002 specified. ADR-002's ordering invariant is enforced: the JSONL append + line-number capture complete before `complete()` returns, so `jsonl_ref` is durable. Three-tier `ModelTier` enum (Haiku/Sonnet/Opus 4.7). Cost computed via a date-stamped (2026-05-13) rate table — flagged as needs-verification. Telemetry path: `data/logs/YYYY-MM-DD/<workflow_id>.jsonl`, 1-indexed lines, single-writer assumption documented in `_append_telemetry`. structlog for operational logs; no retry layer, no async, no file locking, no budget caps (all later slices). API key loads from .env via python-dotenv. Tests: 13 deterministic unit tests (cost math across all three tiers, telemetry path layout, append + line-numbering, parent-dir creation, JSONL one-object-per-line invariant, frozen-model behavior, required-fields contract) + 1 `@pytest.mark.live` guarded Haiku smoke. mypy strict and ruff clean. Calibration notes from the live call: end-to-end round-trip ~1.6s; response content concatenation via `TextBlock` blocks worked first try; `usage.input_tokens` / `usage.output_tokens` populated as expected on the real `Message` shape — these aren't observations a mocked test would have surfaced. The `live` pytest marker was registered in `pyproject.toml` so `-m "not live"` runs cleanly in CI.

## Next Session Entry Point
Fresh session, Opus high. **Phase 1 Slice 2 — persistence layer per ADR-002.** Build `src/agent_habitat/state/persistence.py`: schema creation (workflows, workflow_steps, events DDL from ADR-002) against `data/state/agent_habitat.db`; typed CRUD (Pydantic v2 models for `Workflow`, `WorkflowStep`, `Event`); wire LangGraph's default `SqliteSaver` to the same `.db` file so the two parallel schemas share storage and `workflows.id == thread_id`. The audit path consumes the `LLMResult.cost_usd` / `LLMResult.jsonl_ref` contract `llm.py` now provides: agent-step code writes a pre-step `workflow_steps` row (status='running', finished_at NULL), invokes `complete()`, then updates the row with `cost_usd`, `output_ref = result.jsonl_ref`, `status='completed'`, `finished_at`. Halt/resume round-trip tests: save a workflow mid-step, reopen the connection, verify both LangGraph's checkpoint and our audit rows reload to consistent state. Plus a recovery-sweep test: orphaned `workflow_steps` (running + finished_at NULL) reconcile cleanly.
