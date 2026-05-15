# ADR-006: Phase 2 crew architecture, `run_step()` extraction, fabrication-resistance contract

**Status:** Accepted (2026-05-14)

**Supersedes:** ADR-005 (Cross-agent fabrication-resistance contract) — folded into §3 of the Decision section below. See README index for the status change.

## Context

Phase 1 shipped the habitat: persistence (ADR-002), the LLM wrapper (`llm.py`), observability (events table + JSONL + structlog), budget tracking, and the CheckpointSystem. One demonstration workload — the URL summarizer — threads all five subsystems end-to-end. Phase 2 builds the first real workload: a 5-agent lead-enrichment crew (researcher → extractor → scorer → drafter → critic) running on the existing habitat. The orchestrator package exists but is empty; LangGraph (chosen in ADR-001, version 1.2.0 confirmed in this session) has not yet been wired.

This ADR is the blueprint for Phase 2. It is one ADR rather than three because three decisions are forced together by their dependencies — none can be resolved without constraining the other two:

1. **The crew architecture.** Five agents, what handoff topology, what shared state, where does the human checkpoint sit, what happens on failure. ADR-001 picked LangGraph; this ADR commits to a specific shape of LangGraph graph.

2. **The `run_step()` extraction (STATUS.md Open Question).** The summarizer's `_run_step` / `_run_summarize_step` lifecycle (~188 lines in `src/agent_habitat/agents/summarizer.py`) implements the open-RUNNING-row → emit step.started → do work → close COMPLETED/FAILED → emit result-event sequence that every Phase 2 agent will need. Five agents × ~188 lines = ~940 lines of duplicated audit-grade boilerplate, with five independent copies of the contract that can drift. The shape of the shared utility constrains, and is constrained by, the crew architecture: the orchestrator wraps every agent invocation, so it has to know what an agent's lifecycle looks like.

