"""``predarb storage`` -- inspect the durable catalogue and verify it.

Read-only except for ``migrate``. Verification never repairs: replay is
authoritative, and silently rewriting a mismatched row would destroy the only
evidence that something went wrong.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import typer

from predarb.replay.knowledge import KnowledgeBase
from predarb.replay.plan import DetectorPlan
from predarb.storage.catalogue import Catalogue
from predarb.storage.engine import (
    DEFAULT_DATABASE_PATH,
    connect,
    create_catalogue_engine,
    current_schema_version,
)
from predarb.storage.migrations import migrate
from predarb.storage.schema import SCHEMA_VERSION
from predarb.storage.verification import verify_session_decisions
from predarb.venues.kalshi.replay_context import KalshiReplayContext

storage_app = typer.Typer(
    help="Inspect and verify the durable Phase-1 catalogue.", no_args_is_help=True
)


def _kalshi_provider(knowledge: KnowledgeBase, plan: DetectorPlan) -> KalshiReplayContext:
    """The venue resolver, injected so ``storage`` stays a leaf layer."""
    return KalshiReplayContext(knowledge=knowledge, plan=plan)


@storage_app.command("migrate")
def storage_migrate(
    database: Path = typer.Option(DEFAULT_DATABASE_PATH, help="Catalogue path or URL."),
) -> None:
    """Bring the catalogue to the schema version this build understands."""
    engine = create_catalogue_engine(database)
    try:
        applied = migrate(engine)
        with engine.connect() as connection:
            typer.echo(f"schema version: {current_schema_version(connection)}")
        typer.echo(f"applied: {applied or '(already current)'}")
    finally:
        engine.dispose()


@storage_app.command("sessions")
def storage_sessions(
    database: Path = typer.Option(DEFAULT_DATABASE_PATH),
) -> None:
    """List collection sessions, newest first."""
    engine = create_catalogue_engine(database)
    try:
        with connect(engine) as connection:
            catalogue = Catalogue(connection)
            rows = catalogue.list_sessions()
            typer.echo(f"{len(rows)} session(s), schema {SCHEMA_VERSION}:")
            for stored in rows:
                observations = catalogue.observation_count(stored.session_id)
                decisions = catalogue.decision_count(stored.session_id)
                typer.echo(
                    f"  {stored.session_id[:12]}  {stored.started_at.isoformat()}  "
                    f"{stored.status:<10s} {observations:>7d} observation(s)  "
                    f"{decisions:>6d} decision(s)"
                )
    finally:
        engine.dispose()


@storage_app.command("show")
def storage_show(
    session_id: str,
    database: Path = typer.Option(DEFAULT_DATABASE_PATH),
) -> None:
    """Everything the catalogue holds about one session."""
    engine = create_catalogue_engine(database)
    try:
        with connect(engine) as connection:
            catalogue = Catalogue(connection)
            resolved = catalogue.resolve_session_id(session_id)
            stored = None if resolved is None else catalogue.get_session(resolved)
            if stored is None:
                typer.secho(
                    f"no session matching {session_id!r} (or the prefix is ambiguous)",
                    fg=typer.colors.RED,
                    err=True,
                )
                raise typer.Exit(code=1)
            session_id = stored.session_id
            typer.echo(f"session      : {stored.session_id}")
            typer.echo(f"started      : {stored.started_at.isoformat()}")
            typer.echo(f"status       : {stored.status}")
            typer.echo(f"raw journal  : {stored.raw_journal_path}")
            typer.echo(f"observations : {catalogue.observation_count(stored.session_id)}")
            typer.echo(f"decisions    : {catalogue.decision_count(stored.session_id)}")
            typer.echo("\ndetectors:")
            for detector, count in sorted(catalogue.detector_counts(session_id).items()):
                typer.echo(f"  {count:>6}  {detector}")
            typer.echo("\nclassifications:")
            for name, count in sorted(catalogue.classification_counts(session_id).items()):
                typer.echo(f"  {count:>6}  {name}")
            health = catalogue.health_counts(session_id)
            if health:
                typer.echo("\nhealth events:")
                for kind, count in sorted(health.items()):
                    typer.echo(f"  {count:>6}  {kind}")
    finally:
        engine.dispose()


@storage_app.command("verify-decisions")
def storage_verify_decisions(
    session_id: str,
    database: Path = typer.Option(DEFAULT_DATABASE_PATH),
    report: Path = typer.Option(None, help="Write the verification report here."),
) -> None:
    """Replay a session's source observations and compare every decision.

    Exits non-zero on any mismatch. Nothing is repaired: a mismatch is the
    signal worth stopping for, and overwriting it would remove the evidence.
    """
    engine = create_catalogue_engine(database)
    try:
        with connect(engine) as connection:
            resolved = Catalogue(connection).resolve_session_id(session_id)
        if resolved is None:
            typer.secho(
                f"no session matching {session_id!r} (or the prefix is ambiguous)",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)
        result = verify_session_decisions(
            engine,
            resolved,
            provider_factory=_kalshi_provider,
            verified_at=datetime.now(tz=UTC),
        )
    except KeyError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    finally:
        engine.dispose()

    colour = typer.colors.GREEN if result.exact_match else typer.colors.RED
    typer.secho(result.describe(), fg=colour)
    typer.echo(f"  observations replayed : {result.observations_replayed}")
    typer.echo(f"  decisions persisted   : {result.decisions_persisted}")
    typer.echo(f"  decisions regenerated : {result.decisions_regenerated}")
    typer.echo(f"  fingerprint matches   : {result.fingerprint_matches}")
    typer.echo(f"  network calls         : {result.network_calls}")
    for label, rows in (
        ("missing from catalogue", result.missing_persisted),
        ("present without source", result.extra_persisted),
    ):
        if rows:
            typer.secho(f"  !! {label}: {list(rows)[:10]}", fg=typer.colors.RED)
    for ordinal, persisted, regenerated in result.fingerprint_mismatches[:10]:
        typer.secho(
            f"  !! decision {ordinal}: persisted {persisted[:12]} vs "
            f"regenerated {regenerated[:12]}",
            fg=typer.colors.RED,
        )

    if report is not None:
        report.write_text(json.dumps(result.payload(), indent=2, sort_keys=True) + "\n")
        typer.echo(f"\nWrote {report}")
    if not result.exact_match:
        raise typer.Exit(code=1)
