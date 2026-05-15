# ADR-003: Web search tool for the Researcher agent

**Status:** Accepted (2026-05-14)

## Context

Phase 2 Slice 2 is the Researcher agent (ADR-006 §1 handoff contract): given a company name, produce a `RawSignals` Pydantic v2 output — a list of signal records, each carrying raw text (search-result snippets or scraped excerpts) plus a source URL and a retrieved-at timestamp. `RawSignals` is the upstream half of the fabrication-resistance contract (ADR-006 §3): the drafter may cite only text that already lives in `RawSignals` (or in the extractor's profile, which itself quotes from `RawSignals`). The critic's pure-Python substring check is run against the concatenated text of those upstream outputs.

That contract makes the web search tool choice a load-bearing one. Two properties are non-negotiable, on top of the CLAUDE.md non-obvious constraints:

1. **The text the researcher's LLM consumed must be the same text we persist into `RawSignals`.** If the model summarises results we never captured, the substring check is checking against the wrong corpus and the fabrication contract is a hope, not a validated contract.
2. **The call has to fit cleanly into the audit chain** ADR-002 and ADR-006 specify: every LLM call routes through `llm.py` (CLAUDE.md rule 9); per-call cost lands on `workflow_steps.cost_usd`; one JSONL telemetry line per call; the `output_ref` resolves.

The choice is not pre-made. ADR-001 picked LangGraph; ADR-002 fixed the persistence schema; ADR-006 fixed the crew topology. This ADR fills in the one Researcher-shaped hole that ADR-006 explicitly deferred: the external-call shape.

### Ground truth verified 2026-05-14

- **Anthropic `web_search`** (server-side tool, enabled via `tools=[{"type": "web_search_*"}]` on `messages.create`): $10 per 1,000 searches, billed in addition to standard token costs. Search-result text is returned in the same response as the model's reply, as `search_result` content blocks (source URL + title + content text blocks). Citations point back to those blocks. Routes through the existing `anthropic` SDK path `llm.py` already uses — no new client.
- **Anthropic `web_fetch`** (companion server-side tool): token cost only, no per-fetch fee. Useful follow-on, not a search substitute.
- **Tavily Search API**: $0.008/search PAYG (1,000/month free) or ~$0.005–$0.0067/search on monthly plans. Dedicated LLM-tuned search backend; returns clean JSON of scored snippets. Sits BESIDE `llm.py` as a second client.
- **Brave Search API**: $5/1,000 searches ($0.005/search) for new users via $5/month free credit; legacy free tier deprecated Feb 2026. General-purpose search results (title + URL + description). Sits BESIDE `llm.py` as a second client.
- **Custom httpx + BeautifulSoup**: the project already uses these in the summarizer for fetch-and-parse. Has no search backend of its own — given a company name, there is no URL set to fetch.

## Decision

**Use Anthropic's built-in `web_search` server-side tool, enabled on the Researcher's single Haiku call through `llm.py`.**

The Researcher's node body becomes one `llm.complete()` call with `tools=[{"type": "web_search_*", "max_uses": N}]` and a system prompt instructing the model to search, summarise per-signal relevance, and emit a structured signals list. Search-result content blocks are captured verbatim from the response into `RawSignals.signals[].text` (with the block's source URL into `.source_url`); the model's natural-language summary lives in the response's text block and is NOT what the substring check grounds against — the raw block text is.

**Why in one sentence:** the search results are returned in the same SDK call the model already makes, in `search_result` content blocks we can persist directly — which means the audit chain stays one writer, the fabrication-resistance substring check grounds against exactly the text the model saw, and CLAUDE.md rule 9 is honoured without inventing a "what counts as an LLM call" carve-out.

**Architectural placement relative to `llm.py`:**

