# agent-habitat Roadmap

## Phase 1 — Habitat Infrastructure (target ~3 weeks part-time, 6-8 slices)
Goal: a single-agent workflow runs end-to-end with full observability, persistence, and cost tracking. No multi-agent yet. The habitat infrastructure is the deliverable; the single agent exercises it.

Each slice = ADR if needed + implementation + live API smoke + tests + retro.
- Slice 1: Scaffold + ADR-001 (LangGraph over CrewAI, alternatives rejected) + ADR-002 (persistence schema for workflow state) + initial llm.py wrapper.
- Slice 2: WorkflowState Pydantic model + SQLite persistence layer. Workflow can be saved, loaded, queried. State round-trip tests.
- Slice 3: Cost tracking module. Per-call cost from model + tokens x current rates, written to JSONL telemetry. Per-workflow daily budget cap with halt-on-exceed.
- Slice 4: ObservabilityLayer — event log per agent action, structured logging via structlog, JSONL telemetry consolidated in data/logs/.
- Slice 5: CheckpointSystem — workflows can request human approval before flagged actions. Pause workflow, write pending-approval record, CLI command to approve/reject and resume.
- Slice 6: Demo agent — URL summarizer. httpx fetch, BeautifulSoup parse, Sonnet summarize via llm.py. Boring but real; exercises the full stack.
- Slice 7: Live API smoke across 3-5 URLs. Calibration story documented. Phase 1 README + Loom plan.
- Slice 8 (optional): Phase 1 polish — Mermaid architecture diagram, hiring-manager README pass, contact section.

End state: working habitat with one agent; halt/resume works; cost tracking works; human checkpoints work; telemetry shows what happened. Shippable as "infrastructure for multi-agent workflows."

## Phase 2 — 5-Agent Lead Enrichment Crew (target ~3-4 weeks part-time, 6-8 slices)
Goal: 5 agents coordinated by a LangGraph state machine doing real lead enrichment end-to-end. Outreach drafts gated by human checkpoint.
- Slice 1: ADR for crew architecture — which agents, what handoffs, parallel vs sequential, shared state, error/retry strategy.
- Slice 2: Researcher agent (Haiku) — company name in, web search, RawSignals structured output. Web search tool choice via ADR-003.
- Slice 3: Extractor agent (Sonnet) — structured CompanyProfile from RawSignals (size, industry, tech stack, recent news, decision makers). ExtractionGap pattern: surface missing fields, never fabricate.
- Slice 4: Scorer agent (Sonnet) — applies ICP rubric (TOML, operator-tunable) to score CompanyProfile. Produces ScoredCompany with reasoning.
- Slice 5: Drafter agent (Opus 4.7) — personalized outreach from profile + score. No-fabrication contract: cites ONLY signals earlier agents produced; post-extraction substring check against upstream outputs.
- Slice 6: Orchestrator — LangGraph state machine wiring agents together. Handoffs, error states, halt/resume. Checkpoint before drafter (human reviews score before paying Opus tokens).
- Slice 7: Critic agent (Haiku, optional) — cheap second-pass review of drafter output for fabrication, tone, length, citation correctness. A validator, not a peer.
- Slice 8: Live API smoke across 3-5 real test companies. Calibration story documented. README polish + Loom + public push prep.

End state: working 5-agent crew producing audit-grade lead enrichment with persistent state, full telemetry, cost tracking, and a human checkpoint before any draft is shown as "ready to send."

## Phase 3 — Public Push + Positioning (1-2 weeks)
README polish, Loom recording (~4-5 min), public GitHub push. Real metrics, calibration story, Mermaid architecture diagram, anonymous-friendly contact section.

## Optional Phase 4+
Second workload (research synthesizer, code review crew, etc.) demonstrating habitat re-use. Decided based on contract conversations at that point.

## ADRs queued
- ADR-001: LangGraph over CrewAI. Axis: state-machine debuggability vs natural-language handoff opacity. LangGraph is the production choice.
- ADR-002: Persistence schema. SQLite tables — workflows (id, status, started_at, finished_at, cost_total); workflow_steps (workflow_id, agent_name, started_at, status, input_ref, output_ref, cost); events (workflow_id, timestamp, level, message, structured_data). output_ref points at a JSONL line in data/logs/ for large outputs.
- ADR-003: Web search tool for researcher. Options: Anthropic web_search, Tavily, Brave, custom. Decide before Phase 2 Slice 2.
- ADR-004: ICP rubric format. TOML, operator-tunable. Dimensions likely: fit, intent signal, decision-maker accessibility, recency of relevant news.
- ADR-005: Cross-agent fabrication-resistance contract. How the drafter proves citations are grounded in upstream output — substring check across all upstream agent outputs, reference IDs, or similar.
