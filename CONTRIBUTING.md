# Contributing

agent-habitat is a working portfolio project demonstrating production-grade multi-agent infrastructure patterns. Issues and PRs are welcome; responses are best-effort on a single-maintainer timeline.

## Before opening a PR

The CI workflow runs these four gates. All must pass:

```bash
pytest -m "not live"
ruff check src tests
ruff format --check src tests
mypy src
```

The 538-deterministic-test baseline is load-bearing. PRs that drop tests or weaken assertions need a clear rationale.

## ADR discipline

Non-trivial changes require an ADR in [`docs/adr/`](docs/adr/). The Alternatives section is mandatory — what else was considered and why it was rejected.

**Non-trivial:** new agent, change to the fabrication chain or audit-event taxonomy, change to the persistence schema, new runtime dependency, change to the retry policy, change to the LangGraph orchestration topology.

**Trivial:** bug fixes within an existing module, documentation, test improvements, small refactors that don't cross module boundaries.

When in doubt, open an issue to discuss before writing code.

## Layer A / Layer B discipline

Agents follow a strict split: pure Layer A node functions (no DB writes, no `run_step` context) wrapped by Layer B workflow ownership (`run_step`, persistence, event emission). See any existing agent in [`src/agent_habitat/agents/`](src/agent_habitat/agents/) for the pattern. PRs that blur the split will be asked to refactor.

## Protected test files

These files define agent behavior contracts:

- `tests/agents/test_researcher.py`
- `tests/agents/test_extractor.py`
- `tests/agents/test_scorer.py`
- `tests/agents/test_summarizer.py`
- `tests/agents/test_drafter.py`
- `tests/agents/test_critic.py`
- `tests/orchestration/test_crew_graph.py`

Editing them is a behavior change, not a refactor. PRs touching these files need an explicit rationale and usually a paired ADR.

## Commit messages

Conventional prefixes: `feat:`, `fix:`, `docs:`, `chore:`, `refactor:`, `calibration:`, `ci:`. Reference the ADR or issue number when applicable. Keep the subject line under 72 characters; explain the *why* in the body when the change isn't self-evident from the diff.

## Tool discipline

All LLM calls go through [`src/agent_habitat/llm.py`](src/agent_habitat/llm.py). No direct `anthropic` SDK imports elsewhere — this is enforced by convention and by code review.
