"""``predarb`` command-line entry point.

Only ``version`` and ``config`` are implemented. The remaining Phase 1 commands
are registered as explicit stubs that exit non-zero rather than being absent,
so that ``predarb --help`` documents the intended surface and no command
silently appears to succeed while doing nothing.

There is deliberately no command that places, modifies or cancels an order.
"""

from __future__ import annotations

import typer

from predarb import __version__
from predarb.cli.semantics import certificates_app, evidence_app
from predarb.config import Settings

app = typer.Typer(
    name="predarb",
    help="Prediction-market arbitrage research (Phase 1: Kalshi, read-only).",
    no_args_is_help=True,
    add_completion=False,
)

app.add_typer(evidence_app, name="evidence")
app.add_typer(certificates_app, name="certificates")

_NOT_YET = "Not implemented yet. See docs/architecture.md section 6 for build order."


def _todo(name: str) -> None:
    typer.secho(f"{name}: {_NOT_YET}", fg=typer.colors.YELLOW, err=True)
    raise typer.Exit(code=2)


@app.command()
def version() -> None:
    """Print the package version."""
    typer.echo(__version__)


@app.command(name="config")
def show_config() -> None:
    """Show the resolved configuration, with secrets omitted."""
    settings = Settings()
    typer.echo(f"kalshi_env        : {settings.kalshi_env.value}")
    typer.echo(f"rest_base_url     : {settings.rest_base_url}")
    typer.echo(f"ws_url            : {settings.ws_url}")
    typer.echo(f"has_credentials   : {settings.has_credentials}")
    typer.echo(f"raw_journal_dir   : {settings.raw_journal_dir}")
    typer.echo(f"max_book_age_ms   : {settings.max_book_age_ms}")
    typer.echo(f"log_level         : {settings.log_level}")


@app.command(name="sync-metadata")
def sync_metadata() -> None:
    """Sync Kalshi series/event/market metadata into the catalogue."""
    _todo("sync-metadata")


@app.command(name="capture-live")
def capture_live() -> None:
    """Capture raw REST and WebSocket data to the append-only journal."""
    _todo("capture-live")


@app.command(name="scan-live")
def scan_live() -> None:
    """Run detectors against live reconstructed books."""
    _todo("scan-live")


@app.command(name="replay")
def replay() -> None:
    """Replay captured data point-in-time through the same detectors."""
    _todo("replay")


@app.command(name="audit-opportunity")
def audit_opportunity() -> None:
    """Explain a stored opportunity in full, from stored evidence."""
    _todo("audit-opportunity")


@app.command(name="validate-books")
def validate_books() -> None:
    """Cross-check reconstructed books against current REST state."""
    _todo("validate-books")


@app.command(name="list-relations")
def list_relations() -> None:
    """List logical relations and their verification status."""
    _todo("list-relations")


@app.command(name="verify-relation")
def verify_relation() -> None:
    """Record human verification of a logical relation."""
    _todo("verify-relation")


if __name__ == "__main__":  # pragma: no cover
    app()
