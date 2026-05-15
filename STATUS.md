# agent-habitat STATUS

## Current Phase
Phase 2 — 5-Agent Lead Enrichment Crew (full plan: docs/ROADMAP.md)

## Current Slice
**Pre-orchestrator refactor — DONE 2026-05-14.** Extracted pure Layer A
node functions from the four built agents (researcher, extractor, scorer,
summarizer) so Phase 2 Slice 6 (LangGraph orchestrator) can compose them
cleanly. Behavior-preserving: four existing agent test suites pass UNCHANGED
(0 test edits); 13 new Layer A tests added; ruff/format/mypy strict all
clean; one live smoke (summarizer round-trip + Wikipedia truncation) green
through the refactored standalone path.

Each Phase 2 agent now exposes `<name>_node(...)` returning `<Name>NodeOutput`
(typed agent output + cost_usd + output_ref + structured_data). The standalone
`run_<name>(...)` wrapper preserves the workflow-lifecycle envelope
(`insert_workflow` → `with run_step(...) as step: out = <name>_node(...);
step.record_*(out.*)` → finalise COMPLETED/FAILED), so the CLI commands
(`run-researcher`, `run-extractor`, `run-scorer`, `run-summarizer`) keep
working identically. `cli.py` is untouched. The summarizer (a 3-step
intra-agent workflow predating the crew) gets the same treatment via a new
sibling `summarize_text(...)` returning `SummarizerNodeOutput`; its two
already-pure helpers (`fetch_url`, `extract_readable_text`) stay as-is.

Slice 6 (orchestrator) now inherits four Layer A pure node functions it
can wrap as LangGraph nodes — the wrap is `with run_step(conn, ...) as
step: out = <name>_node(state...); step.record_cost(out.cost_usd); ...;
return {"<key>": out.<typed_output>}`. `run_step.py` is unchanged.

