# agent-habitat — Carry-Forward Patterns

These patterns are established across three prior projects and are the portfolio's through-line. agent-habitat inherits all of them.

1. **AUDIT-GRADE OUTPUT** — every decision logged with rule fired, reasoning trace, confidence/score; JSONL telemetry per LLM call.

2. **FABRICATION-RESISTANCE AS VALIDATED CONTRACT** — prior projects: reasoning quotes the lead's actual text; offending_phrase must be a verbatim substring of source or it errors; ExtractionGap surfaces missing fields instead of fabricating. For agent-habitat: the drafter cites ONLY signals earlier agents produced; cross-agent substring validation.

3. **CALIBRATION STORY** — live API runs surface things mocked tests cannot; document these as features, not bugs. agent-habitat will produce its own calibration moments — capture them.

4. **DECISION-SUPPORT, NOT ADVICE** — every consequential user-visible output is structurally disclaimed.

5. **OPERATOR TUNES OUTCOMES; DEVELOPERS TUNE PROMPTS** — TOML rubrics for operator-tunable scoring; agent prompts in versioned .md files; thresholds in code.

6. **ADR-BEFORE-CODE** — every non-trivial decision gets a numbered ADR with alternatives considered, before implementation.

7. **THREE-TIER MODEL ROUTING, DEFAULT DOWN** — Haiku grunt work, Sonnet workhorse, Opus premium. A bad outreach message is recoverable in seconds; a bad architecture decision costs a week.

8. **CONSISTENT SCAFFOLDING** — same layout, same CLAUDE.md sections, same Working Agreement, same STATUS.md format across projects.

9. **FIREWALL HOOK** — pre-bash-firewall.sh blocks destructive commands. It has caught real mistakes. Never remove or work around it.

10. **CONTEXT DISCIPLINE** — see Working Agreement rule 12. Added after a 99%-daily-usage day caused by parallel Opus-xHigh sessions at high context.
