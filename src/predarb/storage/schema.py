"""The Phase-1 durable catalogue, as explicit SQLAlchemy Core tables.

Two categories, and the boundary between them is the whole design.

**Source evidence** is what the venue actually said: raw frames, lifecycle
events, REST payloads, registry snapshots. It is captured append-only and is
never regenerable -- if it is lost, that moment is gone.

**Derived data** is what we concluded: reconstructed books, execution curves,
fee quotes, decisions. All of it can be rebuilt from source evidence by replay.

So derived rows always reference the source rows they came from, and no derived
table is the only copy of something needed to reconstruct history. A decision
row without its observations would be an assertion nobody could check.

Where the bytes live
--------------------
The append-only raw journal stays the raw-source store (`architecture.md` §5.5)
and is not duplicated here. Observations carry a content hash plus a journal
reference, which is enough to fetch the original bytes and prove they have not
changed. Copying multi-kilobyte frames into SQL as well would double the disk
cost to store the same thing twice.

Backend
-------
SQLAlchemy Core, so the same statements run on SQLite and Postgres. Phase 1
defaults to local SQLite with WAL -- a single research process, one file, no
daemon to keep alive across a soak. `architecture.md` names Postgres for the
structured catalogue, and that remains reachable by changing one URL; nothing
here uses a SQLite-only construct.

Money and quantities are canonical strings, never floats. A REAL column would
undo Step 1 at the storage layer.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.engine.interfaces import Dialect

__all__ = [
    "METADATA",
    "SCHEMA_VERSION",
    "UtcDateTime",
    "connection_epochs",
    "decisions",
    "health_events",
    "observations",
    "replay_verifications",
    "sessions",
]

SCHEMA_VERSION = "phase1-catalogue/1"
"""Bumped on any migration. Stored in the database and checked on open, so
newer data cannot be read by older code that would misinterpret it."""

METADATA = MetaData()


class UtcDateTime(TypeDecorator[datetime]):
    """A timestamp that is timezone-aware UTC on both sides of the boundary.

    SQLite has no native timestamp type: ``DateTime(timezone=True)`` stores a
    string and hands back a **naive** ``datetime``. Every other module in this
    codebase refuses naive times, so without this the catalogue would be the
    one place that quietly reintroduced them -- and a naive time compared
    against an aware one raises at the worst possible moment, mid-soak.

    Normalising here rather than at each call site means a column typed as a
    timestamp is aware by construction, on SQLite and Postgres alike.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, _dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(
                f"naive datetime {value!r} reached the catalogue; all stored times "
                "must be timezone-aware UTC"
            )
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, _dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


sessions = Table(
    "sessions",
    METADATA,
    # Content-addressed from the plan and start instant, so a retried startup
    # resumes one session rather than forking a second identical one.
    Column("session_id", String(64), primary_key=True),
    Column("started_at", UtcDateTime, nullable=False),
    Column("ended_at", UtcDateTime, nullable=True),
    Column("venue", String(32), nullable=False),
    Column("environment", String(16), nullable=False),
    Column("status", String(32), nullable=False),
    # The exact monitoring configuration, stored verbatim. Replay evaluates only
    # what the session said it was watching; inferring it later would let a
    # replay consider work the live system never did.
    Column("detector_plan", Text, nullable=False),
    Column("detector_plan_sha256", String(64), nullable=False),
    Column("schema_version", String(64), nullable=False),
    Column("raw_journal_path", Text, nullable=True),
    Column("notes", Text, nullable=False, server_default=""),
)


connection_epochs = Table(
    "connection_epochs",
    METADATA,
    Column("session_id", String(64), ForeignKey("sessions.session_id"), primary_key=True),
    Column("epoch", Integer, primary_key=True),
    Column("opened_at", UtcDateTime, nullable=False),
    Column("closed_at", UtcDateTime, nullable=True),
    Column("close_reason", Text, nullable=True),
)


