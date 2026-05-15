# agent-habitat — Status

_Tight current-state summary. The full build log (every slice retro, every decision context, every internal-debate note from kickoff through Phase 3) lives in [docs/PROJECT_HISTORY.md](docs/PROJECT_HISTORY.md). The forward plan lives in [docs/ROADMAP.md](docs/ROADMAP.md). The design rationale for every non-trivial decision lives in [docs/adr/](docs/adr/)._

---

## Current phase

**Phase 3 — Public push.** The framework is feature-complete, calibrated, and hardened. This phase positions it for public release: marketing-quality README, Mermaid architecture diagram, calibration re-baseline at current Opus 4.7 rates, Loom recording, and the deliberate visibility flip on GitHub.

## What is done

- **Phase 1 (Habitat infrastructure)** — complete. Persistence ([ADR-002](docs/adr/ADR-002-persistence-schema.md)), observability, cost tracking, daily budget caps, human-in-the-loop checkpoints. Demo single agent (URL summarizer) exercises every primitive end-to-end. See [PROJECT_HISTORY.md § Phase 1](docs/PROJECT_HISTORY.md).
- **Phase 2 (5-agent lead-enrichment crew)** — complete. Researcher → Extractor → Scorer → [human checkpoint] → Drafter → Critic, wired by a LangGraph `StateGraph` per [ADR-006](docs/adr/ADR-006-crew-architecture.md). Calibrated against four real companies in Slice 8; the bounded fabrication-retry edge fired on 2 of 3 drafter-invoked runs, recovering once and halting once (Plaid). See [PROJECT_HISTORY.md § Phase 2 Slice 8](docs/PROJECT_HISTORY.md).
- **Phase 3 prep** — complete (commits `943221a`, `3782f8d`, `03959cf`). ADR-007 retry/backoff policy ([ADR-007](docs/adr/ADR-007-llm-retry-policy.md)); README Production Considerations section; defensive hardening; rate-table verification of current Opus 4.7 pricing (`$5/$25` per MTok, repriced from `$15/$75`).
- **Phase 3 public push** — in progress (this session). Six items: Mermaid diagram, standalone-drafter footer fix, Loom script, marketing-polished README, calibration re-baseline at current rates, visibility flip.

## Phase 3 public-push session deliverables (2026-05-15)

1. **Item 1 — Calibration re-baseline.** Two real crew invocations at current Opus 4.7 rates. **Anthropic**: 76.00 score, retry fired and succeeded, $0.135 total. **Plaid**: 100.00 score (Tier A), retry fired and failed, workflow halted as designed, $0.111 total. Plaid replayed Slice 8's halt-not-ship outcome at new pricing — the bounded-retry edge is stable across companies and pricing regimes. ADR-006 §1 updated with a `Current calibration` subsection alongside the preserved Slice 8 historical addendum.
2. **Item 2 — Mermaid architecture diagram.** Replaces the Phase 1 ASCII in README; shows all five agents, scorer-gating branches, human-checkpoint branches, bounded-retry edge, and both terminal-halt paths.
3. **Item 3 — Standalone-drafter footer fix.** `cli.py::DRAFTER_DECISION_FOOTER` no longer references Slice 7 as future-tense work; now states plainly that the critic is NOT run in `run-drafter` (only in `run-crew`) and points operators to the full crew for fabrication-resistance enforcement.
4. **Item 4 — Public README.** Marketing-polished, differentiator-led structure with every claim traced to an ADR, slice, or commit. ~2,200 words; 5-minute read.
5. **Item 5 — Loom recording script.** `docs/loom-script.md` — 4–7 min walkthrough script with talking points, shot list, production checklist, and fallback talking points for variable-outcome branches.
6. **Item 6 — Visibility flip.** Pre-flip audit (11 checks) executed; LICENSE added; `.gitignore` patched to cover date-stamped subdirectories under `data/logs/`; STATUS.md split into this tight file plus `docs/PROJECT_HISTORY.md`. The `gh repo edit --visibility public` command itself is operator-run.

## What's current

- Working tree: documentation + ADR-006 addendum + cli.py footer fix + LICENSE + pyproject.toml license field + .gitignore fix + this STATUS.md split.
- Tests: **538 deterministic** + a handful of `@pytest.mark.live` smokes. `ruff check`, `ruff format --check`, `mypy --strict` all clean.
- ADRs: seven accepted, indexed in [docs/adr/README.md](docs/adr/README.md).
- Source: 5,969 lines across `agents/` + `orchestration/`, plus ~3,100 lines across `llm.py` + `state/` + `observability/` + `checkpoint/` + `budget/` + `scoring/`.

## What's next

- **Public-release entry:** appended below once the visibility flip lands.
- **Phase 4 (next workload)** — deferred. The habitat is generic enough to run a second workload (research synthesizer, code review crew, compliance triage). Trigger condition: a real contract conversation surfaces a specific workload worth investing in. The lead-enrichment crew remains the demonstration workload.
- **Deferred items with named triggers** — see [README § Roadmap status](README.md#roadmap-status) and [CLAUDE.md](CLAUDE.md) for the full deferred-with-trigger list (parallel agents, PII redaction, checkpoint auth, Postgres/Redis/Celery, web UI, LangSmith, vector store).

## Known Follow-Ups

**1. FABRICATION_DETECTED event gap (crew path).** ADR-006 §3 names a `FABRICATION_DETECTED` event row in the audit-event taxonomy. The standalone `run_critic` Layer B wrapper emits it; the orchestrator's `_critic_adapter` in `crew_graph.py` does not emit it when the crew halts on `terminate_reason="critic_failure"`. The failure is recorded (workflow status FAILED, terminate_reason set, failure stats in structured_data), but the dedicated event row is missing on the crew path. Small fix — one event emission in `_critic_adapter`. Queued for post-public iteration.

**2. Gitignore glob near-miss (resolved).** Phase 3's pre-flip audit found that `data/logs/*.jsonl` did not match files in date-stamped subdirectories like `data/logs/2026-05-15/*.jsonl`. A `git add .` would have committed verbatim LLM telemetry to the public repo. Fix landed in this session's pre-flip audit: `.gitignore` extended with `data/logs/**/*.jsonl` and `data/logs/[0-9]*/`. Documented here as an example of why pre-public audit discipline matters.

## Public release

**Repository flipped from private to public on 2026-05-15** at commit [`e5e4054`](https://github.com/ktech7moon/agent-habitat/commit/e5e4054) (Phase 3 pre-flip audit). Public URL: <https://github.com/ktech7moon/agent-habitat>. The deliberate visibility flip closes Phase 3.

What's next (outside the repo): record the Loom walkthrough within 7 days using [`docs/loom-script.md`](docs/loom-script.md) and add the link to the README in a follow-up commit. Substantive iteration after that follows real feedback — Phase 4 (second workload) is trigger-gated, not scheduled.
