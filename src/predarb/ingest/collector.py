"""The Phase-1 collector: one loop, all three detectors, durable throughout.

    receive observation
      -> persist SOURCE evidence          (fatal if it fails)
      -> mutate authoritative state
      -> evaluate through the coordinator
      -> persist DERIVED decision         (recoverable if it fails)

That ordering is the system's central safety property. Source evidence is not
regenerable: if it is lost, the moment is gone and no replay can check anything
that followed. Derived decisions are regenerable by construction, so a failure
there costs a re-run, not an audit trail.

Reversing it would let the collector hold decisions whose inputs were never
durably captured -- an audit trail nobody can verify, which is worse than none
because it looks like one.

Backpressure, not sampling
--------------------------
Persistence sits **in the receive path**: an observation is durably stored
before the next one is read. So the collector cannot silently fall behind --
pressure shows up as slower consumption of the socket, never as a dropped
frame. A silently sampled source stream reconstructs a different book, and the
gap is invisible in every count that reads it afterwards.

The bound on unpersisted work is therefore a guard rather than something the
synchronous path exercises: ``queue_high_water_mark`` is 1 in normal operation.
It exists so that introducing an async buffer later cannot quietly turn
"cannot keep up" into "drops messages" -- exceeding it marks the collector
unhealthy and stops it. That behaviour is tested directly.

Read-only throughout. No order, balance, position or fill endpoint is reachable
from here.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import Engine

from predarb.books.reconstruction import OrderBookReconstructor
from predarb.clock import Clock
from predarb.ingest.raw_journal import RawFrameJournal
from predarb.replay.decision import DecisionRecord
from predarb.replay.knowledge import KnowledgeBase
from predarb.replay.live import LiveSession
from predarb.replay.observation import Observation, ObservationKind
from predarb.replay.plan import DetectorPlan
from predarb.storage.catalogue import Catalogue, SourcePersistenceError
from predarb.storage.engine import connect
from predarb.venues.kalshi.replay_context import KalshiReplayContext

__all__ = [
    "CollectorHealth",
    "CollectorMetrics",
    "DurableCollector",
    "QueueOverflowError",
    "drain_with_backpressure",
    "resume_ordinal",
]


class QueueOverflowError(RuntimeError):
    """Persistence fell behind the feed. The collector stops rather than drop."""


@dataclass
class CollectorMetrics:
    """Counters and latencies, reported rather than inferred."""

    observations_received: int = 0
    observations_persisted: int = 0
    observations_duplicate: int = 0
    decisions_produced: int = 0
    decisions_persisted: int = 0
    decision_write_failures: int = 0
    triggers: int = 0
    queue_high_water_mark: int = 0
    frames: int = 0
    lifecycle_events: int = 0
    context_observations: int = 0
    persist_latencies_ms: list[float] = field(default_factory=list)
    decision_latencies_ms: list[float] = field(default_factory=list)
    book_latencies_ms: list[float] = field(default_factory=list)

    def percentiles(self, samples: list[float]) -> dict[str, float]:
        if not samples:
            return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0}
        ordered = sorted(samples)

        def at(fraction: float) -> float:
            index = min(len(ordered) - 1, int(fraction * len(ordered)))
            return round(ordered[index], 3)

        return {
            "p50": at(0.50),
            "p95": at(0.95),
            "p99": at(0.99),
            "max": round(ordered[-1], 3),
        }

    def payload(self) -> dict[str, Any]:
        return {
            "observations_received": self.observations_received,
            "observations_persisted": self.observations_persisted,
            "observations_duplicate": self.observations_duplicate,
            "frames": self.frames,
            "lifecycle_events": self.lifecycle_events,
            "context_observations": self.context_observations,
            "triggers": self.triggers,
            "decisions_produced": self.decisions_produced,
            "decisions_persisted": self.decisions_persisted,
            "decision_write_failures": self.decision_write_failures,
            "queue_high_water_mark": self.queue_high_water_mark,
            "latency_ms": {
                "receive_to_persisted": self.percentiles(self.persist_latencies_ms),
                "receive_to_book": self.percentiles(self.book_latencies_ms),
                "receive_to_decision": self.percentiles(self.decision_latencies_ms),
            },
        }


@dataclass
class CollectorHealth:
    """Why the collector is or is not still eligible to scan."""

    healthy: bool = True
    reason: str | None = None

    def fail(self, reason: str) -> None:
        self.healthy = False
        self.reason = reason


@dataclass
class DurableCollector:
    """Wires the production live session to the durable catalogue.

    Owns no economic logic. The detectors it drives are the same pure functions
    replay drives, reached through the same coordinator -- which is what makes
    "live and replay agree" mean something.
    """

    engine: Engine
    plan: DetectorPlan
    clock: Clock
    session_id: str
    journal: RawFrameJournal | None = None
    max_queue_depth: int = 10_000
    metrics: CollectorMetrics = field(default_factory=CollectorMetrics)
    health: CollectorHealth = field(default_factory=CollectorHealth)
    knowledge: KnowledgeBase = field(default_factory=KnowledgeBase)
    session: LiveSession | None = None
    _ordinal: int = 0
    _pending: int = 0

    def __post_init__(self) -> None:
        if self.session is None:
            reconstructor = (
                OrderBookReconstructor(journal=self.journal) if self.journal is not None else None
            )
            self.session = LiveSession.over(
                self.plan,
                self.knowledge,
                KalshiReplayContext(knowledge=self.knowledge, plan=self.plan),
                reconstructor=reconstructor,
            )

    @property
    def reconstructor(self) -> OrderBookReconstructor:
        assert self.session is not None
        return self.session.reconstructor

    def next_observation(
        self,
        kind: ObservationKind,
        payload: dict[str, Any],
        *,
        observed_at: datetime | None = None,
        epoch: int | None = None,
        source: str = "live-collect",
    ) -> Observation:
        observation = Observation(
            ordinal=self._ordinal,
            observed_at=observed_at or self.clock.now(),
            kind=kind,
            payload=payload,
            source=source,
            connection_epoch=epoch,
        )
        self._ordinal += 1
        return observation

    def ingest(
        self,
        observation: Observation,
        *,
        raw_journal_ref: str | None = None,
        received_at: datetime | None = None,
    ) -> tuple[DecisionRecord, ...]:
        """One observation, all the way through. Raises if source storage fails.

        The steps are ordered so that every derived claim has durable evidence
        behind it, and so that a failure at the derived end never destroys the
        evidence that would let it be regenerated.
        """
        if not self.health.healthy:
            raise QueueOverflowError(
                f"collector is unhealthy ({self.health.reason}); refusing to "
                "continue scanning on a feed it cannot durably record"
            )

        started = received_at or observation.observed_at
        self.metrics.observations_received += 1
        self._pending += 1
        self.metrics.queue_high_water_mark = max(self.metrics.queue_high_water_mark, self._pending)
        if self._pending > self.max_queue_depth:
            self.health.fail(
                f"unpersisted backlog {self._pending} exceeded the bound of "
                f"{self.max_queue_depth}; stopping rather than sampling the feed"
            )
            raise QueueOverflowError(self.health.reason or "queue overflow")

        # 1. SOURCE EVIDENCE. Fatal on failure: nothing downstream is checkable
        #    without it, so there is no safe way to continue.
        try:
            with connect(self.engine) as connection:
                inserted = Catalogue(connection).persist_observation(
                    self.session_id, observation, raw_journal_ref=raw_journal_ref
                )
        except Exception as exc:
            self.health.fail(f"source persistence failed: {type(exc).__name__}: {exc}")
            raise SourcePersistenceError(
                f"could not durably record observation {observation.ordinal} "
                f"({observation.kind.value}); the collector must stop rather than "
                "scan on evidence it cannot prove it had"
            ) from exc

        self._pending -= 1
        if inserted:
            self.metrics.observations_persisted += 1
        else:
            self.metrics.observations_duplicate += 1
        self.metrics.persist_latencies_ms.append(self._elapsed_ms(started))

        if observation.kind is ObservationKind.FRAME_RECEIVED:
            self.metrics.frames += 1
        elif observation.kind.is_transport:
            self.metrics.lifecycle_events += 1
        else:
            self.metrics.context_observations += 1

        # 2-3. Authoritative state, then evaluation. Same coordinator as replay.
        assert self.session is not None
        before_triggers = self.session.triggers
        produced = self.session.observe(observation)
        self.metrics.book_latencies_ms.append(self._elapsed_ms(started))
        self.metrics.triggers += self.session.triggers - before_triggers
        self.metrics.decisions_produced += len(produced)

        # 4. DERIVED. Recoverable on failure: replay regenerates it from the
        #    observations already durable above, so collection continues.
        if produced:
            try:
                with connect(self.engine) as connection:
                    catalogue = Catalogue(connection)
                    for record in produced:
                        if catalogue.persist_decision(self.session_id, record):
                            self.metrics.decisions_persisted += 1
            except Exception as exc:
                self.metrics.decision_write_failures += len(produced)
                self._record_health(
                    kind="DECISION_WRITE_FAILED",
                    severity="WARNING",
                    detail=(
                        f"{type(exc).__name__}: {exc}; the source observations are "
                        "durable, so replay can regenerate these decisions"
                    ),
                )
            self.metrics.decision_latencies_ms.append(self._elapsed_ms(started))

        return produced

    def _elapsed_ms(self, started: datetime) -> float:
        return max(0.0, (self.clock.now() - started).total_seconds() * 1000.0)

    def _record_health(self, *, kind: str, severity: str, detail: str) -> None:
        with contextlib.suppress(Exception), connect(self.engine) as connection:
            Catalogue(connection).record_health(
                self.session_id,
                kind=kind,
                severity=severity,
                at=self.clock.now(),
                detail=detail,
                counters=self.metrics.payload(),
            )

    def finish(self, *, status: str = "COMPLETED") -> None:
        with connect(self.engine) as connection:
            catalogue = Catalogue(connection)
            catalogue.record_health(
                self.session_id,
                kind="SESSION_SUMMARY",
                severity="INFO",
                at=self.clock.now(),
                detail=status,
                counters=self.metrics.payload(),
            )
            catalogue.close_session(self.session_id, status=status, at=self.clock.now())


def resume_ordinal(engine: Engine, session_id: str) -> int:
    """Where a restart should continue numbering.

    Read from durable storage rather than memory, which is the point: after a
    crash the in-process counter is gone, and reusing an ordinal would make
    "what was known at #100" ambiguous.
    """
    with connect(engine) as connection:
        highest = Catalogue(connection).max_observation_ordinal(session_id)
    return 0 if highest is None else int(highest) + 1


async def drain_with_backpressure(
    queue: asyncio.Queue[Observation],
    handle: Callable[[Observation], Any],
    *,
    stop: asyncio.Event,
) -> None:
    """Consume a bounded queue until stopped. Exists to be tested directly."""
    while not stop.is_set() or not queue.empty():
        try:
            observation = await asyncio.wait_for(queue.get(), timeout=0.25)
        except TimeoutError:
            continue
        try:
            handle(observation)
        finally:
            queue.task_done()
