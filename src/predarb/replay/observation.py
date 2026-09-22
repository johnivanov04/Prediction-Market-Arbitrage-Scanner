"""Bitemporal observations: what we learned, and when we learned it.

Two temporal dimensions, never conflated
----------------------------------------
**Valid time** is when the venue says something takes effect: a fee change's
``scheduled_ts``, a market's open time, a certificate's ``valid_from``.

**Knowledge time** is when *we* learned it: the moment a payload was fetched, a
frame arrived, evidence was captured, a certificate was issued.

A decision replayed at simulated time ``T`` may use a fact only if we had
observed it by ``T``, **and** its valid time permits use at ``T``. Checking
valid time alone is the classic backtest leak:

    download historical fee changes today
    -> replay last month
    -> silently price with a schedule we did not possess then

The effective timestamp says "Wednesday", so a valid-time-only filter happily
admits it into a Tuesday replay. Knowledge time is what rejects it.

Ordinals, not just timestamps
-----------------------------
Every observation carries a monotonically increasing **capture ordinal**
assigned locally at record time. Two observations can share a microsecond;
ordering between them is still defined, and a detector triggered by ordinal 100
must not see ordinal 101 merely because their clocks round the same.

Knowledge cut-offs are therefore expressed as ordinals wherever a decision is
triggered by an observation, and as instants only where a caller genuinely has
nothing but a time.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Self

from predarb.clock import ensure_utc

__all__ = [
    "KnowledgeHorizon",
    "Observation",
    "ObservationKind",
    "ObservationStream",
    "ObservedVersion",
    "VersionHistory",
    "assign_ordinals",
]


class ObservationKind(StrEnum):
    """Everything a replay must be able to re-experience in order.

    Book frames alone are not enough: a disconnect that live reconstruction saw
    -- and that invalidated every book -- leaves no trace in the application
    frame stream. Replaying frames without lifecycle would keep a book alive
    through an outage the live system correctly treated as fatal.
    """

    CONNECTION_OPENED = "CONNECTION_OPENED"
    CONNECTION_CLOSED = "CONNECTION_CLOSED"
    CONNECTION_FAILED = "CONNECTION_FAILED"
    SUBSCRIBED = "SUBSCRIBED"
    FRAME_RECEIVED = "FRAME_RECEIVED"

    MARKET_METADATA = "MARKET_METADATA"
    EVENT_METADATA = "EVENT_METADATA"
    SERIES_METADATA = "SERIES_METADATA"
    FEE_OBSERVATION = "FEE_OBSERVATION"

    SETTLEMENT_EVIDENCE = "SETTLEMENT_EVIDENCE"
    SETTLEMENT_CERTIFICATE = "SETTLEMENT_CERTIFICATE"
    RELATION_EVIDENCE = "RELATION_EVIDENCE"
    RELATION_CERTIFICATE = "RELATION_CERTIFICATE"

    # -- explicit knowledge snapshots -------------------------------------
    #
    # "We queried this authoritative source at this ordinal and here is what it
    # held -- possibly nothing." Without one of these, an absent certificate is
    # indistinguishable from a certificate nobody looked for, and a replay could
    # report a confident negative it has not earned.
    SETTLEMENT_REGISTRY_SNAPSHOT = "SETTLEMENT_REGISTRY_SNAPSHOT"
    RELATION_REGISTRY_SNAPSHOT = "RELATION_REGISTRY_SNAPSHOT"
    FEE_KNOWLEDGE_SNAPSHOT = "FEE_KNOWLEDGE_SNAPSHOT"
    METADATA_SNAPSHOT = "METADATA_SNAPSHOT"
    DETECTOR_PLAN = "DETECTOR_PLAN"

    @property
    def is_knowledge_snapshot(self) -> bool:
        """Whether this observation records that a source was *checked*."""
        return self in {
            ObservationKind.SETTLEMENT_REGISTRY_SNAPSHOT,
            ObservationKind.RELATION_REGISTRY_SNAPSHOT,
            ObservationKind.FEE_KNOWLEDGE_SNAPSHOT,
            ObservationKind.METADATA_SNAPSHOT,
        }

    @property
    def is_transport(self) -> bool:
        return self in {
            ObservationKind.CONNECTION_OPENED,
            ObservationKind.CONNECTION_CLOSED,
            ObservationKind.CONNECTION_FAILED,
            ObservationKind.SUBSCRIBED,
            ObservationKind.FRAME_RECEIVED,
        }


@dataclass(frozen=True, slots=True)
class Observation:
    """One thing the system learned, at one moment, in a definite order.

    ``ordinal`` is assigned at capture and is the authority on ordering.
    ``observed_at`` is for reporting and for coarse time-based cut-offs; it is
    never used to re-sort, because sorting by timestamp cannot recreate
    sub-millisecond live ordering and pretending otherwise would silently
    reorder a frame stream.
    """

    ordinal: int
    observed_at: datetime
    kind: ObservationKind
    payload: dict[str, Any]
    source: str = ""
    connection_epoch: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "observed_at", ensure_utc(self.observed_at))
        if self.ordinal < 0:
            raise ValueError(f"capture ordinal must not be negative, got {self.ordinal}")

    @property
    def content_hash(self) -> str:
        body = json.dumps(
            {
                "ordinal": self.ordinal,
                "observed_at": self.observed_at.isoformat(),
                "kind": self.kind.value,
                "payload": self.payload,
                "source": self.source,
                "connection_epoch": self.connection_epoch,
            },
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(body.encode()).hexdigest()

    def to_json_line(self) -> str:
        return json.dumps(
            {
                "ordinal": self.ordinal,
                "observed_at": self.observed_at.isoformat(),
                "kind": self.kind.value,
                "payload": self.payload,
                "source": self.source,
                "connection_epoch": self.connection_epoch,
            },
            sort_keys=True,
            default=str,
        )

    @classmethod
    def from_json_line(cls, line: str) -> Self:
        body = json.loads(line)
        return cls(
            ordinal=body["ordinal"],
            observed_at=datetime.fromisoformat(body["observed_at"]),
            kind=ObservationKind(body["kind"]),
            payload=body["payload"],
            source=body.get("source", ""),
            connection_epoch=body.get("connection_epoch"),
        )

    def describe(self) -> str:
        return f"#{self.ordinal} {self.observed_at.isoformat()} {self.kind.value}"


@dataclass(frozen=True, slots=True)
class KnowledgeHorizon:
    """How much of the observation stream a decision is allowed to see.

    Expressed as an ordinal because that is what makes same-timestamp ordering
    decidable. ``at`` travels with it for valid-time reasoning and for the
    simulated clock.
    """

    ordinal: int
    at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "at", ensure_utc(self.at))

    def admits(self, observation: Observation) -> bool:
        """Whether this observation was already known at the horizon."""
        return observation.ordinal <= self.ordinal

    def describe(self) -> str:
        return f"known through #{self.ordinal} at {self.at.isoformat()}"


@dataclass(frozen=True, slots=True)
class ObservedVersion[T]:
    """One version of a fact, tagged with when we learned it and when it applies."""

    value: T
    observed_at: datetime
    observed_ordinal: int
    effective_from: datetime | None = None
    effective_until: datetime | None = None
    source: str = ""
    version_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "observed_at", ensure_utc(self.observed_at))
        for name in ("effective_from", "effective_until"):
            current = getattr(self, name)
            if current is not None:
                object.__setattr__(self, name, ensure_utc(current))
        if (
            self.effective_from is not None
            and self.effective_until is not None
            and self.effective_until < self.effective_from
        ):
            raise ValueError("effective_until precedes effective_from")

    def known_by(self, horizon: KnowledgeHorizon) -> bool:
        return self.observed_ordinal <= horizon.ordinal

    def effective_at(self, instant: datetime) -> bool:
        moment = ensure_utc(instant)
        if self.effective_from is not None and moment < self.effective_from:
            return False
        return not (self.effective_until is not None and moment > self.effective_until)

    def describe(self) -> str:
        window = ""
        if self.effective_from is not None:
            window = f" effective {self.effective_from.isoformat()}"
        return f"{self.version_id or '?'} observed #{self.observed_ordinal}{window}"


@dataclass(frozen=True, slots=True)
class VersionHistory[T]:
    """Every version of one fact we ever observed, resolvable bitemporally.

    Resolution is strictly two-phase, and the order matters:

    1. **discard** everything observed after the horizon -- we did not have it;
    2. **then** pick, from what remains, the version effective at the instant.

    Doing it the other way round would let a record we had not yet seen win the
    effective-time comparison and then be filtered too late, or -- worse -- let
    an effective-time filter mask the fact that the only applicable record was
    one we learned about afterwards.
    """

    key: str
    versions: tuple[ObservedVersion[T], ...] = ()

    def with_version(self, version: ObservedVersion[T]) -> VersionHistory[T]:
        return VersionHistory(key=self.key, versions=(*self.versions, version))

    def known_at(self, horizon: KnowledgeHorizon) -> tuple[ObservedVersion[T], ...]:
        """Phase one: what we had actually observed by the horizon."""
        return tuple(v for v in self.versions if v.known_by(horizon))

    def resolve(self, horizon: KnowledgeHorizon) -> ObservedVersion[T] | None:
        """Both phases. ``None`` means we knew nothing applicable then.

        ``None`` is a real answer and must not be papered over with a later
        observation: it says the replay has no point-in-time knowledge here.
        """
        candidates = [v for v in self.known_at(horizon) if v.effective_at(horizon.at)]
        if not candidates:
            return None
        # Latest effective version wins; ties break on observation order so the
        # result cannot depend on dict or file iteration order.
        return max(
            candidates,
            key=lambda v: (
                v.effective_from or datetime.min.replace(tzinfo=v.observed_at.tzinfo),
                v.observed_ordinal,
            ),
        )

    def scheduled_but_not_yet_effective(
        self, horizon: KnowledgeHorizon
    ) -> tuple[ObservedVersion[T], ...]:
        """Versions we know about whose effective time is still in the future.

        Legitimately knowable -- a Monday observation of a Wednesday change is
        real knowledge on Tuesday -- but it must not be *applied* on Tuesday.
        Exposed separately so a caller can report it without using it.
        """
        return tuple(
            v
            for v in self.known_at(horizon)
            if v.effective_from is not None and v.effective_from > horizon.at
        )

    def describe(self) -> str:
        return f"{self.key}: {len(self.versions)} version(s)"


@dataclass(frozen=True, slots=True)
class ObservationStream:
    """An ordered, immutable observation log.

    Ordering is by capture ordinal and nothing else. The stream refuses to be
    constructed out of order or with duplicate ordinals, because either would
    make "what was known at #100" ambiguous.
    """

    observations: tuple[Observation, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        ordinals = [o.ordinal for o in self.observations]
        if ordinals != sorted(ordinals):
            raise ValueError(
                "observations must be supplied in capture order; re-sorting by "
                "timestamp cannot recreate sub-millisecond live ordering"
            )
        if len(set(ordinals)) != len(ordinals):
            duplicates = sorted({o for o in ordinals if ordinals.count(o) > 1})
            raise ValueError(f"duplicate capture ordinals: {duplicates}")

    def __iter__(self) -> Iterator[Observation]:
        return iter(self.observations)

    def __len__(self) -> int:
        return len(self.observations)

    @classmethod
    def from_iterable(cls, observations: Iterable[Observation]) -> Self:
        return cls(observations=tuple(observations))

    def up_to(self, horizon: KnowledgeHorizon) -> ObservationStream:
        return ObservationStream(
            observations=tuple(o for o in self.observations if horizon.admits(o))
        )

    def of_kind(self, *kinds: ObservationKind) -> tuple[Observation, ...]:
        wanted = set(kinds)
        return tuple(o for o in self.observations if o.kind in wanted)

    def horizon_at(self, ordinal: int) -> KnowledgeHorizon:
        """The horizon just after a given observation."""
        matching = [o for o in self.observations if o.ordinal == ordinal]
        if not matching:
            raise KeyError(f"no observation with ordinal {ordinal}")
        return KnowledgeHorizon(ordinal=ordinal, at=matching[0].observed_at)

    @property
    def span(self) -> tuple[datetime, datetime] | None:
        if not self.observations:
            return None
        return self.observations[0].observed_at, self.observations[-1].observed_at

    @property
    def connection_epochs(self) -> tuple[int, ...]:
        return tuple(sorted({o.connection_epoch for o in self.observations if o.connection_epoch}))

    def content_hash(self) -> str:
        digest = hashlib.sha256()
        for observation in self.observations:
            digest.update(observation.content_hash.encode())
        return digest.hexdigest()

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for observation in self.observations:
            counts[observation.kind.value] = counts.get(observation.kind.value, 0) + 1
        return dict(sorted(counts.items()))


def assign_ordinals(
    records: Sequence[tuple[datetime, ObservationKind, dict[str, Any], str, int | None]],
) -> ObservationStream:
    """Build a stream from raw tuples, numbering them in the order given.

    The caller's order is the capture order. Nothing here sorts.
    """
    return ObservationStream.from_iterable(
        Observation(
            ordinal=index,
            observed_at=observed_at,
            kind=kind,
            payload=payload,
            source=source,
            connection_epoch=epoch,
        )
        for index, (observed_at, kind, payload, source, epoch) in enumerate(records)
    )
