# agent-habitat STATUS

## Current Phase
Phase 1 — Habitat Infrastructure (full plan: docs/ROADMAP.md)

## Current Slice
Slice 1 — Scaffold + ADR-001 + ADR-002 + initial llm.py wrapper

## Slice 1 Subtasks
- [x] Scaffold project skeleton + plan docs
- [x] ADR-001: LangGraph over CrewAI (Accepted 2026-05-13)
- [ ] ADR-002: Persistence schema — next session, Opus high, fresh context
- [ ] llm.py wrapper implementation — next session, Opus high (or Sonnet medium if scope is small), fresh context
- [ ] Slice 1 live smoke: single Haiku call through llm.py, verifies JSONL telemetry write

## Open Questions
- (none from this session)

## Last Session
ADR-001 written and accepted: LangGraph over CrewAI. Rejected alternatives (CrewAI, AutoGen, custom asyncio) presented at their genuine best before rejection. Core argument: state-machine model gives explicit transitions, native checkpointing, clean persistence boundaries, and inspectable handoffs — all four non-negotiable for the regulated-industry production posture. Accepted costs documented: steeper learning curve, more boilerplate for trivial workflows, LangChain ecosystem coupling, API still evolving (pin the version). Forward dependency handed to ADR-002: how LangGraph's built-in Checkpointer (runtime state blobs) relates to our audit-grade tables (queryable per-step rows with cost/status/timestamps) — three viable shapes flagged but not resolved.

## Next Session Entry Point
Fresh Claude Code session, Opus high. Write ADR-002 (persistence schema). The central question to resolve is the one ADR-001 handed forward: relationship between LangGraph's Checkpointer schema and our audit-grade workflow/workflow_steps/events tables. Three shapes already on the table — two parallel schemas in one SQLite file, single schema via custom `BaseCheckpointSaver` subclass, or LangGraph-only with a view layer. Pick one with explicit tradeoffs. Also resolve: per-step cost column placement, `output_ref` mechanics (pointer into `data/logs/*.jsonl` for large blobs), and `thread_id`/`workflow_id` identity.
