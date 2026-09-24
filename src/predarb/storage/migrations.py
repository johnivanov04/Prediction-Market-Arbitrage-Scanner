"""Forward schema migrations, numbered and explicit.

Durable storage means the schema outlives the code that wrote it, so a build
must be able to say which shape it is looking at and move an older one forward.

Why a numbered runner rather than Alembic
------------------------------------------
Alembic is a declared dependency and is the right tool against an evolving ORM
with autogenerate. This schema is six hand-written Core tables that change
rarely and deliberately, and the property worth testing is narrow: a fresh
database reaches head, an older one moves forward exactly once per step, and a
newer one is refused. A list of ``(version, callable)`` makes that testable in
a few lines with no revision graph, no ``env.py`` and no filesystem discovery
to go wrong during a soak.

Backward migrations are deliberately absent. Phase 1 never needs to downgrade,
and a downgrade path that is never exercised is a path that does not work.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sqlalchemy import Connection, Engine, text

from predarb.storage.engine import current_schema_version
from predarb.storage.schema import METADATA, SCHEMA_VERSION

__all__ = [
    "MIGRATIONS",
    "Migration",
    "MigrationError",
    "migrate",
    "pending_migrations",
]

_VERSION_TABLE = "predarb_schema_version"


class MigrationError(RuntimeError):
    """The database cannot be moved to the version this build needs."""


@dataclass(frozen=True, slots=True)
class Migration:
    """One forward step, from a known version to the next."""

    from_version: str | None
    """``None`` means an empty database."""

    to_version: str
    description: str
    apply: Callable[[Connection], None]


def _create_initial_schema(connection: Connection) -> None:
    """The Phase-1 catalogue, created in one transaction."""
    METADATA.create_all(connection, checkfirst=True)
    connection.execute(text(f"CREATE TABLE IF NOT EXISTS {_VERSION_TABLE} (version TEXT NOT NULL)"))


MIGRATIONS: Sequence[Migration] = (
    Migration(
        from_version=None,
        to_version="phase1-catalogue/1",
        description="initial Phase-1 catalogue: sessions, observations, decisions, health, replay",
        apply=_create_initial_schema,
    ),
)


def pending_migrations(current: str | None) -> list[Migration]:
    """The steps from ``current`` to head, in order.

    Raises when the database is *ahead* of this build. Silently opening it
    would let older code write rows into a shape it does not understand, which
    corrupts an audit trail in the least visible way available.
    """
    if current == SCHEMA_VERSION:
        return []

    known = {migration.from_version for migration in MIGRATIONS} | {
        migration.to_version for migration in MIGRATIONS
    }
    if current is not None and current not in known:
        raise MigrationError(
            f"database schema {current!r} is unknown to this build (head is "
            f"{SCHEMA_VERSION!r}); it was probably written by newer code, and "
            "reading it here could misinterpret columns"
        )

    steps: list[Migration] = []
    position = current
    by_source = {migration.from_version: migration for migration in MIGRATIONS}
    while position != SCHEMA_VERSION:
        migration = by_source.get(position)
        if migration is None:
            raise MigrationError(f"no migration path from {position!r} to {SCHEMA_VERSION!r}")
        steps.append(migration)
        position = migration.to_version
    return steps


def migrate(engine: Engine) -> list[str]:
    """Bring the catalogue to head. Returns the versions applied.

    Idempotent: running it against a current database applies nothing and
    returns an empty list, so a collector may call it unconditionally at
    startup rather than guessing whether it is needed.
    """
    with engine.begin() as connection:
        current = current_schema_version(connection)
        steps = pending_migrations(current)
        applied: list[str] = []
        for migration in steps:
            migration.apply(connection)
            connection.execute(text(f"DELETE FROM {_VERSION_TABLE}"))
            connection.execute(
                text(f"INSERT INTO {_VERSION_TABLE} (version) VALUES (:v)"),
                {"v": migration.to_version},
            )
            applied.append(migration.to_version)
        return applied
