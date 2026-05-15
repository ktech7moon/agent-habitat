# ADR-001: LangGraph over CrewAI

**Status:** Accepted (2026-05-13)

## Context

agent-habitat is the operational layer beneath any multi-agent workload — its value is what it provides around the agents (persistence, observability, cost control, human checkpoints), not the agents themselves. The orchestration framework choice locks in how every one of those concerns is implemented; getting it wrong cascades into every subsequent slice.

The framework must support four requirements that are non-negotiable for the regulated-industry production posture this project is built around:

1. **Halt/resume across sessions.** A workflow started Monday must be resumable Wednesday from exactly the step it stopped on, with full state. This is a hard requirement, not a "nice to have" — it's what separates infrastructure from a script.

2. **Human-in-the-loop checkpoints.** The workflow pauses before flagged actions (sending outreach, publishing, anything irreversible), writes a pending-approval record, and resumes cleanly on approval. The pause/resume primitive must be native, not bolted on.

3. **Per-action observability and audit-grade telemetry.** When an agent handoff fails in production, the answer to "what state were we in, what did the previous agent return, what did the next agent receive" must be a query against structured data — not a re-run with verbose logging. Every transition needs to be an inspectable event.

4. **Inspectable multi-agent coordination.** A 5-agent crew's handoff topology must be readable as a diagram, debuggable as a graph, and modifiable without rewriting prompts. Coordination logic that lives inside LLM prompts is opaque to static analysis, opaque to operators tuning rubrics, and opaque to anyone reading the code six months later.

These aren't generic "good engineering" requirements — they're table stakes for the buyer pattern this portfolio targets (AI tooling companies, regulated-industry operators, FDE teams). A framework that makes any of them awkward is wrong for this project, even if it's right for many others.

## Decision

**Use LangGraph as the orchestration framework.** LangGraph models a multi-agent workflow as an explicit state machine: nodes are agents, edges are transitions (including conditional), and a typed State object flows through the graph. Every state transition is an inspectable event, checkpointing is a first-class primitive (not an afterthought), and the four non-negotiable requirements above each map cleanly to a single LangGraph concept — which means we build infrastructure around the framework rather than fighting it.

## Alternatives Considered

| Option | Best case for it | Why rejected for agent-habitat |
|--------|------------------|--------------------------------|
| **CrewAI** | Fastest path to a working multi-agent prototype. Intuitive role + task model maps neatly to how non-engineers describe agent crews ("a researcher, an analyst, a writer"). Excellent tutorial story; growing community. | Handoffs are expressed inside prompts ("delegate to the Researcher"), which makes them opaque to static inspection and to checkpointing. Pause/resume and durable state are retrofits, not primitives. For a 30-minute prototype, CrewAI wins; for infrastructure an auditor traces, the missing seams cost more than the role-model ergonomics save. |
| **AutoGen** | Powerful conversation-based model; strong support for agents that negotiate iteratively. Microsoft Research backing and an active ecosystem. The conversation model is genuinely the right abstraction for some problems (research synthesis, debate-style workflows). | The "what state is the workflow in right now" question is fundamentally harder when state is implicit in a message history. Termination conditions become heuristic. Checkpointing exists but is less mature than LangGraph's. For our requirements, modeling workflows as conversations is the wrong shape — we want explicit graphs, not implicit chats. |
| **Custom asyncio orchestration** | Maximum flexibility; zero framework lock-in; no version-churn dependency. The state model, checkpointer, and observability can be designed for exactly our schema. | Rebuilds LangGraph's checkpointer, state typing, conditional-edge primitives, and visualization tooling from scratch — work that doesn't differentiate this project from the next freelancer's. The right call only if a framework's constraints become an active obstacle; today they don't. |

CrewAI deserves a second look specifically because its prototyping speed is real and not a strawman: the role/task abstraction does map well to how the operator describes the Phase 2 crew ("Researcher, Extractor, Scorer, Drafter, Critic"). The decision is not "CrewAI is bad" but "for this project, the things CrewAI hides — handoff mechanics, pause points, state — are the things we need to expose."

