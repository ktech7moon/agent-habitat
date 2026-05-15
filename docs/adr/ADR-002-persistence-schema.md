# ADR-002: Persistence schema

**Status:** Accepted (2026-05-13)

## Context

ADR-001 selected LangGraph and explicitly handed this decision forward: LangGraph ships its own `BaseCheckpointSaver` interface with built-in `SqliteSaver` and `PostgresSaver` implementations. Those serialize a runtime-state blob keyed by `thread_id` and `checkpoint_id`. The interface is optimized for one job — rehydrating a workflow's full graph state on resume — and the blob is opaque from SQL.

agent-habitat needs something different: queryable rows with per-step status, cost, and timestamps. Auditors do not want to know "what does the State dataclass look like at checkpoint #17"; they want to know "which agent ran at 14:03, what was its outcome, and how much did it cost." Those are two data models — one for state restoration, one for retrospective query — and conflating them produces something good at neither.

Phase 1-2 constraints frame the decision:

- SQLite only. The Postgres migration trigger (per `CLAUDE.md`) is >100k workflow records OR multi-process concurrency; neither is in scope.
- Single-process execution.
- Phase 2's 5-agent crew runs at least 5 `workflow_steps` per workflow before any retry/critic edges. Step-level queries must be cheap.
- `output_ref` pattern: large outputs live in `data/logs/*.jsonl`; the DB holds references, not blobs.
- Halt/resume across sessions is non-negotiable per ADR-001 — the resume path must work without custom replay logic.
- Per-step cost, status, and timestamps must be normal SQL columns, not JSON inside a blob.

## Decision

**Two parallel schemas in one SQLite file (Option 1).** LangGraph's `SqliteSaver` writes its checkpoint tables; agent-habitat writes `workflows`, `workflow_steps`, `events` to the same `data/state/agent_habitat.db` file. They share an identifier: agent-habitat's `workflows.id` is passed to LangGraph as `thread_id`. The two writers are independent — no shared transaction, no inheritance hierarchy crossing the boundary.

In one sentence: the two systems answer different questions, so they get different tables, and we use the simplest possible mechanism (shared file + shared key) to relate them.

### Schema (illustrative DDL — implementation is Slice 2)

```sql
-- agent-habitat audit tables.
-- LangGraph's own tables (checkpoints, writes, …) are created by SqliteSaver
-- in the same SQLite file. We do not write to them, and they do not write to ours.

CREATE TABLE workflows (
  id              TEXT PRIMARY KEY,        -- also passed to LangGraph as thread_id
  workflow_type   TEXT NOT NULL,           -- 'url_summarizer', 'lead_enrichment', …
  status          TEXT NOT NULL CHECK (
                    status IN ('running','paused','completed','failed','cancelled')),
  started_at      TEXT NOT NULL,           -- ISO-8601 UTC
  finished_at     TEXT,
  cost_total_usd  REAL NOT NULL DEFAULT 0.0,
  metadata        TEXT                     -- JSON; free-form workflow-specific fields
);

CREATE TABLE workflow_steps (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id     TEXT NOT NULL REFERENCES workflows(id),
  step_index      INTEGER NOT NULL,        -- monotonic within the workflow
  agent_name      TEXT NOT NULL,
  status          TEXT NOT NULL CHECK (
                    status IN ('running','completed','failed','skipped')),
  started_at      TEXT NOT NULL,
  finished_at     TEXT,
  input_ref       TEXT,                    -- 'data/logs/YYYY-MM-DD/<wf>.jsonl:<line>'
  output_ref      TEXT,                    -- same shape
  cost_usd        REAL NOT NULL DEFAULT 0.0,
  error_message   TEXT,
  UNIQUE (workflow_id, step_index)
);

CREATE TABLE events (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id     TEXT NOT NULL REFERENCES workflows(id),
  step_id         INTEGER REFERENCES workflow_steps(id),   -- nullable
  timestamp       TEXT NOT NULL,
  level           TEXT NOT NULL,           -- 'info','warn','error','checkpoint','approval'
  message         TEXT NOT NULL,
  structured_data TEXT                     -- JSON
);

CREATE INDEX idx_workflow_steps_wf  ON workflow_steps(workflow_id, step_index);
CREATE INDEX idx_events_wf_time     ON events(workflow_id, timestamp);
```

### `output_ref` mechanics

