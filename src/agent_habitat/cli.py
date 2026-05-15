"""agent-habitat command-line entry point.

Click group with three surfaces today:

  - `version`              — prints the package version (kickoff stub).
  - `checkpoint {list,show,approve,reject}`
                           — the operator-facing HITL approval surface
                             added in Slice 5.
  - `run-summarizer URL`   — Slice 6 demo agent; runs the URL summarizer
                             end-to-end through the habitat.

The orchestrator surface lands in Phase 2.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from agent_habitat import __version__
from agent_habitat.agents import (
    ExtractorResult,
    ResearcherResult,
    SummarizerResult,
    run_extractor,
    run_researcher,
    run_summarizer,
)
from agent_habitat.agents.models import PROFILE_FIELD_NAMES
from agent_habitat.checkpoint import (
    Checkpoint,
    CheckpointError,
    approve_checkpoint,
    get_checkpoint,
    list_pending_checkpoints,
    reject_checkpoint,
)
from agent_habitat.state import DEFAULT_DB_PATH, WorkflowStatus, init_db


# Decision-support framing operators see before approving. Keeps the
# project's posture explicit on the only user-visible surface that asks
# a human to consent to an action.
DECISION_SUPPORT_FOOTER = (
    "This summary is operational context for your approval decision. "
    "It is not legal, medical, or financial advice; verify the action "
    "and its consequences before approving."
)

#: Decision-support footer printed below the agent's summary output.
#: Project posture: every consequential user-visible workflow output is
#: structurally disclaimed (CLAUDE.md non-obvious constraint).
SUMMARIZER_DECISION_FOOTER = (
    "Automated summary; the model can omit, mis-emphasise, or misread "
    "content. Treat as decision support, not as a substitute for reading "
    "the source page."
)

RESEARCHER_DECISION_FOOTER = (
    "Automated research signals; the model can miss, mis-cite, or "
    "stale-cite content. Treat as decision support, not as a substitute "
    "for verifying each source before acting."
)

EXTRACTOR_DECISION_FOOTER = (
    "Automated extraction; structured fields are grounded against the "
    "cited source spans shown but the model can omit, mis-attribute, or "
    "over-narrow. Treat as decision support, not as verified facts."
)


@click.group()
def main() -> None:
    """agent-habitat: production-grade multi-agent orchestration framework."""


@main.command()
def version() -> None:
    """Print the package version."""
    click.echo(__version__)


@main.group()
@click.option(
    "--db",
    "db_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=DEFAULT_DB_PATH,
    show_default=True,
    help="Path to the agent-habitat SQLite file.",
)
@click.pass_context
def checkpoint(ctx: click.Context, db_path: Path) -> None:
    """Human-in-the-loop approval surface.

    Workflows request a checkpoint before a flagged action (sending
    outreach, publishing, irreversible state changes). Use `list` to see
    what's pending, `show` to inspect one, and `approve` / `reject` to
    decide.
    """
    ctx.ensure_object(dict)
    ctx.obj["db_path"] = db_path


@checkpoint.command("list")
@click.option(
    "--workflow",
    "workflow_id",
    default=None,
    help="Filter to a single workflow id.",
)
@click.pass_context
def cmd_list(ctx: click.Context, workflow_id: str | None) -> None:
    """List checkpoints awaiting an operator decision."""
    conn = init_db(ctx.obj["db_path"])
    try:
        pending = list_pending_checkpoints(conn, workflow_id)
    finally:
        conn.close()

    if not pending:
        click.echo("No pending checkpoints.")
        return

    click.echo(f"{len(pending)} pending checkpoint(s):")
    click.echo("")
    for cp in pending:
        click.echo(_format_summary_line(cp))


@checkpoint.command("show")
@click.argument("checkpoint_id", type=int)
@click.pass_context
def cmd_show(ctx: click.Context, checkpoint_id: int) -> None:
    """Show full details for one checkpoint (pending or resolved)."""
    conn = init_db(ctx.obj["db_path"])
    try:
        cp = get_checkpoint(conn, checkpoint_id)
    finally:
        conn.close()
    if cp is None:
        raise click.ClickException(f"unknown checkpoint id: {checkpoint_id}")
    click.echo(_format_detail(cp))


@checkpoint.command("approve")
@click.argument("checkpoint_id", type=int)
@click.option(
    "--reviewer",
    required=True,
    help="Your name or identifier; recorded on the approval event.",
)
@click.option(
    "--note",
    default=None,
    help="Optional free-text context to attach to the audit trail.",
)
@click.pass_context
def cmd_approve(
    ctx: click.Context,
    checkpoint_id: int,
    reviewer: str,
    note: str | None,
) -> None:
    """Approve a pending checkpoint; the workflow returns to running."""
    conn = init_db(ctx.obj["db_path"])
    try:
        try:
            cp = approve_checkpoint(conn, checkpoint_id, reviewer=reviewer, note=note)
        except CheckpointError as exc:
            raise click.ClickException(str(exc)) from exc
    finally:
        conn.close()
    click.echo(f"Approved checkpoint {cp.id} (workflow {cp.workflow_id} -> running).")


@checkpoint.command("reject")
@click.argument("checkpoint_id", type=int)
@click.option(
    "--reviewer",
    required=True,
    help="Your name or identifier; recorded on the rejection event.",
)
@click.option(
    "--reason",
    default=None,
    help="Why the action was rejected; recorded on the audit trail.",
)
@click.pass_context
def cmd_reject(
    ctx: click.Context,
    checkpoint_id: int,
    reviewer: str,
    reason: str | None,
) -> None:
    """Reject a pending checkpoint; the workflow is cancelled (terminal)."""
    conn = init_db(ctx.obj["db_path"])
    try:
        try:
            cp = reject_checkpoint(conn, checkpoint_id, reviewer=reviewer, reason=reason)
        except CheckpointError as exc:
            raise click.ClickException(str(exc)) from exc
    finally:
        conn.close()
    click.echo(f"Rejected checkpoint {cp.id} (workflow {cp.workflow_id} -> cancelled).")


# ---------------------------------------------------------------------------
# Slice 6: demo agent — URL summarizer
# ---------------------------------------------------------------------------


@main.command("run-summarizer")
@click.argument("url")
@click.option(
    "--db",
    "db_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=DEFAULT_DB_PATH,
    show_default=True,
    help="Path to the agent-habitat SQLite file.",
)
@click.option(
    "--workflow-id",
    default=None,
    help="Override the generated workflow id (used for tests / reruns).",
)
def cmd_run_summarizer(url: str, db_path: Path, workflow_id: str | None) -> None:
    """Run the Slice 6 demo agent: fetch, parse, and summarize a URL.

    Exercises the full habitat stack end-to-end: workflow + steps + events
    persisted, telemetry written through llm.py, cost rolled up. The summary
    itself is printed to stdout with a decision-support footer.
    """
    conn = init_db(db_path)
    try:
        result = run_summarizer(conn, url=url, workflow_id=workflow_id)
    finally:
        conn.close()

    click.echo(_format_summarizer_result(result))
    if result.status is WorkflowStatus.FAILED:
        raise click.exceptions.Exit(code=1)


# ---------------------------------------------------------------------------
# Slice 2 (Phase 2): Researcher agent
# ---------------------------------------------------------------------------


@main.command("run-researcher")
@click.argument("company_name")
@click.option(
    "--db",
    "db_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=DEFAULT_DB_PATH,
    show_default=True,
    help="Path to the agent-habitat SQLite file.",
)
@click.option(
    "--workflow-id",
    default=None,
    help="Override the generated workflow id (used for tests / reruns).",
)
@click.option(
    "--max-searches",
    default=3,
    show_default=True,
    type=int,
    help="Cap on web_search requests the model may issue this run.",
)
def cmd_run_researcher(
    company_name: str,
    db_path: Path,
    workflow_id: str | None,
    max_searches: int,
) -> None:
    """Run the Researcher agent: one Haiku call with web_search, RawSignals out.

    Exercises the full habitat stack end-to-end: workflow + step + events
    persisted, telemetry written through llm.py (with the tools= passthrough
    and server-tool fee folded into cost), citations captured into a
    RawSignals payload, cost rolled up. The signals are printed to stdout
    with a decision-support footer.
    """
    conn = init_db(db_path)
    try:
        result = run_researcher(
            conn,
            company_name=company_name,
            workflow_id=workflow_id,
            max_searches=max_searches,
        )
    finally:
        conn.close()

    click.echo(_format_researcher_result(result))
    if result.status is WorkflowStatus.FAILED:
        raise click.exceptions.Exit(code=1)


# ---------------------------------------------------------------------------
# Slice 3 (Phase 2): Extractor agent
# ---------------------------------------------------------------------------


@main.command("run-extractor")
@click.argument("company_name")
@click.option(
    "--db",
    "db_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=DEFAULT_DB_PATH,
    show_default=True,
    help="Path to the agent-habitat SQLite file.",
)
@click.option(
    "--max-searches",
    default=3,
    show_default=True,
    type=int,
    help="Researcher's web_search cap (forwarded to run-researcher).",
)
@click.option(
    "--researcher-workflow-id",
    default=None,
    help="Override the Researcher workflow id (used for tests / reruns).",
)
@click.option(
    "--extractor-workflow-id",
    default=None,
    help="Override the Extractor workflow id (used for tests / reruns).",
)
def cmd_run_extractor(
    company_name: str,
    db_path: Path,
    max_searches: int,
    researcher_workflow_id: str | None,
    extractor_workflow_id: str | None,
) -> None:
    """Run the Extractor agent: produces a CompanyProfile from RawSignals.

    Slice 3 has no orchestrator yet, and `RawSignals` only exists in memory
    after a Researcher run, so this CLI sequences two separate workflows:
    first the Researcher (one Haiku call with web_search → RawSignals),
    then the Extractor (one Sonnet call → CompanyProfile). If the Researcher
    fails, the Extractor is not invoked. If the Researcher returns an empty
    RawSignals, the Extractor still runs and produces an all-gaps profile
    (a valid empty-outcome result per ADR-006 §1).

    Phase 2 Slice 6's LangGraph orchestrator will collapse these into one
    workflow; for now keeping them separate keeps the audit story honest.
    """
    conn = init_db(db_path)
    try:
        researcher_result = run_researcher(
            conn,
            company_name=company_name,
            workflow_id=researcher_workflow_id,
            max_searches=max_searches,
        )
        if researcher_result.status is WorkflowStatus.FAILED:
            click.echo(_format_researcher_result(researcher_result))
            raise click.exceptions.Exit(code=1)

        extractor_result = run_extractor(
            conn,
            raw_signals=researcher_result.raw_signals,
            workflow_id=extractor_workflow_id,
        )
    finally:
        conn.close()

    click.echo(_format_researcher_result(researcher_result))
    click.echo("")
    click.echo("=" * 60)
    click.echo("")
    click.echo(_format_extractor_result(extractor_result))
    if extractor_result.status is WorkflowStatus.FAILED:
        raise click.exceptions.Exit(code=1)


def _format_extractor_result(result: ExtractorResult) -> str:
    lines: list[str] = [
        f"Workflow {result.workflow_id} — status: {result.status.value.upper()}",
        f"Company  : {result.company_name}",
        f"Cost USD : {result.cost_usd:.6f}",
        f"Gaps     : {result.profile.gap_count}/{len(PROFILE_FIELD_NAMES)} fields",
        "",
    ]
    if result.status is WorkflowStatus.COMPLETED:
        for name in PROFILE_FIELD_NAMES:
            field = result.profile.field(name)
            lines.append(f"  {name}:")
            if field.is_gap:
                assert field.gap is not None
                lines.append(f"    GAP — {field.gap.reason}")
            else:
                for v in field.values:
                    lines.append(f"    - {v}")
                for span in field.source_spans:
                    preview = span.quote.strip().replace("\n", " ")
                    if len(preview) > 160:
                        preview = preview[:160] + "…"
                    lines.append(f'      [signal {span.signal_index}] "{preview}"')
            lines.append("")
        lines.append(EXTRACTOR_DECISION_FOOTER)
    else:
        lines.append(f"Failed at step: {result.error_step or '(unknown)'}")
        lines.append(f"Error: {result.error_message or '(no message)'}")
    return "\n".join(lines)


def _format_researcher_result(result: ResearcherResult) -> str:
    lines: list[str] = [
        f"Workflow {result.workflow_id} — status: {result.status.value.upper()}",
        f"Company  : {result.company_name}",
        f"Cost USD : {result.cost_usd:.6f}",
        f"Signals  : {result.raw_signals.signal_count} "
        f"(across {result.raw_signals.source_count} source(s))",
        "",
    ]
    if result.status is WorkflowStatus.COMPLETED:
        if result.raw_signals.signals:
            for i, sig in enumerate(result.raw_signals.signals, start=1):
                title = sig.source_title or "(untitled)"
                lines.append(f"  [{i}] {title}")
                lines.append(f"      {sig.source_url}")
                # Print first ~200 chars of the cited span; full text is in JSONL.
                preview = sig.text.strip().replace("\n", " ")
                if len(preview) > 200:
                    preview = preview[:200] + "…"
                lines.append(f'      "{preview}"')
                lines.append("")
        else:
            lines.append("  (no signals surfaced — a valid empty-outcome result)")
            lines.append("")
        lines.append(RESEARCHER_DECISION_FOOTER)
    else:
        lines.append(f"Failed at step: {result.error_step or '(unknown)'}")
        lines.append(f"Error: {result.error_message or '(no message)'}")
    return "\n".join(lines)


def _format_summarizer_result(result: SummarizerResult) -> str:
    lines: list[str] = [
        f"Workflow {result.workflow_id} — status: {result.status.value.upper()}",
        f"URL      : {result.url}",
        f"Cost USD : {result.cost_usd:.6f}",
        "",
    ]
    if result.status is WorkflowStatus.COMPLETED and result.summary is not None:
        lines.append("Summary:")
        lines.append(result.summary)
        lines.append("")
        lines.append(SUMMARIZER_DECISION_FOOTER)
    else:
        lines.append(f"Failed at step: {result.error_step or '(unknown)'}")
        lines.append(f"Error: {result.error_message or '(no message)'}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _format_summary_line(cp: Checkpoint) -> str:
    requested_by = cp.requested_by or "(unspecified)"
    return (
        f"  [{cp.id}] workflow={cp.workflow_id}  action={cp.action}\n"
        f"        requested {cp.requested_at} by {requested_by}\n"
        f"        {cp.summary}"
    )


def _format_detail(cp: Checkpoint) -> str:
    step = str(cp.step_id) if cp.step_id is not None else "(none)"
    requested_by = cp.requested_by or "(unspecified)"
    lines: list[str] = [
        f"Checkpoint #{cp.id}",
        f"  Workflow      : {cp.workflow_id}",
        f"  Step          : {step}",
        f"  Action        : {cp.action}",
        f"  Requested by  : {requested_by}",
        f"  Requested at  : {cp.requested_at}",
        "",
        "  Summary:",
        f"    {cp.summary}",
    ]
    if cp.proposed_payload:
        lines.append("")
        lines.append("  Proposed payload:")
        rendered = json.dumps(cp.proposed_payload, indent=2, sort_keys=True)
        for line in rendered.splitlines():
            lines.append(f"    {line}")
    lines.append("")
    if cp.is_pending:
        lines.append("  Status        : PENDING")
        lines.append("")
        lines.append(f"  {DECISION_SUPPORT_FOOTER}")
    else:
        assert cp.resolution is not None
        lines.append(f"  Status        : {cp.resolution.value.upper()}")
        lines.append(f"  Reviewer      : {cp.reviewer or '(unspecified)'}")
        lines.append(f"  Resolved at   : {cp.resolved_at or '(unknown)'}")
        if cp.note:
            lines.append("  Note:")
            lines.append(f"    {cp.note}")
    return "\n".join(lines)
