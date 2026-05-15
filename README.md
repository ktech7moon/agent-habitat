# agent-habitat

**Production-grade infrastructure for multi-agent AI workloads.** Persistent workflow state, structured observability, per-call cost telemetry with budget caps, human-in-the-loop approval checkpoints, and a fabrication-resistance posture — built for environments where you have to be able to say exactly what an agent did, why, and at what cost.

The product is the habitat itself, not any particular workload. Phase 1 ships the habitat exercised end-to-end by a single demo agent. Phase 2 stands up a five-agent lead-enrichment crew on the same infrastructure. Future workloads reuse it.

## Status

**Phase 1 — Habitat Infrastructure: complete.** Single-agent workflows persist, halt/resume, log to a queryable audit trail, write per-LLM-call cost, and pause for human approval on flagged actions. Calibrated against five live URL summarisations spanning Wikipedia, Python docs, a PEP, a near-empty page, and a 404.

Phase 2 (five-agent crew) and Phase 3 (public push, Loom, polish) are next. See [docs/ROADMAP.md](docs/ROADMAP.md).

## Why this exists

Most multi-agent tutorials ship a demo: a CrewAI script that chains three agents and calls it production. Production needs more than that:

- **Halt/resume across sessions.** A long-running workflow has to survive a process restart, a budget overshoot, or an operator stepping away mid-run.
- **Per-call cost telemetry.** Every LLM call records model, tokens, USD cost, and a back-reference into a JSONL detail line. Budget caps halt workflows before they exceed budget — not after.
- **Audit-grade event logs.** Every workflow and step transition writes a typed event with structured data. The audit chain `workflow → step → telemetry record` resolves end-to-end with `path:line` references.
- **Human-in-the-loop checkpoints.** Flagged actions (sending outreach, publishing, irreversible state changes) pause the workflow and write a pending-approval record. An operator approves or rejects through the CLI; resolution is logged.
- **Fabrication resistance as a validated contract**, not a hope. Phase 2 will enforce that the drafter cites only signals upstream agents actually produced; Phase 1 establishes the audit primitives that contract will run on.

Target buyer pattern: AI tooling companies, regulated-industry operators, and FDE teams deploying agents into customer environments — places where "we don't really know what the agent did" is not an acceptable answer.

## What Phase 1 delivers

Five subsystems, each independently exercised by tests and live-run smoke:

| Subsystem | Module | What it does |
|---|---|---|
| **LLM wrapper** | `src/agent_habitat/llm.py` | Every model call routes through one `complete()`. Records JSONL telemetry per call (model, tokens, cost USD, response text, `stop_reason`) and returns an `LLMResult` carrying the `path:line` reference. No direct Anthropic SDK calls anywhere else. |
| **State persistence** | `src/agent_habitat/state/` | SQLite-backed `workflows`, `workflow_steps`, `events` tables (ADR-002). Pydantic v2 models for round-trip; orphan reconciliation on startup; `recompute_cost_total` rolls per-step cost up onto the workflow. |
| **Observability** | `src/agent_habitat/observability/` | `emit_event()` over the canonical `EventType` taxonomy (workflow.{started,completed,failed}, step.{started,completed,failed}, budget.exceeded, checkpoint.{requested,approved,rejected}). Central structlog config. JSONL reader (`iter_telemetry`, `resolve_output_ref`). |
| **Budget control** | `src/agent_habitat/budget/` | TOML config (`config/budgets.toml`), per-workflow-type daily cap, UTC-calendar-day window, `evaluate_budget` pure function, `record_budget_exceeded` event writer, `is_workflow_halted_by_budget` halt-signal query. |
| **Checkpoint system** | `src/agent_habitat/checkpoint/` | `request_checkpoint` → workflow paused; `approve_checkpoint` / `reject_checkpoint` resolve it; CLI surface for operators (`agent-habitat checkpoint list|show|approve|reject`). Audit-additive on the events table — no schema change. |

The demo agent (`src/agent_habitat/agents/summarizer.py`) is deliberately boring: fetch a URL with httpx, extract readable text with BeautifulSoup, summarise with Sonnet. It exists to exercise every Phase 1 primitive on a single real workflow.

**Tests**: 174 deterministic + 2 marked-live (one Slice 1 Haiku round-trip, one Slice 6 summarizer round-trip). `pytest` + `ruff check` + `ruff format --check` + `mypy --strict` all clean.

## Live calibration

Five real Anthropic API runs across deliberately varied page types, 2026-05-14. Each row is a single live invocation captured from the persisted workflow + JSONL telemetry.

