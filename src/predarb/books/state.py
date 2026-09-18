"""Book integrity state and provenance.

Structural integrity and freshness are kept apart on purpose.

**Integrity** asks: can we prove this book is what the venue sent? It is broken
by a sequence violation, a disconnect, or an impossible mutation.

**Freshness** asks: how recently did anything happen? A resting quote can sit
unchanged for hours and still be perfectly valid, so "no recent update" is not
evidence of a problem. Conflating the two produces both failure modes at once:
quiet markets get wrongly excluded, and genuinely broken books stay eligible
because they happen to be receiving traffic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

__all__ = [
    "BookIntegrity",
    "BookProvenance",
    "InvalidationReason",
]


class BookIntegrity(StrEnum):
    """Whether a book's contents can be proven.

    Only :attr:`VALID` may ever be scanned. Every other state means we cannot
    show the book matches the venue, and a book we cannot prove is worse than
    no book at all -- it looks tradeable.
    """

    WAITING_SNAPSHOT = "WAITING_SNAPSHOT"
    """Subscribed, but no authoritative snapshot yet in this epoch.

    Deltas arriving now cannot be applied: there is no base state to apply them
    to, and applying them to an empty book would fabricate one."""

    VALID = "VALID"
    """A snapshot established this book in the current epoch, and every frame
    since has passed sequence and structural checks."""

    INTEGRITY_UNKNOWN = "INTEGRITY_UNKNOWN"
    """Something happened that we cannot reason past: a sequence violation on
    the owning sid, a disconnect, or a mutation that would have produced an
    impossible state. Recoverable only by a fresh snapshot."""

    UNSUBSCRIBED = "UNSUBSCRIBED"
    """No longer subscribed, so no further updates will arrive. The contents may
    be the last thing the venue sent, but nothing keeps them current."""

    @property
    def is_scannable(self) -> bool:
        return self is BookIntegrity.VALID


class InvalidationReason(StrEnum):
    """Why a book left :attr:`BookIntegrity.VALID`.

    Recorded per book so an audit can answer "why was this not eligible?"
    without reconstructing the timeline from logs.
    """

    SEQUENCE_VIOLATION = "SEQUENCE_VIOLATION"
    """A gap, repeat or decrease on the owning sid. Note this invalidates every
    book on that sid, not just one, because the counter is shared."""

    CONNECTION_LOST = "CONNECTION_LOST"
    DELTA_BEFORE_SNAPSHOT = "DELTA_BEFORE_SNAPSHOT"
    NEGATIVE_QUANTITY = "NEGATIVE_QUANTITY"
    """A delta would have taken a level below zero. Never clamped: the book
    state is demonstrably wrong, and clamping hides it."""

    MALFORMED_PAYLOAD = "MALFORMED_PAYLOAD"
    UNKNOWN_SEQUENCED_FRAME = "UNKNOWN_SEQUENCED_FRAME"
    """A seq-bearing frame type we cannot prove is irrelevant to book state.
    Advancing past it would mean assuming something we have not verified."""

    DUPLICATE_PRICE_LEVEL = "DUPLICATE_PRICE_LEVEL"
    UNSUBSCRIBED = "UNSUBSCRIBED"
    JOURNAL_FAILURE = "JOURNAL_FAILURE"
    """The raw journal could not record a frame. Without the journal an
    opportunity cannot be audited later, so the book stops being eligible."""


@dataclass(frozen=True, slots=True)
class BookProvenance:
    """Compact answer to "where did this book come from?".

    Deliberately *not* an unbounded list of every frame that touched the book:
    a busy market would grow that without limit inside a live object. The raw
    journal holds the full chain; this holds the coordinates needed to find it.
    """

    connection_epoch: int
    sid: int
    snapshot_raw_id: str | None = None
    """Raw-journal id of the snapshot that established this book."""

    snapshot_seq: int | None = None
    snapshot_received_at: datetime | None = None
    latest_raw_id: str | None = None
    """Raw-journal id of the most recent frame applied."""

    latest_seq: int | None = None
    latest_received_at: datetime | None = None
    applied_frames: int = 0
    """How many frames mutated this book since the snapshot."""

    def describe(self) -> str:
        return (
            f"epoch={self.connection_epoch} sid={self.sid} "
            f"snapshot(seq={self.snapshot_seq}, id={self.snapshot_raw_id}) "
            f"latest(seq={self.latest_seq}, id={self.latest_raw_id}) "
            f"applied={self.applied_frames}"
        )
