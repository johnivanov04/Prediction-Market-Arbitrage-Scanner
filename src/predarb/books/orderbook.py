"""Live order-book state: a mutable builder and an immutable consumer view.

The split is the point of this module. Reconstruction needs to mutate a book
thousands of times a minute; consumers need a value they can reason about
without it changing underneath them. So the mutable structure never escapes:
:meth:`MutableOrderBook.view` produces an immutable :class:`BookView`, and the
internal dictionaries are not reachable from it.

What this module stores is **exactly what Kalshi sends**: YES bids and NO bids.
No derived asks. Kalshi publishes bids only, so an ask is an arithmetic
consequence of the opposing bid against the contract's notional -- a derivation,
not venue truth. Mixing the two here would make it impossible to tell later
which is which, and consuming a derived ask double-counts the resting interest
it came from. Derived asks belong to the execution view, built later.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from predarb.books.levels import BookLevel, BookSource, BookTransport, QuotedBook
from predarb.books.state import BookIntegrity, BookProvenance, InvalidationReason
from predarb.domain.enums import MarketSide
from predarb.domain.money import Price, Quantity, QuantityDelta

__all__ = ["BookMutationError", "BookView", "MutableOrderBook", "book_source_for"]


class BookMutationError(Exception):
    """A mutation would have produced a state the venue could not have sent.

    Carries the reason so the caller can record *why* a book was invalidated
    rather than just that it was.
    """

    def __init__(self, message: str, *, reason: InvalidationReason) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class BookView:
    """An immutable point-in-time view of one market's book.

    Handed to consumers instead of the live structure. Levels are ordered
    best-first on each side, which for a bid book means descending by price.
    """

    market_ticker: str
    integrity: BookIntegrity
    yes_bids: tuple[BookLevel, ...]
    no_bids: tuple[BookLevel, ...]
    provenance: BookProvenance
    invalidation_reason: InvalidationReason | None = None
    last_change_at: datetime | None = None
    """When this market's book last actually changed.

    Distinct from sid activity and from connection liveness: a resting quote can
    legitimately sit unchanged, so an old value here is not a fault."""

    @property
    def is_structurally_valid(self) -> bool:
        """Whether the contents are provably what the venue sent.

        Says nothing about freshness or connection health -- those are separate
        questions, answered by the reconstructor.
        """
        return self.integrity.is_scannable

    @property
    def best_yes_bid(self) -> BookLevel | None:
        return self.yes_bids[0] if self.yes_bids else None

    @property
    def best_no_bid(self) -> BookLevel | None:
        return self.no_bids[0] if self.no_bids else None

    @property
    def is_empty(self) -> bool:
        return not self.yes_bids and not self.no_bids

    def total_quantity(self, side: MarketSide) -> Quantity:
        levels = self.yes_bids if side is MarketSide.YES else self.no_bids
        return Quantity.from_units(sum(level.quantity.units for level in levels))

    def as_quoted_book(self, source: BookSource) -> QuotedBook:
        """Render as a :class:`QuotedBook` for code that takes the Step 2 type."""
        return QuotedBook(source=source, yes_bids=self.yes_bids, no_bids=self.no_bids)


class MutableOrderBook:
    """The live, mutating book for one market.

    Not thread-safe and not meant to be: it is driven from the single
    authoritative receive path, in arrival order. Concurrency here would mean
    frames applied out of order, which is exactly the thing sequence checking
    exists to prevent.
    """

    __slots__ = (
        "_applied_frames",
        "_integrity",
        "_invalidation_reason",
        "_last_change_at",
        "_latest_raw_id",
        "_latest_received_at",
        "_latest_seq",
        "_no_bids",
        "_snapshot_raw_id",
        "_snapshot_received_at",
        "_snapshot_seq",
        "_yes_bids",
        "connection_epoch",
        "market_ticker",
        "sid",
    )

    def __init__(self, market_ticker: str, *, connection_epoch: int, sid: int) -> None:
        self.market_ticker = market_ticker
        self.connection_epoch = connection_epoch
        self.sid = sid
        # price units -> quantity units, one map per side.
        self._yes_bids: dict[int, int] = {}
        self._no_bids: dict[int, int] = {}
        self._integrity = BookIntegrity.WAITING_SNAPSHOT
        self._invalidation_reason: InvalidationReason | None = None
        self._snapshot_raw_id: str | None = None
        self._snapshot_seq: int | None = None
        self._snapshot_received_at: datetime | None = None
        self._latest_raw_id: str | None = None
        self._latest_seq: int | None = None
        self._latest_received_at: datetime | None = None
        self._applied_frames = 0
        self._last_change_at: datetime | None = None

    # -- state ---------------------------------------------------------------

    @property
    def integrity(self) -> BookIntegrity:
        return self._integrity

    @property
    def invalidation_reason(self) -> InvalidationReason | None:
        return self._invalidation_reason

    @property
    def last_change_at(self) -> datetime | None:
        return self._last_change_at

    @property
    def is_structurally_valid(self) -> bool:
        return self._integrity.is_scannable

    def invalidate(self, reason: InvalidationReason) -> None:
        """Move to ``INTEGRITY_UNKNOWN``. Contents are kept for diagnosis only.

        The book is not cleared: the last known levels are useful when
        investigating what went wrong. They are unreachable to a scanner
        because integrity gates that, not emptiness.
        """
        self._integrity = BookIntegrity.INTEGRITY_UNKNOWN
        self._invalidation_reason = reason

    def mark_unsubscribed(self) -> None:
        self._integrity = BookIntegrity.UNSUBSCRIBED
        self._invalidation_reason = InvalidationReason.UNSUBSCRIBED

    def reset_for_new_epoch(self, *, connection_epoch: int, sid: int) -> None:
        """Prepare for a fresh snapshot after reconnect or recovery.

        Contents are dropped, not carried over. A book from a previous
        connection has no sequence authority in the new one, and keeping its
        levels around invites exactly the mistake this class exists to prevent.
        """
        self.connection_epoch = connection_epoch
        self.sid = sid
        self._yes_bids.clear()
        self._no_bids.clear()
        self._integrity = BookIntegrity.WAITING_SNAPSHOT
        self._invalidation_reason = None
        self._snapshot_raw_id = None
        self._snapshot_seq = None
        self._snapshot_received_at = None
        self._latest_raw_id = None
        self._latest_seq = None
        self._latest_received_at = None
        self._applied_frames = 0

    # -- mutation ------------------------------------------------------------

    def apply_snapshot(
        self,
        *,
        yes_levels: tuple[tuple[Price, Quantity], ...],
        no_levels: tuple[tuple[Price, Quantity], ...],
        seq: int | None,
        raw_id: str,
        received_at: datetime,
    ) -> None:
        """Replace the book wholesale with authoritative state.

        A snapshot is replacement, never a merge. Merging would let a level the
        venue has dropped survive indefinitely, and the whole value of a
        snapshot is that it settles what the book *is*.

        Validation happens before anything is mutated, so a malformed snapshot
        leaves the previous state untouched rather than half-applied.
        """
        fresh_yes = self._build_side(yes_levels, MarketSide.YES)
        fresh_no = self._build_side(no_levels, MarketSide.NO)

        # Only now, with both sides validated, does the book change.
        self._yes_bids = fresh_yes
        self._no_bids = fresh_no
        self._integrity = BookIntegrity.VALID
        self._invalidation_reason = None
        self._snapshot_raw_id = raw_id
        self._snapshot_seq = seq
        self._snapshot_received_at = received_at
        self._latest_raw_id = raw_id
        self._latest_seq = seq
        self._latest_received_at = received_at
        self._applied_frames = 0
        self._last_change_at = received_at

    @staticmethod
    def _build_side(levels: tuple[tuple[Price, Quantity], ...], side: MarketSide) -> dict[int, int]:
        """Validate and index one side of a snapshot.

        A repeated price is rejected rather than merged: the venue publishes
        aggregated levels, so a duplicate means the payload is malformed, and
        summing it would invent depth that does not exist.
        """
        built: dict[int, int] = {}
        for price, quantity in levels:
            if price.units in built:
                raise BookMutationError(
                    f"duplicate {side.value} price level {price} in snapshot",
                    reason=InvalidationReason.DUPLICATE_PRICE_LEVEL,
                )
            if quantity.is_zero:
                # A zero level carries no executable depth; storing it would put
                # a price point in the book that nothing can trade against.
                continue
            built[price.units] = quantity.units
        return built

    def apply_delta(
        self,
        *,
        side: MarketSide,
        price: Price,
        delta: QuantityDelta,
        seq: int | None,
        raw_id: str,
        received_at: datetime,
    ) -> None:
        """Apply a signed relative change to one price level.

        Refuses to run before a snapshot has established a base: applying a
        delta to an empty book would fabricate state that was never sent.

        A result below zero is an invariant failure, never clamped. Clamping
        would paper over a missed message and leave a book that looks correct.
        """
        if self._integrity is not BookIntegrity.VALID:
            raise BookMutationError(
                f"delta for {self.market_ticker} arrived while book is "
                f"{self._integrity.value}; no authoritative base to apply it to",
                reason=(
                    InvalidationReason.DELTA_BEFORE_SNAPSHOT
                    if self._integrity is BookIntegrity.WAITING_SNAPSHOT
                    else InvalidationReason.SEQUENCE_VIOLATION
                ),
            )

        levels = self._yes_bids if side is MarketSide.YES else self._no_bids
        existing = levels.get(price.units, 0)
        updated = existing + delta.units
        if updated < 0:
            raise BookMutationError(
                f"delta {delta} at {price} on {side.value} would take "
                f"{self.market_ticker} to {updated} quantity units from {existing}; "
                "a negative resting size is impossible, so the book state is wrong",
                reason=InvalidationReason.NEGATIVE_QUANTITY,
            )

        if updated == 0:
            levels.pop(price.units, None)
        else:
            levels[price.units] = updated

        self._latest_raw_id = raw_id
        self._latest_seq = seq
        self._latest_received_at = received_at
        self._applied_frames += 1
        self._last_change_at = received_at

    def note_sequenced_frame(self, *, seq: int | None, raw_id: str, received_at: datetime) -> None:
        """Record a seq-bearing frame that legitimately does not mutate the book.

        A control frame consumes a sequence number without changing any book, so
        the sid's activity advances while ``last_change_at`` deliberately does
        not -- the book genuinely did not change.
        """
        self._latest_raw_id = raw_id
        self._latest_seq = seq
        self._latest_received_at = received_at

    # -- views ---------------------------------------------------------------

    def provenance(self) -> BookProvenance:
        return BookProvenance(
            connection_epoch=self.connection_epoch,
            sid=self.sid,
            snapshot_raw_id=self._snapshot_raw_id,
            snapshot_seq=self._snapshot_seq,
            snapshot_received_at=self._snapshot_received_at,
            latest_raw_id=self._latest_raw_id,
            latest_seq=self._latest_seq,
            latest_received_at=self._latest_received_at,
            applied_frames=self._applied_frames,
        )

    def view(self) -> BookView:
        """An immutable snapshot of current state.

        Levels are materialised into tuples here, so a consumer holding a view
        is unaffected by every subsequent mutation.
        """
        return BookView(
            market_ticker=self.market_ticker,
            integrity=self._integrity,
            yes_bids=self._levels_best_first(self._yes_bids, MarketSide.YES),
            no_bids=self._levels_best_first(self._no_bids, MarketSide.NO),
            provenance=self.provenance(),
            invalidation_reason=self._invalidation_reason,
            last_change_at=self._last_change_at,
        )

    @staticmethod
    def _levels_best_first(levels: dict[int, int], side: MarketSide) -> tuple[BookLevel, ...]:
        return tuple(
            BookLevel(
                price=Price.from_units(price_units),
                quantity=Quantity.from_units(quantity_units),
                side=side,
            )
            for price_units, quantity_units in sorted(levels.items(), reverse=True)
        )

    def __repr__(self) -> str:
        return (
            f"MutableOrderBook({self.market_ticker!r}, integrity={self._integrity.value}, "
            f"epoch={self.connection_epoch}, sid={self.sid}, "
            f"levels={len(self._yes_bids)}/{len(self._no_bids)})"
        )


def book_source_for(view: BookView, *, received_at: datetime) -> BookSource:
    """Provenance describing where a reconstructed view came from."""
    return BookSource(
        transport=BookTransport.WS_DELTA_APPLIED,
        instrument_ticker=view.market_ticker,
        received_at=received_at,
        sequence=view.provenance.latest_seq,
        subscription_id=view.provenance.sid,
        raw_message_id=view.provenance.latest_raw_id,
    )
