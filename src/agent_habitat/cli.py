import click
from agent_habitat import __version__


@click.group()
def main() -> None:
    """agent-habitat: production-grade multi-agent orchestration framework."""


@main.command()
def version() -> None:
    """Print the package version."""
    click.echo(__version__)
