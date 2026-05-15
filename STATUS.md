# agent-habitat STATUS

## Current Phase
Phase 1 — Habitat Infrastructure (full plan: docs/ROADMAP.md)

## Current Slice
Slice 1 — Scaffold + ADR-001 + ADR-002 + initial llm.py wrapper

## Slice 1 Subtasks
- [x] Scaffold project skeleton + plan docs
- [x] ADR-001: LangGraph over CrewAI (Accepted 2026-05-13)
- [x] ADR-002: Persistence schema (Accepted 2026-05-13)
- [ ] llm.py wrapper implementation — next session, Opus high (or Sonnet medium if scope is small), fresh context
- [ ] Slice 1 live smoke: single Haiku call through llm.py, verifies JSONL telemetry write

## Open Questions
- (none from this session)

## Last Session
ADR-002 written and accepted: two parallel schemas in one SQLite file. LangGraph's default `SqliteSaver` writes its checkpoint tables; agent-habitat writes `workflows` / `workflow_steps` / `events` to the same `.db` file. They share an identifier (`workflows.id` == LangGraph `thread_id`) but the two writers are independent — no shared transaction, no inherited subclass. The decision turned on the dual-write hazard analysis: Option 2 (custom `BaseCheckpointSaver` that writes both) claims atomicity that depends on LangGraph's internals continuing to cooperate across versions — a brittle invariant we can't statically verify. Option 1's two writes don't claim atomicity but the failure mode is recoverable: pre-step audit rows (status='running', finished_at NULL) are the recovery anchor, and a startup sweep reconciles orphaned steps against LangGraph state. Saga-with-idempotency, not dual-write-under-transaction. Options 3 (LangGraph-only + view) and 4 (LangGraph-as-stateless) were considered: 3 erodes the project's distinguishing property (queryable audit rows), 4 contradicts ADR-001's reason for picking LangGraph. Illustrative DDL is inside the ADR. `output_ref` format: `data/logs/YYYY-MM-DD/<workflow_id>.jsonl:<line>` (1-indexed; ripgrep/awk/editor friendly). Forward dependency handed to `llm.py`: every call must return `{cost_usd, input_tokens, output_tokens, jsonl_ref}` and the JSONL append must complete before the function returns — that's the contract `llm.py` owes upstream and the reason it lands before Slice 2 wires the audit path.

## Next Session Entry Point
Fresh Claude Code session, Opus high (or Sonnet medium if the wrapper scope is genuinely small — judge on arrival). Implement `src/agent_habitat/llm.py` — the single-entry wrapper through which every LLM call in the codebase flows. Contract: takes (model_tier, messages, optional system, optional max_tokens) and returns a structured `LLMResult` containing the response, `cost_usd`, `input_tokens`, `output_tokens`, and a `jsonl_ref` (`path:line`) into `data/logs/YYYY-MM-DD/<workflow_id>.jsonl`. The JSONL append must complete before the function returns. No direct `anthropic` SDK imports anywhere else in the codebase (CLAUDE.md rule 9). Three-tier routing (Haiku/Sonnet/Opus 4.7) is the model dispatch surface; default DOWN. Slice 1 live smoke after: a single Haiku call through `llm.py` that verifies the JSONL line lands and the cost-per-call math matches current rates.