## Phase 2 Slice 4 (Scorer agent) — DONE 2026-05-14
Three new
files (`src/agent_habitat/agents/scorer.py`,
`src/agent_habitat/scoring/rubric.py`, `tests/test_scorer.py`),
extensions to `agents/models.py` (`DimensionScore` + `ScoredCompany`)
and `cli.py` (`run-scorer`); ADR-004 implemented as written.
Renormalise-with-coverage scoring, grounding-chain extension
(reusing Slice 3's `_normalise_for_substring` by import), per-
dimension LLM scoring against the operator's prose rubric via one
Sonnet call. 79 deterministic tests + 1 live smoke (PASSED first
contact on Anthropic). Full suite (357 deterministic + 1 scorer
live smoke) clean; ruff check + ruff format + mypy strict all
clean. **Phase 2 Slice 5 (Drafter) is now UNBLOCKED.**

## Phase 2 Slice 4 Subtasks (Scorer + ScoredCompany + rubric loader)
- [x] `agents/models.py` extension — `DimensionScore` (per-dimension carrier: `field`, `weight`, `score | None`, `grounded_quote | None`, `reasoning`; scored↔excluded mutual-exclusivity validator), `ScoredCompany` (composite carrier: `score | None`, `coverage`, `floor`, `min_coverage`, `passed_floor`, `passed_coverage`, `tier | None`, `gated_by | None`, `dimensions: list[DimensionScore]`; tier-iff-score invariant), `TIER_VALUES` + `GATED_BY_VALUES` enums. All `ConfigDict(extra="forbid", frozen=True)`.
- [x] `scoring/rubric.py` + `scoring/__init__.py` — `RubricConfig` + `DimensionConfig` Pydantic v2 models + `load_rubric()` over stdlib `tomllib` mirroring `budget/config.py`'s pattern. Validates: weights sum to 1.0 (epsilon 1e-9); every `field` is in `PROFILE_FIELD_NAMES`; `tier_a_min > tier_b_min > tier_c_min >= floor`; `missing_data_policy == "renormalise"` (Phase 2 sole option); all required `[defaults]` keys present; duplicate-field rejection across dimensions. Operator-debuggable errors path-named.
- [x] `agents/scorer.py` — `run_scorer(conn, *, profile, rubric, ...)`, Sonnet tier via `llm.complete()` (no `llm.py` change), schema-in-system-prompt + `model_validate_json` per Slice 3's established structured-output choice, `run_step()` per ADR-006 §2. All-gaps profile / no-scorable-fields short-circuits to all-excluded ScoredCompany with NO LLM call (cost $0), step COMPLETED. Pure helpers (`_compute_composite`, `_assign_tier`, `_gating`, `_grounded_in_field`, `_assemble_dimensions`, `_all_excluded`) each unit-testable; `run_scorer` reads as a flat lifecycle.
- [x] Renormalise-with-coverage scoring (ADR-004 §2) — `coverage = sum(d.weight for present)`; `score = (sum(d.score * d.weight for present) / coverage) * 20`; `score=None` iff no dimension is present. Gating: coverage-precedence when both gates fail (more informative empty-outcome).
- [x] Grounding-chain extension (ADR-004 §3, ADR-006 §3) — each `DimensionScore.grounded_quote` substring-matches (whitespace-collapse + lowercase, REUSING Slice 3's `_normalise_for_substring` by import; not re-implemented) one of the cited `ProfileField.source_spans[].quote` values. Failure downgrades the dimension to excluded — same shape as Slice 3's `_ground_field` (gap-on-over-reach). The Scorer never lets a fabrication through.
- [x] Projection mirrored onto `step.completed`: `{score, coverage, floor, min_coverage, passed_floor, passed_coverage, tier, gated_by}` per ADR-004 §5 + ADR-006 §1.3.
- [x] `cli.py` — `agent-habitat run-scorer COMPANY_NAME [--db PATH] [--rubric PATH] [--max-searches N] [--*-workflow-id ID]` sequences Researcher → Extractor → Scorer as three separate workflows (Slice 6's orchestrator will unify them). `SCORER_DECISION_FOOTER` explicitly surfaces the coverage number (ADR-004 §2 + PATTERNS.md #4 — "this draft scored 75/100 against the rubric, but the rubric covered only 20% of the operator's stated ICP dimensions on this run"). FAILED rubric-load surfaces as a clean ClickException; FAILED scorer workflow exits non-zero.
- [x] 79 deterministic tests in `tests/test_scorer.py` — rubric loader (15: valid + every documented failure mode incl. weights-not-summing-to-1, invalid field, mis-ordered tiers, missing defaults keys, invalid policy, no dimensions, duplicate fields, bundled-file sanity); model validation (15: DimensionScore + ScoredCompany scored/excluded/frozen/extra=forbid/round-trip); pure scoring math (12: full / partial / all-gaps coverage; every tier band; gating coverage-precedence + all-gaps both branches per ADR-004 Forward deps); grounding check (6: substring pass + normalisation + over-reach reject + gap-field + empty-quote + normaliser-identity); end-to-end agent (16: full happy run + projection mirror + step+events persistence; all-gaps short-circuit no LLM call; gap-heavy renormalisation; below-floor / below-coverage COMPLETED-not-failed; grounding-downgrade through the agent; code-fence stripping; malformed JSON / schema mismatch / missing dimension / extra dimension / API exception infrastructure failures); CLI (5: happy three-stage path; researcher / extractor / scorer failures each exit non-zero; missing rubric file fails cleanly).
- [x] One live smoke (`@pytest.mark.live`) — see "Phase 2 Slice 4 Live Smoke Calibration" below.
- [x] Full suite (357 deterministic + 1 scorer live smoke) passes; ruff check + ruff format --check + mypy strict all clean.

## Phase 2 Slice 4 Live Smoke Calibration

One live three-agent run against `Anthropic` on 2026-05-14
(researcher: `claude-haiku-4-5-20251001` + web_search; extractor +
scorer: `claude-sonnet-4-6`). The first real end-to-end audit-grade
score on the full Researcher → Extractor → Scorer chain:

- **Scorer cost: $0.009669** for one Sonnet call on the 3-of-5
  scorable dimensions ($0.0097 is the project's first real per-Scorer-run
  number; consistent with the ~$0.005-$0.010 projection range).
  **Three-agent chain combined: $0.046176** (Researcher $0.025809 +
  Extractor $0.010698 + Scorer $0.009669). Lower than the Slice 2/3
  $0.073 total because the Researcher self-paced to 1 search this
  run rather than hitting `max_searches=3`. The model decides when
  to stop searching; `max_searches` is a cap, not a target.
- **Wall-time: 7s for the Scorer**, ~18s for the full three-agent
  chain. Consistent with Slice 2's "~15-25s end-to-end for 5
  agents" projection.
- **Score: 82.86 / coverage: 70% / tier: A / gated_by: None.** The
  ScoredCompany passed both gates; an orchestrator with this output
  would route to the drafter. Three dimensions scored at 3.0 / 5.0 /
  5.0 (industry / tech_stack / recent_news); two dimensions excluded
  (size + decision_makers — the Extractor returned gaps for them on
  this run).
- **THE SCORE-VS-COVERAGE SPREAD IS THE HEADLINE FINDING.** Score
  82.86 reads as "Tier A — top band" in isolation. But coverage is
  70% — the rubric was only judging 70% of the operator's stated
  ICP dimensions. The `score × coverage = 0.58 → 58` collapsed-
  composite that ADR-004 §A explicitly rejected would have flattened
  this into a low-confidence "58/100" — which is exactly the
  dual-source-of-truth failure ADR-004 §A names. The
  renormalise-with-coverage policy preserves both signals: the score
  reflects rubric quality on covered dimensions, the coverage flags
  the operator that the ranking carries a 30%-of-rubric blind spot.
  PATTERNS.md #4's coverage-aware decision-support framing
  ("scored 82.86/100 against the rubric, but the rubric covered
  only 70% of the operator's stated ICP dimensions on this run")
  is exactly the disclosure the Drafter (Slice 5) needs to inherit.
- **GROUNDING SURVIVED FIRST CONTACT — NO DOWNGRADES CAUGHT.** All
  three LLM-scored dimensions' `grounded_quote` values substring-
  matched (after normalisation) at least one of the cited
  ProfileField's `source_spans[].quote` values. Different from the
  Extractor's first live smoke (Slice 3) which caught two real
  over-reaches: the Scorer's grounding corpus is tighter (already-
  grounded ProfileField source_spans, not raw Signal text), so
  over-reach is structurally harder. Watch on subsequent runs —
  one data point is not yet calibration. The substring check is
  load-bearing precisely because it would catch over-reach when it
  occurs, not because it always does.
- **Per-dimension calibration table** (full run, in
  PROFILE_FIELD_NAMES canonical order):
    `size            (w=0.20): EXCLUDED — field was an ExtractionGap`
    `industry        (w=0.30): 3.0/5    — quote: "Anthropic launches Claude for Small Business"`
    `tech_stack      (w=0.20): 5.0/5    — quote: "...Claude into tools like QuickBooks, PayPal..."`
    `recent_news     (w=0.20): 5.0/5    — quote: "$30 billion in fresh financing"`
    `decision_makers (w=0.10): EXCLUDED — field was an ExtractionGap`
  The model's 3.0/5 industry score against the rubric prose's "5 —
  fintech, healthcare, legal/compliance, insurance, regtech" band
  is interesting — Anthropic is AI-safety/regulated-adjacent but
  not literally fintech. The model picked the 3 band ("adjacent
  regulated-adjacent — enterprise SaaS, dev tools with compliance
  customers"). Calibration is honest, not deferential.
- **The Scorer's audit chain holds end-to-end.** Workflow row + one
  step row + four events (`workflow.started` → `step.started` →
  `step.completed` → `workflow.completed`); step.completed
  structured_data carries the full projection
  `{score, coverage, floor, min_coverage, passed_floor,
  passed_coverage, tier, gated_by}`; JSONL telemetry at `output_ref`
  resolves to a real Sonnet response. The chain back from
  `ScoredCompany.dimensions[i].grounded_quote` →
  `CompanyProfile.<field>.source_spans[j].quote` →
  `RawSignals.signals[k].text` → `Citation.cited_text` is
  traceable end-to-end on the live run.
- **Cost expectations updated.** A typical three-agent run lands at
  **~$0.045-$0.075** depending on Researcher's self-paced search
  count. Add the Opus Drafter ($0.025-$0.060) + Haiku Critic
  (~$0.001) and a five-agent run lands at ~$0.07-$0.14 — consistent
  with the Slice 3 projection. `lead_enrichment` $10/day affords
  ~70-140 full runs. The forward-dependency `budgets.toml`
  re-validation queued for Slice 8 can use this as one of the
  inputs.

## Phase 2 ADR-004 Highlights
- **TOML format mirrors `budgets.toml`'s idiom.** `[defaults]` for
  global tunables (`floor`, `tier_a_min`/`tier_b_min`/`tier_c_min`,
  `min_coverage`, `missing_data_policy`); `[dimensions.<name>]` per
  scoring dimension (one dimension scores exactly one
  `PROFILE_FIELD_NAMES` field; weights MUST sum to 1.0; `prose` is
  the operator-authored rubric the LLM applies).
- **Missing-data handling: renormalise-with-coverage.** Gap-excluded
  dimension's weight drops from denominator; `score` reflects rubric
  quality on covered dimensions; `coverage` flags the operator that
  ranking is unreliable when coverage is low. Rejected: gap=0 (makes
  every Slice-3-typical 4/5-gap input route to terminate_no_draft —
  habitat becomes useless), and collapsed-composite (`score × coverage`
  — dual-source-of-truth failure, can't tune the two effects
  independently).
- **`score` is the number ADR-006 §1's floor gates on.** Routing
  contract unchanged. The optional `min_coverage` second gate adds
  `gated_by = "coverage"` vs `gated_by = "score"` to the
  `step.completed` projection so the calibration story can tell the
  two empty-outcomes apart.
- **Grounding chain extends through the Scorer.** Per-dimension
  reasoning carries a `grounded_quote` which must substring-match
  (whitespace-collapse + lowercase normalised) one of the cited
  field's `source_spans` quotes. Pure-Python check; same shape Slice
  3 established. Five-hop chain now: `Citation.cited_text →
  Signal.text → ProfileField.source_spans → ScoredCompany.grounded_quote
  → Draft claim`, each hop a substring of the previous. Slice 7
  Critic inherits this chain unchanged.
- **Scoring mechanism: LLM-as-judge against a prose rubric** (not
  pure-deterministic keyword lists, not hybrid). The audit-grade
  posture is achieved by grounding (substring check on
  `grounded_quote`), not by avoiding the LLM — the consistent move
  across ADR-003, ADR-006 §3, ADR-004.
- **Honest limitation surfaced:** the rubric is an un-validated
  operator hypothesis. agent-habitat has no CRM and no closed-won
  dataset; the industry's "tune against 50 closed-won deals" step
  does not exist here. The substitute is Slice 8 calibration —
  record per-company human-judged fit vs rubric score and surface
  disagreements. The ADR documents this honestly rather than
  pretending the rubric is data-validated.
- **`config/rubric.toml` shipped as a clearly-marked format
  template** (banner comment: "NOT a real tuned rubric"). Five
  example dimensions with placeholder weights summing to 1.0 +
  illustrative prose. The operator replaces the values; the format
  is the ADR-004 contract.

## Phase 2 Slice 3 Subtasks (Extractor + CompanyProfile + ExtractionGap)
- [x] `agents/models.py` extension — `SourceSpan`, `ExtractionGap`, `ProfileField` (mutually-exclusive value-or-gap with model_validator), `CompanyProfile` (5 fields: size / industry / tech_stack / recent_news / decision_makers). Every model `ConfigDict(extra="forbid", frozen=True)`. `PROFILE_FIELD_NAMES` tuple is the canonical iteration order.
- [x] `agents/extractor.py` — `run_extractor(conn, *, raw_signals, ...)`, Sonnet tier via `llm.complete()` (no `llm.py` change), `run_step()` per ADR-006 §2. Empty `RawSignals` short-circuits to all-gaps profile with NO LLM call (cost $0), step COMPLETED. Projection mirrored: `{has_size, has_industry, has_tech_stack, has_recent_news, has_decision_makers, gap_count}`.
- [x] Substring-grounding validator (`_ground_field` / `_ground_profile`) — every `SourceSpan.quote` must appear (whitespace-collapse + lowercase normalised) in `raw_signals.signals[span.signal_index].text`. Over-reach is downgraded to `ExtractionGap(reason="span_not_grounded")`; the Extractor never lets a fabrication through. This is also how the short-cited-text-span forward dependency from the ADR-003 addendum is HANDLED in this slice (see Open Questions resolution below).
- [x] `cli.py` — `agent-habitat run-extractor COMPANY_NAME [--db PATH] [--max-searches N] [--researcher-workflow-id ID] [--extractor-workflow-id ID]` sequences Researcher then Extractor as two separate workflows (Slice 6's orchestrator will unify them). Decision-support footer on the output; non-zero exit on FAILED.
- [x] 52 deterministic tests in `tests/test_extractor.py` — models (16); parse + grounding helpers including the over-reach downgrade + out-of-range-signal-index downgrade (11); happy run + projection + audit chain (6); empty-outcome (3); sparse signals (1); short-span over-reach (2 — THE inherited forward-dependency); infrastructure failure including malformed-JSON and bad-schema responses (3); CLI happy + researcher-failure + extractor-failure (3). One live smoke (`@pytest.mark.live`).
- [x] Full suite (278 deterministic + 1 extractor live smoke) passes; ruff check + ruff format --check + mypy strict all clean.

## Phase 2 Slice 3 Structured-Output Method Choice
**Choice: schema-in-system-prompt + Pydantic `model_validate_json` on `LLMResult.content`.** Documented in `extractor.py`'s module docstring.

The Researcher does NOT parse a structured JSON object out of the LLM
response — it reads typed `LLMResult.citations` produced by `llm.py`. So
there is no pre-existing project pattern for "parse a Pydantic object out
of `LLMResult.content`"; Slice 3 establishes one. The three API-level
alternatives are:

  1. `messages.parse(output_format=Model)` — different SDK entrypoint;
     requires an `llm.py` change.
  2. Strict tool use (`tools=[{strict: True, ...}]` + `tool_choice`) —
     requires adding a `tool_choice` kwarg to `llm.complete()`.
  3. Schema-in-system-prompt + parse-from-content (CHOSEN) — uses the
     existing `llm.complete()` contract unchanged; the Extractor parses
     `LLMResult.content` itself.

Choice rationale: (1) and (2) both require `llm.py` changes, which
ADR-006 makes their own decision and the Slice 3 kickoff explicitly says
to STOP and flag for. (3) consumes only what `llm.complete()` already
returns and keeps the Extractor's interface narrow.
`ConfigDict(extra="forbid")` on every Slice 3 Pydantic model makes the
schema strict on parse — unexpected fields the model invents are
rejected, not silently accepted. The trade-off accepted: the model can
emit malformed JSON; we treat that as an infrastructure failure (no
retry), exactly the same way the Researcher treats an API exception.

## Phase 2 Slice 3 Live Smoke Calibration

One live Researcher+Extractor run against `Anthropic` on 2026-05-14
(researcher: `claude-haiku-4-5-20251001` + web_search; extractor:
`claude-sonnet-4-6`). Genuine observations a mocked test could not
have surfaced — the first real audit-grade extraction round-trip:

- **Extractor cost: $0.008772** for one Sonnet call on ~2K-3K input
  tokens + ~300 output tokens (5 signals as input). Significantly cheaper
  than the Researcher ($0.064634 same run): the Extractor neither
  invokes web_search nor processes inline search results, just structured
  output from short prose. **Researcher+Extractor combined: $0.073406**
  for the two agents.
- **Wall-time: 5.64s for the Extractor**, 8.16s for the Researcher.
  Total ~14s for the two-agent slice on a real workload. Consistent with
  Slice 2's "~15-25s end-to-end for 5 agents" projection — five agents
  remain plausibly within that range if Scorer/Critic stay cheap.
- **THE FABRICATION-RESISTANCE VALIDATOR CAUGHT TWO REAL OVER-REACHES.**
  On the first live extraction the Sonnet model proposed extractions for
  `industry` and `tech_stack` with `source_spans` whose `quote` text did
  NOT substring-match (after normalisation) the cited
  `raw_signals.signals[span.signal_index].text`. Both fields were
  downgraded to `ExtractionGap(reason="span_not_grounded")` — exactly
  the failure mode the design exists to catch. This validates the contract
  in the wild, not just in a mocked unit test:
    `recent_news: VALUE  — extracted, 3 grounded spans`
    `size: GAP — field_not_in_signals (model honestly returned a gap)`
    `industry: GAP — span_not_grounded (OVER-REACH CAUGHT)`
    `tech_stack: GAP — span_not_grounded (OVER-REACH CAUGHT)`
    `decision_makers: GAP — field_not_in_signals (model honestly returned a gap)`
  Field extraction rate on a 5-signal news-heavy input: 1/5 (20%). Gap
  rate: 4/5 (80%) — of which 2 are model-honest gaps and 2 are over-reach
  catches.
- **Short cited_text spans show up in practice as expected.** 3 of the
  signals used by the extractor were <200 chars (short cited_text
  fragments — the forward dependency from the ADR-003 addendum). The
  substring validator handled them correctly: the one field where the
  model honestly grounded against short spans (`recent_news`) survived;
  the two fields where it over-reached against narrow spans
  (`industry`, `tech_stack`) were caught and gapped.
- **Cost calibration update for the full 5-agent pipeline.** Slice 2's
  recalibrated projection of $0.10–$0.15 per full run still looks correct
  on these numbers: Researcher ($0.065) + Extractor ($0.009) is $0.074;
  add Haiku Critic (~$0.001), Sonnet Scorer (~$0.005), Opus Drafter
  (~$0.04) and the projection lands at ~$0.12. ADR-003 addendum's queued
  `budgets.toml` re-validation (Slice 8) gets more data here but
  $10/day still affords ~80 full runs.
- **JSONL telemetry round-trip resolves end-to-end on the Extractor.**
  step.output_ref → JSONL line → `workflow_id`, `agent_name=extractor`,
  `model=claude-sonnet-4-6`, both token counts, cost, full response text.
  Same audit shape as the Researcher's path. The chain back from
  `CompanyProfile.size.source_spans[0]` → `raw_signals.signals[idx].text`
  → `Citation.cited_text` in the Researcher's JSONL is traceable end-to-end.

## Phase 2 Slice 2 Subtasks (Researcher + llm.py tools= + RawSignals)
- [x] `llm.py` extended additively: optional `tools=` param on `complete()` forwarded to `messages.create`; `compute_cost_usd` extended with `web_search_requests=0` kw-only and adds `n * $0.01` server-tool fee onto `cost_usd`; new dated `_WEB_SEARCH_FEE_USD = 0.01` constant ("NEEDS VERIFICATION against current Anthropic pricing"); JSONL telemetry record gains additive `web_searches` / `web_search_fee_usd` keys (only when `tools` is provided — ordinary no-tools calls keep their record shape unchanged); `LLMResult` gains additive `web_searches: int = 0` and `citations: list[Citation] = []`; existing fields untouched, contract still additive
- [x] `agents/models.py` — new `Signal` (frozen) + `RawSignals` (frozen) Pydantic v2 models with `signal_count` / `source_count` derived properties; empty `signals` list is a valid result (ADR-006 §1 empty-outcome contract)
- [x] `agents/researcher.py` — `run_researcher(conn, *, company_name, max_searches=3, ...)` makes one Haiku call with `tools=[web_search_20250305]`, builds `Signal` records from `LLMResult.citations[].cited_text`, wires through `run_step()` per ADR-006 §2; projection `{signal_count, source_count, web_searches}` mirrored onto `step.completed`; empty signals → COMPLETED; infrastructure errors propagate from `run_step` and finalise workflow FAILED; no retries (ADR-006 §1, Slice 1)
- [x] `cli.py` — new `agent-habitat run-researcher COMPANY_NAME [--db PATH] [--workflow-id ID] [--max-searches N]` reusing the `--db` pattern; decision-support footer on the output; FAILED workflow exits non-zero
- [x] 16 deterministic tests in `tests/test_researcher.py` (RawSignals model + happy run + empty-signals outcome + infrastructure failure + CLI happy/failure); 10 new deterministic tests in `tests/test_llm.py` covering `compute_cost_usd` extension, `_web_search_request_count` defensive defaults, `_extract_web_search_citations`, tools-forwarding, server-tool-fee aggregation, citations onto `LLMResult`, additive JSONL keys, and the no-tools-call-unaffected contract
- [x] One live smoke against `Anthropic` (well-known public footprint): full habitat round-trip verified — workflow COMPLETED, 4 signals across 3 distinct sources, `cost_total_usd = $0.066871`, `web_searches = 3` (model hit the `max_uses=3` cap), `output_ref` resolves to a real JSONL line with the additive `web_searches` / `web_search_fee_usd` keys, real readable Bloomberg + PYMNTS cited spans
- [x] Full suite (227 deterministic + 1 researcher live smoke) passes; ruff check + ruff format --check + mypy strict all clean

## Phase 2 Slice 2 Live Smoke Calibration

One live researcher run against `Anthropic` on 2026-05-14 with
`claude-haiku-4-5-20251001`. Genuine observations a mocked test could not
have surfaced — the first real audit-grade web_search call through the
habitat:

- **Real cost: $0.066871** — **~50% higher than ADR-003's $0.035-$0.045
  estimate.** Driver: input_tokens = 34,971 (not the ~3K ADR-003 assumed
  for "snippet content"). The web_search tool feeds substantial result
  content into the model's context window as it reasons across searches;
  the "~3K snippets" figure understated the inline injection by an order
  of magnitude. Breakdown: $0.034971 input + $0.0019 output + $0.030 fee
  (3 searches × $0.01) = $0.066871. The fee component is ~45% of total —
  significant but not dominant; input tokens are the bigger driver.
- **THE KNOWN WRINKLE confirmed.** The raw `web_search_tool_result`
  block's per-result `content[i].encrypted_content` is opaque (Anthropic
  designed it for multi-turn round-trip, not plain reading). URL, title,
  and `page_age` are plain readable; the snippet text is not. The
  plain-readable equivalent — and the corpus this Researcher actually
  grounds against — is `TextBlock.citations[i].cited_text` from
  `CitationsWebSearchResultLocation` blocks: verbatim source spans tied
  to source URL + title that the model chose to cite. **ADR-003's stated
  premise ("persist the raw search_result block text") cannot be
  honoured literally; the substantively equivalent grounding shape is
  citation `cited_text` spans, which is what RawSignals.signals[].text
  is built from.** Recorded as an Open Question for an ADR-003 addendum.
- **Citations are real source prose, not narrative.** The four signals
  surfaced were verbatim Bloomberg + PYMNTS spans ("May 12, 2026 at
  9:08 PM UTC · Save · Anthropic PBC is in early talks with investors
  to raise at least $30 billion in fresh financing…"). The model's own
  narrative TextBlocks are separate and are NOT pulled into Signal
  records — exactly the fabrication-grounding discipline ADR-006 §3
  asks for.
- **The model issued exactly `max_uses=3` searches** — it hit the cap.
  Suggests the cap is the *real* per-run budget knob; the model tends
  to spend whatever budget the operator provides. Operator-tunable per
  call via `--max-searches` and the `run_researcher(max_searches=...)`
  kwarg.
- **Signal-to-search ratio is NOT 1:1.** Three searches produced four
  citations (`signal_count=4, source_count=3`) — multiple citations can
  come from the same source page; one search can produce zero citations
  if the model doesn't ground a claim against its results. `signal_count`
  and `source_count` are distinct projections for a reason.
- **Wall-time: 5.44s** total (one llm.complete call). Of that, ~5s is
  Anthropic round-trip (model + server-side searches + reasoning).
  Phase 2's 5-agent pipeline at this per-agent latency is ~15-25s
  end-to-end on a single run, before any parallelisation.
- **Audit chain holds end-to-end on the new tool path.** Workflow row
  + one step row + four events (`workflow.started` → `step.started` →
  `step.completed` → `workflow.completed`); step.completed structured
  data carries `{signal_count: 4, source_count: 3, web_searches: 3,
  cost_usd, output_ref}`. JSONL telemetry record at the `output_ref`
  resolves and includes both additive keys `web_searches: 3` and
  `web_search_fee_usd: 0.03`. First real exercise of `tools=` through
  `llm.py`; the additive contract worked first try.

**Cost expectations updated.** A typical Researcher run is **~$0.06-$0.07**,
not the ADR-003 estimate of $0.035-$0.045. Implication for Slice 8
budget calibration: a 5-agent pipeline run is now expected at
~$0.10-$0.15 (Researcher dominant; Extractor/Scorer/Critic on cheaper
tiers + smaller token loads; Drafter on Opus is the other heavy line).

## Slice 1 Subtasks
- [x] Scaffold project skeleton + plan docs
- [x] ADR-001: LangGraph over CrewAI (Accepted 2026-05-13)
- [x] ADR-002: Persistence schema (Accepted 2026-05-13)
- [x] llm.py wrapper implementation (2026-05-13)
- [x] Slice 1 live smoke: single Haiku call through llm.py, JSONL telemetry write verified (2026-05-13)

## Slice 2 Subtasks
- [x] Step 0: `stop_reason` + derived `truncated` on LLMResult (committed separately)
- [x] Pydantic v2 models (`Workflow`, `WorkflowStep`, `Event`) + status/level enums
- [x] DDL bootstrap (idempotent `init_schema`) for `workflows`, `workflow_steps`, `events`
- [x] Typed CRUD: insert / update / load / query-by-status
- [x] Cost rollup: `recompute_cost_total` sums `workflow_steps.cost_usd` → `workflows.cost_total_usd`
- [x] Orphan reconciliation: `reconcile_orphan_steps` (startup sweep per ADR-002)
- [x] 33 deterministic tests in `tests/test_state.py`; full suite passes, ruff + mypy strict clean

## Slice 4 Subtasks
- [x] `observability/events.py` — `emit_event()` conventioned writer over the existing `events` table; `EventType` taxonomy enum; `EVENT_LEVEL_GUIDE` semantics; `events_of_type()` `json_extract` query primitive
- [x] `observability/logging.py` — central `configure_logging()` (console renderer default, JSON output opt-in); `bind_workflow_context()` / `clear_log_context()` via structlog contextvars; idempotent
- [x] `observability/jsonl.py` — `iter_telemetry(workflow_id, log_root, day=None)` over `data/logs/YYYY-MM-DD/<wf>.jsonl`; `resolve_output_ref(path:line)` canonical resolver; `TelemetryReadError` for malformed/missing
- [x] Module-level coherence docstring spelling out the three surfaces (`events` table, JSONL, structlog) and what each is for
- [x] `llm.py` untouched (no ad-hoc structlog config existed to align; rule #14 leaves the working JSONL writer in place)
- [x] 29 deterministic tests in `tests/test_observability.py`; full suite (119 tests) passes, ruff + ruff format + mypy strict clean

## Slice 3 Subtasks
- [x] Budget config: `config/budgets.toml` (operator-tunable, stdlib `tomllib`, no new dep)
- [x] `BudgetConfig` pydantic model + `load_budget_config()` with operator-debuggable errors
- [x] `cap_for_workflow_type()` resolution: override wins, else default
- [x] UTC calendar-day window helper (`utc_day_window`) — decision recorded under Open Questions
- [x] `cost_within_window` — SUM over `workflow_steps.cost_usd` for steps with `started_at` in window
- [x] Pure `evaluate_budget(cost, cap, threshold) → UNDER | APPROACHING | OVER`
- [x] `check_workflow_budget()` — composed end-to-end check returning a `BudgetCheck` dataclass
- [x] `record_budget_exceeded()` — writes a structured `budget.exceeded` event into the EXISTING `events` table (no ADR-002 schema change)
- [x] `is_workflow_halted_by_budget()` — `json_extract`-backed halt-signal query for the Phase 2 orchestrator
- [x] 40 deterministic tests in `tests/test_budget.py`; full suite (90 tests) passes, ruff + mypy strict clean

## Slice 7 Subtasks
- [x] Live API smoke across 5 deliberately varied URLs (Wikipedia article w/ `<main>`, Python docs w/o semantic tags, PEP 20 w/ `<article>`, example.com sparse, httpbin 404 controlled failure)
- [x] Per-run dataset captured from persisted workflow + JSONL telemetry: status, cost USD, input/output tokens, wall-time per step, parse path, output_ref
- [x] Synthesis: cost-distribution analysis, fixed-floor confirmation, MAX_PROMPT_CHARS truncation discovery, parse-path coverage of all three branches, budget-cap calibration
- [x] Phase 1 README rewritten: hiring-manager-honest framing, real-numbers calibration table, what-the-live-runs-taught-us section, honest scope (what Phase 1 is NOT), verified setup/run commands
- [x] Full suite (174 deterministic) passes; ruff check + ruff format + mypy strict all clean

## Slice 7 Calibration Findings

Five live runs on 2026-05-14 with `claude-sonnet-4-6`. Full dataset is in README.md
("Live calibration" section); the findings that came out of it:

- **Cost spans 14× across page types** ($0.001212 → $0.016830). The fixed-cost-floor pattern (Slice 6 finding on example.com) holds and is now quantified at ~$0.0012 per minimal run. Above ~500 input tokens, cost is input-dominated; output tokens are bounded 60–238 by the "three to five sentences" instruction regardless of input size.
- **`MAX_PROMPT_CHARS=12_000` fires silently on heavy real-world pages.** Wikipedia (45,079 extracted chars) and Python docs (26,239 extracted chars) both got truncated to the first 12K before reaching the LLM. Slice 6's single trivial page (142 chars) could not have surfaced this. Recorded in README as an honest product behavior; queued below as an Open Question for Phase 2 (log a `WARN` event on truncation, or chunk-and-stitch — ADR-gated).
- **All three parse-strategy branches got real-world coverage.** `<main>` (Wikipedia), `<article>` (PEP 20 — first observation; Slice 6 only saw fallback), whole-soup fallback (Python docs + example.com). The fallback is load-bearing on the public web: Python's official docs use neither semantic tag.
- **Latency is LLM-bound at 92–94%** of wall time on completed runs. Fetch + parse total 0.2–0.5s; everything else is Sonnet. Wall time scales at ~1.5s per 1,000 input tokens. Implication for Phase 2: each agent in a five-agent chain adds 2–7s LLM wall time depending on payload — pipelining and Haiku-for-grunt-work matter for end-to-end latency, not just dollars.
- **example.com run-to-run variance is tight.** Slice 6: 104 in / 54 out / $0.001122. Slice 7: 104 in / 60 out / $0.001212. Input deterministic, output ±10%. Telemetry numbers are trustworthy signal, not noise.
- **Failure contract held in the wild.** The httpbin 404 produced exactly the expected four-event trail (`workflow.started`, `step.started`, `step.failed`, `workflow.failed`), `FAILED` workflow + step rows with `finished_at` stamped, fast exit (0.52s), zero LLM cost. No stuck-RUNNING workflow.

**Budget cap recommendation — KEEP $2/day for `url_summarizer`.** Most expensive observed
run was $0.01683 (Python docs); $2/day = ~118 max-cost runs/day, or ~218 at the
mean of the four successful runs ($0.009187). The cap is now calibrated against
real heavy-page cost rather than extrapolated from the trivial-page floor —
generous headroom for operator-paced exploration without being so loose it
provides no safety signal. No config change needed; `config/budgets.toml` stays
at $2.00 daily for the `url_summarizer` override.

## Slice 6 Subtasks
- [x] `src/agent_habitat/agents/summarizer.py` — `run_summarizer`, `fetch_url`, `extract_readable_text`, `SummarizerResult`, `SummarizerError`; three synchronous steps (fetch / parse / summarize), Sonnet tier via `llm.complete()`, decision-support framing in the CLI output
- [x] `src/agent_habitat/cli.py` — `run-summarizer URL` command reusing the `--db` pattern; non-zero exit on FAILED workflow
- [x] Habitat integration: one workflow row + three step rows + the conventioned event sequence (`workflow.started`, `step.{started,completed}` ×3, `workflow.completed`) on success; FAILED workflow + `workflow.failed` + matching `step.failed` on any step failure, `finished_at` always stamped, no stuck-RUNNING workflows
- [x] Cost path wired end to end: `LLMResult.cost_usd` → `workflow_steps.cost_usd` (summarize step) → `recompute_cost_total` → `workflows.cost_total_usd`; `output_ref` on the summarize step points at the JSONL line `llm.py` wrote
- [x] 26 deterministic tests in `tests/test_summarizer.py` (httpx `MockTransport` for fetch, `unittest.mock.patch` for the LLM): fetch happy + 404 + network error + oversize + empty + bad scheme + bad URL; parse happy + script/style stripped + `<main>` preference + fallback + too-short rejected; happy run persists everything correctly; four failure paths (bad scheme, 404, network error, empty page, LLM error) end the workflow FAILED with the right step row + events; CLI happy + failure exits non-zero
- [x] One live smoke (`@pytest.mark.live`) against `https://example.com/`: full round-trip verified, calibration observations captured (see "Slice 6 Live Smoke Calibration" below)
- [x] Full suite (175 tests, including the live smoke) passes; ruff check + ruff format + mypy strict clean

## Slice 6 Live Smoke Calibration

One live run against `https://example.com/` on 2026-05-14 with Sonnet 4.6.
Genuine observations a mocked test could not have surfaced — these feed the
Slice 7 calibration story / README:

- **Real cost: $0.001122** for the whole run (104 input tokens, 54 output tokens). That's ~0.06% of the configured `url_summarizer` $2/day cap. Even ~1800 trivial-page runs/day would not trip the cap — the cap is generous for short pages and only meaningful for longer documents or many calls.
- **Input-token floor is dominated by the system prompt + boilerplate, not the page.** example.com's readable text is ~140 chars; the prompt still came in at 104 input tokens. Useful sizing intuition: cost-per-run has a non-trivial fixed floor regardless of how short the page is.
- **Sonnet self-paced to ~3 sentences without truncation** (54 output tokens vs 512 cap, `stop_reason=end_turn`). The system-prompt instruction "three to five sentences in plain prose" held; no markdown leaked, no preamble. Encouraging for a prose contract that has to survive without an explicit JSON schema.
- **`<main>`/`<article>` preference did NOT fire on the simplest real page.** example.com has neither tag, so the parser fell back to whole-soup extraction. The mocks asserted the *preference path* works; the *real* path on the simplest production URL is the fallback. Implication for Slice 7: invest more in the fallback's quality, since real-world pages skew toward unsemantic markup.
- **Latency: 2.84s total**, dominated by the Sonnet call. The httpx fetch to example.com was sub-100ms; parse is negligible. LLM time is the binding latency — useful when reasoning about Phase 2 multi-agent pipelines (each agent call adds ~2s of LLM wall time even on trivial input).
- **Eight events emitted in the expected order** — workflow.started → step.started/completed × 3 → workflow.completed. The taxonomy survived first contact with a real workload; no convention drift versus what Slice 4 documented.
- **Telemetry round-trip is real.** The summarize step's `output_ref` (a path + line number) resolved to a JSONL record carrying `workflow_id`, `agent_name=url_summarizer`, `model=claude-sonnet-4-6`, both token counts, cost, and the full response text. This is the audit story working end-to-end on a real call rather than a fixture.

## Slice 5 Subtasks
- [x] `checkpoint/system.py` — `request_checkpoint`, `approve_checkpoint`, `reject_checkpoint`, `get_checkpoint`, `list_pending_checkpoints`, `is_workflow_paused_for_checkpoint`; `Checkpoint` frozen dataclass, `CheckpointResolution` enum, `CheckpointError`
- [x] Pending-approval record is additive on ADR-002's events table (request event id IS the checkpoint id; resolution events back-reference via `structured_data.checkpoint_id`) — no schema change
- [x] Workflow state transitions: RUNNING → PAUSED on request; PAUSED → RUNNING on approve; PAUSED → CANCELLED (terminal, `finished_at` stamped) on reject
- [x] One pending checkpoint per workflow — a second request on a still-paused workflow raises
- [x] `cli.py` extended with `checkpoint {list,show,approve,reject}` group; `--db` option on the group; decision-support footer on pending `show` output; `CheckpointError` surfaces as a clean `click.ClickException`
- [x] 30 deterministic tests in `tests/test_checkpoint.py`; full suite (149 tests) passes, ruff check + ruff format + mypy strict clean

## Open Questions
- **ADR-003 premise — RESOLVED 2026-05-14** by the ADR-003 Addendum (`docs/adr/ADR-003-web-search-tool.md`, "Addendum (2026-05-14): `cited_text` grounding + cost recalibration"). The core ADR-003 decision (Anthropic `web_search`, single SDK call, cost through `llm.py`, no second client) stands; the addendum corrects the mechanism — Signals are built from `citations[].cited_text` rather than the opaque `web_search_tool_result.encrypted_content` — and ratifies the consequence that uncited model narrative produces no Signal (right fabrication-resistance semantics). ADR-006 §3's substring-check mechanism works as written against `cited_text` spans; **no ADR-006 amendment required**. Forward dependencies the addendum surfaced are recorded as their own entries below.
- **ADR-003 cost recalibration — RESOLVED 2026-05-14** in the addendum's §3. Documented per-run cost is now ~$0.067 (real breakdown: $0.034971 input + $0.0019 output + $0.030 fee). The two downstream re-checks the recalibration forces are recorded as separate forward-dependency entries below; no decision pending here.
- **Forward dependency from ADR-003 Addendum (Slice 3, Extractor): short `cited_text` spans are legitimate-but-narrow grounding — RESOLVED 2026-05-14 (Slice 3).** The Extractor handles short upstream spans via the `_ground_field` substring validator in `agents/extractor.py`: every extracted `SourceSpan.quote` is verified (whitespace-collapse + lowercase normalised) as a substring of `raw_signals.signals[span.signal_index].text`. A short span is legitimate grounding for whatever the span text literally supports; a `quote` that over-reaches beyond the span text is downgraded to `ExtractionGap(reason="span_not_grounded")` rather than allowed through. Confirmed empirically on the Slice 3 live smoke: 3 of the cited spans used were <200 chars (short fragments), the honest-grounding case survived (`recent_news`), and TWO real over-reaches by the live Sonnet model were caught and gapped (`industry`, `tech_stack`). Forward dependency for Slice 7 (Critic) stands: the Critic's red-team smoke should still paraphrase beyond the boundaries of a short cited span to verify the equivalent substring check rejects it in the drafter→critic direction.
- **Forward dependency from ADR-003 Addendum (Slice 8 / budgets.toml re-check).** `config/budgets.toml` `lead_enrichment` daily cap = $10.00 was set against the original $0.035–$0.045 per-Researcher-run estimate. At the recalibrated ~$0.07/Researcher-run and a projected ~$0.10–$0.15 per full 5-agent run, $10/day still affords ~66–100 full runs/day — likely still adequate but **explicitly unverified against the corrected numbers**. Re-validate the cap when Slice 8 produces real end-to-end cost numbers across all five agents. Not an emergency tightening; a calibration update.
- **Forward dependency from ADR-003 Addendum (ADR-006 §1 checkpoint cost-rationale re-check).** ADR-006 §1's checkpoint placement rationale reads: "the rest of the upstream chain at Haiku+Sonnet costs roughly $0.01–$0.02 combined." With the corrected Researcher cost (~$0.07 alone), the upstream chain is more honestly ~$0.07–$0.10. The checkpoint break-even *logic* is unaffected (Opus draft cost $0.025–$0.060 dominates the savings calculus regardless), but §1's prose understates upstream cost by ~5×. When Slice 8 calibration data lands, either update §1's prose with the real number or attach a calibrated-figure pointer; do not silently leave the stale figure.
- **Consolidate `llm.py`'s JSONL telemetry writer through the ObservabilityLayer.** Today `llm.py._append_telemetry` writes JSONL directly; Slice 4 added the conventioned READ side (`iter_telemetry`, `resolve_output_ref`) but did NOT touch the writer — `LLMResult` is a load-bearing contract and rule #14 forbids broad refactors without an ADR. Future work: either (a) route llm.py's writer through an ObservabilityLayer writer module so the path/line/format conventions live in one place, or (b) explicitly document the writer-stays-in-llm.py boundary as the chosen architecture. Trigger: any second writer of JSONL telemetry (Slice 5 checkpoint payloads? Phase 2 agent intermediate artefacts?) — that's the moment to centralise.
- **Rate table needs verification.** `_RATES_USD_PER_MTOK` in `llm.py` uses best-known values (Haiku $1/$5, Sonnet $3/$15, Opus $15/$75 per MTok input/output) stamped 2026-05-13. Joseph: cross-check against the public Anthropic pricing page before relying on the cost numbers for any budget decision (Slice 3 enforcement is now wired but reads the same rate table).
- **ADR-002 underspecification: `workflows.id` generation algorithm.** ADR-002 fixes the *relationship* (id is shared with LangGraph as `thread_id`) and the *type* (TEXT PRIMARY KEY) but does not name a generation method. Slice 2 defaults to `uuid.uuid4().hex` via `new_workflow_id()`; callers may override. Revisit with an ADR-002 addendum if Phase 2 needs sortable or time-prefixed ids (ULID, snowflake) for cheap range scans.
- **ADR-002 underspecification: orphan reconciliation target without LangGraph state.** ADR-002 says orphans reconcile to "failed with a synthesized event, or resume." The "or resume" branch needs LangGraph checkpoint state to decide whether resume is safe; that wiring lands with the Phase 2 orchestrator. Slice 2 implements the deterministic half: mark orphan FAILED, set `finished_at=now`, synthesize a WARN event. The resume branch can layer on top later without changing this contract.
- **Slice 3 "daily" definition resolved: UTC calendar day.** "Daily budget cap" = the half-open interval `[today 00:00:00 UTC, tomorrow 00:00:00 UTC)`. Caps reset at UTC midnight. Why UTC over rolling-24h or local-tz: aligns with the JSONL telemetry directory layout (`data/logs/YYYY-MM-DD/` already UTC), is trivially auditable, and makes window queries simple ISO-string range comparisons. Revisit if a workload needs per-tenant local-tz semantics.
- **`MAX_PROMPT_CHARS` truncation — visibility resolved; chunk-and-stitch still ADR-gated.** Visibility half RESOLVED (2026-05-14, post-Slice-7 surgical fix): `SummarizerResult` now carries `input_truncated` + `original_chars`/`used_chars`/`dropped_chars`, and the summarize step's `step.completed` event records the same keys additively in `structured_data` (only when truncation actually fired — no false-positive keys on under-limit input). Live-smoke confirmed on https://en.wikipedia.org/wiki/Anthropic: 45,079 → 12,000 chars, dropped 33,079, run still COMPLETED, signal visible end-to-end. Same instinct as `LLMResult.stop_reason` (Slice 2) applied to the INPUT side. What remains: whether to chunk-and-stitch (summarise sections, then summarise the summaries) so a heavy page is actually fully covered rather than just transparently truncated — that is a behaviour/cost change, ADR-worthy, and queued for the Phase 2 Slice 1 crew-architecture ADR. The MAX_PROMPT_CHARS *value* (12,000) is a separate tuning question; the visibility data this fix generates is what should inform it. Source: Slice 7 live calibration finding #1, recorded in README; visibility commit references this entry.
- **`run_step()` utility — IMPLEMENTED 2026-05-14.** `src/agent_habitat/orchestration/run_step.py` ships the `StepRecorder` dataclass + `run_step()` context manager exactly per ADR-006 §2. Summarizer retrofitted onto it in the same commit; 20 deterministic tests in `tests/test_run_step.py`; all 196 deterministic tests pass; live smoke confirmed. Cosmetic trim (docstring, section dividers, WORKFLOW_TYPE/AGENT_NAME inlined) rode along. summarizer.py: 645 → 391 lines.

## Last Session
**Pre-orchestrator refactor — Layer A / Layer B split across the four
built agents.** Opus 4.7 high, behavior-preserving refactor (working,
tested code; bar = provably unchanged behavior, not "it works"). Four
files reorganised; zero edits to the four existing agent test suites;
13 new deterministic tests added for the pure Layer A node functions;
one live summarizer round-trip + Wikipedia truncation smoke green
through the refactored path.

**The fused shape was the problem.** Each `run_<agent>` previously
owned the entire workflow: `insert_workflow` + `run_step` + LLM call +
parse/ground + `update_workflow` + `recompute_cost_total` + the
WORKFLOW_COMPLETED/FAILED event. That is correct for a standalone CLI
command but the wrong shape for LangGraph — the orchestrator wants
NODE functions (pure functions taking typed input, returning typed
output + telemetry to record). Without this refactor, Slice 6 would
either duplicate heavily or force a painful four-agent refactor AFTER
the graph was written.

**The split — same-file siblings, no new modules.** Each agent file now
exposes a `<name>_node(...)` returning a frozen `<Name>NodeOutput`
dataclass (`raw_signals|profile|scored_company|summary`, `cost_usd`,
`output_ref`, `structured_data`). The pure node:
  - takes typed upstream input(s) + `workflow_id` (for `llm.complete()`
    telemetry attribution only) + optional `log_root` + optional `now`.
  - calls `llm.complete()`, parses, applies grounding checks.
  - does NOT touch the database, NOT call `run_step`, NOT
    insert/update workflows.
  - raises on infrastructure error (the wrapper's job to convert
    into a FAILED step).
The standalone `run_<name>(...)` is now a thin wrapper: insert_workflow
→ emit `workflow.started` → `with run_step(...) as step: out =
<name>_node(...); step.record_cost(out.cost_usd); [maybe
step.record_output_ref(out.output_ref);] step.record_structured_data(
out.structured_data)` → finalise COMPLETED/FAILED. Same signature, same
return type (`<Name>Result`), same persistence behaviour.

**Summarizer treated for consistency, not because it's a crew node.**
The summarizer is a 3-step intra-agent workflow (fetch / parse /
summarize), not a candidate LangGraph node — its Layer A is the three
pure work functions: `fetch_url` (already), `extract_readable_text`
(already), and the new sibling `summarize_text(...) ->
SummarizerNodeOutput`. The wrapper preserves three `with run_step(...)`
blocks so the per-step audit shape is unchanged; the test that asserts
`[s.agent_name for s in steps] == ["fetch", "parse", "summarize"]`
passes verbatim.

**The empty-input short-circuits stay inside Layer A.** Extractor's
zero-signals branch (no LLM call, $0 cost, all-gaps profile,
`output_ref=None`) and the scorer's no-scorable-fields branch (all-
excluded ScoredCompany, no LLM call, `output_ref=None`) are properties
of the agent logic, not of workflow plumbing. They sit on Layer A, with
the wrapper's `if node_out.output_ref is not None: step.record_output_ref(...)`
preserving exact behaviour.

**Correctness oracle: the four existing test files were not edited.**
The 179 existing tests (researcher 16 + extractor 53 + scorer 80 +
summarizer 30, including 4 live smokes that ran with the API key set)
all pass unchanged. Test patch targets (`patch.object(<agent>_mod,
"complete", ...)`) work as-is because `complete` is still a module-level
name in each agent file. Test imports (`AGENT_NAME`, `WORKFLOW_TYPE`,
the various private helpers like `_normalise_for_substring`,
`_ground_profile`, `_compute_composite`, etc.) all still resolve at the
same locations. `cli.py` is untouched.

**13 new Layer A tests in `tests/test_node_pure.py`** — each pure node
function called without a `sqlite3.Connection`, with `complete()`
patched, asserts: typed output, telemetry tuple shape, empty-input
short-circuit (no LLM call for extractor/scorer), infrastructure-error
propagation. Proves the layer is genuinely pure of the database.

**Slice 6 (orchestrator) now inherits a clean wrap point.** Each Phase
2 agent's LangGraph node will be roughly:
```python
def researcher_node_lg(state: CrewState) -> dict:
    with run_step(conn, workflow_id=state["workflow_id"],
                  step_index=1, agent_name="researcher") as step:
        out = researcher_node(company_name=state["company_name"],
                              workflow_id=state["workflow_id"])
        step.record_cost(out.cost_usd)
        if out.output_ref is not None:
            step.record_output_ref(out.output_ref)
        step.record_structured_data(out.structured_data)
    return {"raw_signals": out.raw_signals}
```
No re-derivation of the prompt, parse, grounding, or projection — those
live on the pure node. The Phase 2 §1 `CrewState` `TypedDict` and the
graph wiring are still Slice 6's job (this refactor explicitly does
NOT introduce CrewState as live code or any LangGraph imports — that
is the next slice).

**Split was clean for all four agents — no STOP-flag.** The Layer A /
Layer B boundary fell naturally on each: prompt-construction +
`complete()` + parse + ground + projection-build on one side; workflow
envelope + `run_step` audit on the other. The summarizer's three-step
shape is faithfully preserved by keeping three `with run_step` blocks
in the wrapper rather than collapsing the three steps into one.

File outputs: `src/agent_habitat/agents/researcher.py` (extracted
`researcher_node` + `ResearcherNodeOutput`),
`src/agent_habitat/agents/extractor.py` (extracted `extractor_node` +
`ExtractorNodeOutput`), `src/agent_habitat/agents/scorer.py` (extracted
`scorer_node` + `ScorerNodeOutput`), `src/agent_habitat/agents/summarizer.py`
(extracted `summarize_text` + `SummarizerNodeOutput`),
`tests/test_node_pure.py` (NEW — 13 deterministic Layer A tests).
`tests/test_researcher.py`, `tests/test_extractor.py`,
`tests/test_scorer.py`, `tests/test_summarizer.py` — UNCHANGED.
`src/agent_habitat/cli.py` — UNCHANGED. `src/agent_habitat/orchestration/run_step.py`
— UNCHANGED. `STATUS.md` updated.

No `pip install`. No LangGraph imports. No new orchestration modules.
No ADR-002 / ADR-006 / `llm.py` changes. The shared-models surface
(`agents/models.py`) is untouched — only `agents/__init__.py` continues
to re-export the public Result/Error types as before.

Per-file diff (insertions / deletions):
- `agents/researcher.py`: +92 / -32
- `agents/extractor.py`:  +92 / -23
- `agents/scorer.py`:     +108 / -36
- `agents/summarizer.py`: +93 / -42
- `tests/test_node_pure.py`: NEW (~340 lines)

## Prior Session
**Phase 2 Slice 4 — Scorer agent + `ScoredCompany` model + rubric
loader.** Opus 4.7 high, slice implementation against ADR-004 as
the blueprint. Built the Sonnet-tier Scorer consuming a
`CompanyProfile` and a loaded `RubricConfig`, producing a
`ScoredCompany` with renormalise-with-coverage scoring. Three new
modules + extensions to two existing ones:

- `agents/models.py` extended with `DimensionScore` (per-dimension
  carrier with scored↔excluded mutual-exclusivity validator) and
  `ScoredCompany` (composite + dimensions list + tier-iff-score
  invariant). Both `extra="forbid", frozen=True`.
- `scoring/rubric.py` (new) + `scoring/__init__.py` — rubric loader
  via stdlib `tomllib` mirroring `budget/config.py`'s shape.
  Validates weights-sum-1.0 (epsilon 1e-9), field validity against
  `PROFILE_FIELD_NAMES`, tier ordering, `missing_data_policy ==
  "renormalise"`, required defaults keys, duplicate-field rejection.
  Operator-debuggable errors path-named.
- `agents/scorer.py` (new) — `run_scorer(conn, *, profile, rubric,
  ...)`, one Sonnet call (schema-in-system-prompt + Pydantic v2 parse,
  same shape as Slice 3), `run_step()` per ADR-006 §2. Pure helpers
  (`_compute_composite`, `_assign_tier`, `_gating`,
  `_grounded_in_field`, `_assemble_dimensions`, `_all_excluded`).
  All-gaps profile / no-scorable-fields short-circuits to all-excluded
  ScoredCompany with no LLM call.
- `cli.py` — `run-scorer` command sequencing Researcher → Extractor →
  Scorer (three separate workflows for now; Slice 6 unifies via
  orchestrator); `SCORER_DECISION_FOOTER` surfaces coverage
  explicitly.

**Grounding-chain extension landed cleanly.** Per-dimension
`grounded_quote` substring-matches one of the cited `ProfileField`'s
`source_spans[].quote` values (whitespace-collapse + lowercase),
REUSING Slice 3's `_normalise_for_substring` by import — the kickoff
constraint held. Over-reach downgrades the dimension to excluded
(same shape as Slice 3's `_ground_field` downgrades a field to gap).
A test (`test_check_uses_slice3_normaliser`) pins the import identity
so future drift is caught.

**ADR-004 spec implemented as written, no improvisation.** The
all-gaps behaviour follows ADR-004 §5 Forward deps exactly
(`score=None`, `coverage=0.0`, `passed_floor=False`,
`gated_by="coverage"` if `min_coverage > 0` else `"score"`). The
grounding-failure behaviour is "same shape as Slice 3's
`_ground_field`" per ADR-004 §3 — downgrade-to-excluded, the direct
analog of Slice 3's downgrade-to-gap. Coverage-precedence on the two
gates makes a low-coverage run the more informative empty-outcome,
matching ADR-004 §2's "calibration story can tell the two
empty-outcomes apart."

**79 deterministic tests + 1 live smoke.** Coverage: rubric loader
(15 — valid + every documented failure mode incl. weights-sum-not-1,
invalid field, mis-ordered tiers, missing defaults keys, invalid
policy, no dimensions, duplicate fields, bundled-file sanity); model
validation (15); pure scoring math + gating (12); grounding check (6,
including the normaliser-identity check); end-to-end agent (16:
happy + projection + persistence; all-gaps no-LLM-call;
below-floor / below-coverage COMPLETED-not-failed; grounding
downgrade; code-fence stripping; 5 infrastructure-failure paths);
CLI (5: happy three-stage + 3 failures + missing rubric). All pass;
ruff check + ruff format + mypy strict all clean. Full suite (357
deterministic + 1 scorer live smoke) green.

**Live smoke caught real calibration data.** Anthropic as the
subject; 18s wall-time for the three-agent chain;
$0.046 combined cost (Scorer $0.0097 first real number); score
82.86, coverage 70%, tier A. Headline finding: **the
score-vs-coverage spread is the structurally honest disclosure
ADR-004 was designed to surface.** Score 82.86 (Tier A) on its own
would read as "top band"; coverage 70% reveals the rubric was only
judging 70% of the operator's stated ICP dimensions. The collapsed-
composite the ADR rejected would have flattened this to 58/100,
losing the signal. Five-hop grounding chain traceable end-to-end on
the real call. NO grounding-downgrades caught on first contact — one
data point, watch on later runs (Scorer's grounding corpus is
tighter than Extractor's by construction, so over-reach is
structurally harder; the substring check is load-bearing in case it
occurs, not because it always does).

File outputs: `src/agent_habitat/agents/scorer.py` (new),
`src/agent_habitat/scoring/__init__.py` (new),
`src/agent_habitat/scoring/rubric.py` (new),
`src/agent_habitat/agents/models.py` (+ `DimensionScore` /
`ScoredCompany` / `TIER_VALUES` / `GATED_BY_VALUES`),
`src/agent_habitat/agents/__init__.py` (export surface updated),
`src/agent_habitat/cli.py` (`run-scorer` command +
`SCORER_DECISION_FOOTER` + formatter), `tests/test_scorer.py` (79
deterministic + 1 live smoke), `STATUS.md` updated.

No `pip install`. No `llm.py` / `run_step.py` / ADR-002 /
`RawSignals` / `CompanyProfile` changes; Slice 3's normaliser
imported, not modified. No `budgets.toml` / `rubric.toml`
operator-config edits (the bundled `config/rubric.toml` is the
ADR-004 format template; operator tunes it).

## Prior Session
**ADR-004 — ICP rubric format and missing-data handling.** Fresh
session, Opus high, ADR-only. Settled three coupled decisions: (1)
the TOML wire format mirrors `config/budgets.toml`'s idiom — no
second config style in the project; (2) the load-bearing
missing-data policy is renormalise-with-coverage — a gap-shaped
`ProfileField` excludes its dimension from the score and drops the
weight from the denominator, and a parallel `coverage` number rides
alongside `score`; (3) the grounding chain extends through the
Scorer via a `grounded_quote` substring check, re-using Slice 3's
normaliser. ADR-006 §1's floor gates on `score`; `min_coverage`
is an OPTIONAL second gate (default off). Scoring mechanism:
LLM-as-judge against operator-authored `prose` rubric per
dimension, audit-grade posture preserved by the grounding chain.

Honest limitation recorded in the ADR: the rubric is an
un-validated operator hypothesis (no CRM, no closed-won dataset).
The substitute for the industry's "tune against 50 closed-won
deals" step is Slice 8 calibration.

File outputs: `docs/adr/ADR-004-icp-rubric-format.md` (new),
`docs/adr/README.md` (ADR-004 flipped Proposed → Accepted),
`config/rubric.toml` (clearly-marked format template, NOT a tuned
rubric), `STATUS.md` updated. No code changes; no `pip install`.
No `llm.py` / `run_step.py` / `agents/` / `budgets.toml` / ADR-002
/ ADR-006 changes — the existing contracts hold.

Phase 2 Slice 4 (Scorer) is UNBLOCKED and is the next session's
work.

## Prior Session
Phase 2 Slice 3 — Extractor agent + `CompanyProfile` model + ExtractionGap
pattern + source-span grounding. Built `agents/extractor.py` and extended
`agents/models.py` with `SourceSpan` / `ExtractionGap` / `ProfileField` /
`CompanyProfile`; wired through `run_step()` per ADR-006 §2; added
`run-extractor` CLI; 52 deterministic tests + one live smoke that passed
on the first run and caught two real over-reaches in the wild.

**Structured-output method chosen: schema-in-system-prompt + Pydantic
`model_validate_json(LLMResult.content)`.** No `llm.py` changes — the
two API-level alternatives (`messages.parse()` and strict tool use with
`tool_choice`) both would have required them. `ConfigDict(extra="forbid")`
on every Slice 3 model makes the schema strict on parse; the Extractor
treats malformed JSON / schema mismatch as infrastructure failure (no
retry), same shape as the Researcher's API-exception path.

**The substring grounding validator works.** The most important Slice 3
finding came from the live smoke: the Sonnet model proposed extractions
for `industry` and `tech_stack` with `source_spans` whose `quote` did
not actually substring-match the cited `raw_signals.signals[idx].text`.
The `_ground_field` validator caught both and downgraded them to
`ExtractionGap(reason="span_not_grounded")`. This is the
fabrication-resistance contract working on real data on the first call —
the design instinct is validated in the wild, not just in a mocked unit
test. ADR-003 addendum's short-span forward-dependency for Slice 3 is
RESOLVED with this validator (still open for Slice 7's Critic, which
runs the equivalent check in the drafter→critic direction).

**Calibration: Extractor cost is $0.009/run on real Researcher output**,
~14x cheaper than the Researcher's $0.065/run on the same input. Total
Researcher+Extractor: $0.073. Wall-time: 5.64s for the Extractor, 14s
combined. Field extraction rate on a 5-signal news-heavy input: 1/5
(20%) extracted, 4/5 (80%) gaps — of which 2 are model-honest gaps and
2 are over-reach catches. Full calibration table in the "Phase 2 Slice 3
Live Smoke Calibration" section above.

File outputs: `src/agent_habitat/agents/extractor.py` (new),
`src/agent_habitat/agents/models.py` (extended with the 4 new Pydantic
models + `PROFILE_FIELD_NAMES`), `src/agent_habitat/agents/__init__.py`
(export surface), `src/agent_habitat/cli.py` (`run-extractor` command +
`EXTRACTOR_DECISION_FOOTER` + formatter), `tests/test_extractor.py`
(52 deterministic + 1 live smoke), `STATUS.md` updated. Full suite: 278
deterministic + 1 extractor live smoke passes; ruff check + ruff
format --check + mypy strict all clean. No `pip install`. No `llm.py` /
`run_step.py` / ADR-002 / `RawSignals` changes.

## Prior Session
ADR-003 Addendum — `cited_text` grounding correction + per-Researcher-run cost
recalibration. **Documentation/decision only — zero code changes.**

Appended an "Addendum (2026-05-14): `cited_text` grounding + cost recalibration"
section to `docs/adr/ADR-003-web-search-tool.md`. The original ADR-003 Decision
(Anthropic `web_search` server-side tool, single SDK call through `llm.py`, no
second client) is unchanged; the addendum corrects the *mechanism* and the
*cost estimate* against live evidence from Phase 2 Slice 2's first real
`web_search` call.

The corrected mechanism: ADR-003 said "persist the raw `search_result` block
text into `RawSignals.signals[].text`." That premise cannot hold literally —
`web_search_tool_result.content[i].encrypted_content` is opaque (Anthropic
designs it for multi-turn round-trip, not client persistence). The
plain-readable, substantively-equivalent grounding corpus is
`CitationsWebSearchResultLocation.cited_text` — verbatim source spans the
model surfaces via citations, each tied to source URL + title. The as-built
code already does the right thing: `llm.py::_extract_web_search_citations`
walks `TextBlock.citations`; `agents/researcher.py` builds each `Signal` from
`Citation.cited_text`. The addendum documents this as the corrected ADR-003
mechanism.

The three grounding questions answered explicitly: (1) `cited_text` is reliably
present on every citation the SDK surfaces (required field on
`CitationsWebSearchResultLocation`); zero-citation responses correctly produce
an empty `RawSignals` and a COMPLETED workflow per ADR-006 §1. (2) Span length
is what the model chose to quote — could be short. A short span is a *tighter*
grounding corpus, not a broken one; the substring check is well-formed for any
non-empty span. Forward dependency recorded for Slice 3 / Slice 7. (3) ADR-006
§3's substring-check mechanism works AS WRITTEN against `cited_text` spans;
**no ADR-006 amendment is required**. Also explicitly ratified: a model claim
made without a citation produces no Signal — the right fabrication-resistance
semantics, now on the record.

The cost recalibration: ADR-003 estimated $0.035–$0.045 per Researcher run;
live smoke measured **$0.066871** for a 3-search run, ~50% higher. Real
breakdown: $0.034971 input (34,971 tokens, ~10× the estimate) + $0.0019 output
(380 tokens) + $0.030 fee (3 × $0.01). Driver: search-result content is
injected into the model's context as it reasons across searches, not billed as
a flat snippet load. The addendum flags exactly two downstream re-checks: (a)
`config/budgets.toml` `lead_enrichment` cap ($10/day) needs re-validation
against Slice 8's calibrated end-to-end numbers; (b) ADR-006 §1's
checkpoint-rationale prose ("upstream chain ~$0.01–$0.02") understates upstream
cost by ~5× and needs updating when Slice 8 data lands. **Neither change was
made this session.**

What still stands: the core ADR-003 decision (Anthropic `web_search`, single
SDK call, `LLMResult.cost_usd` aggregation, no second client) and all four
reasons given for it. The corrected mechanism preserves every structural
property — same SDK call, same single writer, same one-source-of-truth for the
substring check, same bounded vendor-lock-in blast radius.

File outputs: `docs/adr/ADR-003-web-search-tool.md` (Status line updated +
Addendum section appended), `docs/adr/README.md` (ADR-003 index row notes the
addendum + the recalibrated cost), `STATUS.md` (two Open Questions resolved;
two precise forward-dependencies recorded for Slice 8 and a §1 prose re-check;
"Last Session" + "Next Session Entry Point" updated).

No code changes. No `pip install`. No tests run — the addendum documents the
as-built behaviour; the test suite has been clean since the Slice 2 commit
(eac285c) and was not touched. Working tree before this session: clean except
for an untracked `probe_web_search.py` (the live-probe artefact from Slice 2,
left intentionally for reference; not part of this commit).

## Prior Session
Phase 2 Slice 2 (remainder) — Researcher agent + `llm.py` `tools=` passthrough +
`RawSignals` model.

Extended `llm.py` additively: optional `tools=` parameter on `complete()`
forwarded unchanged to `messages.create`; `compute_cost_usd` extended with
`web_search_requests=0` kw-only and adds `n × $0.01` server-tool fee onto
`cost_usd` so `LLMResult.cost_usd` stays the single aggregated cost figure
the budget primitives and `run_step` consume; dated `_WEB_SEARCH_FEE_USD = 0.01`
constant ("NEEDS VERIFICATION against current Anthropic pricing"); JSONL
telemetry record gains additive `web_searches` / `web_search_fee_usd` keys
**only when** `tools` is provided — ordinary no-tools calls keep their record
shape exactly as before. `LLMResult` gains additive `web_searches: int = 0`
and `citations: list[Citation] = []`; existing fields and the contract are
untouched. New `Citation` Pydantic v2 model (`cited_text`, `source_url`,
`source_title`) — surfaces `CitationsWebSearchResultLocation` blocks from
the response in a typed shape.

Built `src/agent_habitat/agents/models.py` with `Signal` (frozen v2 model,
`text` + `source_url` + `source_title` + `retrieved_at`) and `RawSignals`
(frozen v2 model, `company_name` + `signals: list[Signal]` plus derived
`signal_count` and `source_count` properties). The `Signal.text` field is
sourced from `Citation.cited_text` — verbatim source prose surfaced by
Anthropic's web_search tool, NEVER the model's own narrative phrasing.
This is the upstream half of ADR-006 §3's fabrication-resistance grounding
invariant.

Built `src/agent_habitat/agents/researcher.py`: `run_researcher(conn, *,
company_name, max_searches=3, ...)` opens a `lead_enrichment_researcher`
workflow, wraps one `llm.complete()` call (Haiku tier, `tools=[web_search_20250305]`)
through `run_step()` per ADR-006 §2, builds `Signal` records from
`LLMResult.citations[].cited_text`, mirrors `{signal_count, source_count,
web_searches}` onto `step.completed`'s structured_data, and finalises
`workflow.completed` with the cost rolled up. Empty signals → COMPLETED
(ADR-006 §1 empty-outcome contract: empty is a valid result, not a
failure). Infrastructure errors propagate from `run_step` → step FAILED →
workflow FAILED with `finished_at` stamped → `workflow.failed` event
emitted; no retries (ADR-006 §1, Slice 1). The Researcher never raises to
its caller; the caller gets a `ResearcherResult` carrying status, signals,
cost, and error context.

CLI surface: `agent-habitat run-researcher COMPANY_NAME [--db PATH]
[--workflow-id ID] [--max-searches N]`. Reuses the `--db` pattern. Prints
workflow id + status + cost + each surfaced signal (title + URL + first
~200 chars of cited prose), footer-disclaimed for decision-support framing
(CLAUDE.md non-obvious constraint). Failed runs exit non-zero.

Tests: 16 deterministic in `tests/test_researcher.py` (RawSignals model
validation/round-trip; happy run persists workflow + step + events; cost
rolled up + output_ref set + projection mirrored; empty-signals run
produces a structured empty-but-typed `RawSignals` and a COMPLETED (not
FAILED) workflow; LLM error path finalises step FAILED + workflow FAILED
without escaping the exception; CLI happy + failure + `--max-searches`
override). 10 new deterministic tests in `tests/test_llm.py` cover the
`tools=` passthrough surface (server-tool fee aggregation, citation
extraction, additive JSONL keys, no-tools-call-unaffected contract).
Full suite: 227 deterministic tests pass; ruff check + ruff format
--check + mypy strict all clean.

**THE KNOWN WRINKLE landed and was resolved honestly.** ADR-003's stated
premise — "persist the raw search_result block text" — does not hold
against the actual API response shape: per-result `encrypted_content` is
opaque. The honest grounding corpus is `citations[].cited_text` (verbatim
source spans the model chose to cite), and that is what `RawSignals.text`
is built from — recorded as an Open Question for an ADR-003 addendum
(see Open Questions section above).

One live smoke against `Anthropic` ran successfully: 5.44s wall-time,
$0.066871 cost (3 searches × $0.01 fee + 34,971 input tokens + 380
output tokens on Haiku), 4 signals across 3 sources, full audit chain
resolves end-to-end including the additive JSONL telemetry keys. Real
calibration data captured (see "Phase 2 Slice 2 Live Smoke Calibration"
section above): cost estimate updated to ~$0.07/run (ADR-003 was low by
~50%), max_uses cap is the operator's real per-run budget knob, signal
quality is honestly verbatim Bloomberg + PYMNTS source prose. First
real exercise of `tools=` through `llm.py`; the additive contract worked
first try.

## Prior Session
Phase 2 Slice 2 (partial) — `run_step()` extraction + summarizer retrofit.

Extracted `src/agent_habitat/orchestration/run_step.py` as the shared step
lifecycle context manager per ADR-006 §2. `StepRecorder` dataclass yields from
`run_step()` with three recording verbs (`record_cost`, `record_output_ref`,
`record_structured_data` — additive, last-write-wins). Lifecycle contract:
open RUNNING step row → emit step.started → yield recorder → on normal exit
finalise COMPLETED with accumulated values and emit step.completed (standard
keys: step_name, step_index, cost_usd, output_ref when present, merged with
recorder's structured_data); on exception finalise FAILED with
`error_message=f"{type(exc).__name__}: {exc}"`, emit step.failed, re-raise.
No LangGraph dependency. 20 new deterministic tests in `tests/test_run_step.py`
cover all three branches (normal, exception, recorder verbs).

Retrofitted `src/agent_habitat/agents/summarizer.py` onto the new utility.
`_run_step` / `_run_summarize_step` (~188 lines) collapsed into three `with
run_step(...)` blocks (~40 lines). Cosmetic trim rode along: module docstring
trimmed from 35 → 8 lines; 6 section dividers removed; WORKFLOW_TYPE /
AGENT_NAME inlined as literal strings; `agents/__init__.py` updated to drop the
now-absent re-exports. summarizer.py: 645 → 391 lines (-254 lines, -39%).

All 28 existing summarizer tests pass UNCHANGED — proving the retrofit preserved
behaviour. Live smoke against https://example.com/ completed in 2.91s,
cost=$0.001257, full audit chain (workflow → step → JSONL telemetry) resolves
end-to-end via the retrofitted path. Full suite: 196 deterministic tests pass;
ruff check + ruff format + mypy strict all clean.

## Prior Session (ADR-003)
ADR-003 — web search tool for the Researcher agent. Chose **Anthropic's built-in
`web_search` server-side tool**, enabled on the Researcher's single Haiku call
through `llm.py`. Three alternatives considered at their best and rejected for
THIS project (not in general): Tavily (LLM-tuned snippets but sits beside
`llm.py` as a second client; dual source of truth between API response and
`RawSignals` breaks the fabrication-resistance grounding invariant unless an
alignment check is added), Brave (cheapest but same structural objection PLUS
results are raw search-engine descriptions rather than LLM-ready spans —
requires a second LLM/`web_fetch` round-trip), and custom httpx+BS4 (has no
search backend; it's a fetch-and-parse layer, not a search-tool decision).

Architectural placement: `llm.complete()` gains a `tools=` passthrough (Slice 2
scope to design the exact surface); `compute_cost_usd` extends to add
`num_web_searches * $0.01` onto `LLMResult.cost_usd`; JSONL telemetry gains
additive `web_searches` / `web_search_fee_usd` keys. The Researcher captures
`search_result` content blocks from the response verbatim into
`RawSignals.signals[].text` — the exact prose the model read becomes the corpus
the drafter's substring check grounds against (ADR-006 §3 contract holds by
construction, not by reconciliation). Cost lands on `workflow_steps.cost_usd`
via the existing `run_step()` + `step.record_cost(result.cost_usd)` path; no
second writer, no new schema. Realistic per-Researcher-run cost: $0.035–$0.045
(~3 searches × $0.01 fee + Haiku tokens). Tradeoff accepted: vendor lock-in to
Anthropic's search backend — bounded blast radius because the Researcher is
encapsulated and its handoff contract is `RawSignals`, not the search tool.

Verified current pricing 2026-05-14: Anthropic `web_search` $10/1K searches +
token costs; Tavily $0.008/search PAYG (1K/mo free); Brave $5/1K ($5/mo credit,
legacy free tier deprecated Feb 2026). Numbers stamped in ADR-003 with the same
"verify before trusting" discipline the existing rate-table Open Question
applies. Forward dependency handed to Slice 2: extend `llm.py` `tools=`
passthrough + cost aggregation; build `RawSignals` from `search_result` content
blocks (not from the model's narrative text); land
`orchestration/run_step.py` and retrofit the summarizer through it in the same
diff (ADR-006 §2 forward dependency).

No product code changed. ADR-003 promoted from Proposed to Accepted in
`docs/adr/README.md`. STATUS.md updated to reflect Phase 2 Slice 2 fully
unblocked.

## Prior Session
Slice 7 — multi-URL live calibration + Phase 1 README. Ran the URL summarizer
against five deliberately varied real URLs (Wikipedia article with `<main>`,
Python docs with no semantic tags, PEP 20 with `<article>`, sparse example.com,
controlled httpbin 404), captured a clean per-run dataset from the persisted
workflow + JSONL telemetry, synthesised findings, and rewrote `README.md` as a
hiring-manager-facing Phase 1 showcase grounded in those real numbers — full
dataset table + the calibration-story section (what live runs taught us that
mocks couldn't). No product code changed; all quality gates rerun clean (174
deterministic tests, ruff check, ruff format, mypy strict). Total live-API
spend for the sweep: ~$0.037 across five runs.

Key calibration outcomes: (1) cost spans 14× across page types and is
input-dominated above ~500 tokens — fixed-cost floor pattern from Slice 6
holds and is now quantified at ~$0.0012/run; (2) `MAX_PROMPT_CHARS=12_000`
truncation fires silently on heavy real-world pages (Wikipedia, Python docs)
— recorded as a new Open Question; (3) all three parse-strategy branches got
real coverage including the previously-unseen `<article>` branch; (4) latency
is LLM-bound at 92–94% of wall time; (5) example.com run-to-run variance is
tight (input deterministic, output ±10%); (6) failure contract held in the
wild on the 404; (7) budget cap of $2/day for `url_summarizer` is recommended
to STAY — now calibrated against real heavy-page cost ($0.01683 max) rather
than extrapolated from the trivial-page floor.

Phase 1 is shippable. Five subsystems (LLM wrapper, state, observability,
budget, checkpoint) each independently exercised by tests and live runs; one
demo agent threads them end-to-end; the audit chain `workflow → step →
telemetry record` resolves on every successful run; the failure path holds.

Implemented `src/agent_habitat/agents/summarizer.py` — the Slice 6 demo agent. Three synchronous steps (fetch / parse / summarize) hand-wired through the existing habitat: workflow + step rows via Slice 2's persistence, lifecycle and step events via Slice 4's `emit_event` over the canonical `EventType` taxonomy, the LLM call routed through `llm.py` (Sonnet tier — the kickoff prompt overrode STATUS.md's earlier "Haiku" note), and `recompute_cost_total` rolling the summarize step's real cost up onto `workflows.cost_total_usd`. The summarize step's `output_ref` points at the JSONL line `llm.py` wrote, so the audit chain `workflow → step → telemetry record` resolves end-to-end on the first real workload.

Failure-path contract: any step error transitions the workflow to `FAILED` with `finished_at` stamped, emits a `step.failed` + `workflow.failed` pair, and returns a `SummarizerResult(status=FAILED, error_step, error_message)`. The agent never crashes uncaught from the caller's perspective, never leaves a workflow stuck in `RUNNING`. Exercised in tests across four real failure modes (bad URL scheme, 404, httpx network error, empty/SPA page) plus an injected LLM error — each one verifies the workflow row, the step row, and the event sequence.

CLI surface: `agent-habitat run-summarizer URL [--db PATH] [--workflow-id ID]`. Reuses the Slice 5 `--db` pattern. Prints workflow id + status + cost + the summary, footer-disclaimed for decision-support framing (CLAUDE.md non-obvious constraint). Failed runs exit non-zero.

Explicitly out of scope: no LangGraph (that's Phase 2), no checkpoint invocation (the summarizer has no flagged actions; Slice 5's system exists but isn't called), no active budget enforcement (cost is *recorded* so the existing budget primitives can find it — actually halting on exceed is the orchestrator's job). Per the kickoff prompt's hard stops, no new orchestration machinery was invented to make pieces fit.

Tests: 26 deterministic in `tests/test_summarizer.py` (httpx `MockTransport` for fetch, `unittest.mock.patch` for the LLM call); full suite (175 tests including the new live smoke) passes; ruff check + ruff format + mypy strict all clean. One live smoke ran successfully against https://example.com/ — observations recorded under "Slice 6 Live Smoke Calibration" above.

Implemented `src/agent_habitat/checkpoint/` — the CheckpointSystem. One package, one core module (`system.py`) plus the public-API `__init__.py`. The Slice 5 surface is six functions, one frozen dataclass, one enum, one exception:

  `request_checkpoint(conn, *, workflow_id, action, summary, proposed_payload=None, step_id=None, requested_by=None, now=None) → Checkpoint`
  `approve_checkpoint(conn, checkpoint_id, *, reviewer, note=None, now=None) → Checkpoint`
  `reject_checkpoint(conn, checkpoint_id, *, reviewer, reason=None, now=None) → Checkpoint`
  `get_checkpoint(conn, checkpoint_id) → Checkpoint | None`
  `list_pending_checkpoints(conn, workflow_id=None) → list[Checkpoint]`
  `is_workflow_paused_for_checkpoint(conn, workflow_id) → bool`

Pending-approval storage decision: ADDITIVE on ADR-002's events table — no schema change. The `checkpoint.requested` event's `events.id` IS the checkpoint id; resolution events (`checkpoint.approved` / `checkpoint.rejected`) carry `structured_data.checkpoint_id` back-referencing the request. A checkpoint is pending iff no resolution row references it — expressed in SQL via `NOT EXISTS (SELECT 1 FROM events e2 WHERE json_extract(...) = e1.id)`. Same idiom Slice 3 uses for `is_workflow_halted_by_budget`. ADR-002 already supports this shape (Consequences section explicitly named "Slice 5 (CheckpointSystem)" as the prototype for `level='approval'` rows over the events table), so no addendum was needed.

Workflow state machine: `request` flips `workflows.status` to PAUSED; `approve` flips it back to RUNNING; `reject` flips it to CANCELLED with `finished_at` stamped (terminal). Halt-signal query (`is_workflow_paused_for_checkpoint`) consults only the events table, not `workflows.status` — same pattern as the budget halt-signal, so the signal stays correct even if a status update lags. One pending checkpoint per workflow is enforced at the API boundary: a second request on a still-paused workflow raises `CheckpointError`. Multi-pending semantics would force a design choice about which resolution drives the workflow status transition; serialised resolution is the simpler, audit-clearer shape and matches the "halt the workflow until the human decides" framing.

Slice 5's new work is the checkpoint LOGIC + the CLI; actually pausing or resuming a *running* graph mid-execution remains the orchestrator's job (Phase 2). A workflow with an unresolved checkpoint reads as not-runnable — the orchestrator (when it lands) will call `is_workflow_paused_for_checkpoint` before scheduling each step and obey it. Slice 5 establishes the fact; the orchestrator obeys it later.

CLI surface in `src/agent_habitat/cli.py`:

  `agent-habitat checkpoint [--db PATH] list [--workflow ID]`
  `agent-habitat checkpoint [--db PATH] show CHECKPOINT_ID`
  `agent-habitat checkpoint [--db PATH] approve CHECKPOINT_ID --reviewer NAME [--note TEXT]`
  `agent-habitat checkpoint [--db PATH] reject  CHECKPOINT_ID --reviewer NAME [--reason TEXT]`

`--db` defaults to `DEFAULT_DB_PATH` (the production `data/state/agent_habitat.db`). `--reviewer` is required on approve/reject — every decision lands with a name on it for audit. `list` renders a per-checkpoint two-line block (id + workflow + action + requested-at + requester, then the summary). `show` renders a full detail card with the proposed payload pretty-printed as JSON; pending checkpoints include the project's decision-support footer ("operational context for your approval decision, not legal/medical/financial advice — verify before approving"). Resolved checkpoints render the resolution + reviewer + resolved_at + note. `CheckpointError` (unknown id, terminal workflow, already-resolved, already-pending) surfaces as a clean `click.ClickException` with a non-zero exit and no traceback noise.

Audit shape on the events table — every checkpoint emits two or three rows total:
- `checkpoint.requested` at level CHECKPOINT, payload `{event_type, action, summary, proposed_payload?, requested_by?}`.
- `checkpoint.approved` OR `checkpoint.rejected` at level APPROVAL, payload `{event_type, checkpoint_id, reviewer, note?}`.

This is the first time the CHECKPOINT and APPROVAL `EventLevel`s are written (Slice 4 added the taxonomy + level guide; Slice 5 is the first writer). The `EventType.CHECKPOINT_REQUESTED/APPROVED/REJECTED` members from Slice 4 are used verbatim — not redefined.

Tests: 30 deterministic in `tests/test_checkpoint.py`. Six test classes plus one round-trip:
- `TestRequest` — event-row shape, level, taxonomy; workflow → PAUSED; optional fields omitted when unset; unknown workflow / terminal workflow (parametrised over completed/failed/cancelled) / already-pending all raise.
- `TestResolveApprove` — workflow PAUSED → RUNNING with `finished_at` unchanged; approval event back-references checkpoint id and records reviewer + note.
- `TestResolveReject` — workflow PAUSED → CANCELLED with `finished_at` stamped; rejection event carries checkpoint_id + reason.
- `TestQueries` — `list_pending_checkpoints` filters resolved out and supports workflow filtering; `is_workflow_paused_for_checkpoint` mirrors the pending flag across request/resolve; `get_checkpoint` returns None for unknown ids and ignores non-checkpoint event ids (defends against passing a `workflow.note` row id).
- `TestResolveErrors` — approve/reject of unknown id raises; double-approve and approve-after-reject raise.
- `TestCLI` (click.testing.CliRunner over a tmp_path DB):
    - `list` empty + `list` with one pending
    - `show` pending renders decision-support footer; `show` resolved renders the resolution block (no footer)
    - `approve` + `reject` happy paths through the CLI, then re-open the DB and verify workflow status + resolution
    - `show` unknown id, `approve` unknown id, `reject` already-resolved id all exit non-zero with the right message
    - `approve` without `--reviewer` exits non-zero (click's own required-option enforcement)
- `test_proposed_payload_json_roundtrip` — nested dicts + lists in `proposed_payload` survive emit_event → SQLite TEXT(json) → load_events → get_checkpoint.

Full suite (149 tests including llm.py, state, budget, observability, checkpoint) passes. ruff check + ruff format --check + mypy strict all clean.

Implemented `src/agent_habitat/observability/` — the ObservabilityLayer. Three files, deliberately proportionate:

`events.py` — unified writer over the existing ADR-002 `events` table. `emit_event(conn, *, workflow_id, event_type, level, message, structured_data=None, step_id=None, timestamp=None)` always stamps `structured_data.event_type` as the first key (rejects caller-supplied `event_type` inside the payload to prevent two-source-of-truth bugs). `EventType` enum is the canonical taxonomy: workflow.{started,completed,failed,cancelled,note}, step.{started,completed,failed}, budget.exceeded (Slice 3 owns the writer, listed here for completeness), checkpoint.{requested,approved,rejected} (Slice 5), agent.fabrication_detected (Phase 2). `EVENT_LEVEL_GUIDE` documents INFO/WARN/ERROR/CHECKPOINT/APPROVAL semantics — the contract every future emitter inherits. `events_of_type()` is the generic `json_extract`-backed read primitive (same pattern Slice 3 uses in `is_workflow_halted_by_budget`).

`logging.py` — central structlog configuration. `configure_logging(level, json_output, stream)` sets processors (merge_contextvars, add_log_level, ISO UTC timestamper, stack/exc renderers) and dispatches to either `ConsoleRenderer` (dev default, no colours) or `JSONRenderer(sort_keys=True)` (production / log shipping). Idempotent — re-call to reconfigure cleanly in tests. `bind_workflow_context(workflow_id, agent_name=None)` and `clear_log_context()` thin-wrap structlog contextvars so every downstream `structlog.get_logger(__name__).info(...)` is auto-tagged. `llm.py` was NOT touched: it already does `log = structlog.get_logger(__name__)` only — no ad-hoc `structlog.configure()` call existed to delete, so "alignment" was a no-op.

`jsonl.py` — thin reader over `llm.py`'s telemetry. `iter_telemetry(workflow_id, log_root, day=None)` yields decoded records in line order, decorating each with `_path` and `_line` so callers can echo a usable output_ref. Day=None walks every dated subdir in calendar order (workflows that span midnight have records under two dirs). `resolve_output_ref(ref)` is the canonical `path:line` resolver. Empty lines silently skipped (mid-write tolerance); malformed JSON, missing files, out-of-range lines, zero-indexed refs, and non-object payloads all raise `TelemetryReadError` with file:line context. Intentionally NO filtering / aggregation / indexing — the trigger to move OFF JSONL (CLAUDE.md deferred list) is "queries harder than ripgrep can handle", so the layer stays grep-friendly by design.

`__init__.py` carries a module-level docstring spelling out the three surfaces and how they relate (`events` table = persistent queryable audit; JSONL = per-LLM-call detail; structlog = operational logs) — the coherence statement the prompt asked for.

Tests: 29 deterministic in `tests/test_observability.py` covering (1) `emit_event` row shape, payload merge, caller-supplied-event_type rejection, string event_type acceptance, explicit step_id/timestamp persistence, `events_of_type` json_extract filtering across multiple workflows and event types, `EVENT_LEVEL_GUIDE` coverage of every `EventLevel`; (2) structlog config: JSON output, console renderer default, contextvars binding, context clearing, level filtering, idempotent reconfigure with autouse `structlog.reset_defaults()` teardown; (3) JSONL reader: single-day + multi-day iteration, missing-workflow/missing-root empty cases, blank-line skipping with correct `_line` numbers, malformed-JSON and non-object-payload errors, `resolve_output_ref` success + every documented failure mode (malformed no-colon, non-integer line, missing path, line-out-of-range, zero-indexed rejected, empty target line) and a round-trip against the `path:line` format `llm.py` actually writes.

Full suite (119 tests including llm.py, state, budget, observability) passes. ruff check + ruff format + mypy strict all clean.

Implemented `src/agent_habitat/budget/` — `config.py` (Pydantic v2 `BudgetConfig`, `load_budget_config()` over stdlib `tomllib`, `cap_for_workflow_type()`, and a `BudgetConfigError` with path-named messages) and `tracker.py` (`utc_day_window`, `cost_within_window`, pure `evaluate_budget`, `check_workflow_budget`, `record_budget_exceeded`, `is_workflow_halted_by_budget`, plus `BudgetCheck` dataclass and `BudgetStatus` enum). Bundled `config/budgets.toml` ships default daily cap = $5.00 with `approaching_threshold = 0.80`, and overrides for `lead_enrichment` ($10) and `url_summarizer` ($2). No new dependency — Python 3.11+ `tomllib`.

Halt-signal representation: an ERROR-level row in the EXISTING `events` table with `structured_data` carrying `{event_type: "budget.exceeded", workflow_type, cost_usd, cap_usd, window_start, window_end}`. The halt query uses SQLite's built-in `json_extract` against `$.event_type`. No ADR-002 schema change was required — the events table's structured_data column was designed for exactly this kind of additive use.

Cost attribution by `workflow_steps.started_at` (the day the step *began* is the day its cost counts). ISO-8601 UTC strings sort correctly as text, so the window query is a direct string range scan — no datetime coercion. Boundaries are inclusive lower / exclusive upper (`>= start AND < end`).

Slice 3's actual new work (per the kickoff prompt) was the budget-check + halt-signal LOGIC; actually stopping a running workflow is the orchestrator's job in Phase 2 and not built here. `is_workflow_halted_by_budget` is the primitive the orchestrator will call before scheduling each step.

Tests: 40 deterministic tests in `tests/test_budget.py` — pure evaluator across boundary/edge cases (zero cap, threshold=0, threshold=1, at-cap, at-threshold); UTC window correctness including tz conversion and naive-datetime defensive path; `cost_within_window` inclusion/exclusion at boundaries and isolation across workflows; end-to-end check with override resolution; exceed-event row shape; halt-signal query including the "unrelated error event must not trip the halt" anti-confusion case; TOML config loading including missing file, malformed TOML, missing required key, missing [defaults] section, override without `daily_cap_usd`, threshold out of range, and a sanity-check that the bundled `config/budgets.toml` loads. Full suite (90 tests including llm.py and state) passes cleanly. ruff check + ruff format + mypy strict all clean.

## Next Session Entry Point
**Two unblocked candidates — Joseph picks the sequencing.** The pre-
orchestrator refactor (this session) leaves both ready:

- **Phase 2 Slice 5 — Drafter agent + `Draft` model.** Natural next
  build: completes the 5-agent chain on the standalone-CLI path (the
  same Researcher → Extractor → Scorer → Drafter sequence, with the
  Drafter as a fourth `run_drafter` Layer B wrapping a new
  `drafter_node` Layer A from the start — no later refactor needed
  because the pattern is now established).
- **Phase 2 Slice 6 — LangGraph orchestrator.** Now genuinely cheap to
  build: the four existing pure node functions wrap directly. Builds
  the crew on Researcher / Extractor / Scorer / Summarizer-shaped
  nodes (the Summarizer isn't a crew node; the Drafter slot is
  empty until Slice 5 lands). Slice 6 could even land FIRST with a
  placeholder/no-op drafter node so the topology lands before the
  fifth agent — but the cleaner sequence is probably Slice 5 first so
  Slice 6 has a real Drafter to wire in.

**Recommendation — Slice 5 first, then Slice 6.** Reasons: (a) Slice 5
is the smaller, more contained piece (one new agent + one new model);
(b) it surfaces the first real Opus 4.7 per-Drafter cost number, which
the Slice 8 budget recalibration wants; (c) Slice 6 with a real
Drafter avoids placeholder-node code that gets thrown away. But the
ordering is reversible — both unblocked.

Opus high (slice implementation — but note the Drafter itself runs
on **Opus 4.7**, the user-visible-quality tier; CLAUDE.md model
routing table). Per ADR-006 §1 + ROADMAP:

Slice 5 scope (from ADR-006 §4 "Forward dependencies handed to Slice
5"):
- Build the `Draft` Pydantic v2 model. ADR-006 lists the projection
  `{paragraph_count, char_count}` but does not name fields; Slice 5
  owns them. At minimum: outreach prose plus enough structure that
  the Slice 7 Critic can decompose it into claims. Match the
  existing `extra="forbid", frozen=True` conventions.
- Implement `src/agent_habitat/agents/drafter.py` —
  `run_drafter(conn, *, raw_signals, profile, scored_company,
  workflow_id, ...)`. **Opus 4.7 tier** via `llm.complete()` (no
  `llm.py` change expected). The Drafter sees `(raw_signals,
  profile, scored_company)` — the union of upstream prose — so the
  drafter's claim language can later substring-check against any of
  the three (ADR-006 §3 grounding corpus). Wire through `run_step()`
  per ADR-006 §2.
- The Slice 5 Drafter does NOT yet integrate the fabrication-
  resistance retry loop (that's Slice 7 with the Critic). The
  contract here is: produce a Draft + the structured-data
  projection. ADR-006 §1 says input to the Drafter on retry will
  include `Critique.violations`; the Drafter's prompt should make
  room for that input but Slice 5 doesn't yet ship the retry path
  (Slice 6 orchestrator + Slice 7 Critic do).
- Decision-support framing: PATTERNS.md #4 says the drafter's
  output must be structurally disclaimed with the `coverage`
  number — "this draft scored 82.86/100 against the rubric, but
  the rubric covered only 70% of the operator's stated ICP
  dimensions on this run." Slice 4's live smoke proved this is
  the right shape; Slice 5 implements it on the user-visible
  surface.
- CLI: `agent-habitat run-drafter COMPANY_NAME [--db PATH]
  [--rubric PATH] [--max-searches N] [--*-workflow-id ID]` —
  sequences Researcher → Extractor → Scorer → Drafter as four
  separate workflows (Slice 6's orchestrator unifies them).
  Decision-support footer with the coverage disclosure baked in.
- Score-below-floor + coverage-below-min handling: per ADR-006 §1,
  these are the orchestrator's job (terminate_no_draft routing
  via Slice 6). For Slice 5's CLI, the cleanest shape is to skip
  the Drafter call when `scored_company.gated_by is not None` and
  print a clearly-labelled "no draft" outcome (workflow COMPLETED,
  not FAILED). This previews the orchestrator's behaviour without
  building it.
- Deterministic tests: model validation; Drafter happy run +
  audit chain + projection; Drafter skipped when scorer gates;
  infrastructure failure (malformed/empty response); CLI happy +
  upstream failures. One live smoke; record cost (first real Opus
  4.7 number for the chain) + draft prose preview + the
  coverage-aware disclosure in the output. Full suite + ruff +
  mypy strict must all be clean.
- Mind the **Opus pricing recalibration** (Open Questions):
  ADR-006 §1's per-drafter cost estimate is $0.025-$0.060; Slice
  5's live smoke is the first chance to verify this against Opus
  4.7's actual pricing ($15/$75 per MTok). Record the real number
  in STATUS.md; the recalibration may feed the Slice 8
  budget re-validation.

Two forward dependencies remain queued for Slice 8 (Open
Questions): `budgets.toml` re-validation against real end-to-end
cost (now with Slice 4's three-agent number as one input), and
ADR-006 §1's checkpoint-rationale prose re-check (upstream-chain
cost figure is ~5× understated). Neither blocks Slice 5.
