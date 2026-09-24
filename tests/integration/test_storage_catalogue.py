"""Durable catalogue: idempotency, crash recovery, and the safety ordering.

The property under test throughout is that **source evidence is never lost and
never duplicated**, and that derived data can always be rebuilt from it. Every
other guarantee in Phase 1 rests on those two.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from predarb.clock import FrozenClock
from predarb.ingest.collector import (
    DurableCollector,
    QueueOverflowError,
    resume_ordinal,
)
from predarb.replay.knowledge import KnowledgeBase
from predarb.replay.observation import Observation, ObservationKind
from predarb.replay.plan import DetectorPlan
from predarb.storage.catalogue import (
    Catalogue,
    SourcePersistenceError,
    observation_identity,
    persist_decisions,
    persist_observations,
    session_identity,
)
from predarb.storage.engine import (
    SchemaVersionError,
    connect,
    create_catalogue_engine,
    current_schema_version,
    table_names,
)
from predarb.storage.migrations import MigrationError, migrate, pending_migrations
from predarb.storage.schema import SCHEMA_VERSION
from predarb.storage.verification import verify_session_decisions
from predarb.venues.kalshi.replay_context import KalshiReplayContext
from tests.integration.replay_fixtures import BINARY_PLAN, binary_stream, run

pytestmark = pytest.mark.integration


def kalshi_provider(knowledge: KnowledgeBase, plan: DetectorPlan) -> KalshiReplayContext:
    """The venue resolver, injected so ``storage`` stays a leaf layer."""
    return KalshiReplayContext(knowledge=knowledge, plan=plan)


T0 = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """A migrated catalogue, disposed on teardown.

    Disposal matters beyond tidiness: a long soak that leaked a connection per
    write would exhaust file handles hours in, which is exactly when it is most
    expensive to discover.
    """
    created = create_catalogue_engine(tmp_path / "catalogue.sqlite")
    migrate(created)
    yield created
    created.dispose()


@pytest.fixture
def session(engine: Engine) -> str:
    with connect(engine) as connection:
        stored = Catalogue(connection).open_session(
            detector_plan=BINARY_PLAN.to_payload(),
            started_at=T0,
            schema_version=SCHEMA_VERSION,
        )
    return stored.session_id


class TestMigrations:
    def test_a_fresh_database_reaches_head(self, tmp_path: Path) -> None:
        fresh = create_catalogue_engine(tmp_path / "fresh.sqlite")
        try:
            assert migrate(fresh) == [SCHEMA_VERSION]
            with fresh.connect() as connection:
                assert current_schema_version(connection) == SCHEMA_VERSION
        finally:
            fresh.dispose()

    def test_every_declared_table_exists(self, engine: Engine) -> None:
        present = table_names(engine)
        for table in (
            "sessions",
            "observations",
            "decisions",
            "connection_epochs",
            "health_events",
            "replay_verifications",
        ):
            assert table in present

    def test_migrating_twice_applies_nothing(self, engine: Engine) -> None:
        assert migrate(engine) == []

    def test_a_newer_database_is_refused_not_opened(self, engine: Engine) -> None:
        """Older code reading newer rows corrupts an audit trail invisibly."""
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM predarb_schema_version"))
            connection.execute(
                text("INSERT INTO predarb_schema_version (version) VALUES ('phase1-catalogue/99')")
            )
        with pytest.raises(SchemaVersionError, match="refusing to read or write"), connect(engine):
            pass
        with pytest.raises(MigrationError, match="unknown to this build"):
            pending_migrations("phase1-catalogue/99")

    def test_an_unversioned_database_is_refused(self, tmp_path: Path) -> None:
        bare = create_catalogue_engine(tmp_path / "bare.sqlite")
        try:
            with pytest.raises(SchemaVersionError, match="no schema version"), connect(bare):
                pass
        finally:
            bare.dispose()

    def test_sqlite_is_configured_not_assumed(self, engine: Engine) -> None:
        """Foreign keys are OFF by default in SQLite; a declared reference
        that did nothing would accept orphaned decisions."""
        with engine.connect() as connection:
            assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1
            assert str(connection.execute(text("PRAGMA journal_mode")).scalar()).lower() == "wal"


class TestIdempotentWrites:
    def test_the_same_session_resumes_rather_than_forking(self, engine: Engine) -> None:
        with connect(engine) as connection:
            first = Catalogue(connection).open_session(
                detector_plan=BINARY_PLAN.to_payload(),
                started_at=T0,
                schema_version=SCHEMA_VERSION,
            )
        with connect(engine) as connection:
            second = Catalogue(connection).open_session(
                detector_plan=BINARY_PLAN.to_payload(),
                started_at=T0,
                schema_version=SCHEMA_VERSION,
            )
            assert second.session_id == first.session_id
            assert len(Catalogue(connection).list_sessions()) == 1

    def test_a_different_plan_is_a_different_session(self) -> None:
        other = replace(BINARY_PLAN, notes="different configuration")
        assert session_identity(
            detector_plan=other.to_payload(), started_at=T0
        ) != session_identity(detector_plan=BINARY_PLAN.to_payload(), started_at=T0)

    def test_replaying_the_same_observations_inserts_nothing_new(
        self, engine: Engine, session: str
    ) -> None:
        stream = binary_stream()
        with connect(engine) as connection:
            assert persist_observations(Catalogue(connection), session, stream) == (14, 0)
        with connect(engine) as connection:
            catalogue = Catalogue(connection)
            assert persist_observations(catalogue, session, stream) == (0, 14)
            assert catalogue.observation_count(session) == 14

    def test_replaying_the_same_decisions_inserts_nothing_new(
        self, engine: Engine, session: str
    ) -> None:
        decisions = run(BINARY_PLAN, binary_stream()).decisions
        with connect(engine) as connection:
            assert persist_decisions(Catalogue(connection), session, decisions)[0] == 6
        with connect(engine) as connection:
            catalogue = Catalogue(connection)
            assert persist_decisions(catalogue, session, decisions) == (0, 6)
            assert catalogue.decision_count(session) == 6

    def test_identity_covers_payload_not_just_ordinal(self, session: str) -> None:
        """Two different payloads at one ordinal must not collide silently."""
        first = Observation(
            ordinal=7, observed_at=T0, kind=ObservationKind.MARKET_METADATA, payload={"a": 1}
        )
        second = Observation(
            ordinal=7, observed_at=T0, kind=ObservationKind.MARKET_METADATA, payload={"a": 2}
        )
        assert observation_identity(session, first) != observation_identity(session, second)

    def test_health_events_are_idempotent_too(self, engine: Engine, session: str) -> None:
        for _ in range(3):
            with connect(engine) as connection:
                Catalogue(connection).record_health(
                    session, kind="RECONNECT", severity="WARNING", at=T0, detail="same"
                )
        with connect(engine) as connection:
            assert Catalogue(connection).health_counts(session) == {"RECONNECT": 1}


class TestSourceStreamRoundTrip:
    def test_the_stream_reloads_byte_identically(self, engine: Engine, session: str) -> None:
        """Derived data is only regenerable if the source really round-trips."""
        stream = binary_stream()
        with connect(engine) as connection:
            persist_observations(Catalogue(connection), session, stream)
        with connect(engine) as connection:
            reloaded = Catalogue(connection).load_observations(session)
        assert reloaded.content_hash() == stream.content_hash()
        assert len(reloaded) == len(stream)

    def test_reloaded_timestamps_are_timezone_aware(self, engine: Engine, session: str) -> None:
        """SQLite hands back naive datetimes; every other module refuses them."""
        with connect(engine) as connection:
            persist_observations(Catalogue(connection), session, binary_stream())
        with connect(engine) as connection:
            reloaded = Catalogue(connection).load_observations(session)
        assert all(o.observed_at.tzinfo is not None for o in reloaded)

    def test_a_referenced_frame_keeps_its_hash_without_inlining(
        self, engine: Engine, session: str
    ) -> None:
        """The journal already holds the bytes; storing them twice is waste."""
        frame = next(o for o in binary_stream() if o.kind is ObservationKind.FRAME_RECEIVED)
        with connect(engine) as connection:
            Catalogue(connection).persist_observation(
                session, frame, raw_journal_ref="raw/2026-09-23.jsonl#42"
            )
        with connect(engine) as connection:
            row = connection.execute(
                text("SELECT payload_json, raw_journal_ref, payload_sha256 FROM observations")
            ).one()
        assert row[0] is None
        assert row[1] == "raw/2026-09-23.jsonl#42"
        assert row[2] == frame.content_hash


class TestCollectorSafetyOrdering:
    def collector(self, engine: Engine, session: str) -> DurableCollector:
        return DurableCollector(
            engine=engine, plan=BINARY_PLAN, clock=FrozenClock(T0), session_id=session
        )

    def test_source_persistence_failure_is_fatal(
        self, engine: Engine, session: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No source evidence means nothing downstream can ever be checked."""
        collector = self.collector(engine, session)

        def explode(*_a: Any, **_k: Any) -> bool:
            raise RuntimeError("simulated source write failure")

        monkeypatch.setattr(Catalogue, "persist_observation", explode)

        with pytest.raises(SourcePersistenceError, match="must stop rather than scan"):
            collector.ingest(next(iter(binary_stream())))
        assert collector.health.healthy is False

    def test_an_unhealthy_collector_refuses_to_continue(self, engine: Engine, session: str) -> None:
        collector = self.collector(engine, session)
        collector.health.fail("forced")
        with pytest.raises(QueueOverflowError, match="refusing to continue"):
            collector.ingest(next(iter(binary_stream())))

    def test_derived_write_failure_is_recoverable_not_fatal(
        self, engine: Engine, session: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Decisions are regenerable, so losing one must not stop collection."""
        collector = self.collector(engine, session)

        def explode(*_a: Any, **_k: Any) -> bool:
            raise RuntimeError("simulated derived write failure")

        monkeypatch.setattr(Catalogue, "persist_decision", explode)

        produced = 0
        for observation in binary_stream():
            produced += len(collector.ingest(observation))

        assert produced > 0
        assert collector.health.healthy is True
        assert collector.metrics.decision_write_failures > 0
        # The evidence survived, which is what makes the loss recoverable.
        with connect(engine) as connection:
            assert Catalogue(connection).observation_count(session) == 14

    def test_lost_decisions_are_regenerated_by_replay(
        self, engine: Engine, session: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        collector = self.collector(engine, session)
        monkeypatch.setattr(
            Catalogue,
            "persist_decision",
            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("down")),
        )
        for observation in binary_stream():
            collector.ingest(observation)
        monkeypatch.undo()

        report = verify_session_decisions(
            engine, session, provider_factory=kalshi_provider, verified_at=T0
        )
        assert report.decisions_persisted == 0
        assert report.decisions_regenerated == 6
        assert len(report.missing_persisted) == 6
        assert report.exact_match is False


class TestBackpressure:
    def test_exceeding_the_bound_stops_rather_than_samples(
        self, engine: Engine, session: str
    ) -> None:
        """A silently sampled source stream reconstructs a different book."""
        collector = DurableCollector(
            engine=engine,
            plan=BINARY_PLAN,
            clock=FrozenClock(T0),
            session_id=session,
            max_queue_depth=0,
        )
        with pytest.raises(QueueOverflowError, match="rather than sampling"):
            collector.ingest(next(iter(binary_stream())))
        assert collector.health.healthy is False

    def test_the_high_water_mark_is_recorded(self, engine: Engine, session: str) -> None:
        collector = DurableCollector(
            engine=engine, plan=BINARY_PLAN, clock=FrozenClock(T0), session_id=session
        )
        for observation in binary_stream():
            collector.ingest(observation)
        assert collector.metrics.queue_high_water_mark >= 1


class TestCrashRecovery:
    def ingest_through(self, engine: Engine, session: str, stop_after: int) -> DurableCollector:
        collector = DurableCollector(
            engine=engine, plan=BINARY_PLAN, clock=FrozenClock(T0), session_id=session
        )
        for index, observation in enumerate(binary_stream()):
            if index >= stop_after:
                break
            collector.ingest(observation)
        return collector

    @pytest.mark.parametrize("stop_after", [1, 5, 9, 14])
    def test_restart_resumes_from_durable_state(
        self, engine: Engine, session: str, stop_after: int
    ) -> None:
        """Whatever moment the process died, the evidence is intact and
        numbering resumes without reusing an ordinal."""
        self.ingest_through(engine, session, stop_after)

        with connect(engine) as connection:
            assert Catalogue(connection).observation_count(session) == stop_after
        assert resume_ordinal(engine, session) == stop_after

    def test_re_ingesting_after_a_crash_creates_no_duplicates(
        self, engine: Engine, session: str
    ) -> None:
        """A restart that replays the tail must not double-count it."""
        self.ingest_through(engine, session, 9)
        restarted = DurableCollector(
            engine=engine, plan=BINARY_PLAN, clock=FrozenClock(T0), session_id=session
        )
        for observation in binary_stream():
            restarted.ingest(observation)

        with connect(engine) as connection:
            assert Catalogue(connection).observation_count(session) == 14
        assert restarted.metrics.observations_duplicate == 9

    def test_decisions_survive_and_verify_after_a_full_run(
        self, engine: Engine, session: str
    ) -> None:
        collector = self.ingest_through(engine, session, 14)
        collector.finish()

        report = verify_session_decisions(
            engine, session, provider_factory=kalshi_provider, verified_at=T0
        )
        assert report.exact_match
        assert report.fingerprint_matches == 6
        assert report.network_calls == 0

    def test_an_empty_session_verifies_as_empty_not_as_broken(
        self, engine: Engine, session: str
    ) -> None:
        report = verify_session_decisions(
            engine, session, provider_factory=kalshi_provider, verified_at=T0
        )
        assert report.observations_replayed == 0
        assert report.exact_match


class TestVerificationDetectsTampering:
    def _populate(self, engine: Engine, session: str) -> None:
        collector = DurableCollector(
            engine=engine, plan=BINARY_PLAN, clock=FrozenClock(T0), session_id=session
        )
        for observation in binary_stream():
            collector.ingest(observation)

    def test_a_rewritten_fingerprint_is_reported_not_repaired(
        self, engine: Engine, session: str
    ) -> None:
        """Replay is authoritative; silently fixing the row would erase the
        only evidence that something went wrong."""
        self._populate(engine, session)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE decisions SET fingerprint_digest='0'||substr(fingerprint_digest,2) "
                    "WHERE decision_ordinal=1"
                )
            )
        report = verify_session_decisions(
            engine, session, provider_factory=kalshi_provider, verified_at=T0
        )
        assert len(report.fingerprint_mismatches) == 1
        assert report.exact_match is False

        with engine.connect() as connection:
            still_tampered = connection.execute(
                text("SELECT fingerprint_digest FROM decisions WHERE decision_ordinal=1")
            ).scalar_one()
        assert str(still_tampered).startswith("0")

    def test_a_decision_with_no_source_behind_it_is_reported_as_extra(
        self, engine: Engine, session: str
    ) -> None:
        self._populate(engine, session)
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM observations WHERE ordinal >= 10"))
        report = verify_session_decisions(
            engine, session, provider_factory=kalshi_provider, verified_at=T0
        )
        assert report.extra_persisted
        assert report.exact_match is False

    def test_verification_makes_no_network_call(
        self, engine: Engine, session: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._populate(engine, session)

        def refuse(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("verification attempted a network connection")

        monkeypatch.setattr(socket.socket, "connect", refuse)
        monkeypatch.setattr(socket, "create_connection", refuse)
        monkeypatch.setattr(socket, "getaddrinfo", refuse)

        report = verify_session_decisions(
            engine, session, provider_factory=kalshi_provider, verified_at=T0
        )
        assert report.exact_match
        assert report.network_calls == 0

    def test_the_verification_is_recorded_for_audit(self, engine: Engine, session: str) -> None:
        self._populate(engine, session)
        verify_session_decisions(
            engine,
            session,
            provider_factory=kalshi_provider,
            verified_at=T0 + timedelta(minutes=1),
        )
        with engine.connect() as connection:
            rows = connection.execute(
                text("SELECT COUNT(*) FROM replay_verifications")
            ).scalar_one()
        assert rows == 1


class TestSessionPrefixResolution:
    """Operators type the abbreviated id the listing prints."""

    def test_a_unique_prefix_resolves(self, engine: Engine, session: str) -> None:
        with connect(engine) as connection:
            assert Catalogue(connection).resolve_session_id(session[:12]) == session

    def test_the_full_id_resolves(self, engine: Engine, session: str) -> None:
        with connect(engine) as connection:
            assert Catalogue(connection).resolve_session_id(session) == session

    def test_an_unknown_prefix_resolves_to_nothing(self, engine: Engine) -> None:
        with connect(engine) as connection:
            assert Catalogue(connection).resolve_session_id("ffffffff") is None

    def test_an_ambiguous_prefix_refuses_rather_than_guessing(self, engine: Engine) -> None:
        """Picking a row is how the wrong session gets verified."""
        with connect(engine) as connection:
            catalogue = Catalogue(connection)
            for minute in range(6):
                catalogue.open_session(
                    detector_plan=BINARY_PLAN.to_payload(),
                    started_at=T0 + timedelta(minutes=minute),
                    schema_version=SCHEMA_VERSION,
                )
            assert catalogue.resolve_session_id("") is None


class TestLatencyMetrics:
    def test_percentiles_of_an_empty_sample_are_zero_not_missing(self) -> None:
        collector = DurableCollector(
            engine=None,  # type: ignore[arg-type]
            plan=BINARY_PLAN,
            clock=FrozenClock(T0),
            session_id="unused",
        )
        assert collector.metrics.percentiles([]) == {
            "p50": 0.0,
            "p95": 0.0,
            "p99": 0.0,
            "max": 0.0,
        }

    def test_percentiles_are_ordered(self) -> None:
        collector = DurableCollector(
            engine=None,  # type: ignore[arg-type]
            plan=BINARY_PLAN,
            clock=FrozenClock(T0),
            session_id="unused",
        )
        result = collector.metrics.percentiles([float(n) for n in range(1, 101)])
        assert result["p50"] <= result["p95"] <= result["p99"] <= result["max"]

    def test_the_metrics_payload_is_json_serialisable(self, engine: Engine, session: str) -> None:
        """It is written into a health row, so it must survive serialisation."""
        collector = DurableCollector(
            engine=engine, plan=BINARY_PLAN, clock=FrozenClock(T0), session_id=session
        )
        for observation in binary_stream():
            collector.ingest(observation)
        payload = collector.metrics.payload()
        assert json.loads(json.dumps(payload))["observations_persisted"] == 14
        assert set(payload["latency_ms"]) == {
            "receive_to_persisted",
            "receive_to_book",
            "receive_to_decision",
        }