## Consequences

**What becomes easier:**

- **Checkpointing.** LangGraph ships a `BaseCheckpointSaver` interface with SQLite and Postgres implementations. The Slice 5 CheckpointSystem can build on this rather than inventing pause/resume from scratch.
- **Persistence boundaries.** State enters and leaves each node as a typed object — Slice 2's WorkflowState Pydantic model lines up naturally with LangGraph's State protocol. Snapshots happen at edges.
- **Observability.** Every state transition is a discrete event LangGraph emits; the Slice 4 ObservabilityLayer can hook the graph rather than wrapping each agent call manually.
- **Multi-agent coordination (Phase 2).** The 5-agent crew becomes a graph with explicit edges (Researcher → Extractor → Scorer → [checkpoint] → Drafter → Critic), not a string of prompted handoffs. Conditional edges express retry/error logic clearly.

**What becomes harder (accepted costs):**

- **Learning curve.** LangGraph's graph-first mental model is less immediately intuitive than CrewAI's role model. Plan to spend Slice 1 internalizing the primitives.
- **Boilerplate for trivial workflows.** A single-agent workflow in CrewAI is ~10 lines; in LangGraph it is ~40. We accept this because Slice 6's demo agent is a stepping stone to Phase 2's real multi-agent workload — boilerplate amortizes.
- **API stability.** LangGraph is post-1.0 but still actively evolving; minor versions have historically introduced breaking changes. **Pin the version in `pyproject.toml`** and treat upgrades as their own ADR-worthy decisions.
- **LangChain ecosystem coupling.** `langgraph` brings `langchain-core` (and we add `langchain-anthropic` for model bindings). LangChain's own version churn becomes a dependency surface to monitor. We accept this rather than reimplementing graph primitives.
- **Team must think in graph terms.** Not a fit for collaborators who expect a role-based mental model. Documentation and ADR-002 onward should reinforce the state-machine framing.

**What is deferred:**

- **LangGraph Studio / cloud features.** Local-only execution for Phase 1-2. Revisit if multi-process orchestration becomes a need.
- **LangSmith observability.** Already deferred in CLAUDE.md until JSONL queries outgrow ripgrep. LangGraph integrates with LangSmith but we won't enable it.

**Forward dependency for ADR-002 (persistence schema):**

LangGraph ships its own `Checkpointer` abstraction with built-in `SqliteSaver` / `PostgresSaver` implementations. These serialize full graph state blobs keyed by `thread_id` and `checkpoint_id` — they're optimized for "restore the workflow's runtime state," not "report on what happened, when, and at what cost." Our audit-grade requirements need queryable rows: `workflows(id, status, started_at, finished_at, cost_total)`, `workflow_steps(workflow_id, agent_name, …, cost)`, `events(workflow_id, timestamp, level, …)`.

**ADR-002 must decide how these two schemas relate.** Three viable shapes:

1. **Two parallel schemas, same SQLite file.** LangGraph's checkpointer writes its tables; we write ours. They share a `thread_id`/`workflow_id`. Maximally honest separation; some duplication of timestamps.
2. **Single schema, custom checkpointer.** Implement a `BaseCheckpointSaver` subclass that writes both LangGraph's checkpoint blobs and our audit tables atomically. Tighter coupling; more code to maintain through LangGraph version upgrades.
3. **LangGraph-only, view layer.** Use LangGraph's tables as the sole storage and project the audit views from them. Cheapest to maintain; loses the per-step cost column unless we shoehorn it into LangGraph's metadata.

ADR-001 does not resolve this — it hands it to ADR-002 as the question to answer first.

**What would invalidate this decision:**

- LangGraph deprecates or substantially redesigns its Checkpointer interface (forces a re-evaluation of options 1-3 above).
- A regulated-industry buyer mandates a specific orchestration framework as a procurement requirement.
- The cost of carrying `langchain-core` becomes higher than the cost of reimplementing graph primitives (unlikely on the Phase 1-2 horizon).
