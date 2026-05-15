# ADR-003: Web search tool for the Researcher agent

**Status:** Accepted (2026-05-14) — **Addendum 2026-05-14** (cited_text grounding + cost recalibration; see end of file). The Decision is unchanged: Anthropic's built-in `web_search` server-side tool, single SDK call through `llm.py`. The Addendum corrects the *mechanism* (which response field the Researcher reads) and the *cost estimate*, on live evidence from Slice 2.

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

---

## Addendum (2026-05-14): `cited_text` grounding + cost recalibration

This addendum corrects two specifics in the original ADR-003 against live evidence from Phase 2 Slice 2's first real `web_search` call through the habitat (Anthropic's own public footprint; `claude-haiku-4-5-20251001`; 3 searches at `max_uses=3`; full round-trip persisted). **The Decision section above is unchanged** — Anthropic's built-in `web_search` server-side tool remains the chosen tool, for the same reasons. What changes is the *mechanism* (which field of the response the Researcher reads to build `RawSignals`) and the per-run *cost estimate*. Both came up at first contact with the live API; documenting them honestly is the audit-grade story.

### 1. The corrected mechanism — `citations[].cited_text`, not raw `search_result` block content

ADR-003's Decision section stated:

> "Search-result content blocks are captured verbatim from the response into `RawSignals.signals[].text`."

That premise **cannot be honoured literally** against the actual API response shape. Live verification on 2026-05-14:

- The response carries `web_search_tool_result` blocks whose `.content[i]` items expose `url`, `title`, `page_age`, and `encrypted_content` — the first three are plain-readable, but `encrypted_content` is **opaque**. Anthropic designs it for multi-turn round-trip (the model can re-consume it in later turns); it is not intended for client-side persistence as readable text.
- The plain-readable, substantively-equivalent grounding corpus is `TextBlock.citations[i].cited_text` from `CitationsWebSearchResultLocation` blocks — verbatim source spans the model surfaces, each tagged with its source URL and page title. These are *the same source content the search tool fetched*, surfaced through the citations channel rather than the encrypted block.
- Slice 2's live smoke confirmed `cited_text` is genuine verbatim source prose (Bloomberg + PYMNTS spans like "May 12, 2026 at 9:08 PM UTC · Save · Anthropic PBC is in early talks with investors to raise at least $30 billion in fresh financing…"), **not** the model's own narrative paraphrase.

**As-built code path** (the addendum documents, does not change):

- `llm.py::_extract_web_search_citations` iterates `response.content`, picks `TextBlock`s, walks `block.citations`, and emits a typed `Citation(cited_text, source_url, source_title)` for every `CitationsWebSearchResultLocation` it finds.
- `agents/researcher.py` builds each `Signal` from a `Citation`: `Signal(text=c.cited_text, source_url=c.source_url, source_title=c.source_title, retrieved_at=...)`. `Signal.text` is therefore a verbatim source span by construction.

The substantive property ADR-003 needed (the corpus the substring check grounds against IS the text the model actually saw, not a paraphrase) **holds against `cited_text` for the same structural reason it would have held against raw block content**: both are tool-surfaced source content, not model output. The dual-source-of-truth hazard the alternatives section rejected Tavily/Brave for is still avoided — `cited_text` lives inside the same `messages.create` response, returned by the same single SDK call, and is persisted by one writer.

### 2. The three grounding questions

ADR-006 §3 (fabrication-resistance) and Slices 3 and 7 depend on the answers to three concrete questions about `cited_text` as a grounding corpus. Answered against the as-built code and the live smoke evidence:

**Q1. Is `cited_text` reliably present on every result the Researcher keeps?**

