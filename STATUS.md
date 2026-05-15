# agent-habitat STATUS

## Current Phase
Phase 1 — Habitat Infrastructure (full plan: docs/ROADMAP.md)

## Current Slice
Slice 1 — Scaffold + ADR-001 + ADR-002 + initial llm.py wrapper

## Slice 1 Subtasks
- [x] Scaffold project skeleton + plan docs (this session)
- [ ] ADR-001: LangGraph over CrewAI — next session, Opus high, fresh context
- [ ] ADR-002: Persistence schema — next session, Opus high, fresh context
- [ ] llm.py wrapper implementation — next session, Opus high (or Sonnet medium if scope is small), fresh context
- [ ] Slice 1 live smoke: single Haiku call through llm.py, verifies JSONL telemetry write

## Open Questions
- (none from kickoff)

## Last Session
Kickoff: scaffolded skeleton, wrote full plan into CLAUDE.md / docs/ROADMAP.md / docs/PATTERNS.md / docs/adr/, pyproject.toml with deps + tool config, venv, .gitignore, .claude/settings.json, firewall hook verified, initial commit. pytest passes (1 smoke test), ruff clean, mypy clean.

## Next Session Entry Point
Fresh Claude Code session, Opus high. Write ADR-001 (LangGraph over CrewAI). Alternatives to document: CrewAI, AutoGen, custom asyncio orchestration. Criteria: state-machine debuggability, handoff observability, human-in-the-loop checkpoint fit, production track record.
