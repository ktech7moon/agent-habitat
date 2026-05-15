# agent-habitat

## Project Context

agent-habitat is a production-grade multi-agent orchestration framework with persistent state, observability, cost tracking, and human-in-the-loop checkpoints. It's the operational layer that lets multiple AI agents coordinate on real workloads without falling over in production.

The product is the habitat infrastructure, not any specific workload. The first demonstration workload (Phase 2) is a 5-agent lead enrichment crew used for the operator's own freelance prospecting. Future workloads run on the same habitat.

Target buyer pattern: AI tooling companies, regulated-industry operators, and FDE teams deploying agents into customer environments. Audit-grade, fabrication-resistant, cost-controlled patterns differentiate this from CrewAI/LangGraph tutorials — most multi-agent frameworks ship demos, not production-ready systems.

Non-obvious constraints:
- Every LLM call goes through the llm.py wrapper. No direct SDK calls in agent code.
- Every workflow persists state across sessions. Workflows can be halted and resumed.
- Every agent action is observable via event log + JSONL telemetry.
- Cost caps halt workflows before they exceed budget.
- Human-in-the-loop checkpoints required for flagged actions (sending outreach, publishing, irreversible actions).
- No fabrication across agents: drafter cites only signals the researcher actually produced. Cross-agent validation is a first-class design constraint.
- Decision-support framing: every consequential workflow output is structurally disclaimed.

## Technical Stack

Runtime: Python 3.11+ on a standard venv
- anthropic (latest)
- langgraph (latest stable; NOT CrewAI — state-machine model is more debuggable)
- langchain-core, langchain-anthropic
- pydantic v2
- click
- structlog
- python-dotenv
- httpx (async-ready, for researcher agent web fetches)
- beautifulsoup4 (lightweight HTML parsing)

Dev: pytest, pytest-cov, ruff, mypy strict

Models — three-tier routing, default DOWN:
- claude-haiku-4-5-20251001 — Researcher, Critic
- claude-sonnet-4-6        — Extractor, Scorer, Orchestrator
- claude-opus-4-7          — Drafter only (user-visible prose quality)

Deferred — DO NOT install until trigger conditions hit:
- Postgres (SQLite sufficient Phase 1-2; trigger: >100k workflow records OR multi-process concurrency)
- Redis (trigger: cross-workflow context sharing becomes a real bottleneck)
- Celery/RQ (trigger: workflows need background or parallel execution)
- Web UI (trigger: human-in-the-loop approvals frequent enough to justify it)
- LangSmith / observability SaaS (trigger: telemetry queries outgrow ripgrep-on-JSONL)
- Vector store (trigger: agents need a knowledge base bigger than fits in a prompt)

## Working Agreement

1. ASK don't guess. One clarifying question beats a wrong assumption.
2. ADR-before-code for any non-trivial decision. Alternatives section mandatory.
3. STATUS.md is the source of truth between sessions. Update it before ending any session.
4. Audit-grade output: every decision logged with rule fired, reasoning trace, confidence/score. JSONL telemetry per LLM call.
5. Fabrication-resistance is a validated contract, not a hope. Cross-agent: drafter cites only signals upstream agents produced; enforce with substring checks where applicable.
6. Decision-support framing on every consequential user-visible output. No legal/medical/financial advice posture.
7. Operator tunes outcomes (TOML rubrics); developers tune prompts (versioned .md files).
8. Three-tier model routing, default down. Haiku grunt work, Sonnet workhorse, Opus 4.7 only where prose/architecture quality genuinely matters.
9. Every LLM call goes through llm.py. No direct anthropic SDK calls in agent code.
10. Every workflow persists state. Halt/resume must work.
11. Cost caps halt workflows before they exceed budget. Per-call cost in telemetry.
12. CONTEXT DISCIPLINE: /compact at ~100k tokens with a focus directive; /clear between unrelated tasks when session >50k; /clear or /exit at session end; /usage at the start of each day. Default Sonnet medium for routine work, Opus high for slice implementation, Opus xHigh only for architecture ADRs.
13. Git: never push, never reset --hard, never branch -D, never force checkout. The firewall hook enforces this — do not work around it.
14. No broad refactors without an ADR. Conservative edits. Don't fix things outside the current slice.

## Operator & Collaboration Norms

Operator: 15-year senior software engineer, building a freelance practice targeting AI agent contract work in regulated industries (mortgage, healthcare middleware, compliance). Positioning: "senior engineer building production AI agents in regulated industries."

Collaboration norms:
- Prefers honest assessment over validation. Push back when something is wrong; surface what's questionable in any work product.
- Reviews pasted status reports from multiple Claude Code windows like code review — grade what's strong, flag what's weak, recommend next step.
- STATUS.md handles session handoff. Trust it over remembered chat context.
- Energy comes in waves; ambitious late-night plans need morning recalibration. Help keep scope honest without being preachy.
- Cross-checks against other AI tools to triangulate. Engage with the substance when an outside opinion is raised.

## Tool Discipline

- All LLM calls go through `llm.py`. No direct `anthropic` SDK imports outside `llm.py`.
- Web fetch via `httpx`. HTML parsing via `beautifulsoup4`.
- No direct SDK imports in agent code.

## Code-Writing Discipline

- Pydantic v2 at all structured data boundaries.
- `mypy` strict passes clean before any commit.
- `ruff` clean before any commit.
- Conservative edits — no broad refactors without an ADR.
- Don't fix things outside the current slice.

## Git Discipline

Never push. Never `reset --hard`. Never `branch -D`. Never force checkout. The firewall hook enforces this — do not work around it.

## Context Management

/compact at ~100k tokens with a focus directive; /clear between unrelated tasks when session >50k; /clear or /exit at session end; /usage at the start of each day. Default Sonnet medium for routine work, Opus high for slice implementation, Opus xHigh only for architecture ADRs.

## Model Routing

| Tier   | Model                     | Used for                              |
|--------|---------------------------|---------------------------------------|
| Haiku  | claude-haiku-4-5-20251001 | Researcher, Critic (grunt work)       |
| Sonnet | claude-sonnet-4-6         | Extractor, Scorer, Orchestrator       |
| Opus   | claude-opus-4-7           | Drafter only (user-visible prose)     |

Default DOWN: start at Haiku, escalate only when quality genuinely requires it.

## Plan & Status

- Full roadmap: `docs/ROADMAP.md`
- Portfolio carry-forward patterns: `docs/PATTERNS.md`
- Session-to-session status: `STATUS.md`
- Architecture decision records: `docs/adr/`
