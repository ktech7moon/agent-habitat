# ADR-004: ICP rubric format and missing-data handling

**Status:** Accepted (2026-05-14)

## Context

Phase 2 Slice 4 builds the Scorer: a Sonnet-tier agent that consumes the
Extractor's `CompanyProfile` (ADR-006 §1, Slice 3 — DONE) and an
operator-tunable ICP rubric, and emits a `ScoredCompany`. ADR-006 §1's
empty-outcome contract names a specific routing rule that depends on the
Scorer's output: *score below operator-tunable floor → `terminate_no_draft`,
workflow finalised as COMPLETED, no Opus tokens spent on a low-fit lead.*
This ADR settles the rubric's wire format and — load-bearing — how the rubric
handles the realities of the Scorer's actual input.

Three coupled constraints force this one ADR rather than three:

1. **The format must mirror `config/budgets.toml`'s idiom.** That file is the
   project's existing operator-tunable surface (`[defaults]` + per-instance
   override sections, comments-as-documentation). CLAUDE.md rule 7 and
   PATTERNS.md #5 — "operator tunes outcomes; developers tune prompts" — make
   two parallel TOML idioms in the same `config/` directory a real wart for a
   project whose pitch is exactly the consistency of that surface.

2. **The rubric is an un-validated hypothesis.** The industry-standard B2B
   ICP rubric is a ~100-point weighted model (firmographic ~40-50%,
   technographic ~20-30%, intent ~20-30%) **validated against closed-won
   deal data**. agent-habitat has no CRM, no closed-won dataset, no
   validation corpus. The rubric the operator writes is a hand-authored
   hypothesis tuned in a TOML file; the industry's "validate weights against
   50 closed-won deals and re-tier quarterly" step does not exist here. The
   ADR must acknowledge this honestly rather than pretend the rubric is
   data-validated — the calibration story (PATTERNS.md #3) lives on this
   kind of honesty.

3. **The Scorer's input is gap-heavy.** Slice 3's live smoke produced a
   `CompanyProfile` where 1/5 fields was extracted (20%) and 4/5 were
   `ExtractionGap`s (80%) — of which 2 were model-honest gaps and 2 were
   over-reach catches the substring-grounding validator downgraded. This is
   not a corner case; it is the typical shape on a 5-signal news-heavy
   input. The format MUST specify how a gap is scored — any policy that
   treats a gap as a 0 (disqualifying) silently encodes "researcher effort
   = company fit," a known wrong proxy. The missing-data decision is the
   load-bearing call in this ADR.

ADR-006 §1 says Slice 4 is blocked on ADR-004; the README index agrees. This
ADR is the unblock.

## Decision

### 1. TOML format — mirrors `budgets.toml`'s idiom

`config/rubric.toml` carries the rubric. Structure:

- `[defaults]` — operator tuning knobs: `floor` (the gating score below which
  ADR-006 §1 routes to `terminate_no_draft`), the tier thresholds
  (`tier_a_min`, `tier_b_min`, `tier_c_min`), the missing-data policy
  (`missing_data_policy = "renormalise"` — see §2), and the optional second
  gate `min_coverage` (default `0.0` — off).
- `[dimensions.<name>]` — one section per scoring dimension. Each dimension
  declares `field` (one of `PROFILE_FIELD_NAMES`: `size` | `industry` |
  `tech_stack` | `recent_news` | `decision_makers`), `weight` (float in
  [0, 1]; weights MUST sum to 1.0 across all dimensions — Slice 4 validates
  this on rubric load), and `prose` (a multi-line operator-authored
  rubric string the Scorer LLM applies to the extracted field values to
  produce a per-dimension score in `[0, 5]`).

This is structurally `budgets.toml`'s pattern: `[defaults]` for global
tunables, `[noun.<name>]` for per-instance config. The default-vs-override
split is identical; only the noun differs (`workflow_types` →
`dimensions`). No second idiom; an operator who has already learned to edit
`budgets.toml` can edit `rubric.toml` without re-orienting.

One dimension scores exactly one `ProfileField`. Multi-field dimensions
(e.g., "firmographic" = size + industry combined) are deliberately not
supported in this format: they complicate the grounding chain (§3), and
Phase 2's 5-field profile makes 1:1 dimensions cover the space directly.
Multi-field dimensions are deferred to a future addendum if a workload
genuinely needs them.

A clearly-marked template lands at `config/rubric.toml` — placeholder
values + comments documenting the format. It is a template the operator
replaces with their own rubric; it is NOT a real tuned rubric and ships
with a banner comment saying so.

### 2. Missing-data handling — score is renormalised over present dimensions; coverage rides alongside

For each dimension, the Scorer first checks whether its declared `field`
on the `CompanyProfile` is a gap (`ProfileField.is_gap`):

- **If the field is extracted** (has `values` + `source_spans`): the Scorer
  LLM applies the dimension's `prose` rubric to the field values and emits
  a per-dimension score in `[0, 5]` with reasoning grounded against a
  source-span quote (§3).
- **If the field is a gap**: the dimension is EXCLUDED from the score and
  its weight is dropped from the denominator. The dimension is recorded
  with score `null` and reasoning "field was an ExtractionGap (reason=...);
  excluded from score per missing_data_policy='renormalise'."

The composite `score` is:

```
sum(dim.score * dim.weight  for each present dimension)
        / sum(dim.weight    for each present dimension)
        * 20                                                # rescale 0–5 → 0–100
```

Parallel to `score`, the Scorer also emits `coverage`:

```
coverage = sum(dim.weight for present dimensions)   # in [0, 1]
```

`score` is the rubric quality given what's known; `coverage` is the
fraction of total rubric weight that was actually scorable.

**Which number does ADR-006 §1's floor gate on?** `score`. The
`floor` in `[defaults]` is in the same 0–100 units as `score`, and a run
with `score < floor` routes to `terminate_no_draft` exactly as ADR-006 §1
specifies — no change to ADR-006's routing contract.

`coverage` is the OPTIONAL second gate. If `[defaults].min_coverage > 0`,
a run with `coverage < min_coverage` ALSO routes to `terminate_no_draft`,
with a distinct `step.completed` projection key (`gated_by =
"coverage"` vs `gated_by = "score"`) so the calibration story can tell
the two empty-outcomes apart. Default `min_coverage = 0.0` keeps Phase 2
single-gate; operators tightening the rubric on real data turn it up.

The honest framing carries through to PATTERNS.md #4: the Drafter's
downstream output will be structurally disclaimed with the `coverage`
number — "this draft scored 75/100 against the rubric, but the rubric
covered only 20% of the operator's stated ICP dimensions on this run" —
rather than collapsing the two numbers into a single comparable score.

### 3. Grounding chain extends through the Scorer

The Scorer's per-dimension reasoning IS in the fabrication-resistance
grounding corpus that ADR-006 §3 names for the Drafter. The rubric format
requires every per-dimension reasoning record to carry:

- `field` — the `ProfileField` name the dimension scored.
- `grounded_quote` — for a scored (non-excluded) dimension, a verbatim
  quote drawn from one of the field's `source_spans[].quote` values. For
  an excluded dimension, `null`.
- `reasoning` — operator-readable prose explaining the per-dimension
  score (or the exclusion).

A pure-Python substring check — the same shape as Slice 3's
`_ground_field` and ADR-006 §3's planned `agents/fabrication.py` — verifies
that `grounded_quote`, where present, is a substring (after
whitespace-collapse + lowercase normalisation) of the cited ProfileField's
source spans. The Scorer's PR review (Slice 4) re-uses Slice 3's
normalisation helper rather than inventing a second normaliser.

The grounding chain is now: `Citation.cited_text → Signal.text →
ProfileField.source_spans[].quote → ScoredCompany.dimensions[].grounded_quote
→ Draft claim`. Five hops, each one a substring of the previous, each one
auditable from cold storage with no LLM call. The Critic in Slice 7 inherits
this chain unchanged.

### 4. Scoring mechanism — LLM-driven per-dimension scoring against a prose rubric

Three options were on the table (per "Next Session Entry Point"):

- **Pure deterministic** (TOML carries thresholds + keyword lists; no
  LLM judgment beyond profile-to-feature mapping). Most auditable, but
  forces the operator to enumerate keywords for every dimension —
  brittle on prose fields like `recent_news` where the signal is
  semantic.
- **LLM-as-judge against a prose rubric (CHOSEN).** TOML carries the
  prose rubric; the Sonnet model emits a per-dimension score with
  reasoning. The grounding chain (§3) keeps it auditable — the reasoning
  must cite a source-span — even though the score itself is
  LLM-generated.
- **Hybrid** (deterministic feature score + LLM commentary). The most
  common shape in production, but introduces two scoring methods in one
  rubric — and §2's missing-data policy has to handle each
  differently. Deferred until calibration data justifies the
  complexity.

The choice is LLM-driven scoring grounded by §3's substring discipline.
The audit-grade posture is achieved by grounding, not by avoiding the
LLM — that is the consistent design move across ADR-003, ADR-006 §3, and
now ADR-004.

### 5. What ADR-004 does NOT decide

- The actual weights and dimension `prose` content. That is the operator's
  job; the bundled `config/rubric.toml` is a clearly-marked template.
- The `Scorer` agent implementation (Slice 4 owns it).
- The exact `ScoredCompany` Pydantic v2 model field names (Slice 4 owns
  them, though this ADR specifies what it must carry: `score`,
  `coverage`, `floor`, `min_coverage`, `passed_floor`, `passed_coverage`,
  `tier`, and a `dimensions: list[DimensionScore]` where each
  `DimensionScore` carries `field`, `weight`, `score | None`,
  `grounded_quote | None`, `reasoning`).
- Whether to support multi-field dimensions, multiple parallel rubrics
  (e.g., per-vertical), or tiered floors. Deferred to addendum ADRs if
  Phase 2 / Phase 4 workloads need them.

## Alternatives Considered

### A. Missing-data handling — THE load-bearing decision

| Option | Best case for it | Why rejected |
|---|---|---|
| **Gap-excluded, renormalise; coverage rides alongside (CHOSEN)** | Honest about what's knowable from the actual gap-heavy input. The score reflects rubric quality on covered dimensions; `coverage` flags the operator that ICP-fit ranking is unreliable when coverage is low. Preserves ADR-006 §1's routing contract (`floor` gates on `score`). Carries forward to the Drafter's decision-support disclaimer (PATTERNS.md #4). | — |
| **Gap = 0 points for that dimension** | Simplest implementation; one number to think about. | On a Slice-3-typical 4/5-gap input, every real company scores ~0–20 against any reasonable rubric — well below any reasonable floor — so the workflow always routes to `terminate_no_draft`. The habitat becomes useless. The deeper failure: silently encodes "researcher effort = company fit," conflating an absence of data with the absence of fit. PATTERNS.md #2 explicitly rejects this conflation for extraction; it must reject it for scoring too. |
| **Score-and-coverage collapsed into one composite (e.g., `score × coverage`)** | Single number; no second gate to operationalise. | Collapses two structurally distinct signals into one — exactly the dual-source-of-truth failure ADR-003 and ADR-006 §3 exist to prevent. A "75 × 0.20 = 15" score is indistinguishable from a "30 × 0.50 = 15" score, but the two runs are saying very different things about confidence in the ranking. The operator cannot tune the two effects independently. |
| **Penalty function** (gap-adjusted score = renormalised_score − k × gap_fraction) | One number; the operator tunes `k`. | Introduces a second weight knob (`k`) on top of dimension weights, doubling the rubric's tuning surface for no real-input benefit; still collapses the two signals. The penalty function is a workaround for not wanting a second gate — but the second gate (`min_coverage`) is honest and the penalty function isn't. |

A genuine fork was checked here: could the Extractor's gap-heavy output
shape and the Scorer's needs be mismatched at the root, i.e., does the
gap rate make any rubric-based scoring suspect at this stage of the
pipeline? Conclusion: no, the renormalise-with-coverage option serves a
gap-heavy input cleanly — `coverage` is the structural surfacing of the
mismatch when it occurs, not a hidden corruption of the score. The
mismatch hypothesis would have been "the score is meaningless below some
coverage threshold," which is exactly what `min_coverage` lets the
operator declare and gate on. No upstream redesign required.

### B. TOML format vs other config shape

| Option | Best case for it | Why rejected |
|---|---|---|
| **TOML mirroring `budgets.toml` (CHOSEN)** | One config idiom across the project; PATTERNS.md #5 honoured; operator who learns one file can edit the other. | — |
| **YAML / JSON** | Wider tooling; YAML supports multi-line prose ergonomically. | Two config idioms in `config/` for no payoff. TOML's multi-line string support (`"""..."""`) handles the `prose` field fine. |
| **Python module config** | Programmatic flexibility; can reference other Python objects. | Closes the operator tuning surface (operator-authored Python is not the bar this project sets); CLAUDE.md rule 7 explicitly carves the operator/developer split along the config/.md-prompt line. |

This is close to pre-decided by `budgets.toml`'s precedent — saying so
honestly is the right shape for the ADR.

### C. Scoring mechanism — covered in Decision §4 above (deterministic vs LLM-judge vs hybrid)

The full discussion is in §4 rather than this table to keep the
load-bearing missing-data alternative (§A above) visually dominant. The
short version: deterministic is brittle on semantic fields, hybrid is
premature complexity, LLM-as-judge grounded by §3's substring discipline
gives the audit-grade posture without enumerating keyword lists.

## Consequences

**What becomes easier:**

- Slice 4 (Scorer) has a single blueprint: load TOML, validate weights
  sum to 1.0, iterate `PROFILE_FIELD_NAMES` in canonical order, emit a
  `ScoredCompany` with `(score, coverage, dimensions)` and the
  grounded-quote chain.
- ADR-006 §1's `terminate_no_draft` routing is unchanged — `score`
  remains the gating number, and the optional `min_coverage` second gate
  layers in without disturbing Slice 6's orchestrator wiring.
- The grounding chain now spans four hops cleanly
  (`cited_text → Signal.text → ProfileField.source_spans → Scorer.grounded_quote`);
  Slice 7's Critic + `agents/fabrication.py` substring checker has one
  shape to enforce, not three.
- Operator tuning surface is one TOML idiom; documentation refers to a
  single pattern (`config/budgets.toml` + `config/rubric.toml`,
  identical structure).

**What becomes harder (accepted costs):**

- **The rubric is an un-validated hypothesis.** The honest framing is
  this: the operator's first `rubric.toml` is a guess; the calibration
  story (PATTERNS.md #3) is what validates it. Slice 8 calibration
  should record, per real test company, both the human-judged fit and
  the rubric score, and surface the disagreements. The industry's
  "tune against 50 closed-won deals" step does not exist here; the
  honest substitute is "tune against the calibration story's disagreements."
- **Two operator knobs (`floor` and `min_coverage`) instead of one.** A
  small surface-area increase that buys structural honesty about the
  coverage/score split. `min_coverage = 0.0` (default) keeps the single-gate
  experience for the operator who doesn't want to think about coverage yet.
- **The Scorer LLM call is the rubric's interpretation surface.** Per-dimension
  reasoning quality depends on prompt design (developer-tuned per CLAUDE.md
  rule 7) AND on the `prose` field the operator authors. A poorly-written
  rubric produces poorly-grounded scores; the grounding-quote substring
  check catches over-reach but not under-specification. Mitigated, not
  eliminated, by Slice 8 calibration.

**What is deferred:**

- **Rubric validation against closed-won data.** Trigger: a workload
  with a real CRM tap. Phase 2's freelance-prospecting workload does
  not have one; the operator's hypothesis stands until calibration
  produces evidence to revise it.
- **Multi-field dimensions, per-vertical rubrics, tiered floors.**
  Phase 2 single-rubric is sufficient. Addendum ADRs if Phase 4+ adds
  a workload that requires them.
- **Hybrid deterministic + LLM scoring.** Re-evaluate if Slice 8
  calibration shows the LLM-judge consistently mis-scoring a dimension
  the operator could express deterministically.
- **Per-dimension confidence weighting** (e.g., a dimension's
  contribution downweighted by the source-span quote length, or by
  Citation count). Premature; `coverage` is the first-order signal and
  is enough for Phase 2.

**Forward dependencies handed to Slice 4 (Scorer):**

- Build `ScoredCompany` Pydantic v2 model per §5's field list.
- Implement `agents/scorer.py` — Sonnet tier via `llm.complete()` (no
  `llm.py` change expected), wired through `run_step()` per ADR-006 §2,
  with the structured-output method choice the Extractor established
  (schema-in-system-prompt + `model_validate_json(LLMResult.content)`).
- Validate the rubric at load time: weights sum to 1.0; every `field` is
  in `PROFILE_FIELD_NAMES`; tier thresholds form a valid `tier_a_min >
  tier_b_min > tier_c_min >= floor` ordering.
- Mirror `{score, coverage, floor, min_coverage, passed_floor,
  passed_coverage, tier, gated_by | null}` onto `step.completed`.
- Re-use Slice 3's whitespace/case normalisation helper for the
  `grounded_quote` substring check; do not invent a second normaliser.
- Empty / all-gaps `CompanyProfile` is a valid input: `score` is
  undefined (no dimensions covered), `coverage = 0.0`, `passed_floor =
  false`, `gated_by = "coverage"` if `min_coverage > 0` else `gated_by =
  "score"`; either way, route to `terminate_no_draft`.
- Live smoke against a real `CompanyProfile` from Slice 3's pipeline;
  record the per-dimension calibration table in STATUS.md, in particular
  the score-vs-coverage spread.

**What would invalidate this decision:**

- Slice 8 calibration shows the renormalise-with-coverage policy
  produces scores that consistently disagree with the operator's
  human-judged fit (i.e., the rubric's covered-dimension scoring is
  systematically biased on low-coverage inputs). Re-evaluate option A
  with the calibration evidence; the penalty-function or
  collapsed-composite shapes return to the table.
- A regulated-industry buyer requires a specific rubric format
  (e.g., NIST AI RMF traceability, an industry-standard 100-point
  weighted model). Addendum ADR; the renormalise-with-coverage logic
  likely survives but the wire format may need to change.
- LLM-judge per-dimension scoring proves too stochastic across runs of
  the same `CompanyProfile` (Slice 8 calibration includes a same-input
  repeatability check). Re-evaluate the hybrid shape from §4.