| URL | Bytes | Extracted chars | Sent chars | Parse path | Status | Input tok | Output tok | Cost USD | Wall (s) |
|---|---:|---:|---:|---|---|---:|---:|---:|---:|
| `https://en.wikipedia.org/wiki/Anthropic` | 336K | 45,079 | **12,000 (capped)** | `<main>` | COMPLETED | 3,812 | 224 | $0.014796 | 6.98 |
| `https://docs.python.org/3/library/json.html` | 113K | 26,239 | **12,000 (capped)** | fallback | COMPLETED | 4,420 | 238 | $0.016830 | 7.31 |
| `https://peps.python.org/pep-0020/` | 10K | 1,590 | 1,590 | `<article>` | COMPLETED | 548 | 151 | $0.003909 | 4.36 |
| `https://example.com/` | 528 B | 142 | 142 | fallback | COMPLETED | 104 | 60 | $0.001212 | 2.93 |
| `https://httpbin.org/status/404` | — | — | — | (fetch failed) | FAILED | — | — | $0.000000 | 0.52 |

Model: `claude-sonnet-4-6` for every summarise call. All four successful runs returned with `stop_reason=end_turn` (no truncation by the output cap of 512 tokens — Sonnet self-paced to 60-238 output tokens on its own, holding to the system prompt's "three to five sentences" instruction). The failed run never reached the LLM; cost is exactly $0.

## What the live runs taught us that mocked tests couldn't

This is the [calibration-story pattern](docs/PATTERNS.md#3): live runs surface things mocks can't, and the right move is to document them as findings, not bury them.

**1. The 12K-char prompt cap silently truncates heavy real-world pages.** Wikipedia's Anthropic article (~45K readable chars after parse) and the Python `json` module docs (~26K) both get cut to the first 12,000 characters before the LLM sees them. Slice 6's single example.com run (142 chars) couldn't have exposed this. Honest framing: heavy-page summaries are partial. A future improvement is to either log a `WARN` event when truncation fires (so it's visible in the audit trail) or chunk-and-stitch — the choice is an ADR item.

**2. All three parse strategies fire in production.** Wikipedia took the `<main>` path; PEP 20 took the `<article>` path (the first observed `<article>` hit — Slice 6 only saw fallback); the Python docs landed on the whole-soup fallback, because their multi-section layout uses neither semantic tag. The fallback is load-bearing on the real web — official Python docs don't use `<main>` or `<article>`.

**3. Cost spans 14× across page types and is input-dominated above ~500 tokens.** Output tokens stay bounded between 60 and 238 regardless of input size (the "three to five sentences" instruction holds). Input tokens span 42× (104 → 4,420). So once a page is large enough to dominate the input, doubling page length roughly doubles cost — but the floor is real and small (~$0.0012). The fixed-cost-floor intuition from Slice 6 holds and is now quantified.

**4. Latency is LLM-bound — 92–94% of wall time on completed runs.** Fetch + parse total 0.2–0.5s; everything else is the Sonnet call. Wall time scales roughly at ~1.5s per 1,000 input tokens. Implication for Phase 2: each agent in a five-agent chain adds ~2–7s of LLM wall time depending on payload; pipelining and Haiku-for-grunt-work matter for end-to-end latency budgets, not just dollars.

**5. example.com is consistent run-to-run.** Slice 6 saw 104 in / 54 out / $0.001122; Slice 7 saw 104 in / 60 out / $0.001212. Input tokens deterministic; output ±10%. Sonnet is consistent enough on minimal tasks that the telemetry numbers are trustworthy signal, not noise.

**6. The failure contract holds in the wild.** The httpbin 404 produced exactly the expected four-event trail (`workflow.started`, `step.started`, `step.failed`, `workflow.failed`), a `FAILED` workflow row with `finished_at` stamped, and a `FAILED` fetch step carrying the upstream error message. No stuck-RUNNING workflow, no LLM cost, fast exit (~0.5s).

**7. The budget cap is now calibrated, not arbitrary.** The Slice 6 single data point set the `url_summarizer` daily cap at $2 against a $0.001 floor; that left an unbounded ratio. With real heavy-page cost at $0.0168, $2/day buys ~118 max-cost runs/day — generous for a demo agent's actual usage profile (operator-paced exploration, not batch) without being so loose it provides no safety signal. **Recommendation: keep $2/day.** The cap shape is now defended by observed page-type data, not extrapolated from a trivial page.

## Architecture

Phase 1 today is a single agent threaded through the habitat — no LangGraph orchestrator yet, no parallel agents. One workflow row, three step rows, the eight-event lifecycle trail, one LLM call, one JSONL telemetry line.

```
                   ┌──────────────┐
   URL  ──────────▶│ run_summarizer │
                   └──────┬───────┘
                          │
                   writes │
                          ▼
   ┌─────────────────────────────────────────────────────────┐
   │  agent-habitat                                          │
   │                                                         │
   │  state/        ──▶ workflows / workflow_steps (SQLite)  │
   │  observability/──▶ events (audit trail) + structlog     │
   │  llm.py        ──▶ Anthropic API + JSONL telemetry      │
   │  budget/       ──▶ cost rollup + halt-on-exceed signal  │
   │  checkpoint/   ──▶ human approval surface (CLI)         │
   │                                                         │
   └─────────────────────────────────────────────────────────┘
```

The Mermaid version of this diagram is queued for Slice 8 (optional Phase 1 polish). Phase 2 introduces a LangGraph state-machine orchestrator wiring five agents together with handoffs and a checkpoint before the drafter.

## Setup

Requires Python 3.13+ (per `pyproject.toml`) and an Anthropic API key.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env       # then edit .env and set ANTHROPIC_API_KEY
pytest -m "not live"       # 174 deterministic tests; no API calls, no cost
```

To run the deterministic suite plus the marked-live smokes (each incurs a real API call, ~$0.001 each):

```bash
pytest                      # all tests including @pytest.mark.live
```

## Run the demo agent

```bash
# Summarise a single URL. Persists workflow + steps + events to the
# default SQLite path (data/state/agent_habitat.db) and writes a
# JSONL telemetry line under data/logs/YYYY-MM-DD/.
agent-habitat run-summarizer https://en.wikipedia.org/wiki/Anthropic

# Custom DB path (used for tests / isolated calibration runs).
agent-habitat run-summarizer --db /tmp/run.db https://example.com/

# Inspect pending human-approval checkpoints (none yet from the
# summarizer — exists for Phase 2 agent integration).
agent-habitat checkpoint list
agent-habitat checkpoint show <id>
agent-habitat checkpoint approve <id> --reviewer "Your Name"
agent-habitat checkpoint reject  <id> --reviewer "Your Name" --reason "..."
```

Failed runs exit non-zero and print the failing step + error to stdout. Successful runs print the summary followed by a decision-support footer ("automated summary; the model can omit, mis-emphasise, or misread content — treat as decision support, not as a substitute for reading the source page").

## What Phase 1 is NOT

Honest scope. Phase 1 is real infrastructure, but it is not yet:

- **A multi-agent orchestrator.** There is no LangGraph state machine, no agent-to-agent handoff, no parallel execution. The summarizer is hand-wired through the habitat to prove the primitives work end-to-end.
- **An active budget enforcer.** Cost is *recorded* and the halt-signal query primitive (`is_workflow_halted_by_budget`) exists; actually halting a running workflow on cap exceedance is the Phase 2 orchestrator's job.
- **A fabrication-resistance enforcer.** The cross-agent substring-validation contract lands in Phase 2 (drafter cites only signals upstream agents produced). Phase 1 establishes the audit primitives that contract will run on.
- **A web UI.** Approvals are CLI today. A web surface is in the deferred list (see [CLAUDE.md](CLAUDE.md)), with an explicit trigger condition before it gets built.
- **Backed by Postgres / Redis / Celery.** SQLite is sufficient for Phase 1–2; each deferred dependency has a named trigger condition.

## Project layout

```
agent-habitat/
├── src/agent_habitat/
│   ├── llm.py                  # the only Anthropic SDK call site
│   ├── state/                  # Pydantic models + SQLite persistence
│   ├── observability/          # events, structlog, JSONL reader
│   ├── budget/                 # config, tracker, halt-signal query
│   ├── checkpoint/             # human-approval system
│   ├── agents/summarizer.py    # Phase 1 demo agent
│   └── cli.py                  # `agent-habitat` entry point
├── tests/                      # 174 deterministic + 2 live
├── config/budgets.toml         # operator-tunable daily caps
├── docs/
│   ├── ROADMAP.md              # phases + ADR queue
│   ├── PATTERNS.md             # carry-forward patterns
│   └── adr/                    # numbered architecture decisions
├── CLAUDE.md                   # project context + working agreement
└── STATUS.md                   # session-to-session source of truth
```

## Roadmap

| Phase | Goal | Status |
|---|---|---|
| **Phase 1** | Habitat infrastructure: persistence, observability, cost + budget, checkpoints, single-agent demo | **Complete** |
| **Phase 2** | Five-agent lead-enrichment crew on the habitat: researcher → extractor → scorer → drafter → critic, LangGraph orchestration, checkpoint before drafter | Queued |
| **Phase 3** | Public push, Loom (~4-5 min), README polish, anonymous-friendly contact section | Queued |

Detailed slice plan: [docs/ROADMAP.md](docs/ROADMAP.md). Architecture decision records: [docs/adr/](docs/adr/).

## Contact

_Coming in Phase 3._