- `llm.complete()` gains an optional `tools=` passthrough parameter (Slice 2 scope, not this ADR's job to spec — surface to be designed in Slice 2 within the existing `LLMResult` contract). The Anthropic API call shape is `messages.create(model=..., tools=[...], messages=[...])`; the tool config is forwarded unchanged.
- A response with server-side tool use returns `server_tool_use` blocks in the usage metadata. `llm.py` extends `compute_cost_usd` to add `num_web_searches * $0.01` onto `cost_usd` so the per-call cost number remains the single source of truth for budget primitives (Slice 3) and step-row cost (Slice 2 persistence).
- The JSONL telemetry record gains additive keys (`web_searches: int`, `web_search_fee_usd: float`); existing keys are untouched. `output_ref` continues to resolve via `path:line` per ADR-002.
- The Researcher's `step.completed` event projection (per ADR-006 §1.3 handoff contract) extends from `{signal_count, source_count}` to `{signal_count, source_count, web_searches}`. No schema change — `structured_data` is JSON-typed in the events table.

**Cost attribution to a `workflow_step`:** the researcher's step row is opened via `run_step()` (ADR-006 §2). The single `llm.complete()` call returns one `LLMResult` whose `cost_usd` already aggregates Haiku tokens + the $0.01-per-search fee for any searches the model issued. `step.record_cost(result.cost_usd)` lands that number on `workflow_steps.cost_usd`; `recompute_cost_total` rolls it up onto `workflows.cost_total_usd` exactly as the summarizer demonstrated end-to-end in Slice 6. No parallel cost source. No second writer.

**Realistic per-Researcher-run cost estimate.** A typical run on Haiku, ~3 searches: $0.03 in search fees + ~3K input tokens of returned snippets (~$0.003) + ~1K input tokens of prompt/system (~$0.001) + ~500 output tokens (~$0.0025) ≈ **$0.035–$0.045 per researcher run**. Bounding `max_uses` in the tool config is the operator's per-run budget knob; ADR-006's overall workflow-level budget cap (`is_workflow_halted_by_budget`) is the safety net.

## Alternatives Considered

| Option | Best case for it | Why rejected for this project |
|---|---|---|
| **Anthropic `web_search` (chosen)** | One SDK call returns model output AND the source blocks the model used. Cost flows through `llm.py` unchanged. Persisted raw blocks ARE the corpus the fabrication-resistance substring check grounds against — no second source of truth. No new API key, client, rate limit, or failure mode. | — |
| **Tavily Search API** | LLM-tuned snippets; pricing is competitive ($0.008/search PAYG, 1,000/mo free covers all of Phase 2 calibration); explicit relevance ranking. Genuinely the most "designed for this job" option of the four. | Sits beside `llm.py` as a second client. Cost lives outside the existing telemetry/budget surface and has to be attributed to the step row via a parallel writer — exactly the second-writer trigger STATUS.md flagged as the moment to refactor the JSONL writer. Worse: the search results returned by Tavily's API have to be packed into a subsequent LLM prompt to be summarised, then ALSO persisted into `RawSignals` — two copies that can drift, breaking the fabrication-resistance grounding invariant unless we add a separate alignment check. The LLM-tuned ranking is a real advantage; it does not outweigh splitting the audit chain. Revisit if Phase 2 calibration shows Anthropic `web_search` result quality is too thin for the lead-enrichment workload. |
| **Brave Search API** | Cheapest per search ($0.005); $5 monthly credit covers all of Phase 2's expected volume; well-known general-purpose index (which Anthropic itself reportedly uses behind `web_search`). | Same "sits beside `llm.py`" structural objection as Tavily, AND results are raw search-engine descriptions (title + URL + ~150-char snippet) rather than LLM-ready spans. The researcher would need a second LLM call (or a `web_fetch`-equivalent step) to convert URLs into the prose `RawSignals.signals[].text` actually wants. Two clients + extra round-trip + same dual-source-of-truth fabrication-resistance hazard. Saves money the budget cap is not pressuring. |
| **Custom httpx + BeautifulSoup** | The project already depends on both; no new dep; maximum control over result shape; zero per-search fee. | Has no search backend. Given a company name there is no candidate URL set. To work, this option needs ITSELF to be paired with one of the three above (or a SerpAPI / Bing / Google CSE backend, none of which are otherwise justified). It is not a fourth option — it is a fetch-and-parse layer that all three of the above can use AFTER the search step. ADR-006 already permits a follow-up `web_fetch` call (Anthropic's token-only companion server-side tool) if the researcher needs to read a full page rather than just a snippet; that path stays open without making custom-fetch a search-tool decision. |

## Consequences

**What becomes easier:**

- **Audit chain stays single-writer.** The researcher's cost, telemetry, output_ref, and projection all flow through the same `run_step()` + `llm.complete()` path the summarizer demonstrated. Slice 2's researcher implementation is a near-mirror of Slice 6's summarize step plus the `tools=` passthrough.
- **Fabrication-resistance grounding is structurally clean.** `RawSignals.signals[].text` IS the search_result block content — the exact prose the model read. The drafter's substring check (ADR-006 §3) does not need any reconciliation step; the contract holds by construction.
- **No new operational surface.** One API key (the one already in `.env`), one client (`llm.py`'s), one rate limit, one failure mode, one billing surface. CLAUDE.md's "default DOWN" framing applies here too — start with what's already in the stack; add a dedicated search vendor only if calibration forces it.

**What becomes harder (accepted costs):**

- **Vendor lock-in to Anthropic's search backend.** If Anthropic deprecates `web_search`, changes its result shape, or its result quality regresses against the lead-enrichment workload, the Researcher must switch. The blast radius is bounded — the Researcher agent is encapsulated, and its handoff contract to the rest of the crew is `RawSignals`, not the search tool. Swap cost is one agent file plus the `tools=` passthrough cleanup in `llm.py`.
- **Per-search fee is a real budget input.** $0.03–$0.04 per researcher run is meaningful at scale; the Phase 2 budget cap calibration in Slice 8 must include the search fee. `llm.py`'s rate table needs the $0.01-per-search constant and a date stamp alongside the existing token rates (the same "verify before trusting" Open Question in STATUS.md applies).
- **Quality is unknown until Slice 8 calibration.** Anthropic does not publish a result-quality benchmark, and this project does not have one yet. Phase 2 Slice 8 (3-5 real test companies) is where the calibration story for Researcher result quality gets told. If quality is thin, the calibration story documents it honestly and a follow-up ADR re-opens this decision with evidence.

**What is deferred:**

- **`web_fetch` follow-on calls.** Anthropic's free server-side fetch tool is the natural Phase 2 follow-up if a search snippet is too sparse to ground a claim. Out of scope here; Slice 2 may add it if `max_uses` budgets prove tight, otherwise queued for a follow-up ADR.
- **`llm.py` `tools=` passthrough surface design.** This ADR commits to the placement (`tools=` parameter on `complete()`; cost aggregated into `LLMResult.cost_usd`; additive JSONL keys). Exact parameter typing, validation, and the cost-rate stamp update land in Slice 2 alongside the researcher.
- **Per-tool cost rate verification.** The $0.01-per-search figure stamps 2026-05-14; carry the same "verify against the public pricing page" discipline already noted for the token rate table in STATUS.md's Open Questions.

**Forward dependency handed to Phase 2 Slice 2 (Researcher):**

- Extend `llm.complete()` with a `tools=` passthrough; extend `compute_cost_usd` to add server-tool fees; add `web_searches` / `web_search_fee_usd` to the JSONL record.
- Build the `RawSignals` Pydantic v2 model: each `Signal` carries `text: str`, `source_url: str`, `retrieved_at: datetime`; the researcher constructs these directly from `search_result` content blocks in the response, NOT from the model's narrative text.
- Wire `researcher_node(state)` through `run_step()` per ADR-006 §2. Mirror `{signal_count, source_count, web_searches}` onto `step.completed`.
- Land `orchestration/run_step.py` and retrofit the summarizer through it in the same diff (ADR-006 §2 forward dependency).

**What would invalidate this decision:**

- Anthropic deprecates or substantially redesigns the `web_search` server-side tool. Re-evaluate Tavily as the most natural fall-back.
- Phase 2 Slice 8 calibration shows result quality is materially worse than Tavily on the lead-enrichment workload (measured by signal-grounding rate per company, not by gut). Re-open with the calibration table as evidence.
- A regulated-industry buyer requires that the search backend be a named, separately-contracted vendor (procurement-shape constraint, not technical). Switch to Tavily/Brave under their own SLA in that engagement; the encapsulation of the Researcher agent makes this a per-deployment swap, not a Phase 2 redesign.