observations = Table(
    "observations",
    METADATA,
    # SOURCE EVIDENCE. Append-only; rows are never updated.
    Column("session_id", String(64), ForeignKey("sessions.session_id"), primary_key=True),
    Column("ordinal", Integer, primary_key=True),
    # Stable identity over (session, ordinal, kind, payload hash). A retried
    # write proves the same row already exists instead of inserting a second.
    Column("observation_id", String(64), nullable=False),
    Column("observed_at", UtcDateTime, nullable=False),
    Column("kind", String(48), nullable=False),
    Column("subject", String(128), nullable=True),
    Column("source", String(64), nullable=False, server_default=""),
    Column("connection_epoch", Integer, nullable=True),
    Column("payload_sha256", String(64), nullable=False),
    # Where the original bytes live. Small payloads are inlined; a frame is
    # referenced, because the journal already holds it byte-for-byte.
    Column("payload_json", Text, nullable=True),
    Column("raw_journal_ref", Text, nullable=True),
    UniqueConstraint("observation_id", name="uq_observations_identity"),
    Index("ix_observations_session_kind", "session_id", "kind"),
    Index("ix_observations_subject", "session_id", "subject"),
    Index("ix_observations_observed_at", "session_id", "observed_at"),
)


decisions = Table(
    "decisions",
    METADATA,
    # DERIVED. Regenerable from the observations above, and verified against
    # them by `predarb storage verify-decisions`.
    Column("session_id", String(64), ForeignKey("sessions.session_id"), primary_key=True),
    Column("decision_ordinal", Integer, primary_key=True),
    Column("decision_id", String(64), nullable=False),
    Column("detector", String(48), nullable=False),
    Column("trigger_ordinal", Integer, nullable=False),
    Column("trigger_reason", String(48), nullable=False),
    Column("decision_time", UtcDateTime, nullable=False),
    Column("subjects", Text, nullable=False),
    Column("quantity", String(32), nullable=True),
    Column("detector_did_run", Boolean, nullable=False),
    Column("classification", String(48), nullable=False),
    Column("blocking_reason", Text, nullable=True),
    Column("missing_knowledge", Text, nullable=False, server_default="[]"),
    Column("context_ids", Text, nullable=False, server_default="{}"),
    Column("completeness", Text, nullable=False, server_default="{}"),
    Column("fingerprint_version", String(48), nullable=False),
    Column("fingerprint_digest", String(64), nullable=False),
    Column("fingerprint_components", Text, nullable=False, server_default="{}"),
    Column("warnings", Text, nullable=False, server_default="[]"),
    UniqueConstraint("decision_id", name="uq_decisions_identity"),
    Index("ix_decisions_session_detector", "session_id", "detector"),
    Index("ix_decisions_classification", "session_id", "classification"),
    Index("ix_decisions_fingerprint", "fingerprint_digest"),
    Index("ix_decisions_trigger", "session_id", "trigger_ordinal"),
)


health_events = Table(
    "health_events",
    METADATA,
    Column("session_id", String(64), ForeignKey("sessions.session_id"), nullable=False),
    Column("event_id", String(64), primary_key=True),
    Column("at", UtcDateTime, nullable=False),
    Column("kind", String(48), nullable=False),
    Column("severity", String(16), nullable=False),
    Column("detail", Text, nullable=False, server_default=""),
    Column("counters", Text, nullable=False, server_default="{}"),
    Index("ix_health_session_at", "session_id", "at"),
)


replay_verifications = Table(
    "replay_verifications",
    METADATA,
    # The audit linkage: which replay checked which session, and what it found.
    Column("verification_id", String(64), primary_key=True),
    Column("session_id", String(64), ForeignKey("sessions.session_id"), nullable=False),
    Column("verified_at", UtcDateTime, nullable=False),
    Column("observations_replayed", Integer, nullable=False),
    Column("decisions_persisted", Integer, nullable=False),
    Column("decisions_regenerated", Integer, nullable=False),
    Column("fingerprint_matches", Integer, nullable=False),
    Column("missing_persisted", Integer, nullable=False),
    Column("extra_persisted", Integer, nullable=False),
    Column("fingerprint_mismatches", Integer, nullable=False),
    Column("network_calls", Integer, nullable=False),
    Column("detail", Text, nullable=False, server_default="{}"),
    Index("ix_replay_session", "session_id"),
)
