# Architecture Decision Records

## Index

| ADR | Title | Status |
|-----|-------|--------|
| ADR-001 | [LangGraph over CrewAI](ADR-001-langgraph-over-crewai.md) — state-machine debuggability vs natural-language handoff opacity | Accepted (2026-05-13) |
| ADR-002 | [Persistence schema](ADR-002-persistence-schema.md) — two parallel schemas in one SQLite file: LangGraph checkpoints + agent-habitat audit tables; output_ref to JSONL | Accepted (2026-05-13) |
| ADR-003 | [Web search tool for Researcher agent](ADR-003-web-search-tool.md) — Anthropic `web_search` server-side tool routed through `llm.py`; per-search fee aggregated into `LLMResult.cost_usd`; **grounding corpus is `citations[].cited_text` (Addendum 2026-05-14, on live evidence: raw `web_search_tool_result.encrypted_content` is opaque; `cited_text` spans are the plain-readable verbatim source prose the substring check grounds against)** | Accepted (2026-05-14), Addendum (2026-05-14) — `cited_text` grounding + cost recalibration to ~$0.067/run |
| ADR-004 | [ICP rubric format and missing-data handling](ADR-004-icp-rubric-format.md) — TOML mirrors `budgets.toml` idiom; gaps excluded and score renormalised over present dimensions with a parallel `coverage` number; ADR-006 §1's floor gates on `score`; grounding chain extends through the Scorer via a `grounded_quote` substring check | Accepted (2026-05-14) |
| ADR-005 | Cross-agent fabrication-resistance contract — how Drafter proves citations are grounded in upstream output | Superseded by ADR-006 (folded in; the contract is set in ADR-006 §3 — critic decomposes prose into claims, pure-Python substring check verifies, one bounded retry on fabrication then halt) |
| ADR-006 | [Phase 2 crew architecture, `run_step()` extraction, fabrication-resistance contract](ADR-006-crew-architecture.md) — strictly sequential 5-agent pipeline with one human checkpoint before the drafter, single shared TypedDict state, `run_step()` context manager owned by `orchestration/`, critic + pure-Python substring check as the fabrication contract | Accepted (2026-05-14) |

## Template

See `ADR-template.md` for the standard structure: Title, Status, Context, Decision, Alternatives Considered, Consequences.

## Conventions

- ADRs are numbered sequentially and never deleted; superseded ADRs are marked "Superseded by ADR-NNN."
- Every non-trivial decision gets an ADR before implementation. The Alternatives Considered section is mandatory.
- ADR-001 and ADR-002 are written in Slice 1 (Opus high, fresh session).
