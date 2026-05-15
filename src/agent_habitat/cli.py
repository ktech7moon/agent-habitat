"""agent-habitat command-line entry point.

Click group with two surfaces today:

  - `version`              — prints the package version (kickoff stub).
  - `checkpoint {list,show,approve,reject}`
                           — the operator-facing HITL approval surface
                             added in Slice 5.

The orchestrator surface lands in Phase 2.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from agent_habitat import __version__
from agent_habitat.checkpoint import (
    Checkpoint,
    CheckpointError,
    approve_checkpoint,
    get_checkpoint,
    list_pending_checkpoints,
    reject_checkpoint,
)
from agent_habitat.state import DEFAULT_DB_PATH, init_db


# Decision-support framing operators see before approving. Keeps the
# project's posture explicit on the only user-visible surface that asks
# a human to consent to an action.
DECISION_SUPPORT_FOOTER = (
    "This summary is operational context for your approval decision. "
    "It is not legal, medical, or financial advice; verify the action "
    "and its consequences before approving."
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
