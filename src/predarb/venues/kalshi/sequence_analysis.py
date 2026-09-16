"""Analysis of observed WebSocket sequence numbers.

Exists to answer open question A-09 with evidence rather than assumption: what
is ``seq`` scoped to, and is it *dense*?

The density question matters more than the scope question. A reconstructor's
natural gap test is ``seq != previous + 1``, but that is only a gap detector if
every integer is used. If ``seq`` is merely monotonic -- increasing but with
holes -- the same test fires constantly on healthy streams, and a system that
cries wolf on every book is worse than one that does not check at all.

So this module measures, per candidate scope:

* adjacent pairs, and how many advance by exactly one (density evidence)
* positive skips, duplicates, and decreases (integrity evidence)

The scope whose grouping yields a coherent, dense, strictly-increasing sequence
is the scope ``seq`` is actually keyed on. This module computes; it does not
conclude. Turning these numbers into an invariant is a deliberate written act,
recorded in ``docs/api_assumptions.md``.

Pure and I/O-free, so it is testable against both synthetic and recorded frames.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from itertools import pairwise

__all__ = [
    "ScopeAnalysis",
    "SequenceRecord",
    "SequenceScope",
    "StreamStats",
    "analyse_all_scopes",
    "analyse_scope",
    "rank_scopes",
    "records_from_observations",
]


class SequenceScope(StrEnum):
    """A candidate keying for ``seq``. Each is tested against the evidence."""

    CONNECTION = "CONNECTION"
    """One counter for the whole socket, shared by every subscription."""

    SID = "SID"
    """One counter per subscription."""

    SID_MARKET = "SID_MARKET"
    """One counter per market within a subscription."""

    MARKET = "MARKET"
    """One counter per market, shared across subscriptions."""


@dataclass(frozen=True, slots=True)
class SequenceRecord:
    """One observed frame, reduced to what sequence analysis needs.

    ``session`` distinguishes connections so reconnect evidence (experiment D)
    is not silently merged with the pre-disconnect stream.
    """

    session: int
    arrival_index: int
    sid: int | None
    seq: int | None
    market_ticker: str
    message_type: str

    def key_for(self, scope: SequenceScope) -> tuple[object, ...] | None:
        """The grouping key under ``scope``, or ``None`` if not applicable.

        A frame missing the field a scope keys on cannot be placed in that
        scope's stream and is excluded rather than bucketed under a fabricated
        default.
        """
        if scope is SequenceScope.CONNECTION:
            return (self.session,)
        if scope is SequenceScope.SID:
            return None if self.sid is None else (self.session, self.sid)
        if scope is SequenceScope.SID_MARKET:
            if self.sid is None or not self.market_ticker:
                return None
            return (self.session, self.sid, self.market_ticker)
        if not self.market_ticker:
            return None
        return (self.session, self.market_ticker)


@dataclass(slots=True)
class StreamStats:
    """Descriptive statistics for one stream under one scope."""

    key: tuple[object, ...]
    values: list[int] = field(default_factory=list)
    message_types: dict[str, int] = field(default_factory=dict)

    @property
    def count(self) -> int:
        return len(self.values)

    @property
    def adjacent_pairs(self) -> int:
        return max(0, len(self.values) - 1)

    @property
    def increments_of_one(self) -> int:
        """Pairs advancing by exactly one. The core density signal."""
        return sum(1 for a, b in pairwise(self.values) if b == a + 1)

    @property
    def positive_skips(self) -> int:
        """Pairs advancing by more than one."""
        return sum(1 for a, b in pairwise(self.values) if b > a + 1)

    @property
    def duplicates(self) -> int:
        return sum(1 for a, b in pairwise(self.values) if b == a)

    @property
    def decreases(self) -> int:
        """Pairs going backwards -- out-of-order arrival or a reset."""
        return sum(1 for a, b in pairwise(self.values) if b < a)

    @property
    def first(self) -> int | None:
        return self.values[0] if self.values else None

    @property
    def last(self) -> int | None:
        return self.values[-1] if self.values else None

    @property
    def density(self) -> float | None:
        """Fraction of adjacent pairs that advance by exactly one.

        ``1.0`` over many pairs is strong evidence the counter is dense within
        this scope. ``None`` when there are too few pairs to say anything.
        """
        if self.adjacent_pairs == 0:
            return None
        return self.increments_of_one / self.adjacent_pairs

    @property
    def is_strictly_increasing(self) -> bool:
        return all(b > a for a, b in pairwise(self.values))

    @property
    def is_perfectly_dense(self) -> bool:
        """Every adjacent pair advances by exactly one, over at least one pair."""
        return self.adjacent_pairs > 0 and self.increments_of_one == self.adjacent_pairs

    def summary(self) -> dict[str, object]:
        return {
            "key": list(self.key),
            "count": self.count,
            "first": self.first,
            "last": self.last,
            "adjacent_pairs": self.adjacent_pairs,
            "increments_of_one": self.increments_of_one,
            "positive_skips": self.positive_skips,
            "duplicates": self.duplicates,
            "decreases": self.decreases,
            "density": self.density,
            "strictly_increasing": self.is_strictly_increasing,
            "perfectly_dense": self.is_perfectly_dense,
            "message_types": dict(self.message_types),
        }


@dataclass(slots=True)
class ScopeAnalysis:
    """Aggregate evidence for one candidate scope."""

    scope: SequenceScope
    streams: dict[tuple[object, ...], StreamStats] = field(default_factory=dict)
    excluded_frames: int = 0
    """Frames lacking the field this scope keys on."""

    @property
    def total_pairs(self) -> int:
        return sum(s.adjacent_pairs for s in self.streams.values())

    @property
    def total_increments_of_one(self) -> int:
        return sum(s.increments_of_one for s in self.streams.values())

    @property
    def total_skips(self) -> int:
        return sum(s.positive_skips for s in self.streams.values())

    @property
    def total_duplicates(self) -> int:
        return sum(s.duplicates for s in self.streams.values())

    @property
    def total_decreases(self) -> int:
        return sum(s.decreases for s in self.streams.values())

    @property
    def overall_density(self) -> float | None:
        if self.total_pairs == 0:
            return None
        return self.total_increments_of_one / self.total_pairs

    @property
    def all_streams_dense(self) -> bool:
        streams = [s for s in self.streams.values() if s.adjacent_pairs > 0]
        return bool(streams) and all(s.is_perfectly_dense for s in streams)

    def summary(self) -> dict[str, object]:
        return {
            "scope": self.scope.value,
            "streams": len(self.streams),
            "excluded_frames": self.excluded_frames,
            "total_pairs": self.total_pairs,
            "increments_of_one": self.total_increments_of_one,
            "positive_skips": self.total_skips,
            "duplicates": self.total_duplicates,
            "decreases": self.total_decreases,
            "overall_density": self.overall_density,
            "all_streams_perfectly_dense": self.all_streams_dense,
            "per_stream": [s.summary() for s in self.streams.values()],
        }


def analyse_scope(records: Iterable[SequenceRecord], scope: SequenceScope) -> ScopeAnalysis:
    """Group records under ``scope`` and compute density/integrity statistics.

    Records are consumed in arrival order, which is what makes ``decreases``
    meaningful: it counts frames that arrived out of order, not merely values
    that are unordered.
    """
    analysis = ScopeAnalysis(scope=scope)
    for record in sorted(records, key=lambda r: (r.session, r.arrival_index)):
        if record.seq is None:
            analysis.excluded_frames += 1
            continue
        key = record.key_for(scope)
        if key is None:
            analysis.excluded_frames += 1
            continue
        stream = analysis.streams.setdefault(key, StreamStats(key=key))
        stream.values.append(record.seq)
        stream.message_types[record.message_type] = (
            stream.message_types.get(record.message_type, 0) + 1
        )
    return analysis


def analyse_all_scopes(
    records: Iterable[SequenceRecord],
) -> dict[SequenceScope, ScopeAnalysis]:
    materialised = list(records)
    return {scope: analyse_scope(materialised, scope) for scope in SequenceScope}


def rank_scopes(analyses: dict[SequenceScope, ScopeAnalysis]) -> list[tuple[SequenceScope, float]]:
    """Order scopes by how coherent their grouping looks, best first.

    The score is simply observed density, and a scope with no adjacent pairs
    scores ``-1`` rather than a flattering default. This ranks evidence; it does
    not decide anything. A scope can top this list and still be the wrong answer
    if the sample is small, which is why the harness reports the underlying
    counts alongside it.
    """
    scored: list[tuple[SequenceScope, float]] = []
    for scope, analysis in analyses.items():
        density = analysis.overall_density
        scored.append((scope, -1.0 if density is None else density))
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored


def records_from_observations(
    observations: Sequence[tuple[int, int, int | None, int | None, str, str]],
) -> list[SequenceRecord]:
    """Build records from raw tuples, for tests and for replaying saved runs."""
    return [
        SequenceRecord(
            session=session,
            arrival_index=index,
            sid=sid,
            seq=seq,
            market_ticker=ticker,
            message_type=kind,
        )
        for session, index, sid, seq, ticker, kind in observations
    ]
