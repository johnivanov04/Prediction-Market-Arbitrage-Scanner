"""Authoritative order-book reconstruction from the WebSocket stream.

Pipeline
--------
Strictly ordered, single-reader, no concurrency inside::

    raw frame text
          |  journal (append-only, before anything else)
          v
    generic envelope extraction
          |
          v
    sid/seq integrity check          <-- BEFORE routing, always
          |
          v
    typed routing
          |
          v
    book mutation (if applicable)

**Sequence checking precedes routing**, and that ordering is load-bearing. The
documented ``ok`` response to ``update_subscription`` carries ``sid`` and
``seq`` (A-37), so control frames consume sequence numbers. A reconstructor that
routes first and sequences only order-book messages sees a phantom gap every
time a control frame passes, and would resynchronise constantly against a
perfectly healthy stream.

**Journalling precedes parsing.** A frame we cannot parse is exactly the frame
worth having on disk, and a book whose inputs were not all recorded cannot be
audited later.

Fail-closed rules
-----------------
* A sequence violation invalidates **every book on that sid**, because the
  counter is shared across the sid's markets and the hole could have carried any
  of them. The violating frame does not mutate anything.
* A seq-bearing frame of an unknown type on an order-book sid invalidates the
  sid. Advancing past a message we cannot prove is irrelevant to book state
  means assuming something unverified.
* A delta that would take a level negative invalidates the book. Never clamped.
* A delta arriving before a snapshot cannot be applied; the market stays
  non-valid until one arrives.
* Losing the connection invalidates every book it sourced.

What this module does not do
----------------------------
No derived asks -- it stores exactly what the venue sends. No detection, no
fees, no payoff. Those consume :class:`~predarb.books.orderbook.BookView`
values later.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Final

from predarb.books.events import EventKind, ReconstructionEvent
from predarb.books.liveness import ConnectionLiveness, LivenessPolicy, LivenessState
from predarb.books.orderbook import BookMutationError, BookView, MutableOrderBook
from predarb.books.registry import SubscriptionRegistry, SubscriptionState
from predarb.books.sequence import SequenceCheck
from predarb.books.state import BookIntegrity, InvalidationReason
from predarb.domain.enums import MarketSide
from predarb.ingest.raw_journal import JournalError, RawFrameJournal, extract_index_fields
from predarb.logging import get_logger
from predarb.venues.kalshi.models import KalshiWsEnvelope, decode_json

__all__ = [
    "BOOK_MUTATING_TYPES",
    "KNOWN_CONTROL_TYPES",
    "OrderBookReconstructor",
    "ReconstructorHealth",
]

logger = get_logger(__name__)

BOOK_MUTATING_TYPES: Final[frozenset[str]] = frozenset({"orderbook_snapshot", "orderbook_delta"})

KNOWN_CONTROL_TYPES: Final[frozenset[str]] = frozenset(
    {
        "ok",
        "subscribed",
        "unsubscribed",
        "error",
    }
)
"""Frame types proven not to carry order-book state.

