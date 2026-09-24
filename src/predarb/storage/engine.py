"""Opening the catalogue, and refusing to open one this code cannot read.

Two safety properties live here.

**Version checking on open.** A database written by newer code may hold columns
this build does not know about, or the same column meaning something else.
Opening it read-write and continuing would corrupt the audit trail quietly, so
an unrecognised schema version raises.

**SQLite is configured, not assumed.** Foreign keys are off by default in
SQLite and WAL is not the default journal mode; both are set explicitly per
connection. A catalogue whose foreign keys silently did nothing would accept
decisions referencing sessions that do not exist.

Backend portability is deliberate. `architecture.md` names Postgres for the
structured catalogue; Phase 1 runs on a local SQLite file because a research
soak should not need a daemon to stay alive alongside it. Nothing in this
package uses a SQLite-only construct, so the same statements run on either.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, create_engine, event, text
from sqlalchemy.engine import make_url

from predarb.storage.schema import METADATA, SCHEMA_VERSION

__all__ = [
    "DEFAULT_DATABASE_PATH",
    "SchemaVersionError",
    "connect",
    "create_catalogue_engine",
    "current_schema_version",
    "declared_tables",
    "table_names",
]

DEFAULT_DATABASE_PATH = Path("./data/catalogue/phase1.sqlite")

_VERSION_TABLE = "predarb_schema_version"


class SchemaVersionError(RuntimeError):
    """The database schema is not the one this build understands."""


def _configure_sqlite(engine: Engine) -> None:
    """Per-connection pragmas. SQLite's defaults are wrong for this use.

    ``foreign_keys`` is OFF by default, so declared references would be
    decorative. ``journal_mode=WAL`` lets a reader (a verification pass) run
    while the collector is still writing, which is how a soak is inspected
    without stopping it. ``synchronous=FULL`` keeps a crash from losing an
    already-acknowledged source observation -- the one thing that must not
    happen, since source evidence is not regenerable.
    """

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=FULL")
        cursor.close()


def create_catalogue_engine(url: str | Path | None = None, *, echo: bool = False) -> Engine:
    """Open (or create) the catalogue. A bare path means local SQLite."""
    if url is None:
        url = DEFAULT_DATABASE_PATH
    if isinstance(url, Path) or "://" not in str(url):
        path = Path(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite+pysqlite:///{path}"

    engine = create_engine(str(url), echo=echo, future=True)
    if make_url(str(url)).get_backend_name() == "sqlite":
        _configure_sqlite(engine)
    return engine


def current_schema_version(connection: Connection) -> str | None:
    """The version recorded in the database, or ``None`` if it is empty."""
    inspector_sql = text(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=:t"
        if connection.engine.dialect.name == "sqlite"
        else "SELECT table_name AS name FROM information_schema.tables WHERE table_name=:t"
    )
    if connection.execute(inspector_sql, {"t": _VERSION_TABLE}).first() is None:
        return None
    row = connection.execute(text(f"SELECT version FROM {_VERSION_TABLE}")).first()
    return None if row is None else str(row[0])


@contextmanager
def connect(engine: Engine, *, require_version: bool = True) -> Iterator[Connection]:
    """A transactional connection, with the schema version checked on entry.

    Transactional on purpose: a partially written batch that looked successful
    is exactly the failure mode the whole catalogue exists to prevent.
    """
    with engine.begin() as connection:
        if require_version:
            found = current_schema_version(connection)
            if found is None:
                raise SchemaVersionError(
                    "catalogue has no schema version; run the migration first "
                    "rather than writing into an unknown shape"
                )
            if found != SCHEMA_VERSION:
                raise SchemaVersionError(
                    f"catalogue schema is {found!r} but this build understands "
                    f"{SCHEMA_VERSION!r}; refusing to read or write a database whose "
                    "columns may not mean what this code thinks they mean"
                )
        yield connection


def _reflect_tables(connection: Connection) -> set[str]:
    if connection.engine.dialect.name == "sqlite":
        rows = connection.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
    else:
        rows = connection.execute(text("SELECT table_name AS name FROM information_schema.tables"))
    return {str(row[0]) for row in rows}


def table_names(engine: Engine) -> set[str]:
    with engine.connect() as connection:
        return _reflect_tables(connection)


def declared_tables() -> set[str]:
    return set(METADATA.tables)
