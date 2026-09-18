"""Structured events emitted during reconstruction.

These exist so that a later audit can answer "why was this opportunity eligible,
or not?" without reconstructing the answer from prose log lines. Each event is a
value with the identifiers needed to join it to the raw journal.

Emitted, not logged: the reconstructor hands events to a sink, and the sink
decides whether they become log lines, metrics, or rows. That keeps the
reconstruction path free of formatting decisions and makes the event stream
testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from predarb.books.sequence import SequenceViolation
from predarb.books.state import InvalidationReason

__all__ = ["EventKind", "ReconstructionEvent"]


class EventKind(StrEnum):
    """What happened. Named for the question an auditor would ask."""

    CONNECTION_OPENED = "CONNECTION_OPENED"
    CONNECTION_CLOSED = "CONNECTION_CLOSED"
    SUBSCRIBED = "SUBSCRIBED"
    SUBSCRIPTION_UPDATED = "SUBSCRIPTION_UPDATED"
    SNAPSHOT_APPLIED = "SNAPSHOT_APPLIED"
    DELTA_APPLIED = "DELTA_APPLIED"
    CONTROL_SEQ_CONSUMED = "CONTROL_SEQ_CONSUMED"
    """A seq-bearing frame advanced the counter without mutating a book."""

    SEQUENCE_VIOLATION = "SEQUENCE_VIOLATION"
    BOOK_INVARIANT_VIOLATION = "BOOK_INVARIANT_VIOLATION"
    BOOKS_INVALIDATED = "BOOKS_INVALIDATED"
    RECONNECT_REQUESTED = "RECONNECT_REQUESTED"
    RECONNECT_COMPLETED = "RECONNECT_COMPLETED"
    RESNAPSHOT_COMPLETED = "RESNAPSHOT_COMPLETED"
    UNKNOWN_SEQUENCED_FRAME = "UNKNOWN_SEQUENCED_FRAME"
    JOURNAL_FAILURE = "JOURNAL_FAILURE"
    FRAME_UNPARSED = "FRAME_UNPARSED"


@dataclass(frozen=True, slots=True)
class ReconstructionEvent:
    """One thing that happened, with the coordinates to find it again."""

    kind: EventKind
    at: datetime
    connection_epoch: int | None = None
    sid: int | None = None
    market_ticker: str | None = None
    seq: int | None = None
    expected_seq: int | None = None
    raw_id: str | None = None
    violation: SequenceViolation | None = None
    reason: InvalidationReason | None = None
    affected_markets: tuple[str, ...] = ()
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def is_failure(self) -> bool:
        return self.kind in {
            EventKind.SEQUENCE_VIOLATION,
            EventKind.BOOK_INVARIANT_VIOLATION,
            EventKind.UNKNOWN_SEQUENCED_FRAME,
            EventKind.JOURNAL_FAILURE,
        }

    def log_fields(self) -> dict[str, Any]:
        """Flat fields for structured logging, omitting empty values.

        Carries no payload and no headers -- only identifiers -- so an event is
        always safe to log.
        """
        fields: dict[str, Any] = {"event": self.kind.value}
        for name, value in (
            ("connection_epoch", self.connection_epoch),
            ("sid", self.sid),
            ("market_ticker", self.market_ticker),
            ("seq", self.seq),
            ("expected_seq", self.expected_seq),
            ("raw_id", self.raw_id),
            ("violation", self.violation.value if self.violation else None),
            ("reason", self.reason.value if self.reason else None),
        ):
            if value is not None:
                fields[name] = value
        if self.affected_markets:
            fields["affected_market_count"] = len(self.affected_markets)
        fields.update(self.detail)
        return fields
