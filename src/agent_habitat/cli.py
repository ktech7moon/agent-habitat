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
    DrafterResult,
    ExtractorResult,
    ResearcherResult,
    ScorerResult,
    SummarizerResult,
    run_drafter,
    run_extractor,
    run_researcher,
    run_scorer,
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
from agent_habitat.orchestration.crew_graph import (
    CrewResult,
    resume_crew,
    run_crew,
)
from agent_habitat.scoring import DEFAULT_RUBRIC_PATH, RubricConfigError, load_rubric
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

SCORER_DECISION_FOOTER = (
    "Automated ICP scoring against an operator-authored rubric that is "
    "itself an un-validated hypothesis. The score is renormalised over "
    "scorable dimensions only; coverage tells you how much of the rubric "
    "this run actually covered. Treat as decision support, not as a "
    "ranking."
)

#: Decision-support footer printed below the Drafter's prose. Per
#: PATTERNS.md #4 the disclosure surfaces both the score and the coverage
#: number so the operator sees what the rubric actually judged on this run
#: ("82.86/100 against the rubric, but the rubric covered only 70% of the
#: operator's stated ICP dimensions"). The Draft itself does NOT embed the
#: coverage in the prose — the operator-internal context lives on the CLI
#: surface, not in the prospect-facing text.
DRAFTER_DECISION_FOOTER = (
    "Automated draft from an Opus 4.7 LLM. The model can fabricate, "
    "over-claim, or mis-attribute; the enumerated claims trace to the "
    "scoring chain but the substring-grounding check is a downstream "
    "agent's job (Slice 7 critic). Verify every concrete claim against "
    "the cited dimension before sending. Treat as decision support, "
    "not as a sendable artefact."
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


# ---------------------------------------------------------------------------
# Slice 4 (Phase 2): Scorer agent
# ---------------------------------------------------------------------------


@main.command("run-scorer")
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
    "--rubric",
    "rubric_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=DEFAULT_RUBRIC_PATH,
    show_default=True,
    help="Path to the ICP rubric TOML file (operator-tunable).",
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
@click.option(
    "--scorer-workflow-id",
    default=None,
    help="Override the Scorer workflow id (used for tests / reruns).",
)
def cmd_run_scorer(
    company_name: str,
    db_path: Path,
    rubric_path: Path,
    max_searches: int,
    researcher_workflow_id: str | None,
    extractor_workflow_id: str | None,
    scorer_workflow_id: str | None,
) -> None:
    """Run the Scorer agent: produces a ScoredCompany from CompanyProfile + rubric.

    Slice 4 has no orchestrator yet, so this CLI sequences three separate
    workflows: Researcher (Haiku + web_search → RawSignals), Extractor
    (Sonnet → CompanyProfile), then Scorer (Sonnet → ScoredCompany). If
    any upstream agent fails, the downstream agents are not invoked. An
    all-gaps profile from the Extractor is a VALID input to the Scorer
    and produces a score=None / coverage=0 ScoredCompany (no LLM call).

    Phase 2 Slice 6's LangGraph orchestrator will collapse these into one
    workflow; for now keeping them separate keeps the audit story honest.
    """
    try:
        rubric = load_rubric(rubric_path)
    except RubricConfigError as exc:
        raise click.ClickException(str(exc)) from exc

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
        if extractor_result.status is WorkflowStatus.FAILED:
            click.echo(_format_researcher_result(researcher_result))
            click.echo("")
            click.echo("=" * 60)
            click.echo("")
            click.echo(_format_extractor_result(extractor_result))
            raise click.exceptions.Exit(code=1)

        scorer_result = run_scorer(
            conn,
            profile=extractor_result.profile,
            rubric=rubric,
            workflow_id=scorer_workflow_id,
        )
    finally:
        conn.close()

    click.echo(_format_researcher_result(researcher_result))
    click.echo("")
    click.echo("=" * 60)
    click.echo("")
    click.echo(_format_extractor_result(extractor_result))
    click.echo("")
    click.echo("=" * 60)
    click.echo("")
    click.echo(_format_scorer_result(scorer_result))
    if scorer_result.status is WorkflowStatus.FAILED:
        raise click.exceptions.Exit(code=1)


# ---------------------------------------------------------------------------
# Slice 5 (Phase 2): Drafter agent
# ---------------------------------------------------------------------------


@main.command("run-drafter")
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
    "--rubric",
    "rubric_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=DEFAULT_RUBRIC_PATH,
    show_default=True,
    help="Path to the ICP rubric TOML file (operator-tunable).",
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
@click.option(
    "--scorer-workflow-id",
    default=None,
    help="Override the Scorer workflow id (used for tests / reruns).",
)
@click.option(
    "--drafter-workflow-id",
    default=None,
    help="Override the Drafter workflow id (used for tests / reruns).",
)
def cmd_run_drafter(
    company_name: str,
    db_path: Path,
    rubric_path: Path,
    max_searches: int,
    researcher_workflow_id: str | None,
    extractor_workflow_id: str | None,
    scorer_workflow_id: str | None,
    drafter_workflow_id: str | None,
) -> None:
    """Run the Drafter agent: produces a Draft from a passing ScoredCompany.

    Slice 5 has no orchestrator yet, so this CLI sequences four separate
    workflows: Researcher (Haiku + web_search → RawSignals), Extractor
    (Sonnet → CompanyProfile), Scorer (Sonnet → ScoredCompany), then
    Drafter (Opus 4.7 → Draft). If any upstream agent fails, downstream
    agents are not invoked.

    A ScoredCompany that does not pass both gates (`gated_by is not None`)
    skips the Drafter call and prints a clearly-labelled "no draft"
    outcome with the scorer result still shown. This previews the
    Slice 6 orchestrator's `terminate_no_draft` routing without building
    it; the workflow is COMPLETED, not FAILED — an ICP miss is a valid
    empty-outcome, not an error.
    """
    try:
        rubric = load_rubric(rubric_path)
    except RubricConfigError as exc:
        raise click.ClickException(str(exc)) from exc

    conn = init_db(db_path)
    drafter_result: DrafterResult | None = None
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
        if extractor_result.status is WorkflowStatus.FAILED:
            click.echo(_format_researcher_result(researcher_result))
            click.echo("")
            click.echo("=" * 60)
            click.echo("")
            click.echo(_format_extractor_result(extractor_result))
            raise click.exceptions.Exit(code=1)

        scorer_result = run_scorer(
            conn,
            profile=extractor_result.profile,
            rubric=rubric,
            workflow_id=scorer_workflow_id,
        )
        if scorer_result.status is WorkflowStatus.FAILED:
            click.echo(_format_researcher_result(researcher_result))
            click.echo("")
            click.echo("=" * 60)
            click.echo("")
            click.echo(_format_extractor_result(extractor_result))
            click.echo("")
            click.echo("=" * 60)
            click.echo("")
            click.echo(_format_scorer_result(scorer_result))
            raise click.exceptions.Exit(code=1)

        if scorer_result.scored_company.routes_to_draft:
            drafter_result = run_drafter(
                conn,
                scored_company=scorer_result.scored_company,
                workflow_id=drafter_workflow_id,
            )
    finally:
        conn.close()

    click.echo(_format_researcher_result(researcher_result))
    click.echo("")
    click.echo("=" * 60)
    click.echo("")
    click.echo(_format_extractor_result(extractor_result))
    click.echo("")
    click.echo("=" * 60)
    click.echo("")
    click.echo(_format_scorer_result(scorer_result))
    click.echo("")
    click.echo("=" * 60)
    click.echo("")
    if drafter_result is None:
        click.echo(_format_no_draft(scorer_result))
    else:
        click.echo(_format_drafter_result(drafter_result, scorer_result))
        if drafter_result.status is WorkflowStatus.FAILED:
            raise click.exceptions.Exit(code=1)


# ---------------------------------------------------------------------------
# Phase 2 Slice 6: the LangGraph crew orchestrator.
# ---------------------------------------------------------------------------


#: Decision-support footer printed below crew results that produced a Draft.
#: Same posture as the standalone Drafter footer plus a one-line nod to the
#: substring-grounding chain still being a Slice-7 (Critic) responsibility.
CREW_DECISION_FOOTER = (
    "Automated four-agent crew (researcher → extractor → scorer → drafter). "
    "The drafter is Opus 4.7; enumerated claims trace to the per-dimension "
    "scoring chain, but the verbatim substring-grounding check is a Slice 7 "
    "(critic) responsibility — not enforced in this run. Verify every "
    "concrete claim against the cited dimension before sending. Treat as "
    "decision support, not as a sendable artefact."
)


@main.command("run-crew")
@click.argument("company_name", required=False)
@click.option(
    "--resume",
    "resume_workflow_id",
    default=None,
    help="Resume a paused crew workflow by id (its approve_drafter checkpoint "
    "must already be approved or rejected via the `checkpoint` CLI).",
)
@click.option(
    "--db",
    "db_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=DEFAULT_DB_PATH,
    show_default=True,
    help="Path to the agent-habitat SQLite file.",
)
@click.option(
    "--rubric",
    "rubric_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=DEFAULT_RUBRIC_PATH,
    show_default=True,
    help="Path to the ICP rubric TOML file (operator-tunable).",
)
@click.option(
    "--max-searches",
    default=3,
    show_default=True,
    type=int,
    help="Researcher's web_search cap.",
)
@click.option(
    "--workflow-id",
    default=None,
    help="Override the generated workflow id (used for tests / reruns).",
)
def cmd_run_crew(
    company_name: str | None,
    resume_workflow_id: str | None,
    db_path: Path,
    rubric_path: Path,
    max_searches: int,
    workflow_id: str | None,
) -> None:
    """Run the full lead-enrichment crew through the LangGraph orchestrator.

    Initial run:    `agent-habitat run-crew COMPANY_NAME`
    Resume paused:  `agent-habitat run-crew --resume WORKFLOW_ID`

    The crew is researcher → extractor → scorer → [human checkpoint] →
    drafter (no critic yet — Slice 7). On a passing ScoredCompany the
    graph pauses at the human checkpoint; approve via
    `agent-habitat checkpoint approve <checkpoint_id>` and then resume
    with `agent-habitat run-crew --resume <workflow_id>`.

    A gated ScoredCompany (`gated_by` set) routes to `terminate_no_draft`
    — workflow COMPLETED, no Drafter cost paid. An operator rejection at
    the checkpoint is also a valid empty-outcome (workflow CANCELLED, no
    Drafter cost paid).
    """
    if resume_workflow_id is not None and company_name is not None:
        raise click.UsageError("--resume and COMPANY_NAME are mutually exclusive — supply one.")
    if resume_workflow_id is None and company_name is None:
        raise click.UsageError(
            "supply COMPANY_NAME (initial run) or --resume WORKFLOW_ID (resume paused)."
        )

    try:
        rubric = load_rubric(rubric_path)
    except RubricConfigError as exc:
        raise click.ClickException(str(exc)) from exc

    conn = init_db(db_path)
    try:
        if resume_workflow_id is not None:
            try:
                result = resume_crew(
                    conn,
                    workflow_id=resume_workflow_id,
                    rubric=rubric,
                    max_searches=max_searches,
                )
            except ValueError as exc:
                raise click.ClickException(str(exc)) from exc
        else:
            assert company_name is not None
            result = run_crew(
                conn,
                company_name=company_name,
                rubric=rubric,
                workflow_id=workflow_id,
                max_searches=max_searches,
            )
    finally:
        conn.close()

    click.echo(_format_crew_result(result))
    if result.status is WorkflowStatus.FAILED:
        raise click.exceptions.Exit(code=1)


def _format_crew_result(result: CrewResult) -> str:
    """Render the orchestrator's outcome. One of four shapes.

    PAUSED   → instructions for the operator to approve/reject + resume.
    COMPLETED with draft     → the drafter's prose + enumerated claims +
                               coverage-aware footer.
    COMPLETED with terminate → which gate fired + the audit framing.
    CANCELLED                → rejection notice.
    FAILED                   → offending step + error message.
    """
    lines: list[str] = [
        f"Workflow {result.workflow_id} — status: {result.status.value.upper()}",
        f"Company  : {result.company_name}",
        f"Cost USD : {result.cost_usd:.6f}",
        "",
    ]
    if result.status is WorkflowStatus.PAUSED:
        cp_id_str = (
            str(result.pending_checkpoint_id)
            if result.pending_checkpoint_id is not None
            else "(unknown)"
        )
        lines.extend(
            [
                "PAUSED — drafter approval required before producing prose.",
                "",
                f"  Pending checkpoint id: {cp_id_str}",
                "",
                "Next steps:",
                f"  1. Inspect:  agent-habitat checkpoint show {cp_id_str}",
                f"  2. Approve:  agent-habitat checkpoint approve {cp_id_str} --reviewer YOUR_NAME",
                "     (or:      agent-habitat checkpoint reject  "
                f"{cp_id_str} --reviewer YOUR_NAME --reason '…')",
                f"  3. Resume:   agent-habitat run-crew --resume {result.workflow_id}",
                "",
                DECISION_SUPPORT_FOOTER,
            ]
        )
        return "\n".join(lines)

    if result.status is WorkflowStatus.CANCELLED:
        lines.extend(
            [
                f"CANCELLED — operator rejected the drafter checkpoint "
                f"(terminate_reason={result.terminate_reason!r}).",
                "No drafter cost was paid; the audit row reflects the rejection.",
            ]
        )
        return "\n".join(lines)

    if result.status is WorkflowStatus.FAILED:
        lines.extend(
            [
                f"Failed at step: {result.error_step or '(unknown)'}",
                f"Error: {result.error_message or '(no message)'}",
            ]
        )
        return "\n".join(lines)

    # COMPLETED. Either we produced a draft, or terminate_no_draft routed.
    if result.draft is not None and result.scored_company is not None:
        sc = result.scored_company
        score_str = f"{sc.score:.2f}" if sc.score is not None else "None"
        coverage_pct = sc.coverage * 100.0
        lines.append("Draft prose:")
        lines.append(result.draft.prose)
        lines.append("")
        lines.append(
            f"Enumerated claims ({result.draft.claim_count}, anchored to ScoredCompany dimensions):"
        )
        for i, claim in enumerate(result.draft.claims, start=1):
            preview = claim.text.strip().replace("\n", " ")
            if len(preview) > 160:
                preview = preview[:160] + "…"
            lines.append(f'  [{i}] dim={claim.supporting_dimension}: "{preview}"')
        lines.append("")
        lines.append(
            f"Scored {score_str}/100 against the rubric, but the rubric covered "
            f"{coverage_pct:.0f}% of the operator's stated ICP dimensions on this run."
        )
        lines.append("")
        lines.append(CREW_DECISION_FOOTER)
        return "\n".join(lines)

    # COMPLETED with terminate_reason — gated, not a failure.
    if result.scored_company is not None:
        sc = result.scored_company
        score_str = f"{sc.score:.2f}" if sc.score is not None else "None"
        coverage_pct = sc.coverage * 100.0
        lines.extend(
            [
                f"NO DRAFT — ScoredCompany was gated (terminate_reason="
                f"{result.terminate_reason!r}).",
                f"  score    : {score_str}   (floor {sc.floor:.1f})",
                f"  coverage : {coverage_pct:.1f}%   (min_coverage {sc.min_coverage:.1%})",
                "",
                "The orchestrator routed to `terminate_no_draft`; no Drafter cost was paid.",
            ]
        )
    else:
        lines.append(
            f"NO DRAFT — terminate_reason={result.terminate_reason!r} (no ScoredCompany available)."
        )
    return "\n".join(lines)


def _format_no_draft(scorer_result: ScorerResult) -> str:
    """Render the Slice-5 stand-in for terminate_no_draft routing.

    The orchestrator (Slice 6) will route gated ScoredCompany values to a
    `terminate_no_draft` node and the workflow will COMPLETE — not FAIL.
    Until then, the CLI surfaces the same outcome by skipping the Drafter
    call and printing this clearly-labelled block. The scorer's gating
    fact is the load-bearing audit row; this block just makes it
    operator-visible.
    """
    sc = scorer_result.scored_company
    score_str = f"{sc.score:.2f}" if sc.score is not None else "None"
    coverage_pct = sc.coverage * 100.0
    return "\n".join(
        [
            f"NO DRAFT — ScoredCompany was gated by {sc.gated_by!r}.",
            f"  score    : {score_str}   (floor {sc.floor:.1f})",
            f"  coverage : {coverage_pct:.1f}%   (min_coverage {sc.min_coverage:.1%})",
            "",
            "The Slice 6 orchestrator will route gated ScoredCompany values to",
            "a terminate_no_draft node — workflow COMPLETED, no Drafter cost. ",
            "This CLI surface previews that behaviour by skipping the call.",
        ]
    )


def _format_drafter_result(result: DrafterResult, scorer_result: ScorerResult) -> str:
    """Render the Drafter result with the coverage-aware decision-support footer.

    Per PATTERNS.md #4, the disclosure surfaces the score AND the coverage
    number — operator-facing prose comes from the Drafter, but the
    operator-internal context (what fraction of the rubric this score
    actually covered) lives on this CLI surface. The Draft model does NOT
    embed coverage in the prose itself; that would conflate the prospect-
    facing artefact with operator audit metadata.
    """
    sc = scorer_result.scored_company
    score_str = f"{sc.score:.2f}" if sc.score is not None else "None"
    coverage_pct = sc.coverage * 100.0
    lines: list[str] = [
        f"Workflow {result.workflow_id} — status: {result.status.value.upper()}",
        f"Company  : {result.company_name}",
        f"Cost USD : {result.cost_usd:.6f}   (this Drafter run only)",
        "",
    ]
    if result.status is WorkflowStatus.COMPLETED and result.draft is not None:
        lines.append("Draft prose:")
        lines.append(result.draft.prose)
        lines.append("")
        lines.append(
            f"Enumerated claims ({result.draft.claim_count}, anchored to ScoredCompany dimensions):"
        )
        for i, claim in enumerate(result.draft.claims, start=1):
            preview = claim.text.strip().replace("\n", " ")
            if len(preview) > 160:
                preview = preview[:160] + "…"
            lines.append(f'  [{i}] dim={claim.supporting_dimension}: "{preview}"')
        lines.append("")
        lines.append(
            f"Scored {score_str}/100 against the rubric, but the rubric covered "
            f"{coverage_pct:.0f}% of the operator's stated ICP dimensions on this run."
        )
        lines.append("")
        lines.append(DRAFTER_DECISION_FOOTER)
    else:
        lines.append(f"Failed at step: {result.error_step or '(unknown)'}")
        lines.append(f"Error: {result.error_message or '(no message)'}")
    return "\n".join(lines)


def _format_scorer_result(result: ScorerResult) -> str:
    sc = result.scored_company
    score_str = f"{sc.score:.2f}" if sc.score is not None else "None (no scorable dimensions)"
    tier_str = sc.tier if sc.tier is not None else "(no tier — score is None)"
    gated_str = (
        sc.gated_by if sc.gated_by is not None else "(passed both gates — eligible for drafter)"
    )
    lines: list[str] = [
        f"Workflow {result.workflow_id} — status: {result.status.value.upper()}",
        f"Company  : {result.company_name}",
        f"Cost USD : {result.cost_usd:.6f}",
        f"Score    : {score_str}   (floor: {sc.floor:.1f})",
        f"Coverage : {sc.coverage:.2%}      (min_coverage: {sc.min_coverage:.2%})",
        f"Tier     : {tier_str}",
        f"Gated by : {gated_str}",
        "",
    ]
    if result.status is WorkflowStatus.COMPLETED:
        lines.append("Per-dimension scores:")
        for dim in sc.dimensions:
            if dim.is_excluded:
                lines.append(f"  {dim.field}  (weight {dim.weight:.2f}): EXCLUDED")
                # Word-wrap reasoning for readability; keep it one logical block.
                lines.append(f"    reason: {dim.reasoning}")
            else:
                assert dim.score is not None and dim.grounded_quote is not None
                quote_preview = dim.grounded_quote.strip().replace("\n", " ")
                if len(quote_preview) > 160:
                    quote_preview = quote_preview[:160] + "…"
                lines.append(f"  {dim.field}  (weight {dim.weight:.2f}): {dim.score:.1f}/5")
                lines.append(f'    grounded_quote: "{quote_preview}"')
                lines.append(f"    reasoning: {dim.reasoning}")
            lines.append("")
        lines.append(SCORER_DECISION_FOOTER)
    else:
        lines.append(f"Failed at step: {result.error_step or '(unknown)'}")
        lines.append(f"Error: {result.error_message or '(no message)'}")
    return "\n".join(lines)


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