Anything outside this set *and* outside :data:`BOOK_MUTATING_TYPES`, arriving
with a ``seq`` on an order-book sid, fails closed. The list is short on purpose:
membership means "we have checked that this cannot change a book", and guessing
would defeat the point.
"""

EventSink = Callable[[ReconstructionEvent], None]


class ReconstructorHealth:
    """Counters for observability. Read by metrics and by the live validator."""

    __slots__ = (
        "books_invalidated",
        "control_frames",
        "deltas_applied",
        "frames_journalled",
        "frames_received",
        "invariant_violations",
        "journal_failures",
        "reconnects",
        "sequence_violations",
        "snapshots_applied",
        "unknown_sequenced_frames",
        "unparsed_frames",
    )

    def __init__(self) -> None:
        self.frames_received = 0
        self.frames_journalled = 0
        self.snapshots_applied = 0
        self.deltas_applied = 0
        self.control_frames = 0
        self.sequence_violations = 0
        self.invariant_violations = 0
        self.unknown_sequenced_frames = 0
        self.books_invalidated = 0
        self.journal_failures = 0
        self.unparsed_frames = 0
        self.reconnects = 0

    def as_dict(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in self.__slots__}


class OrderBookReconstructor:
    """Turns a WebSocket frame stream into provable book state.

    Drive it from exactly one receive loop per connection. It is synchronous and
    not thread-safe by design: arrival order is economically meaningful, and
    fanning frames into concurrent tasks would destroy it.
    """

    def __init__(
        self,
        *,
        journal: RawFrameJournal,
        registry: SubscriptionRegistry | None = None,
        event_sink: EventSink | None = None,
        liveness_policy: LivenessPolicy | None = None,
    ) -> None:
        self._journal = journal
        self._liveness_policy = liveness_policy or LivenessPolicy()
        self._liveness: ConnectionLiveness | None = None
        self._registry = registry or SubscriptionRegistry()
        self._books: dict[str, MutableOrderBook] = {}
        self._events: list[ReconstructionEvent] = []
        self._event_sink = event_sink
        self.health = ReconstructorHealth()
        self._journal_healthy = True

    # -- lifecycle -----------------------------------------------------------

    @property
    def registry(self) -> SubscriptionRegistry:
        return self._registry

    @property
    def journal_healthy(self) -> bool:
        """False once a frame could not be journalled.

        Latching rather than transient: once any frame is missing from the
        record, everything downstream of it is unauditable, and that does not
        heal by the next write succeeding.
        """
        return self._journal_healthy

    @property
    def events(self) -> tuple[ReconstructionEvent, ...]:
        return tuple(self._events)

    def open_connection(self, at: datetime) -> int:
        """Begin a new connection epoch. Returns the epoch number."""
        epoch = self._registry.open_epoch(at)
        self._liveness = ConnectionLiveness(
            connection_epoch=epoch.epoch,
            opened_at=at,
            last_signal_at=at,
            policy=self._liveness_policy,
        )
        self._emit(
            ReconstructionEvent(
                kind=EventKind.CONNECTION_OPENED, at=at, connection_epoch=epoch.epoch
            )
        )
        return epoch.epoch

    def close_connection(self, at: datetime, *, reason: str = "closed") -> None:
        """End the current epoch and invalidate every book it sourced.

        No book survives a disconnect as valid. Its levels may still be the last
        thing the venue sent, but nothing proves they still are, and sequence
        authority does not cross connections.
        """
        epoch = self._registry.current_epoch
        if epoch is None:
            return
        invalidated = self._invalidate_epoch_books(epoch.epoch, InvalidationReason.CONNECTION_LOST)
        self._registry.close_epoch(at)
        if self._liveness is not None:
            self._liveness.close(at)
        self._emit(
            ReconstructionEvent(
                kind=EventKind.CONNECTION_CLOSED,
                at=at,
                connection_epoch=epoch.epoch,
                affected_markets=invalidated,
                detail={"reason": reason},
            )
        )

    def note_subscribed(self, *, sid: int, channel: str, markets: set[str], at: datetime) -> None:
        """Record a server-confirmed subscription and open its books.

        Markets start at ``WAITING_SNAPSHOT``: subscribing does not establish
        state, only a snapshot does.
        """
        state = self._registry.register_subscription(sid=sid, channel=channel, markets=markets)
        epoch = self._registry.current_epoch_number
        if epoch is not None and state.is_orderbook:
            for market in markets:
                self._book_for(market, epoch=epoch, sid=sid).reset_for_new_epoch(
                    connection_epoch=epoch, sid=sid
                )
        self._emit(
            ReconstructionEvent(
                kind=EventKind.SUBSCRIBED,
                at=at,
                connection_epoch=epoch,
                sid=sid,
                affected_markets=tuple(sorted(markets)),
                detail={"channel": channel},
            )
        )

    # -- the single authoritative path --------------------------------------

    def handle_frame(self, payload: str, received_at: datetime) -> None:
        """Process one raw frame. The only entry point for stream data.

        Order inside is fixed: journal, extract, sequence, route, mutate.
        """
        self.health.frames_received += 1
        if self._liveness is not None:
            self._liveness.record_signal(received_at)

        index_fields = extract_index_fields(payload)
        raw_id = self._journal_frame(payload, received_at, index_fields)
        if raw_id is None:
            return  # journal failed; books already invalidated

        envelope = self._parse(payload, received_at, raw_id)
        if envelope is None:
            return

        state, check = self._check_sequence(envelope, received_at, raw_id)
        if check is not None and not check.accepted:
            return  # violation handled; frame must not mutate anything

        self._route(envelope, received_at, raw_id, state)

    def _journal_frame(
        self, payload: str, received_at: datetime, index_fields: Mapping[str, object]
    ) -> str | None:
        """Journal before parsing. Returns the raw id, or ``None`` on failure."""
        try:
            record = self._journal.write(
                payload,
                received_at=received_at,
                message_type=index_fields.get("message_type"),  # type: ignore[arg-type]
                sid=index_fields.get("sid"),  # type: ignore[arg-type]
                seq=index_fields.get("seq"),  # type: ignore[arg-type]
                market_ticker=index_fields.get("market_ticker"),  # type: ignore[arg-type]
            )
        except JournalError as exc:
            self._journal_healthy = False
            self.health.journal_failures += 1
            epoch = self._registry.current_epoch_number
            invalidated = (
                self._invalidate_epoch_books(epoch, InvalidationReason.JOURNAL_FAILURE)
                if epoch is not None
                else ()
            )
            self._emit(
                ReconstructionEvent(
                    kind=EventKind.JOURNAL_FAILURE,
                    at=received_at,
                    connection_epoch=epoch,
                    reason=InvalidationReason.JOURNAL_FAILURE,
                    affected_markets=invalidated,
                    detail={"error": type(exc).__name__},
                )
            )
            logger.exception("books.journal_failure", error=type(exc).__name__)
            return None
        self.health.frames_journalled += 1
        return record.raw_id

    def _parse(self, payload: str, received_at: datetime, raw_id: str) -> KalshiWsEnvelope | None:
        try:
            return KalshiWsEnvelope.model_validate(decode_json(payload))
        except (ValueError, TypeError) as exc:
            self.health.unparsed_frames += 1
            self._emit(
                ReconstructionEvent(
                    kind=EventKind.FRAME_UNPARSED,
                    at=received_at,
                    connection_epoch=self._registry.current_epoch_number,
                    raw_id=raw_id,
                    detail={"error": type(exc).__name__},
                )
            )
            # The frame is journalled, so nothing is lost. It carries no usable
            # sid/seq, so it cannot be sequenced, and it mutates nothing.
            return None

    def _check_sequence(
        self, envelope: KalshiWsEnvelope, received_at: datetime, raw_id: str
    ) -> tuple[SubscriptionState | None, SequenceCheck | None]:
        """Validate ``seq`` before any routing decision is made.

        Returns the subscription and the check. A ``None`` check means the frame
        carried no sequence coordinates and therefore advances nothing.
        """
        epoch = self._registry.current_epoch
        if epoch is None or envelope.sid is None or envelope.seq is None:
            return None, None

        state = epoch.subscription_for(envelope.sid)
        if state is None:
            # A sid we never saw acknowledged. Registered defensively so its
            # sequence is tracked from here on; the channel is unknown, so it is
            # not treated as an order-book sid until an ack says otherwise.
            state = self._registry.register_subscription(sid=envelope.sid, channel="unknown")

        check = state.sequence.check(envelope.seq)
        state.last_activity_at = received_at
        epoch.last_frame_at = received_at

        if check.accepted:
            return state, check

        self.health.sequence_violations += 1
        invalidated = self._invalidate_sid_books(
            epoch.epoch, envelope.sid, InvalidationReason.SEQUENCE_VIOLATION, received_at
        )
        self._emit(
            ReconstructionEvent(
                kind=EventKind.SEQUENCE_VIOLATION,
                at=received_at,
                connection_epoch=epoch.epoch,
                sid=envelope.sid,
                seq=check.actual,
                expected_seq=check.expected,
                raw_id=raw_id,
                violation=check.violation,
                reason=InvalidationReason.SEQUENCE_VIOLATION,
                affected_markets=invalidated,
                detail={"message_type": envelope.type},
            )
        )
        logger.warning(
            "books.sequence_violation",
            sid=envelope.sid,
            expected=check.expected,
            actual=check.actual,
            violation=check.violation.value if check.violation else None,
            invalidated=len(invalidated),
        )
        return state, check

    def _route(
        self,
        envelope: KalshiWsEnvelope,
        received_at: datetime,
        raw_id: str,
        state: SubscriptionState | None,
    ) -> None:
        """Dispatch a sequence-accepted frame to its handler."""
        kind = envelope.type
        if kind == "orderbook_snapshot":
            self._apply_snapshot(envelope, received_at, raw_id)
            return
        if kind == "orderbook_delta":
            self._apply_delta(envelope, received_at, raw_id)
            return
        if kind in KNOWN_CONTROL_TYPES:
            self._note_control(envelope, received_at, raw_id)
            return
        self._handle_unknown(envelope, received_at, raw_id, state)

    def _handle_unknown(
        self,
        envelope: KalshiWsEnvelope,
        received_at: datetime,
        raw_id: str,
        state: SubscriptionState | None,
    ) -> None:
        """A frame type we do not recognise.

        With a sequence, on an order-book sid, this fails closed: we cannot show
        the frame is irrelevant to book state, and advancing past it would be an
        assumption. Without a sequence it is recorded and ignored -- it has no
        sequence implications to get wrong.
        """
        epoch = self._registry.current_epoch_number
        sequenced_on_orderbook = (
            envelope.seq is not None and state is not None and state.is_orderbook
        )
        if not sequenced_on_orderbook:
            self._emit(
                ReconstructionEvent(
                    kind=EventKind.UNKNOWN_SEQUENCED_FRAME,
                    at=received_at,
                    connection_epoch=epoch,
                    sid=envelope.sid,
                    seq=envelope.seq,
                    raw_id=raw_id,
                    detail={"message_type": envelope.type, "sequenced": False},
                )
            )
            return

        self.health.unknown_sequenced_frames += 1
        invalidated = (
            self._invalidate_sid_books(
                epoch, envelope.sid, InvalidationReason.UNKNOWN_SEQUENCED_FRAME, received_at
            )
            if epoch is not None and envelope.sid is not None
            else ()
        )
        self._emit(
            ReconstructionEvent(
                kind=EventKind.UNKNOWN_SEQUENCED_FRAME,
                at=received_at,
                connection_epoch=epoch,
                sid=envelope.sid,
                seq=envelope.seq,
                raw_id=raw_id,
                reason=InvalidationReason.UNKNOWN_SEQUENCED_FRAME,
                affected_markets=invalidated,
                detail={"message_type": envelope.type, "sequenced": True},
            )
        )
        logger.warning(
            "books.unknown_sequenced_frame", sid=envelope.sid, message_type=envelope.type
        )

    def _note_control(self, envelope: KalshiWsEnvelope, received_at: datetime, raw_id: str) -> None:
        """A control frame: it consumed a sequence number and changed no book."""
        self.health.control_frames += 1
        epoch = self._registry.current_epoch_number
        if envelope.type == "ok" and envelope.msg is not None:
            self._apply_ok_membership(envelope, received_at)
        for market in self._markets_for_sid(envelope.sid):
            book = self._books.get(market)
            if book is not None:
                book.note_sequenced_frame(seq=envelope.seq, raw_id=raw_id, received_at=received_at)
        self._emit(
            ReconstructionEvent(
                kind=EventKind.CONTROL_SEQ_CONSUMED,
                at=received_at,
                connection_epoch=epoch,
                sid=envelope.sid,
                seq=envelope.seq,
                raw_id=raw_id,
                detail={"message_type": envelope.type},
            )
        )

    def _apply_ok_membership(self, envelope: KalshiWsEnvelope, received_at: datetime) -> None:
        """Update sid membership from a server-confirmed ``ok`` response.

        Preferred over tracking our own add/remove intentions: the server
        reports the resulting market set outright, and that is what actually
        governs which frames arrive.
        """
        if envelope.sid is None or envelope.msg is None:
            return
        raw = envelope.msg.get("market_tickers")
        if not isinstance(raw, list):
            return
        markets = {t for t in raw if isinstance(t, str)}
        previous = self._registry.markets_for_sid(envelope.sid)
        self._registry.set_markets(envelope.sid, markets)
        epoch = self._registry.current_epoch_number
        if epoch is None:
            return
        for added in markets - previous:
            self._book_for(added, epoch=epoch, sid=envelope.sid).reset_for_new_epoch(
                connection_epoch=epoch, sid=envelope.sid
            )
        for removed in previous - markets:
            book = self._books.get(removed)
            if book is not None:
                book.mark_unsubscribed()
        self._emit(
            ReconstructionEvent(
                kind=EventKind.SUBSCRIPTION_UPDATED,
                at=received_at,
                connection_epoch=epoch,
                sid=envelope.sid,
                affected_markets=tuple(sorted(markets)),
                detail={
                    "added": sorted(markets - previous),
                    "removed": sorted(previous - markets),
                },
            )
        )

    def _apply_snapshot(
        self, envelope: KalshiWsEnvelope, received_at: datetime, raw_id: str
    ) -> None:
        epoch = self._registry.current_epoch_number
        try:
            snapshot = envelope.as_snapshot()
        except ValueError:
            self._fail_malformed(envelope, received_at, raw_id)
            return

        book = self._book_for(snapshot.market_ticker, epoch=epoch or 0, sid=envelope.sid or 0)
        try:
            book.apply_snapshot(
                yes_levels=snapshot.yes_dollars_fp,
                no_levels=snapshot.no_dollars_fp,
                seq=envelope.seq,
                raw_id=raw_id,
                received_at=received_at,
            )
        except BookMutationError as exc:
            self._fail_book(
                book, envelope, received_at, raw_id, reason=exc.reason, message=str(exc)
            )
            return

        self.health.snapshots_applied += 1
        self._emit(
            ReconstructionEvent(
                kind=EventKind.SNAPSHOT_APPLIED,
                at=received_at,
                connection_epoch=epoch,
                sid=envelope.sid,
                market_ticker=snapshot.market_ticker,
                seq=envelope.seq,
                raw_id=raw_id,
                detail={
                    "yes_levels": len(snapshot.yes_dollars_fp),
                    "no_levels": len(snapshot.no_dollars_fp),
                },
            )
        )

    def _apply_delta(self, envelope: KalshiWsEnvelope, received_at: datetime, raw_id: str) -> None:
        epoch = self._registry.current_epoch_number
        try:
            delta = envelope.as_delta()
        except ValueError:
            self._fail_malformed(envelope, received_at, raw_id)
            return

        if delta.side not in {"yes", "no"}:
            self._fail_book(
                self._books.get(delta.market_ticker),
                envelope,
                received_at,
                raw_id,
                reason=InvalidationReason.MALFORMED_PAYLOAD,
                message=f"unknown side {delta.side!r}",
            )
            return
        side = MarketSide.YES if delta.side == "yes" else MarketSide.NO

        book = self._books.get(delta.market_ticker)
        if book is None:
            # A delta for a market with no book: we were never told we are
            # subscribed to it. Recorded, not applied -- inventing a book from a
            # delta would fabricate state that was never snapshotted.
            book = self._book_for(delta.market_ticker, epoch=epoch or 0, sid=envelope.sid or 0)

        try:
            book.apply_delta(
                side=side,
                price=delta.price_dollars,
                delta=delta.delta_fp,
                seq=envelope.seq,
                raw_id=raw_id,
                received_at=received_at,
            )
        except BookMutationError as exc:
            self._fail_book(
                book, envelope, received_at, raw_id, reason=exc.reason, message=str(exc)
            )
            return

        self.health.deltas_applied += 1
        self._emit(
            ReconstructionEvent(
                kind=EventKind.DELTA_APPLIED,
                at=received_at,
                connection_epoch=epoch,
                sid=envelope.sid,
                market_ticker=delta.market_ticker,
                seq=envelope.seq,
                raw_id=raw_id,
            )
        )

    @staticmethod
    def _ticker_hint(envelope: KalshiWsEnvelope) -> str | None:
        """Market ticker from a frame that failed full validation.

        A malformed order-book frame still usually names its market, and we need
        that name: the update it carried was meant for a book, so that book is
        now wrong even though we cannot read the update.
        """
        if envelope.msg is None:
            return None
        raw = envelope.msg.get("market_ticker")
        return raw if isinstance(raw, str) and raw else None

    def _fail_malformed(
        self, envelope: KalshiWsEnvelope, received_at: datetime, raw_id: str
    ) -> None:
        """Handle a frame we could not validate on an order-book sid.

        Fails closed on the narrowest scope we can justify: the named market if
        the frame identifies one, otherwise the whole sid, because an
        unreadable order-book frame could have targeted any of its markets.
        """
        ticker = self._ticker_hint(envelope)
        if ticker is not None:
            self._fail_book(
                self._books.get(ticker),
                envelope,
                received_at,
                raw_id,
                reason=InvalidationReason.MALFORMED_PAYLOAD,
                message=f"could not validate {envelope.type} for {ticker}",
            )
            return
        epoch = self._registry.current_epoch_number
        invalidated = self._invalidate_sid_books(
            epoch, envelope.sid, InvalidationReason.MALFORMED_PAYLOAD, received_at
        )
        self.health.invariant_violations += 1
        self._emit(
            ReconstructionEvent(
                kind=EventKind.BOOK_INVARIANT_VIOLATION,
                at=received_at,
                connection_epoch=epoch,
                sid=envelope.sid,
                seq=envelope.seq,
                raw_id=raw_id,
                reason=InvalidationReason.MALFORMED_PAYLOAD,
                affected_markets=invalidated,
                detail={
                    "message": "unreadable order-book frame names no market",
                    "message_type": envelope.type,
                },
            )
        )

    def _fail_book(
        self,
        book: MutableOrderBook | None,
        envelope: KalshiWsEnvelope,
        received_at: datetime,
        raw_id: str,
        *,
        reason: InvalidationReason,
        message: str = "",
    ) -> None:
        """Record a book-level invariant failure and invalidate.

        Invalidates only the affected book, not the whole sid: the sequence was
        accepted, so the stream is intact and other books on the sid remain
        provable. A *sequence* failure is the case that takes the sid down.
        """
        self.health.invariant_violations += 1
        if book is not None:
            book.invalidate(reason)
            self.health.books_invalidated += 1
        self._emit(
            ReconstructionEvent(
                kind=EventKind.BOOK_INVARIANT_VIOLATION,
                at=received_at,
                connection_epoch=self._registry.current_epoch_number,
                sid=envelope.sid,
                market_ticker=book.market_ticker if book else None,
                seq=envelope.seq,
                raw_id=raw_id,
                reason=reason,
                detail={"message": message, "message_type": envelope.type},
            )
        )
        logger.warning(
            "books.invariant_violation",
            market=book.market_ticker if book else None,
            reason=reason.value,
        )

    # -- invalidation --------------------------------------------------------

    def _markets_for_sid(self, sid: int | None) -> set[str]:
        if sid is None:
            return set()
        registered = self._registry.markets_for_sid(sid)
        if registered:
            return registered
        # Fall back to books that claim this sid, so a sid whose membership was
        # never acknowledged is still fully invalidated on violation.
        return {t for t, b in self._books.items() if b.sid == sid}

    def _invalidate_sid_books(
        self, epoch: int | None, sid: int | None, reason: InvalidationReason, at: datetime
    ) -> tuple[str, ...]:
        """Invalidate every book on a sid.

        Every book, not just the one named in the offending frame: the counter
        is shared across the sid's markets, so a hole could have carried an
        update for any of them.
        """
        invalidated: list[str] = []
        for market in sorted(self._markets_for_sid(sid)):
            book = self._books.get(market)
            if book is None or book.integrity is BookIntegrity.INTEGRITY_UNKNOWN:
                continue
            if epoch is not None and book.connection_epoch != epoch:
                continue
            book.invalidate(reason)
            invalidated.append(market)
        if invalidated:
            self.health.books_invalidated += len(invalidated)
            self._emit(
                ReconstructionEvent(
                    kind=EventKind.BOOKS_INVALIDATED,
                    at=at,
                    connection_epoch=epoch,
                    sid=sid,
                    reason=reason,
                    affected_markets=tuple(invalidated),
                )
            )
        return tuple(invalidated)

    def _invalidate_epoch_books(
        self, epoch: int | None, reason: InvalidationReason
    ) -> tuple[str, ...]:
        invalidated: list[str] = []
        for market, book in sorted(self._books.items()):
            if epoch is not None and book.connection_epoch != epoch:
                continue
            if book.integrity in {BookIntegrity.INTEGRITY_UNKNOWN, BookIntegrity.UNSUBSCRIBED}:
                continue
            book.invalidate(reason)
            invalidated.append(market)
        self.health.books_invalidated += len(invalidated)
        return tuple(invalidated)

    # -- consumer surface ----------------------------------------------------

    def _book_for(self, market: str, *, epoch: int, sid: int) -> MutableOrderBook:
        book = self._books.get(market)
        if book is None:
            book = MutableOrderBook(market, connection_epoch=epoch, sid=sid)
            self._books[market] = book
        return book

    def book(self, market_ticker: str) -> BookView | None:
        """An immutable view of one market, or ``None`` if unknown."""
        book = self._books.get(market_ticker)
        return book.view() if book is not None else None

    def books(self) -> dict[str, BookView]:
        return {ticker: book.view() for ticker, book in sorted(self._books.items())}

    def valid_books(self) -> dict[str, BookView]:
        return {t: v for t, v in self.books().items() if v.is_structurally_valid}

    @property
    def liveness(self) -> ConnectionLiveness | None:
        return self._liveness

    def record_transport_signal(self, at: datetime) -> None:
        """Note transport-level evidence the socket is alive (e.g. a pong).

        Exists so a quiet market cannot look like a dead connection: the
        protocol's keepalive proves the socket works even when no application
        frame has arrived.
        """
        if self._liveness is not None:
            self._liveness.record_signal(at)

    def connection_is_healthy(self, now: datetime) -> bool:
        """Whether the transport is alive by our local policy.

        The threshold is ours, not an exchange guarantee -- Kalshi documents no
        ping cadence (A-39).
        """
        return self._liveness is not None and self._liveness.is_healthy(now)

    def liveness_state(self, now: datetime) -> LivenessState:
        if self._liveness is None:
            return LivenessState.CLOSED
        return self._liveness.state(now)

    def scan_eligible(self, market_ticker: str, now: datetime) -> bool:
        """Whether a book may be handed to a scanner.

        Requires all of: the journal intact (an unauditable book is not usable),
        the connection healthy, an active subscription in the *current* epoch,
        an authoritative snapshot, and no unresolved violation.

        Deliberately excludes economics. Fees, settlement semantics and
        relationship verification are later gates; this answers only "is this
        book provably what the venue sent, right now?".
        """
        if not self._journal_healthy:
            return False
        if not self.connection_is_healthy(now):
            return False
        book = self._books.get(market_ticker)
        if book is None or not book.is_structurally_valid:
            return False
        epoch = self._registry.current_epoch_number
        if epoch is None or book.connection_epoch != epoch:
            return False
        return market_ticker in self._registry.markets_for_sid(book.sid)

    def scan_eligible_books(self, now: datetime) -> dict[str, BookView]:
        return {
            ticker: view for ticker, view in self.books().items() if self.scan_eligible(ticker, now)
        }

    def integrity_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {state.value: 0 for state in BookIntegrity}
        for book in self._books.values():
            counts[book.integrity.value] += 1
        return counts

    def _emit(self, event: ReconstructionEvent) -> None:
        self._events.append(event)
        if self._event_sink is not None:
            self._event_sink(event)

    def __repr__(self) -> str:
        return (
            f"OrderBookReconstructor(epoch={self._registry.current_epoch_number}, "
            f"books={len(self._books)}, journal_healthy={self._journal_healthy})"
        )
