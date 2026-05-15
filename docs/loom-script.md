# Loom recording — agent-habitat walkthrough

**Target length:** 4–7 minutes (aim for ~5).
**Audience:** strangers deciding in the first 60 seconds whether to take this project seriously — AI tooling engineers, FDE teams, regulated-industry operators.
**Format:** talking points, not a word-for-word script (reads wooden when memorised). Demo terminal kept visible the whole time.

---

## Production checklist (before hitting record)

- [ ] Terminal font ≥ 16pt at 720p; verify by recording 10s and previewing.
- [ ] `clear` between every section so each segment starts on a clean screen.
- [ ] One terminal, one tab; no IDE clutter behind it.
- [ ] `.env` exported in this shell (`ANTHROPIC_API_KEY` set).
- [ ] `data/state/agent_habitat.db` exists with recent crew runs in it (we'll query it on screen — pre-populate with at least one historical Anthropic run AND the Slice 8 Plaid halt so both query results are ready).
- [ ] Working tree clean; the `git log --oneline -8` shot in the close needs the Phase 3 prep commits visible.
- [ ] Plan for 2–3 takes. One take is rarely good enough; the first usually surfaces the awkward phrasings to fix on take 2.
- [ ] Speak slightly slower than feels natural. Pauses are fine; rushed delivery is not.
- [ ] Don't read the bullet points verbatim — internalise them, then talk.

---

## 0:00 – 0:30  Open

**Shot:** terminal full-screen, README.md open on a second monitor or window for occasional reference.

**Talking points:**
- Who you are, one line. ("I'm Joseph, a senior engineer building production AI agents in regulated industries.")
- What agent-habitat is, one line. ("A multi-agent orchestration framework with audit-grade persistence, fabrication-resistance enforcement, and bounded halt-not-ship discipline — built for environments where you have to be able to say exactly what an agent did, why, and at what cost.")
- What this video shows. ("In the next five minutes I'll run the 5-agent lead-enrichment crew end-to-end, show you the human checkpoint pause, and then query the audit trail from cold storage with no LLM in the loop.")

**Time discipline:** if you're past 0:35 by the time you finish "what this video shows", cut and re-record. The open is where viewers bounce.

---

## 0:30 – 1:30  The pitch — three differentiators

**Shot:** terminal still; optionally cut to README's "60-second pitch" section briefly to anchor the three points.

**Talking points (one paragraph each):**

1. **Fabrication-resistance as a validated contract, not a hope.**
   ("Most multi-agent demos hand the drafter a profile and trust it. This one runs a critic agent over every claim the drafter makes and walks a five-hop substring chain — from the prose down to the original web_search citation. If any hop fails, the workflow halts rather than ship the draft. That's ADR-006 §3 and the critic's in `src/agent_habitat/agents/critic.py`.")

2. **Audit-grade everything.**
   ("Every workflow row, every step, every event, every LLM call has a cold-storage audit chain you can query without re-running the agent. SQLite for the workflow tables, JSONL telemetry per LLM call with cost in USD and `stop_reason`, and an event taxonomy that includes a dedicated `agent.fabrication_detected` row when the contract fires. I'll show you the chain in section three.")

3. **Bounded retry, then halt-not-ship.**
   ("On a fabrication, the critic feeds violation context back to the drafter for ONE retry. If the second attempt still doesn't ground out, the workflow halts as FAILED with the violating draft, the critic's per-claim verdicts, and the upstream signals all preserved on disk. The framework's job is not to always ship — it's to never ship something it can't defend.")

**Fallback if 1:30 is getting tight:** drop point 2's example and just say the words "audit-grade — I'll show you in a minute."

---

## 1:30 – 3:30  Live demo — `agent-habitat run-crew Anthropic`

**Shot:** terminal, full focus.

**Sequence (~2 minutes):**

```bash
clear
agent-habitat run-crew "Anthropic"
```

**Narrate in real time** as the output rolls:

- "Researcher firing first — Haiku, doing web_search against the live Anthropic API. You'll see cost roll up in the JSONL telemetry."
- "Extractor next — Sonnet, takes the researcher's signals and turns them into a `CompanyProfile` with source-span references back to the citations."
- "Scorer — Sonnet again, applies the operator-tunable TOML rubric. Renormalises over scorable dimensions, surfaces a coverage number."
- **Pause point.** When the workflow hits `request_drafter_approval` and pauses:
  > "Here's the human checkpoint. Before paying Opus 4.7 tokens for the draft, the workflow paused. The score is right there; the rubric coverage too. As the operator I approve or reject from a second terminal."

**Open a second terminal (or split):**

```bash
agent-habitat checkpoint list
agent-habitat checkpoint show <id>
agent-habitat checkpoint approve <id> --reviewer "Joseph (live demo)"
```

- "The approval is durable. If I came back hours from now, the workflow would resume from exactly this point — LangGraph's `SqliteSaver` is wired to the same database file as the audit tables."

**Resume:**

```bash
agent-habitat run-crew --resume <workflow_id>
```

- "Drafter — Opus 4.7. This is the only Opus call in the pipeline; everyone else is Haiku or Sonnet. The Drafter writes the outreach prose with explicit per-claim grounding to scorer dimensions."
- **Critic fires.** Watch for the outcome:
  - **Branch A — critic passes** (most likely on Anthropic since it's been calibrated against): "Critic walked all five hops, every claim grounded. Workflow finalised COMPLETED with the draft."
  - **Branch B — critic fires retry**: "Critic caught a substring miss — likely a paraphrase the Drafter introduced. One bounded retry; you'll see the drafter run again with the violation context attached."
  - **Branch C — critic halts after retry** (rare on Anthropic, very real on harder companies): "Persistent fabrication after one retry. Workflow halts as FAILED. This is the project's posture: refuse to ship rather than ship something we can't defend."

**Fallback talking point** (have ready, do not improvise): "Slice 8's calibration ran four companies. Plaid produced exactly the halt branch — Tier-A score, the critic caught a fabrication, the retry didn't fix it, the workflow refused to ship. I'll show you that audit trail next."

---

## 3:30 – 4:30  The audit trail — query from cold storage

**Shot:** terminal. Use `sqlite3` interactively or via one-liners — pick whichever runs cleaner on your machine.

**Three queries, narrated:**

```bash
sqlite3 data/state/agent_habitat.db
```

```sql
.mode column
.headers on

-- The workflow row (one per crew invocation).
SELECT id, workflow_type, status, started_at, finished_at, cost_total_usd
  FROM workflows
 ORDER BY started_at DESC
 LIMIT 3;
```

- "Three workflows, each with status, timing, and total cost in USD. The Plaid one ended `failed` — that's the halt-not-ship I just talked about."

```sql
-- The per-agent step rows for that workflow.
SELECT step_index, agent_name, status, cost_usd, output_ref
  FROM workflow_steps
 WHERE workflow_id = '<plaid-workflow-id>'
 ORDER BY step_index;
```

- "Five rows — researcher, extractor, scorer, drafter, critic. Each row's `output_ref` points into a JSONL telemetry file."

```sql
-- The fabrication-detected event row.
SELECT timestamp, event_type, level, message
  FROM events
 WHERE workflow_id = '<plaid-workflow-id>'
   AND event_type = 'agent.fabrication_detected';
```

- "Here's the contract firing — `agent.fabrication_detected` at WARN level, with the failed claims listed in `structured_data`. An auditor opens this row and gets every reason the framework refused to ship."

**Then show one JSONL line:**

```bash
ls data/logs/$(date +%Y-%m-%d)/
head -1 data/logs/$(date +%Y-%m-%d)/<file>.jsonl | python -m json.tool | head -30
```

- "And the JSONL telemetry — model, tokens, cost, the response. Re-runnable substring check, no LLM call needed. That's what 'audit-grade' means."

---

## 4:30 – 5:00  Close

**Shot:** terminal, but glance to README on screen-share if useful.

**Talking points:**
- "Repo: github.com/ktech7moon/agent-habitat. README has the full calibration story, every ADR, and a quick-start that runs against your own Anthropic key."
- "If you've built multi-agent systems before, the differentiator here is the audit chain and the halt-not-ship discipline, not the agent count."
- Call to action: "Read the ADRs — ADR-001 (LangGraph over CrewAI), ADR-006 (crew architecture + fabrication contract), ADR-007 (retry policy). Try it. If it's useful, tell me what's missing."

**End.** Do not narrate over a fade-out; just stop talking and let Loom cut.

---

## Shot list (for editing)

| Section | Time | Source | Notes |
|---|---|---|---|
| Open | 0:00–0:30 | Terminal, possibly with README cameo | Keep visible: the project name, your face if comfortable |
| Pitch | 0:30–1:30 | Terminal + README section anchor | One README scroll, no more |
| Demo | 1:30–3:30 | Terminal only | Resist the urge to cut; real-time is the point |
| Audit | 3:30–4:30 | Terminal + sqlite3 | Pre-populate the DB so queries are fast |
| Close | 4:30–5:00 | Terminal | Show `git log --oneline -8` if there's time |

---

## What to AVOID

- Reading the README aloud during the demo. The demo is the artefact; the README is the supporting cast.
- Showing CLAUDE.md or STATUS.md on camera. These are working files; they read as internal.
- Apologising for anything pre-emptively ("it's a small project", "this is just a demo"). The work is real; describe it that way.
- Improvising explanations of code you don't have ready. If a viewer wants more depth, the README + ADRs are where they go — say so and move on.
- Recording the halt-path live. Slice 8's Plaid run produced the halt deterministically against that scoring rubric, but live calibration changes the outcome distribution. Reference it; query its audit trail; don't bet a take on it.

---

## After recording

1. Watch the take through at 1.25× speed once. Note timestamps where you'd cut on a re-record.
2. If take 1 is 90% there, keep it. The first take's energy is hard to recapture.
3. Loom autotranscribes; spot-check the transcript for one or two replaced words and fix in the description.
4. Title: `agent-habitat — production-grade multi-agent orchestration` (or similar; specific enough to filter Loom search).
5. Description: one paragraph (what the framework does, the three differentiators verbatim from §1 of the README), then the repo URL.
6. Update README's "Watch the 5-minute walkthrough" link to the Loom URL.
