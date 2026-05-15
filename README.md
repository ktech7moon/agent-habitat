# agent-habitat

Production-grade multi-agent orchestration framework with persistent state, observability, cost tracking, and human-in-the-loop checkpoints. The operational layer that lets multiple AI agents coordinate on real workloads without falling over in production. The product is the habitat infrastructure, not any specific workload — the first demonstration is a 5-agent lead enrichment crew; future workloads run on the same habitat.

Differentiator: audit-grade, fabrication-resistant, cost-controlled patterns that most multi-agent tutorials skip entirely. Built for regulated-industry operators who need to know exactly what happened, why, and at what cost.

## Status

Phase 1 — Habitat Infrastructure (in progress). Slice 1 kickoff complete. ADRs queued (LangGraph vs CrewAI, persistence schema, web search tool selection). See [docs/ROADMAP.md](docs/ROADMAP.md) for the full plan.

## Why This Exists

CrewAI and LangGraph tutorials ship demos. Production environments need: halt/resume across sessions, per-call cost telemetry with budget caps, human approval checkpoints before irreversible actions, cross-agent fabrication resistance, and structured audit logs. This project builds that operational layer and demonstrates it on a real workload.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # add your ANTHROPIC_API_KEY
pytest
```

## Roadmap

| Phase | Goal | Status |
|-------|------|--------|
| Phase 1 | Habitat infrastructure — persistence, cost tracking, observability, human checkpoints, single demo agent | In progress |
| Phase 2 | 5-agent lead enrichment crew running on the habitat | Queued |
| Phase 3 | Public push, Loom demo, README polish | Queued |

Full detail: [docs/ROADMAP.md](docs/ROADMAP.md)

## Contact

_Contact section — coming in Phase 3._