- **Format:** `data/logs/YYYY-MM-DD/<workflow_id>.jsonl:<line_number>`. One JSONL file per workflow per day keeps reads cheap and grepping by `workflow_id` trivial. Line numbers are 1-indexed (matches `awk NR` / editor jump semantics).
- **Write path:** `llm.py` owns the JSONL append. Every LLM call appends one line — `{timestamp, model, input_tokens, output_tokens, cost_usd, response_text, …}` — and returns the structured response *plus* the resulting `jsonl_ref` (`path:line`). The caller writes that ref into `workflow_steps.output_ref` (or `input_ref` for a predecessor's handed-in output). The JSONL append happens *before* `llm.py` returns, so the ref is durable before any audit row claims it.
- **Why path:line, not blob:** `CLAUDE.md` defers LangSmith/observability SaaS until ripgrep-on-JSONL outgrows itself. String `path:line` refs play cleanly with `rg`, `awk 'NR==N'`, `sed -n 'Np'`, and editor jumps. Blob-in-DB is rejected — it removes the grep ergonomic and inflates SQLite for no query gain.

## Alternatives Considered

| Option | Best case for it | Why rejected |
|--------|------------------|--------------|
| **1. Two parallel schemas, same SQLite file** *(chosen)* | Cleanest separation of concerns; each side does its job. Zero impedance mismatch between LangGraph's blob model and our queryable rows. LangGraph version churn is confined to a single integration point. Halt/resume is "point LangGraph's default `SqliteSaver` at the same `.db` file" — three lines. | — |
| **2. Custom `BaseCheckpointSaver` that writes both** | One write call from agent code; checkpoint and audit row land together. If wrapped in a single SQLite transaction, half-failures are impossible. Forces audit-row creation — you cannot forget to write to `workflow_steps`. | Imports the dual-write problem shape (analyzed below). The atomicity claim depends on LangGraph's saver letting us control the transaction boundary — a contract we don't own and which changes across versions. The interface (`put`/`get`/`list` over State blobs) does not naturally accommodate per-step cost; audit fields would be smuggled through a foreign API. ADR-001 already flagged LangGraph API churn as an accepted cost; this option compounds it. |
| **3. LangGraph-only + view/query layer** | Single source of truth, single schema, smallest maintenance surface. No drift possible. | Per-step cost has to live somewhere; the only place it can live is inside the State blob, which means our "audit view" becomes `json_extract(blob, …)` across blobs. That is precisely the "queryable JSON inside blobs" pattern an auditor or regulated-industry buyer rejects, and it scales badly with Phase 2's many-steps-per-workflow shape. We'd also couple our query layer to LangGraph's internal table layout, which is not stable. The property that distinguishes this project (audit-grade queryable rows) is the property this option erodes. |
| **4. LangGraph-as-stateless; agent-habitat owns all persistence** | Sidesteps dual-write entirely. Total schema control. No LangGraph internals to track. | Re-implements the capability we picked LangGraph for in ADR-001 (first-class checkpointing; the `interrupt`/`Command(resume=...)` primitive Slice 5 needs for human approvals). Materially larger surface area, more bugs, slower path to Phase 2. The contradiction is direct: ADR-001's stated reason for choosing LangGraph was checkpointing-as-primitive — turning it off here would require either reversing ADR-001 or defending a position the prior ADR doesn't support. |

A fifth shape — in-memory checkpointer plus a custom replay path for cross-session resume — was considered and collapses into Option 4 once you require resume *across sessions*, which ADR-001 names as non-negotiable. Outbox-pattern variants are over-engineering for single-process SQLite Phase 1.

### Dual-write hazard — verdict

The decision turns on Option 2's dual-write consistency. The honest answer: both Option 1 and Option 2 involve two writes (one to LangGraph's tables, one to ours), but the failure modes are different in kind.

**Option 2** tries to make the two writes atomic by inheriting LangGraph's checkpointer interface and overriding `put` to also write to our schema. The atomicity claim requires the inherited code to participate in a transaction we control. SQLite makes this *possible* — one file, one process, one connection if we wire it that way — but the property depends on LangGraph's implementation details continuing to cooperate across versions. When that cooperation breaks silently (a future `SqliteSaver` opens its own connection, or commits internally), we have a corrupted invariant we were relying on. That is the dual-write hazard in its classic form: a hidden coupling we cannot statically verify and which fails silently.

**Option 1** has two writes but does not claim atomicity. A failure in step N's audit write while LangGraph's checkpoint already committed is a **recoverable** condition:

- The missing row is detectable by a simple anti-join (`SELECT thread_id FROM langgraph_checkpoints WHERE thread_id NOT IN (SELECT id FROM workflows)`).
- Audit rows are written *before* invoking the agent step — `INSERT workflow_steps (status='running', finished_at=NULL)` — and the pre-step row is itself the recovery anchor.
- On startup recovery, scan `workflow_steps WHERE status='running' AND finished_at IS NULL`, find orphaned steps, and reconcile against LangGraph's checkpoint state (mark `failed` with a synthesized event, or resume).

