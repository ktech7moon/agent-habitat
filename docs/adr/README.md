# Architecture Decision Records

## Index

| ADR | Title | Status |
|-----|-------|--------|
| ADR-001 | [LangGraph over CrewAI](ADR-001-langgraph-over-crewai.md) — state-machine debuggability vs natural-language handoff opacity | Accepted (2026-05-13) |
| ADR-002 | Persistence schema — SQLite tables for workflows, workflow_steps, events; output_ref to JSONL | Proposed |
| ADR-003 | Web search tool for Researcher agent — Anthropic web_search vs Tavily vs Brave vs custom | Proposed |
| ADR-004 | ICP rubric format — TOML, operator-tunable scoring dimensions | Proposed |
| ADR-005 | Cross-agent fabrication-resistance contract — how Drafter proves citations are grounded in upstream output | Proposed |

## Template

See `ADR-template.md` for the standard structure: Title, Status, Context, Decision, Alternatives Considered, Consequences.

## Conventions

- ADRs are numbered sequentially and never deleted; superseded ADRs are marked "Superseded by ADR-NNN."
- Every non-trivial decision gets an ADR before implementation. The Alternatives Considered section is mandatory.
- ADR-001 and ADR-002 are written in Slice 1 (Opus high, fresh session).