Yes, by SDK contract — `cited_text` is a required field on `CitationsWebSearchResultLocation`. The as-built `_extract_web_search_citations` reads `c.cited_text` without a None-check, and `Citation.cited_text: str` is a required Pydantic field; a missing/None value would surface as a Pydantic validation error at construction time, not as a silent empty `Signal`. The relevant absence question is upstream: a model response may carry **zero citations** (the model searched but did not ground any claim against a result, or did not search at all). The as-built path handles this correctly — `_extract_web_search_citations` returns `[]`, the Researcher builds `RawSignals(company_name=..., signals=[])`, the workflow finalises **COMPLETED** with `signal_count == 0` per ADR-006 §1's empty-outcome contract. Empty is a valid result, not a failure.

**Q2. Is a `cited_text` span complete enough to ground a downstream claim against?**

It depends on what the model chose to quote, and that is the right semantics. Live smoke spans were full sentences with date stamps and dollar figures — well above the floor needed for the substring check. In principle a span could be shorter (a phrase, a clause); Anthropic's documentation describes `cited_text` as "the verbatim quoted portion of the source," not "the full search-result snippet." The implications for ADR-006 §3's substring check:

- A shorter span is a **tighter** grounding corpus, not a broken one. The check (`claim.text` normalised → substring of concatenated normalised `Signal.text`) is well-formed for any non-empty corpus.
- A short fragment narrows what a drafter can legitimately cite from that signal — exactly the right pressure for fabrication-resistance. A drafter that wants to make a richer claim must either find a longer grounded span elsewhere in `RawSignals` or surface the claim through `CompanyProfile` (Slice 3) which carries its own structured spans.
- Forward dependency for Slice 3 (Extractor) and Slice 7 (Critic): both should treat short-span signals as legitimate-but-narrow grounding, not as low-quality data to be ignored. The Critic's red-team smoke (ADR-006 §3 forward dependency) should include a fabrication-attempt that paraphrases beyond the boundaries of a short cited span — verifying the substring check rejects it.

**Q3. Does ADR-006 §3's substring-check mechanism still work AS WRITTEN against `cited_text` spans, or does §3 need its own amendment?**

**§3 still works as written; no ADR-006 amendment is required.** §3's language is: "every claim the drafter makes about the company MUST be a verbatim substring of one of the upstream agents' textual outputs," and names `RawSignals` as "raw search-result snippets, scraped page excerpts." `cited_text` spans satisfy that description — they are raw source content tied to a source URL, retrieved by the search tool, persisted verbatim. §3's mechanism (whitespace+case normalisation, pure-Python substring against the concatenated upstream prose) operates on `Signal.text` and does not care which response field that text came from. The contract is grounded in "verbatim source spans from upstream agents"; `cited_text` is verbatim source spans. **The only thing that changes is which response field the Researcher reads** — which is an ADR-003 concern, not an ADR-006 concern.

One semantics implication of `cited_text` grounding that is worth ratifying explicitly here (rather than discovering it in Slice 7): **a model claim made without a citation produces no Signal**. The Researcher cannot fabricate a Signal from the model's uncited narrative; the only path from "model response" to `Signal.text` is via a citation. This is exactly the fabrication-resistance discipline the §3 contract is designed to enforce, applied one level upstream. Ratified.

### 3. Cost recalibration — ~$0.067/run, not $0.035–$0.045

ADR-003's Decision section estimated:

> "$0.03 in search fees + ~3K input tokens of returned snippets (~$0.003) + ~1K input tokens of prompt/system (~$0.001) + ~500 output tokens (~$0.0025) ≈ **$0.035–$0.045 per researcher run**."

Live smoke on 2026-05-14 measured **$0.066871** for the single run. Real breakdown:

| Component | ADR-003 estimate | Live smoke |
|---|---|---|
| Search fees (3 × $0.01) | $0.030 | $0.030 |
| Input tokens (Haiku $1/MTok) | ~3K → ~$0.003 | **34,971 → $0.034971** |
| Output tokens (Haiku $5/MTok) | ~500 → $0.0025 | 380 → $0.0019 |
| **Total** | **~$0.035–$0.045** | **$0.066871** |