This is saga-with-idempotency, not dual-write-under-transaction, and it is the right shape for an audit log anyway — an audit log wants to know about steps that *didn't* complete.

**Verdict:** Option 2's claimed atomicity is brittle in a way Option 1's claimed independence is not. We pay a small drift-detection cost in Option 1 to avoid a much larger invariant-protection cost in Option 2. Option 1 wins.

## Consequences

**What becomes easier:**

- **Halt/resume.** LangGraph's default `SqliteSaver` is wired to `data/state/agent_habitat.db`. Standard `graph.invoke(input, {"configurable": {"thread_id": workflow_id}})` works with no custom checkpointer code.
- **Queryable audit.** Per-step cost, status, and timestamps are normal SQL columns. Daily-budget queries are `SELECT SUM(cost_total_usd) FROM workflows WHERE started_at >= ?`. Step-level inspection is `SELECT * FROM workflow_steps WHERE workflow_id = ? ORDER BY step_index`.
- **LangGraph version isolation.** ADR-001 flagged LangGraph API churn as an accepted cost. Option 1 confines that risk to a single integration point (constructing `SqliteSaver`) rather than spreading it across an inherited subclass.
- **Slice 5 (CheckpointSystem).** Human-in-the-loop checkpoints use LangGraph's native `interrupt`/`Command(resume=...)` while writing an `events` row at `level='approval'` for audit. No conflict between the two systems.

**What becomes harder (accepted costs):**

- **Drift detection.** Two writers means occasional reconciliation. Mitigation: pre-step audit rows are the recovery anchor; a startup sweep on `workflow_steps WHERE status='running' AND finished_at IS NULL` reconciles against LangGraph state. Document the reconciliation procedure in the Slice 2 README.
- **Some timestamp duplication.** LangGraph stamps its checkpoint write time; we stamp `started_at`/`finished_at` on our row. These will not match to the millisecond. Accepted — they aren't the same event (LangGraph stamps a state-snapshot moment; we stamp a step lifecycle).
- **Schema migrations are split.** Ours and theirs live in the same file but evolve independently. A LangGraph minor-version bump may run its own migration on its tables; ours migrate via our own (yet-to-be-written) migration runner. Acceptable for Phase 1 — both schemas are small.
- **Storage overhead.** Some data lives in both stores (a step's outcome appears in our `workflow_steps` row and inside LangGraph's State blob). At Phase 1-2 volume this is negligible; flag for re-evaluation at the SQLite→Postgres trigger.

**What is deferred:**

- **Postgres migration.** Schema is written in vendor-neutral DDL (TEXT for ISO-8601, REAL for cost; `AUTOINCREMENT` is the one SQLite-ism and translates trivially to `BIGSERIAL`). When the trigger fires (>100k workflows OR multi-process), translation is mechanical.
- **Multi-process concurrency.** This decision presumes single-process SQLite. Multi-process orchestration would force a re-evaluation — LangGraph's `PostgresSaver`, our locking semantics, and the migration would all need to land together.
- **Cross-workflow indexing.** No index on `workflows.workflow_type` or `workflows.status` yet — single-table scans are cheap at Phase 1-2 volume. Add when needed.

**Forward dependencies handed onward:**

- **To Slice 2 (WorkflowState + persistence layer):** the DDL above is the spec. `persistence.py` will own schema creation/migration, expose typed CRUD over `workflows`/`workflow_steps`/`events`, and wire LangGraph's `SqliteSaver` to the same `.db` file. Halt/resume tests exercise the LangGraph checkpoint path and the audit-row path independently, then verify a third test that they share the same `workflow_id`/`thread_id`.
- **To `llm.py` (next session's work):** every `llm.py` call returns a structured result that includes `cost_usd`, `input_tokens`, `output_tokens`, and a `jsonl_ref` (`path:line`) for the telemetry line it just wrote. The agent-step layer aggregates `cost_usd` into `workflow_steps.cost_usd` (and rolls up into `workflows.cost_total_usd`) and records `jsonl_ref` in `workflow_steps.output_ref`. The JSONL append must precede `llm.py`'s return, so the ref is durable before any audit row claims it. This is the contract `llm.py` owes upstream — and it's why `llm.py` must land before Slice 2 wires the audit path.

**What would invalidate this decision:**

- LangGraph deprecates or substantially redesigns the `BaseCheckpointSaver` interface (forces re-evaluation but does not necessarily change the Option 1 answer — independence from their interface is the whole point).
- A regulated-industry buyer mandates an event-sourcing or append-only-log persistence model — would force re-evaluation of all four options under that frame.
- Phase 3+ workloads require multi-process orchestration, triggering the Postgres migration. Re-validate the dual-schema split under Postgres concurrency semantics at that time.
