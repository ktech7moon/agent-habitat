# Security Policy

## Reporting a vulnerability

Open a GitHub issue with the `security` label: <https://github.com/ktech7moon/agent-habitat/issues>.

For private disclosure, reach out via the contact path on the maintainer's GitHub profile: <https://github.com/ktech7moon>.

This is a single-maintainer project. Public issues are the primary path; private email is available if the nature of the finding warrants it.

## Supported versions

Pre-1.0. Only `main` is supported. Findings against older commits will be assessed against current `main` — no backports.

## In scope

Issues that affect the framework's audit-chain integrity:

- Ways to bypass or subvert the critic's substring-grounding check (the fabrication contract, [ADR-006 §3](docs/adr/ADR-006-crew-architecture.md)).
- Ways to corrupt the audit chain (`workflows` / `workflow_steps` / `events` rows, JSONL telemetry, or LangGraph checkpoint blobs) such that the trail no longer reflects what the framework actually did.
- Ways to cause the framework to log telemetry that should not be logged, or to leak telemetry across workflows.
- Dependency vulnerabilities reachable from default install (`pip install -e ".[dev]"`).

## Out of scope

Concerns documented as deployment responsibilities in the README's [Production Considerations](README.md#production-considerations) section:

- PII redaction at the observability layer (the framework ships the mechanism, not a redaction rule set).
- Reviewer authentication, identity, and notification fan-out for the human-in-the-loop checkpoint primitive.
- Operator-supplied configuration choices (rubric content, budget caps, API key handling outside the framework).

## Response

Best-effort, single-maintainer project. No formal SLA. Acknowledgement within a reasonable window; remediation timeline depends on severity and reproducibility.