**Root cause:** ADR-003 modelled input tokens as a flat "~3K of snippet content + ~1K of prompt." The live API does not work that way. Anthropic's `web_search` injects substantial result content into the model's context as it reasons across multiple searches — the inline result text, the model's intermediate reasoning, and the cited spans all bill as input tokens on each turn. Input tokens ran **~10× the estimate** for a 3-search run; the fee component is ~45% of total but **input tokens are the bigger driver** (~52%), not the fees as the original ADR implied by ordering them first.

**Downstream assumptions this touches** — explicitly flagged for re-check (not changed in this session):

1. **`config/budgets.toml`'s `lead_enrichment` daily cap = $10.00.** At the corrected per-Researcher-run cost of ~$0.07 (and a projected 5-agent end-to-end cost of ~$0.10–$0.15 per company, per STATUS.md), $10/day still affords ~66–100 full-pipeline runs per day — likely still adequate for an operator-paced freelance workload, but the cap was set against the original $0.035–$0.045 estimate and **should be explicitly re-validated** when Slice 8 calibration produces real end-to-end numbers across all five agents. **No edit this session.**
2. **ADR-006 §1 checkpoint cost-rationale.** §1's text reads: "the rest of the upstream chain at Haiku+Sonnet costs roughly $0.01–$0.02 combined." With the Researcher alone now at ~$0.07, the upstream chain (Researcher + Extractor + Scorer + Critic) is more honestly ~$0.07–$0.10 combined. The checkpoint break-even *logic* still holds — rejecting one in N low-scoring leads still saves the $0.025–$0.060 Opus draft cost — but the §1 text understates upstream cost by ~5×. **Forward dependency: re-check ADR-006 §1's checkpoint-rationale paragraph when Slice 8 calibration data lands**; either update the upstream-chain figure in §1 with the calibrated numbers, or note it as a known-stale figure with a pointer to the calibrated STATUS.md / Slice 8 README. **No edit this session.**
3. **`_WEB_SEARCH_FEE_USD = 0.01` in `llm.py`.** Already flagged "NEEDS VERIFICATION against current Anthropic pricing" with a 2026-05-13 stamp; matches the recalibrated $0.030 search-fee component. No change.

**Forward dependency for Slice 8 (live calibration)**: size the 5-agent pipeline budget cap and the README calibration table against the corrected per-Researcher-run cost (~$0.07), not the original ADR-003 figure.

### 4. What still stands

The core ADR-003 decision is **unchanged**, and every reason given for it survives the corrected mechanism:

| Original reason ADR-003 chose Anthropic `web_search` | Status under `cited_text` grounding |
|---|---|
| One SDK call returns model output AND source content. | ✅ Unchanged — `cited_text` lives in the same `messages.create` response. |
| Cost flows through `llm.py` unchanged; one writer to `workflow_steps.cost_usd`. | ✅ Unchanged — `compute_cost_usd` + the $0.01 server-tool fee land on `LLMResult.cost_usd` exactly as designed. |
| Persisted text IS the corpus the substring check grounds against — no second source of truth. | ✅ Unchanged — `Signal.text` is built from `Citation.cited_text`; the substring check still grounds against tool-surfaced source content, not model paraphrase. The structural property holds for the same reason. |
| No new API key, client, rate limit, billing surface. | ✅ Unchanged. |
| Vendor lock-in to Anthropic accepted as bounded blast radius (Researcher encapsulated, handoff is `RawSignals`). | ✅ Unchanged — the swap cost is the same regardless of which response field the Researcher reads. |

The alternatives (Tavily, Brave, custom httpx+BS4) are rejected for the same structural reasons. The "what would invalidate this decision" list also still applies.

**Net:** the addendum corrects *how* ADR-003 is implemented (read `citations[].cited_text`, not `web_search_tool_result.content[i]`), records the recalibrated cost honestly, and confirms the original Decision and its rationale stand.
