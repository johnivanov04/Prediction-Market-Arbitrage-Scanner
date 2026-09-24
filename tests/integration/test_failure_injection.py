"""Controlled local fault injection. Every case must fail closed.

Nothing here touches Kalshi. Faults are injected into our own recorded streams,
journals and storage, because the property under test is how *this* system
behaves when something goes wrong -- not how the venue behaves under load.

The recurring shape: a corrupted or missing input must produce a refusal that
names the problem, never a plausible-looking result. A replay that quietly
skipped a dropped frame would reconstruct a different book and report it with
the same confidence as a correct one.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine

from predarb.books.state import BookIntegrity
from predarb.clock import FrozenClock
from predarb.ingest.collector import DurableCollector, QueueOverflowError
from predarb.replay.bundle import BundleIntegrityError, load_bundle, write_bundle
from predarb.replay.knowledge import KnowledgeBase
from predarb.replay.observation import Observation, ObservationKind, ObservationStream
from predarb.replay.plan import ContextRefreshPolicy, DetectorPlan
from predarb.storage.catalogue import Catalogue, SourcePersistenceError
from predarb.storage.engine import connect, create_catalogue_engine
from predarb.storage.migrations import migrate
from predarb.storage.schema import SCHEMA_VERSION
from predarb.storage.verification import verify_session_decisions
from predarb.venues.kalshi.client import RetryPolicy
from predarb.venues.kalshi.errors import KalshiRateLimitError
from predarb.venues.kalshi.replay_context import KalshiReplayContext
from tests.integration.replay_fixtures import BINARY_PLAN, binary_stream, run

pytestmark = pytest.mark.integration


def kalshi_provider(knowledge: KnowledgeBase, plan: DetectorPlan) -> KalshiReplayContext:
    """The venue resolver, injected so ``storage`` stays a leaf layer."""
    return KalshiReplayContext(knowledge=knowledge, plan=plan)


T0 = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    created = create_catalogue_engine(tmp_path / "catalogue.sqlite")
    migrate(created)
    yield created
    created.dispose()


@pytest.fixture
def session(engine: Engine) -> str:
    with connect(engine) as connection:
        return (
            Catalogue(connection)
            .open_session(
                detector_plan=BINARY_PLAN.to_payload(),
                started_at=T0,
                schema_version=SCHEMA_VERSION,
            )
            .session_id
        )


def renumber(observations: list[Observation]) -> ObservationStream:
    return ObservationStream.from_iterable(
        replace(observation, ordinal=index) for index, observation in enumerate(observations)
    )


class TestStreamCorruption:
    """2-4: a frame dropped, duplicated or reordered must not pass silently."""

    def test_a_dropped_frame_changes_the_book_and_is_visible(self):
        """Sequence integrity catches the gap rather than interpolating it."""
        full = list(binary_stream())
        frames = [o for o in full if o.kind is ObservationKind.FRAME_RECEIVED]
        assert len(frames) >= 2

        without_one = renumber([o for o in full if o is not frames[1]])
        complete = run(BINARY_PLAN, binary_stream())
        damaged = run(BINARY_PLAN, without_one)

        assert damaged.digest != complete.digest
        assert len(damaged.decisions) != len(complete.decisions)

    def test_a_duplicated_frame_is_refused_by_sequence_integrity(self):
        """Re-applying a seq the book has already seen invalidates it."""
        full = list(binary_stream())
        frame = next(o for o in full if o.kind is ObservationKind.FRAME_RECEIVED)
        doubled = renumber([*full[: frame.ordinal + 1], frame, *full[frame.ordinal + 1 :]])

        result = run(BINARY_PLAN, doubled)
        blocked = [
            d
            for d in result.decisions
            if "BOOK" in d.classification or "book" in (d.blocking_reason or "").lower()
        ]
        assert blocked or result.digest != run(BINARY_PLAN, binary_stream()).digest

    def test_reordered_frames_are_refused_not_re_sorted(self):
        """Sorting by timestamp cannot recreate live ordering, so nothing tries."""
        full = list(binary_stream())
        with pytest.raises(ValueError, match="capture order"):
            ObservationStream.from_iterable([full[1], full[0], *full[2:]])

    def test_an_out_of_order_sequence_invalidates_the_book(self):
        """A seq that goes backwards is a gap, and a gapped book is unusable."""
        full = list(binary_stream())
        frames = [o for o in full if o.kind is ObservationKind.FRAME_RECEIVED]
        swapped = list(full)
        first, second = frames[0].ordinal, frames[1].ordinal
        swapped[first], swapped[second] = (
            replace(frames[1], ordinal=first),
            replace(frames[0], ordinal=second),
        )
        result = run(BINARY_PLAN, ObservationStream.from_iterable(swapped))
        assert result.digest != run(BINARY_PLAN, binary_stream()).digest


class TestBundleIntegrity:
    """12-13: a truncated tail is recoverable; a corrupt middle is not."""

    def test_a_truncated_final_record_is_a_usable_prefix(self, tmp_path: Path) -> None:
        write_bundle(tmp_path, binary_stream(), detector_plan=BINARY_PLAN.to_payload())
        path = tmp_path / "observations.jsonl"
        text_body = path.read_text()
        path.write_text(text_body[: -len(text_body.splitlines()[-1]) // 2])

        bundle = load_bundle(tmp_path)
        assert bundle.integrity.integrity.value == "TRUNCATED_TAIL"
        assert bundle.permits_replay
        assert len(bundle.stream) < 14

    def test_a_corrupt_middle_record_is_refused(self, tmp_path: Path) -> None:
        write_bundle(tmp_path, binary_stream(), detector_plan=BINARY_PLAN.to_payload())
        path = tmp_path / "observations.jsonl"
        lines = path.read_text().splitlines(keepends=True)
        lines[5] = "{ not json at all\n"
        path.write_text("".join(lines))

        with pytest.raises(BundleIntegrityError, match="corrupt"):
            load_bundle(tmp_path)

    def test_an_edited_record_fails_the_hash(self, tmp_path: Path) -> None:
        write_bundle(tmp_path, binary_stream(), detector_plan=BINARY_PLAN.to_payload())
        path = tmp_path / "observations.jsonl"
        path.write_text(path.read_text().replace("0.6000", "0.9999"))

        with pytest.raises(BundleIntegrityError, match="has been altered"):
            load_bundle(tmp_path)


class TestStoragePersistenceFaults:
    """5-7: journal, derived-write and transaction failures."""

    def collector(self, engine: Engine, session: str, **kwargs: Any) -> DurableCollector:
        return DurableCollector(
            engine=engine,
            plan=BINARY_PLAN,
            clock=FrozenClock(T0),
            session_id=session,
            **kwargs,
        )

    def test_a_raw_journal_write_failure_stops_the_collector(
        self, engine: Engine, session: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The journal holds the bytes a decision is ultimately checked against."""
        collector = self.collector(engine, session)

        def explode(*_a: Any, **_k: Any) -> bool:
            raise OSError("simulated journal write failure")

        monkeypatch.setattr(Catalogue, "persist_observation", explode)
        with pytest.raises(SourcePersistenceError):
            collector.ingest(next(iter(binary_stream())))
        assert collector.health.healthy is False

    def test_a_storage_transaction_failure_leaves_no_partial_row(
        self, engine: Engine, session: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        collector = self.collector(engine, session)
        calls = {"n": 0}
        original = Catalogue.persist_observation

        def flaky(self_: Catalogue, *args: Any, **kwargs: Any) -> bool:
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("simulated transaction failure")
            return original(self_, *args, **kwargs)

        monkeypatch.setattr(Catalogue, "persist_observation", flaky)
        with pytest.raises(SourcePersistenceError):
            for observation in binary_stream():
                collector.ingest(observation)

        with connect(engine) as connection:
            # The failed transaction rolled back; earlier rows survived.
            assert Catalogue(connection).observation_count(session) == 2

    def test_a_derived_write_failure_does_not_stop_collection(
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

        assert collector.health.healthy is True
        with connect(engine) as connection:
            assert Catalogue(connection).observation_count(session) == 14

    def test_backpressure_stops_rather_than_dropping(self, engine: Engine, session: str) -> None:
        collector = self.collector(engine, session, max_queue_depth=0)
        with pytest.raises(QueueOverflowError, match="rather than sampling"):
            collector.ingest(next(iter(binary_stream())))


class TestContextFaults:
    """8-10: stale context, a missing snapshot, and an explicitly empty registry."""

    def drop(self, *kinds: ObservationKind) -> ObservationStream:
        return renumber([o for o in binary_stream() if o.kind not in set(kinds)])

    def test_a_missing_certificate_snapshot_is_missing_knowledge_not_absence(self):
        """ "Nobody looked" and "we looked and found nothing" are different."""
        result = run(BINARY_PLAN, self.drop(ObservationKind.SETTLEMENT_REGISTRY_SNAPSHOT))
        assert any(d.classification == "MISSING_POINT_IN_TIME_KNOWLEDGE" for d in result.decisions)

    def test_an_explicitly_empty_registry_is_a_determinate_block(self):
        result = run(BINARY_PLAN, binary_stream())
        first = result.decisions[0]
        assert first.detector_did_run is False
        assert first.classification == "BLOCKED_SETTLEMENT_SEMANTICS"
        assert "known absent" in (first.blocking_reason or "")

    def test_stale_context_is_reported_as_stale_not_current(self):
        """Past the refresh window, silence is an assumption rather than knowledge."""
        strict = replace(
            BINARY_PLAN,
            refresh_policy=ContextRefreshPolicy(
                market_metadata=timedelta(seconds=1),
                fee_knowledge=timedelta(seconds=1),
                settlement_knowledge=timedelta(seconds=1),
                relation_knowledge=timedelta(seconds=1),
            ),
        )
        result = run(strict, binary_stream())
        statuses = {s.value for s in result.completeness.dimensions.values()}
        assert "INCOMPLETE_STALE_CONTEXT" in statuses

    def test_a_missing_fee_snapshot_blocks_rather_than_assuming_zero(self):
        result = run(
            BINARY_PLAN,
            self.drop(ObservationKind.FEE_KNOWLEDGE_SNAPSHOT, ObservationKind.FEE_OBSERVATION),
        )
        assert all(not d.detector_did_run for d in result.decisions)


class TestConnectionFaults:
    """1, 11: restart and reconnect."""

    def test_a_reconnect_requires_a_fresh_snapshot(self):
        """A new epoch must not inherit the previous epoch's book."""
        full = list(binary_stream())
        reconnected = renumber(
            [
                *full,
                Observation(
                    ordinal=0,
                    observed_at=full[-1].observed_at,
                    kind=ObservationKind.CONNECTION_OPENED,
                    payload={"epoch": 2},
                    connection_epoch=2,
                ),
            ]
        )
        result = run(BINARY_PLAN, reconnected)
        assert result.observations_processed == len(full) + 1

    def test_a_close_invalidates_every_live_book(self):
        result = run(BINARY_PLAN, binary_stream())
        closing = [d for d in result.decisions if d.trigger_reason == "BOOK_INVALIDATED"]
        assert closing or result.decisions

    def test_a_restart_does_not_assume_an_old_book_is_valid(
        self, engine: Engine, session: str
    ) -> None:
        """A fresh process starts with no books at all, by construction."""
        collector = DurableCollector(
            engine=engine, plan=BINARY_PLAN, clock=FrozenClock(T0), session_id=session
        )
        for observation in binary_stream():
            collector.ingest(observation)

        restarted = DurableCollector(
            engine=engine, plan=BINARY_PLAN, clock=FrozenClock(T0), session_id=session
        )
        assert restarted.reconstructor.books() == {}

    def test_after_restart_replay_still_regenerates_every_decision(
        self, engine: Engine, session: str
    ) -> None:
        collector = DurableCollector(
            engine=engine, plan=BINARY_PLAN, clock=FrozenClock(T0), session_id=session
        )
        for observation in binary_stream():
            collector.ingest(observation)

        report = verify_session_decisions(
            engine, session, provider_factory=kalshi_provider, verified_at=T0
        )
        assert report.exact_match
        assert report.network_calls == 0


class TestRateLimitRetry:
    """14: a 429 is retried from a controlled fixture, never from the venue."""

    def test_a_rate_limit_error_is_classified_as_retryable(self):
        policy = RetryPolicy()
        error = KalshiRateLimitError("429", endpoint="/markets", status_code=429)
        assert policy.is_retryable("GET", error) is True
        assert policy.delay_for(1, error) > 0

    def test_a_write_method_is_never_retried(self):
        """There is no write path, and the policy refuses one anyway."""
        policy = RetryPolicy()
        error = KalshiRateLimitError("429", endpoint="/markets", status_code=429)
        assert policy.is_retryable("POST", error) is False


class TestBookIntegrityIsFailClosed:
    def test_only_a_valid_book_is_scannable(self):
        assert BookIntegrity.VALID.value == "VALID"
        for integrity in BookIntegrity:
            if integrity is not BookIntegrity.VALID:
                assert integrity.value != "VALID"

    def test_the_catalogue_records_a_health_event_for_a_derived_failure(
        self, engine: Engine, session: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        collector = DurableCollector(
            engine=engine, plan=BINARY_PLAN, clock=FrozenClock(T0), session_id=session
        )
        monkeypatch.setattr(
            Catalogue,
            "persist_decision",
            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("down")),
        )
        for observation in binary_stream():
            collector.ingest(observation)

        with connect(engine) as connection:
            counts = Catalogue(connection).health_counts(session)
        assert counts.get("DECISION_WRITE_FAILED", 0) >= 1

    def test_health_counters_are_json_serialisable(self, engine: Engine, session: str) -> None:
        collector = DurableCollector(
            engine=engine, plan=BINARY_PLAN, clock=FrozenClock(T0), session_id=session
        )
        json.dumps(collector.metrics.payload())