3. **Cross-agent fabrication-resistance (ADR-005 was queued for this).** The drafter must cite only signals upstream agents produced (Working Agreement rule 5; PATTERNS.md #2). This is a property of how state flows through the graph, what each agent's output structure is, and how the critic consumes it. Pulling it into a separate ADR would force this one to reference an unwritten contract by name. The cleaner shape is to set the contract here, alongside the graph that enforces it; ADR-005 is marked superseded.

### What the habitat already provides (do not re-derive)

The Phase 2 orchestrator is a new caller over existing primitives. The ADR builds on these as written; none changes.

- `src/agent_habitat/state/persistence.py` — `insert_workflow`, `insert_step`, `update_step`, `recompute_cost_total`, `reconcile_orphan_steps`. ADR-002's two-parallel-schemas-in-one-SQLite-file shape stands: LangGraph's `SqliteSaver` will write its checkpoint tables to the same `data/state/agent_habitat.db` file, sharing `workflow_id`/`thread_id`.
- `src/agent_habitat/llm.py` — `complete()` returns `LLMResult{content, cost_usd, jsonl_ref, stop_reason, truncated}`. Every agent call routes through this; no direct SDK imports (CLAUDE.md rule 9).
- `src/agent_habitat/observability/events.py` — `emit_event()` with `EventType` taxonomy already includes `STEP_STARTED`, `STEP_COMPLETED`, `STEP_FAILED`, `WORKFLOW_*`, `CHECKPOINT_*`, and **`FABRICATION_DETECTED = "agent.fabrication_detected"`** (Slice 4 added the taxonomy member in anticipation; this ADR is its first writer).
- `src/agent_habitat/checkpoint/system.py` — `request_checkpoint`, `approve_checkpoint`, `reject_checkpoint`. The system establishes the audit fact and the workflow status transition; this ADR specifies how the LangGraph orchestrator obeys that fact at runtime via `interrupt()` / `Command(resume=...)`.
- `src/agent_habitat/budget/tracker.py` — `is_workflow_halted_by_budget()`. The orchestrator consults this before each step.

### LangGraph 1.2.0 API surface used

Confirmed in this session against the installed package:

- `langgraph.graph.StateGraph(schema)` — graph keyed by a typed state schema.
- `START`, `END` sentinels.
- `add_node(name, fn)`, `add_edge(from, to)`, `add_conditional_edges(from, router_fn, mapping)`.
- `compile(checkpointer=...)` — accepts a `BaseCheckpointSaver`.
- `langgraph.types.interrupt(value)` — pauses graph execution from inside a node; raises a resumable exception, persisted by the checkpointer.
- `langgraph.types.Command(resume=..., goto=..., update=...)` — resumes from an interrupt; can also route to a named node.

`langgraph.checkpoint.sqlite.SqliteSaver` lives in the `langgraph-checkpoint-sqlite` package; not currently installed. Adding it is a one-line Phase 2 Slice 6 (orchestrator) dependency bump, not this ADR's concern.

---

## Decision

Three coupled decisions, each stated tightly enough that an implementation slice can be built against it.

### 1. Crew architecture — strictly sequential, single shared state, one human checkpoint before the drafter

**Topology.** A linear pipeline with one approval gate and one bounded-retry edge:

```
START → researcher → extractor → scorer → [checkpoint:approve_drafter] → drafter → critic → END
                                                                            ↑           │
                                                                            └───────────┘
                                                                       (one retry on fabrication)
```

- Any agent that signals an unrecoverable error routes (via conditional edge) to a terminal `halt` node that finalises the workflow as FAILED.
- Any agent that signals a benign-empty outcome (researcher finds no signals; scorer below operator-tunable floor; checkpoint rejected) routes to a `terminate_no_draft` node that finalises the workflow as COMPLETED — the workflow ran successfully; the answer was "not worth drafting." This distinction matters for the audit story: an empty-signals run is not a failure.
- The orchestrator consults `is_workflow_halted_by_budget(workflow_id)` and `is_workflow_paused_for_checkpoint(workflow_id)` before scheduling each node; either returning truthy short-circuits to the matching terminal node.

**Sequential, not parallel.** Each stage strictly depends on the previous stage's output. The researcher's signals feed the extractor; the extractor's profile feeds the scorer; the score determines whether the drafter runs at all. Parallel research-fanout (e.g. two competing search providers) is imaginable but premature for Phase 2's single workload — added complexity with no current-workload payoff. Phase 4+ workloads that genuinely need parallel reads can add a `Send`-fanout node without disturbing the linear backbone.

**Shared state — single `TypedDict`, additive writes, no upstream mutation.**

The crew runs over a single LangGraph `TypedDict` state. Each node returns a partial dict; LangGraph merges it into the shared state. The shape (illustrative — exact field names land in implementation slices):

```python
class CrewState(TypedDict, total=False):
    workflow_id: str
    company_name: str            # input
    raw_signals: RawSignals       # researcher output (pydantic v2)
    profile: CompanyProfile       # extractor output
    score: ScoredCompany          # scorer output
    drafter_approved: bool        # checkpoint resolution
    draft: Draft                  # drafter output
    critique: Critique            # critic output
    fabrication_retries: int      # 0 or 1; bounds the retry edge
    halt: HaltReason | None       # set on any unrecoverable failure
```

Two invariants:

- **Each agent writes ONLY its own field plus the bookkeeping fields it owns** (`fabrication_retries` for drafter; `drafter_approved` for the checkpoint node; `halt` for any node that signals an unrecoverable failure). Upstream agent outputs are read-only from a downstream agent's perspective. Enforcement is structural: each agent's node function returns a partial dict that only contains its own keys; reviewers reject PRs that mutate upstream fields. (No runtime enforcement — LangGraph permits any partial; the cost of a deep-frozen container exceeds the benefit at Phase 2 volume.)
- **All upstream agent outputs remain reachable from the drafter and critic.** This is what makes the fabrication-resistance substring check tractable (§3 below): the critic has access to `(raw_signals, profile, score, draft)` simultaneously, so claim-grounding can be checked against the union of upstream prose without re-fetching anything.

The shared-state shape is what permits this. A per-handoff-payload alternative would force the drafter to receive `(profile, score)` only and would hide `raw_signals` behind the extractor's structured-output abstraction — exactly the fabrication-resistance failure mode (drafter cites signals the researcher produced; if the drafter cannot see the original signal strings, the substring check is unimplementable). Shared state wins because the fabrication-resistance contract demands it.

**Handoff contract — typed Pydantic v2 models, mirrored on `step.completed` for SQL-side audit.**

Each agent has a Pydantic v2 output model (`RawSignals`, `CompanyProfile`, `ScoredCompany`, `Draft`, `Critique`). Three derived obligations:

1. **The full structured output lands in LangGraph's State blob** (LangGraph's `SqliteSaver` serialises it; that is its job — ADR-001's reason for picking LangGraph).
2. **The agent's last LLM call writes one JSONL telemetry line** via `llm.py` (already true); the resulting `jsonl_ref` is stamped on the agent's `workflow_steps.output_ref` (already true for the summarizer's summarize step).
3. **A small, fixed projection of the structured output is mirrored into `step.completed`'s `structured_data`** — enough that an auditor can browse the `events` table without resolving any JSONL refs. For example: researcher's mirror is `{signal_count, source_count}`; extractor's is `{has_size, has_tech_stack, has_decision_makers, gap_count}`; scorer's is `{score, threshold, passed_floor}`; drafter's is `{paragraph_count, char_count}`; critic's is `{passes, violation_count}`. Projections are deliberately small — the JSONL line is the authoritative source for full text.

These three writes plus the `workflow_steps` row are the audit chain ADR-002 specifies. The crew architecture does not change ADR-002; every claim above lands in the existing schema.

**Error / retry strategy.**

Three distinct failure shapes. Slice 1 commits to zero infrastructure retries; the only retry in the graph is the fabrication retry (§3).

| Failure | What happens | Why |
|---|---|---|
| **Infrastructure error** (API 5xx, timeout, network) | Agent function raises; `run_step()` (§2) catches, finalises the step as FAILED with `error_message`, emits `step.failed`, re-raises. Orchestrator catches, routes to the `halt` node, writes `workflow.failed`. No retry. | A failed workflow is recoverable: the operator can re-run from CLI; LangGraph's checkpointer preserves state up to the failing step. Building bounded infra retry now hides the failure rate from the calibration story this project lives or dies on. Queued for a follow-up ADR if Phase 2 calibration shows transient errors are common enough to warrant it. |
| **Semantic empty-outcome** (researcher finds no signals; scorer below operator floor; checkpoint rejected) | Agent returns a structured-but-empty output; the conditional edge routes to `terminate_no_draft`; workflow finalised as COMPLETED with a `workflow.note` event explaining the early termination. No further LLM cost paid. | An empty outcome is a valid result, not an error. Logging it as FAILED would corrupt the workflow-failure-rate metric. The score-below-floor case is the operator's tunable savings: cheap Sonnet runs filter unworthy leads before Opus tokens are spent. |
| **Fabrication-resistance violation** (critic finds a draft claim not grounded in any upstream output) | First violation: increment `fabrication_retries`, route back to `drafter` with the critic's violation report appended to the drafter's input. Second violation: emit `agent.fabrication_detected` event at ERROR level, route to `halt`, workflow FAILED. | One deterministic retry with explicit critic feedback is cheap insurance against stochastic Opus output; persistent fabrication after retry indicates a real upstream-data or prompt-design problem that should halt loudly, not loop forever. |

**Workflow lifecycle invariants** (all already enforced by Slice 6's summarizer; this ADR commits Phase 2 agents to the same contract):

- A workflow row is inserted with `status=RUNNING` and `started_at=now` before any step row exists.
- Every step row is inserted with `status=RUNNING` and `finished_at=NULL` before the agent function runs, and updated to `COMPLETED`/`FAILED` with `finished_at` stamped on exit — pre-step audit row is the recovery anchor (ADR-002 dual-write recovery shape).
- Workflow termination (success or failure) stamps `finished_at` and writes exactly one of `workflow.completed` / `workflow.failed` / `workflow.cancelled`. No stuck-RUNNING workflows survive a normal exit; orphaned RUNNING rows from a hard crash are reconciled by `reconcile_orphan_steps` on next startup.

**Human checkpoint — between scorer and drafter.**

The ROADMAP's placement is confirmed. Rationale, with the Opus pricing actually computed: Opus 4.7 is $15/$75 per MTok (input/output) vs Sonnet's $3/$15 — 5× input, 5× output. A typical drafter call processes the profile + score + signals (~1-3K input tokens) and emits a 200-400 token outreach draft. That puts each drafter run in the $0.025–$0.060 range; the rest of the upstream chain at Haiku+Sonnet costs roughly $0.01–$0.02 combined. The checkpoint pays itself back the moment one in five low-scoring leads is rejected. More importantly, the drafter's output is the only artefact in the workflow that is plausibly user-visible / send-class — "approve before drafting" is the same pause an operator would want before "approve before sending."

**Mechanism — LangGraph `interrupt()` wired to the CheckpointSystem.**

The graph node `request_drafter_approval` is the implementation glue. Its body:

```python
def request_drafter_approval(state: CrewState) -> dict:
    cp = checkpoint.request_checkpoint(
        conn,
        workflow_id=state["workflow_id"],
        action="approve_drafter",
        summary=_summary_for_human(state["score"], state["profile"]),
        proposed_payload={"profile": ..., "score": ...},
    )
    # request_checkpoint already moved workflow.status to PAUSED.
    resume_value = interrupt({"checkpoint_id": cp.id})
    # On resume, resume_value is whatever the operator passed via Command(resume=...).
    # The CLI's `checkpoint approve|reject` resolves the checkpoint and then
    # the orchestrator-runner relays the resolution as the resume value.
    return {"drafter_approved": resume_value["approved"]}
```

A downstream conditional edge routes on `state["drafter_approved"]`: True → drafter; False → `terminate_no_draft`. The interrupt-and-resume flow has two real properties this ADR commits to:

- **Cross-session resume.** The interrupt is durable via LangGraph's checkpointer. The operator can approve or reject hours or days later from the CLI; the workflow resumes from exactly the `request_drafter_approval` node. This is ADR-001's first-non-trivial test of the "halt/resume across sessions" requirement.
- **The CheckpointSystem audit row IS the source of truth.** LangGraph's `interrupt`/`resume` is the runtime mechanism; the `events` table row written by `request_checkpoint` is what an auditor consults. If LangGraph's API changes across versions (ADR-001's flagged risk), the audit fact is untouched.

The Slice 5 CheckpointSystem is unchanged. The orchestrator is the new caller.

### 2. `run_step()` — a context-manager utility owned by `orchestration/`

**Where it lives.** New module `src/agent_habitat/orchestration/run_step.py`. The `orchestration/` package already exists (currently empty); this is its first occupant. Agents import from `orchestration.run_step`; the LangGraph orchestrator (Phase 2 Slice 6) imports the same module to wrap each node function.

**Contract — context manager, explicit recording verbs.**

```python
from contextlib import contextmanager
from collections.abc import Iterator, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
import sqlite3

@dataclass
class StepRecorder:
    """Handle yielded by run_step(). Agent calls record_* to attribute
    cost / output_ref / structured-data projections onto the step row and
    the step.completed event. All record_* calls are additive and idempotent;
    last write wins."""
    workflow_id: str
    step_id: int
    step_index: int
    agent_name: str
    _cost_usd: float = 0.0
    _output_ref: str | None = None
    _structured: dict[str, Any] = field(default_factory=dict)
    def record_cost(self, usd: float) -> None: ...
    def record_output_ref(self, ref: str) -> None: ...
    def record_structured_data(self, data: dict[str, Any]) -> None: ...

@contextmanager
def run_step(
    conn: sqlite3.Connection,
    *,
    workflow_id: str,
    step_index: int,
    agent_name: str,
    now: Callable[[], datetime] | None = None,
) -> Iterator[StepRecorder]:
    """Open a RUNNING step row, emit step.started, yield a recorder, then
    close the row on context exit.

    Normal exit:
      - workflow_steps row finalised as COMPLETED with finished_at, cost_usd,
        output_ref taken from the recorder.
      - step.completed event emitted with the recorder's structured_data
        merged in (plus standard keys: step_name, step_index, cost_usd,
        output_ref when present).

    Exception exit:
      - workflow_steps row finalised as FAILED with finished_at and
        error_message=f"{type(exc).__name__}: {exc}".
      - step.failed event emitted with error message in structured_data.
      - Exception is re-raised. The orchestrator catches and decides what to
        do with the workflow (route to halt vs route to a recovery edge).
    """
```

**Agent usage pattern:**

```python
def researcher_node(state: CrewState) -> dict:
    with run_step(conn, workflow_id=state["workflow_id"], step_index=1,
                  agent_name="researcher") as step:
        signals = _do_research(state["company_name"], step)
        # _do_research called llm.complete(...) and called
        # step.record_cost / step.record_output_ref on the LLMResult.
        step.record_structured_data({"signal_count": len(signals.signals)})
        return {"raw_signals": signals}
```

**Three design choices justified, briefly:**

- **Context manager, not a callable.** The summarizer's current `_run_step(work=lambda: ...)` shape works but forces lambda-wrapping for any non-trivial step body and makes it awkward to record cost from a result the lambda hasn't returned yet. `with run_step(...) as step` is Python-idiomatic, lets the agent body run inline, and gives a named handle for the recording verbs. Cost: slightly more line-noise on the recorder API; benefit: every LLM-bearing agent is a clean two-line wrap (`with run_step ...` + `step.record_cost(result.cost_usd)`).
- **Explicit `record_cost` / `record_output_ref` / `record_structured_data`** instead of inferring from a return value. Inference would require the utility to know about `LLMResult`'s field names, coupling `orchestration/` to `llm.py`'s shape — and it would not generalise to non-LLM steps (the summarizer's fetch and parse steps, which carry no LLM cost). Explicit verbs cost three lines per agent and keep the utility narrow.
- **Lifecycle ONLY; no LangGraph awareness.** `run_step()` does not import LangGraph. It is a habitat-level audit-lifecycle utility; the LangGraph orchestrator wraps each node function around it but does not subclass or extend it. Future workloads (Phase 4+) can use `run_step()` without LangGraph if they don't need a state machine.

**Summarizer retrofit — not in this ADR's scope, but the path is straightforward.** The summarizer's `_run_step` and `_run_summarize_step` reduce to the new utility:

- The summarize step's truncation-info recording lands via `step.record_structured_data({"input_truncated": True, "original_chars": ..., ...})`.
- The summarize step's cost/output_ref lands via `step.record_cost(result.cost_usd)` + `step.record_output_ref(result.jsonl_ref)`.
- The ~188 lines collapse to ~30-40 lines of agent body.

The retrofit is queued as a small follow-up alongside Phase 2 Slice 2 (Researcher) — building the researcher against the new utility is the natural moment to also rewrite the summarizer through it, proving the same contract serves both call sites. The cosmetic-trim items (over-long module docstring, section dividers, `WORKFLOW_TYPE`/`AGENT_NAME` constants) from STATUS.md's Open Question ride along with that retrofit diff.

### 3. Cross-agent fabrication-resistance — critic emits `(claim, source_text)` pairs; substring check is pure Python (ADR-005 folded in)

**The contract.** Every claim the drafter makes about the company MUST be a verbatim substring of one of the upstream agents' textual outputs:

- The researcher's `RawSignals` — raw search-result snippets, scraped page excerpts. The drafter is allowed to cite anything the researcher actually saw.
- The extractor's `CompanyProfile` — structured fields plus extraction-quote spans (the extractor's job already includes recording the source span for each structured field, per the PATTERNS.md `offending_phrase`-style discipline).
- The scorer's `ScoredCompany` — reasoning text. The drafter may reference the score's stated reasoning.

The drafter is NOT allowed to cite anything else. "The company recently launched a fintech product" is fine if those words (or their substring-detectable equivalent) appear in `RawSignals.signals[i].text` or `CompanyProfile.recent_news[j].quote`. It is a fabrication if it doesn't.

**Detection — critic + pure-Python substring check, in that order.**

The critic's job is **structural decomposition**, not vibe-judging:

1. Critic (Haiku, cheap) reads `(draft, raw_signals, profile, score)`. It outputs a `Critique` Pydantic model:
   ```python
   class Claim(BaseModel):
       text: str                    # the verbatim span from the draft
       claimed_source: Literal["raw_signals", "profile", "score"]
   class Violation(BaseModel):
       claim: Claim
       reason: str                  # human-readable
   class Critique(BaseModel):
       claims: list[Claim]
       violations: list[Violation]
       passes: bool                 # = len(violations) == 0
   ```
2. **The substring check itself is a pure function in `agents/fabrication.py` (not an LLM judgment).** For each `Claim`, it normalises whitespace + lowercases, then checks whether `claim.text` is a substring of the concatenated normalised text of the claimed source. If the substring check disagrees with the critic's own `passes` field, the substring check wins and the disagreement is itself emitted as a violation (with reason "critic missed substring failure" or "critic over-flagged a grounded claim"). This is the property that makes the contract auditable: an auditor can re-run the substring check from cold storage with no LLM call.

3. **On any violation (count >= 1):** route per the retry rule in §1. First failure → drafter retry with `Critique.violations` appended as additional system context. Second failure → emit `agent.fabrication_detected` at ERROR level with the violation list in `structured_data`, route to halt, workflow FAILED.

**Why the LLM is in the loop at all.** The substring check is pure but parsing the draft into claims is not — a sentence like "Acme just raised a $50M Series B led by Sequoia" is one logical claim but may need to be checked against multiple source spans. The critic's job is decomposition: turn prose into a list of atomic checkable claims. The pure-Python step is the verification. Splitting these is what keeps the contract from collapsing into "trust the LLM-judge."

**Audit shape.** Every critic run emits one `step.completed` event with the projection `{passes, violation_count, claim_count}` on `structured_data`. A failing critic run additionally emits one `agent.fabrication_detected` event (the existing taxonomy member from Slice 4) carrying the full violation list. Both events live on the existing schema; no addendum to ADR-002.

**Why ADR-005 folds in.** ADR-005's queued question was "how does the drafter prove citations are grounded?" The answer is: via the critic agent's structural decomposition + a pure substring check, in a fixed position in the graph (between drafter and either END or drafter-retry), with the violation event already named in the taxonomy. Every part of that answer is a property of the crew architecture — the position of the critic in the graph, the shape of the shared state, the retry policy. A separate ADR-005 would either (a) repeat all of that or (b) reference an unwritten contract from this one. Folding it in is the honest shape.

### 4. Scope of this ADR vs implementation slices 2-8

**This ADR decides (blueprint):**

- The 5-agent linear topology + the two non-trivial edges (drafter-retry, terminal halt/no-draft).
- The single shared `TypedDict` state shape with per-agent write isolation.
- The handoff contract: typed Pydantic v2 output per agent + small projection mirrored on `step.completed`.
- Error/retry policy: zero infrastructure retries in Slice 1; one bounded fabrication retry; semantic empty-outcomes route to COMPLETED, not FAILED.
- The checkpoint position (before drafter) and the `interrupt()` ↔ CheckpointSystem wiring pattern.
- The fabrication-resistance contract (critic decomposes; substring check verifies; pure function is the source of truth; agreement with critic's `passes` is required).
- The `run_step()` context-manager contract.

**This ADR does NOT decide (implementation slices own these):**

- Exact field names of `RawSignals`, `CompanyProfile`, `ScoredCompany`, `Draft`, `Critique`. Each slice (2-5, 7) lands its agent's model when the agent is built.
- The web search tool. ADR-003 still stands — must be decided before Phase 2 Slice 2.
- The ICP rubric format. ADR-004 still stands — must be decided before Phase 2 Slice 4.
- Prompt files. Developers tune prompts in versioned `.md` files (CLAUDE.md rule 7); not an ADR concern.
- Exact retry-edge LangGraph wiring code, exact node names, exact conditional-edge predicates. The shape is set; the wiring is Slice 6's job.

**This ADR does NOT change:**

- ADR-001 (LangGraph is the orchestrator).
- ADR-002 (persistence schema — every claim above lands in `workflows`/`workflow_steps`/`events` as currently defined).
- The CheckpointSystem public surface (Slice 5).
- `llm.py`'s `complete()` contract.

If implementation reveals that any of these need to change, that is the trigger for an addendum ADR, not a quiet edit.

---

## Alternatives Considered

### A. Topology

| Option | Best case for it | Why rejected |
|---|---|---|
| **Strictly sequential (chosen)** | Single dependency chain; each handoff trivial to reason about; easy to checkpoint; matches Phase 2's single workload exactly. | — |
| **Parallel research fanout** (multiple researchers → merger node → extractor) | Multiple search providers triangulate; redundancy against any one provider's failure. | No current-workload payoff: Phase 2 is one search provider (whichever ADR-003 picks). Adds a merger node, conflict-resolution logic, and parallel-write semantics into LangGraph state — all overhead for a property no Phase 2 user is asking for. The linear shape does not block this — a fanout node can be inserted in Phase 4+ without disturbing downstream stages. |
| **Researcher / extractor in parallel** (researcher fetches; extractor reads cached data simultaneously) | Lower wall-time on a cold run. | They are strictly sequential by data dependency: the extractor cannot extract from signals that don't exist yet. The "cached data" branch is a separate workload (re-enrichment) and not Phase 2's path. |
| **DAG with optional critic skip** | Skip the critic when the drafter has high confidence; cheaper runs. | The critic is the fabrication-resistance enforcer; skipping it is exactly the loophole the contract exists to close. The critic is Haiku — its cost is ~$0.001 per run. There is no real savings, only a real fabrication risk. Rejected on principle. |

### B. State shape

| Option | Best case for it | Why rejected |
|---|---|---|
| **Single shared TypedDict, additive writes (chosen)** | All upstream outputs reachable from every downstream agent — required for the fabrication-resistance substring check, which compares the drafter's claims against the union of upstream prose. Matches LangGraph's idiomatic shape. Easy to checkpoint as one blob. | — |
| **Per-handoff payload** (researcher returns Profile-input to extractor; extractor returns Score-input to scorer; etc.) | Strictly typed handoff signatures; impossible for an agent to read state it shouldn't. | Breaks the fabrication-resistance contract. The drafter would receive `(profile, score)` only and could not see `raw_signals`. The substring check would have to re-fetch or re-serialise upstream outputs — exactly the duplication that has historically caused upstream-mutation bugs. The fabrication-resistance contract is the load-bearing reason this option is rejected; LangGraph idiom is a secondary reason. |
| **Channels with reducers** (one LangGraph channel per agent, with explicit reducer functions) | Maximally explicit about how state evolves; concurrent-write-safe. | The crew is sequential; there is no concurrent write to reduce. The reducer machinery is overhead for a problem the topology does not have. Revisit if Phase 4+ workloads introduce parallel agents. |

### C. Error / retry strategy

| Option | Best case for it | Why rejected |
|---|---|---|
| **Zero infra retry; one fabrication retry (chosen)** | The failure rate of every agent is visible in the calibration story. Bounded fabrication retry is the only retry, and it is justified by Opus's stochasticity. Halt-on-error keeps the FAILED contract clean. | — |
| **Bounded retry at every agent** (e.g. 3 retries with exponential backoff on transient errors) | Higher end-to-end success rate; matches production-grade defaults. | Hides the real failure rate from the calibration story. The honest path is to ship without retry, see what actually breaks in Phase 2 calibration, and add retry as a follow-up ADR with real evidence. Premature optimisation otherwise. |
| **Retry at the orchestrator only** (a single retry budget shared across the whole workflow) | One number to reason about; no per-agent retry config. | Still hides the failure rate. Adds budget-tracking complexity (which agents have consumed retry tokens?) without solving the underlying "we don't know what fails yet" problem. |
| **No fabrication retry — single shot** | Simpler; one failure = halt. | Opus output is genuinely stochastic; a one-shot fabrication failure is sometimes a generation artefact, not a real grounding gap. One deterministic retry with explicit violation feedback costs ~$0.05 and recovers a measurable fraction of false positives. Worth the line of conditional-edge code. |

### D. `run_step()` location and shape

| Option | Best case for it | Why rejected |
|---|---|---|
| **Context manager in `orchestration/run_step.py` (chosen)** | Python-idiomatic; clean two-line wrap for every agent; explicit recording verbs decouple lifecycle from LLM-result shape; no LangGraph dependency in the utility. | — |
| **Callable-with-work-function** (the summarizer's current `_run_step(work=lambda: ...)`) | Already proven in Slice 6; familiar. | Forces lambda-wrapping for non-trivial bodies; can't record cost from a result the lambda hasn't returned yet; awkward for the LLM-bearing step's `record_*` pattern. The summarizer's `_run_summarize_step` exists precisely because the callable shape couldn't accommodate the LLM-result fields cleanly — a sign the abstraction is wrong, not just the call site. |
| **Decorator on agent functions** (`@step(agent_name="researcher", step_index=1)`) | Even shorter at call sites. | Static `step_index` is wrong: the orchestrator owns step indexing across a workflow, not the agent file. Static decorators can't see runtime state. Rejected. |
| **Method on a BaseAgent class; agents subclass** | Object-oriented; natural place for shared agent behaviour. | The habitat's design (every other module: persistence, observability, budget, checkpoint) is functions over data, not classes. A BaseAgent introduces inheritance for a single shared method — an inheritance hierarchy as decoration. Functions and a context manager are the smaller, more habitat-consistent shape. |
| **Lifecycle hooks injected by the LangGraph orchestrator (no agent-side utility)** | Agents become pure functions; the orchestrator wraps them externally. | Pushes the audit-row write inside the LangGraph node-execution machinery — couples our audit chain to LangGraph's internals, exactly the trap ADR-002 spent a section avoiding. Workloads that don't use LangGraph (Phase 4+ hypothetical) can't reuse the lifecycle. |

### E. Fabrication-resistance mechanism

| Option | Best case for it | Why rejected |
|---|---|---|
| **Critic decomposition + pure-Python substring check (chosen, ADR-005 folded in)** | Auditable: the substring check can be re-run from cold storage. The LLM judges only what it must (parsing prose into claims); the verification is deterministic code. | — |
| **LLM-judge only** (critic emits `passes: bool` from a single LLM call) | Simplest implementation; one prompt; one structured output. | This is exactly the pattern PATTERNS.md #2 warns against — fabrication-resistance must be a validated contract, not a hope. An LLM judge of "is this grounded?" is a hope. |
| **Reference IDs instead of substring** (researcher emits signal IDs; drafter must cite by ID) | Stricter than substring; no fuzzy matches. | Forces the drafter to produce machine-parseable citations inline with natural prose — fights the model's natural output. Fails on paraphrase even when the paraphrase is faithful. Substring after whitespace+case normalisation is the calibrated middle: tight enough to catch hallucinations, loose enough to permit faithful rephrasing. Revisit if Phase 2 calibration shows the substring check letting things through. |
| **Embedding-based grounding score** (compute cosine similarity between draft claims and upstream spans; threshold) | Robust to heavier paraphrase. | Drags in a vector-store / embeddings dependency (CLAUDE.md deferred list — trigger: knowledge base bigger than fits in prompt). Costs more than the substring check for ambiguous gain. Not warranted at Phase 2 scale. |

---

## Consequences

**What becomes easier:**

- **Phase 2 Slice 2-8 implementation has a single blueprint to build against.** Each slice's PR review starts with "does this match ADR-006?"
- **The audit chain `workflow → step → telemetry record` extends naturally to five agents.** Same primitives, same `output_ref` format, same event taxonomy — Slice 7's calibration apparatus works on the 5-agent workflow without modification.
- **Fabrication-resistance ships as a property of the architecture, not a bolt-on.** The substring check is one pure function the critic agent depends on; no late-Phase-2 panic about "how do we make this auditable."
- **The `run_step()` utility makes adding a sixth or seventh agent cheap.** No new audit-lifecycle code per agent. Onboarding cost per agent drops from ~188 lines to ~30.
- **Cross-session checkpoint resume is a single integration point** (the `request_drafter_approval` node + `Command(resume=...)`). Tests live alongside Slice 6 (the orchestrator).

**What becomes harder (accepted costs):**

- **The shared-state shape is load-bearing.** Future workloads that don't fit a strict pipeline (chat loops, debate-style refinement) will not reuse this state shape directly — they'll need a separate ADR. The single workload paying for this design is Phase 2; that's an acceptable trade for production-grade Phase 2.
- **`run_step()`'s explicit recording verbs are slightly noisier than implicit cost extraction.** `step.record_cost(result.cost_usd)` reads as boilerplate. The alternative (the utility imports `llm.py` to know about `LLMResult`) couples the audit lifecycle to the LLM result shape and breaks for non-LLM steps. We accept the three lines.
- **Zero infra retry in Slice 1 means real Phase 2 calibration will see real transient errors.** This is the point — the calibration story needs the failure rate to be honest. The retry ADR will be written from data, not from anxiety.
- **The critic's structural decomposition is itself an LLM call.** If the critic mis-parses the draft into claims (under-decomposition), the pure substring check can miss a fabrication. Mitigation: the critic's prompt and the violation taxonomy are versioned in `.md` files; Phase 2 Slice 7 calibration includes a red-team smoke that injects known-fabrications to verify the critic catches them. This is acknowledged tooth — not pretended away.

**What is deferred:**

- **Bounded infra retry policy.** Re-evaluate after Phase 2 Slice 8 calibration data. If transient errors exceed ~5% of runs, write a follow-up ADR ("Phase 2 retry policy") with the calibration table as the evidence.
- **Parallel research fanout.** Phase 4+ workload trigger; out of scope until a workload genuinely needs it.
- **Embedding-based grounding.** Trigger: substring check lets a fabrication through that an embedding check would catch, AND a vector store is justified for some other reason. Both required.
- **LangGraph version upgrades.** Pin `langgraph>=1.2,<2` in `pyproject.toml` as part of Slice 6 (orchestrator). Treat major-version bumps as their own ADRs (ADR-001 already established this rule).
- **`langgraph-checkpoint-sqlite` install.** Phase 2 Slice 6 dependency add; not this ADR's concern. Until then, `is_workflow_paused_for_checkpoint` + the audit row are the only checkpoint state; the LangGraph runtime checkpointer is unused.

**Forward dependencies handed to Slices 2-8:**

- **Slice 2 (Researcher).** Build `RawSignals` Pydantic model. Wire `researcher_node(state)` through `run_step()` (which means Slice 2 also lands `orchestration/run_step.py` and retrofits the summarizer through it). Mirror `{signal_count, source_count}` onto `step.completed`. ADR-003 (web search tool) must land before this slice starts.
- **Slice 3 (Extractor).** Build `CompanyProfile` with ExtractionGap pattern (PATTERNS.md #2). Each structured field carries a source-span reference back into `RawSignals` — this is the data the critic's substring check will consume. Mirror `{has_size, has_tech_stack, ..., gap_count}` onto `step.completed`.
- **Slice 4 (Scorer).** Build `ScoredCompany` with operator-tunable TOML rubric (ADR-004 must land first). Score-below-floor routes via conditional edge to `terminate_no_draft`. Mirror `{score, threshold, passed_floor}`.
- **Slice 5 (Drafter).** Build `Draft` Pydantic model. Opus 4.7. Input includes the prior critic violation list when `fabrication_retries == 1`. Mirror `{paragraph_count, char_count}`.
- **Slice 6 (Orchestrator).** Wire the LangGraph `StateGraph(CrewState)` per §1. Install `langgraph-checkpoint-sqlite`; wire `SqliteSaver` to the existing DB. Implement `request_drafter_approval` with `interrupt()` + `CheckpointSystem`. Implement conditional edges for the empty-outcome paths and the fabrication-retry. The orchestrator's tests verify cross-session resume end-to-end.
- **Slice 7 (Critic).** Build `Claim` / `Violation` / `Critique` Pydantic models. Implement `agents/fabrication.py` with the pure-Python normalisation + substring check. Red-team smoke: inject known fabrications into a synthetic draft, verify the critic + substring check halt the workflow.
- **Slice 8 (Live calibration).** 3-5 real test companies. Real cost numbers across all five agents. Cost-distribution table per agent. README update — first audit-grade multi-agent calibration story in the portfolio.

**What would invalidate this decision:**

- Phase 2 calibration shows the substring check fails (lets fabrications through, or rejects faithful paraphrases at a rate that breaks usability). Re-evaluate option E with the calibration evidence.
- A regulated-industry buyer requires a specific approval-flow shape (e.g. dual sign-off, segregated reviewer roles) that the single-checkpoint pattern can't express. Write a follow-up ADR; the architecture is open to multiple checkpoint nodes if needed.
- LangGraph 2.x redesigns `interrupt`/`Command(resume=...)`. Re-evaluate the checkpoint wiring; the CheckpointSystem audit fact is unaffected (that was the whole point of keeping the two systems independent).
- Phase 4+ introduces a workload that genuinely needs parallel-agent fanout or a non-linear topology. The crew architecture stays; a sibling ADR documents the second-workload shape.
