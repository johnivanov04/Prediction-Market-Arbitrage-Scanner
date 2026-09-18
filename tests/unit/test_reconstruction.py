"""Tests for authoritative order-book reconstruction."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from predarb.books.events import EventKind, ReconstructionEvent
from predarb.books.orderbook import BookView
from predarb.books.reconstruction import OrderBookReconstructor
from predarb.books.state import BookIntegrity, InvalidationReason
from predarb.domain.money import Price, Quantity
from predarb.ingest.raw_journal import RawFrameJournal

pytestmark = pytest.mark.unit

T0 = datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC)


def at(offset_ms: int) -> datetime:
    return T0 + timedelta(milliseconds=offset_ms)


def snapshot_frame(
    ticker: str,
    *,
    sid: int = 1,
    seq: int = 1,
    yes: list[tuple[str, str]] | None = None,
    no: list[tuple[str, str]] | None = None,
) -> str:
    return json.dumps(
        {
            "type": "orderbook_snapshot",
            "sid": sid,
            "seq": seq,
            "msg": {
                "market_ticker": ticker,
                "yes_dollars_fp": [list(p) for p in (yes or [])],
                "no_dollars_fp": [list(p) for p in (no or [])],
            },
        }
    )


def delta_frame(
    ticker: str,
    *,
    sid: int = 1,
    seq: int = 2,
    side: str = "yes",
    price: str = "0.5000",
    delta: str = "10.00",
) -> str:
    return json.dumps(
        {
            "type": "orderbook_delta",
            "sid": sid,
            "seq": seq,
            "msg": {
                "market_ticker": ticker,
                "price_dollars": price,
                "delta_fp": delta,
                "side": side,
            },
        }
    )


def control_frame(kind: str = "ok", *, sid: int = 1, seq: int = 2, **msg: Any) -> str:
    return json.dumps({"type": kind, "id": 1, "sid": sid, "seq": seq, "msg": msg})


@pytest.fixture
def reconstructor(tmp_path: Path) -> Iterator[OrderBookReconstructor]:
    """A reconstructor with an open connection, closed on teardown.

    Closing matters: an unclosed file handle raises a ResourceWarning, and this
    suite treats warnings as errors.
    """
    journal = RawFrameJournal(tmp_path / "j.jsonl", connection_epoch=1)
    recon = OrderBookReconstructor(journal=journal)
    recon.open_connection(T0)
    try:
        yield recon
    finally:
        journal.close()


def subscribed(recon: OrderBookReconstructor, markets: set[str], sid: int = 1) -> None:
    recon.note_subscribed(sid=sid, channel="orderbook_delta", markets=markets, at=T0)


def view_of(recon: OrderBookReconstructor, ticker: str) -> BookView:
    """Fetch a book view, asserting it exists, so tests read without narrowing."""
    view = recon.book(ticker)
    assert view is not None, f"no book for {ticker}"
    return view


# ---------------------------------------------------------------------------
# Sequence
# ---------------------------------------------------------------------------


class TestSequenceHandling:
    def test_dense_stream_keeps_books_valid(self, reconstructor):
        subscribed(reconstructor, {"A"})
        reconstructor.handle_frame(snapshot_frame("A", seq=1, yes=[("0.5000", "10.00")]), at(0))
        reconstructor.handle_frame(delta_frame("A", seq=2), at(1))
        reconstructor.handle_frame(delta_frame("A", seq=3), at(2))
        assert view_of(reconstructor, "A").integrity is BookIntegrity.VALID
        assert reconstructor.health.sequence_violations == 0

    def test_skip_invalidates(self, reconstructor):
        subscribed(reconstructor, {"A"})
        reconstructor.handle_frame(snapshot_frame("A", seq=1, yes=[("0.5000", "10.00")]), at(0))
        reconstructor.handle_frame(delta_frame("A", seq=2), at(1))
        reconstructor.handle_frame(delta_frame("A", seq=4), at(2))
        assert view_of(reconstructor, "A").integrity is BookIntegrity.INTEGRITY_UNKNOWN
        assert (
            view_of(reconstructor, "A").invalidation_reason is InvalidationReason.SEQUENCE_VIOLATION
        )

    def test_duplicate_invalidates(self, reconstructor):
        subscribed(reconstructor, {"A"})
        reconstructor.handle_frame(snapshot_frame("A", seq=1), at(0))
        reconstructor.handle_frame(delta_frame("A", seq=2), at(1))
        reconstructor.handle_frame(delta_frame("A", seq=2), at(2))
        assert not view_of(reconstructor, "A").is_structurally_valid

    def test_decrease_invalidates(self, reconstructor):
        subscribed(reconstructor, {"A"})
        reconstructor.handle_frame(snapshot_frame("A", seq=5), at(0))
        reconstructor.handle_frame(delta_frame("A", seq=6), at(1))
        reconstructor.handle_frame(delta_frame("A", seq=3), at(2))
        assert not view_of(reconstructor, "A").is_structurally_valid

    def test_violating_frame_does_not_mutate_the_book(self, reconstructor):
        subscribed(reconstructor, {"A"})
        reconstructor.handle_frame(snapshot_frame("A", seq=1, yes=[("0.5000", "10.00")]), at(0))
        before = view_of(reconstructor, "A").yes_bids
        reconstructor.handle_frame(delta_frame("A", seq=99, price="0.5000", delta="500.00"), at(1))
        assert view_of(reconstructor, "A").yes_bids == before

    def test_gap_invalidates_every_book_on_the_sid(self, reconstructor):
        """Not just the market named in the offending frame.

        The counter is shared across the sid, so the hole could have carried an
        update for any of its markets.
        """
        subscribed(reconstructor, {"A", "B", "C"})
        reconstructor.handle_frame(snapshot_frame("A", seq=1), at(0))
        reconstructor.handle_frame(snapshot_frame("B", seq=2), at(1))
        reconstructor.handle_frame(snapshot_frame("C", seq=3), at(2))
        assert len(reconstructor.valid_books()) == 3
        reconstructor.handle_frame(delta_frame("A", seq=99), at(3))
        assert reconstructor.valid_books() == {}

    def test_other_sid_is_unaffected_by_a_violation(self, reconstructor):
        subscribed(reconstructor, {"A"}, sid=1)
        subscribed(reconstructor, {"B"}, sid=2)
        reconstructor.handle_frame(snapshot_frame("A", sid=1, seq=1), at(0))
        reconstructor.handle_frame(snapshot_frame("B", sid=2, seq=1), at(1))
        reconstructor.handle_frame(delta_frame("A", sid=1, seq=99), at(2))
        assert not view_of(reconstructor, "A").is_structurally_valid
        assert view_of(reconstructor, "B").is_structurally_valid

    def test_interleaved_sids_reconstruct_independently(self, reconstructor):
        subscribed(reconstructor, {"A"}, sid=1)
        subscribed(reconstructor, {"B"}, sid=2)
        reconstructor.handle_frame(snapshot_frame("A", sid=1, seq=1), at(0))
        reconstructor.handle_frame(snapshot_frame("B", sid=2, seq=1), at(1))
        reconstructor.handle_frame(delta_frame("A", sid=1, seq=2), at(2))
        reconstructor.handle_frame(delta_frame("B", sid=2, seq=2), at(3))
        assert len(reconstructor.valid_books()) == 2
        assert reconstructor.health.sequence_violations == 0


class TestControlFrames:
    def test_ok_frame_consumes_a_sequence_number(self, reconstructor):
        """A control frame advances the counter without mutating a book.

        Sequencing before routing is what makes this work: filtering to
        order-book messages first would read the next delta as a gap.
        """
        subscribed(reconstructor, {"A"})
        reconstructor.handle_frame(snapshot_frame("A", seq=1), at(0))
        reconstructor.handle_frame(control_frame("ok", seq=2, market_tickers=["A"]), at(1))
        reconstructor.handle_frame(delta_frame("A", seq=3), at(2))
        assert view_of(reconstructor, "A").is_structurally_valid
        assert reconstructor.health.control_frames == 1
        assert reconstructor.health.sequence_violations == 0

    def test_control_frame_does_not_change_last_change_time(self, reconstructor):
        subscribed(reconstructor, {"A"})
        reconstructor.handle_frame(snapshot_frame("A", seq=1), at(0))
        changed_at = view_of(reconstructor, "A").last_change_at
        reconstructor.handle_frame(control_frame("ok", seq=2, market_tickers=["A"]), at(50))
        assert view_of(reconstructor, "A").last_change_at == changed_at

    def test_frame_without_seq_does_not_advance_the_counter(self, reconstructor):
        subscribed(reconstructor, {"A"})
        reconstructor.handle_frame(snapshot_frame("A", seq=1), at(0))
        reconstructor.handle_frame(
            json.dumps({"type": "subscribed", "id": 1, "msg": {"channel": "x", "sid": 1}}), at(1)
        )
        reconstructor.handle_frame(delta_frame("A", seq=2), at(2))
        assert view_of(reconstructor, "A").is_structurally_valid
        assert reconstructor.health.sequence_violations == 0

    def test_unknown_sequenced_frame_fails_closed(self, reconstructor):
        """We cannot prove it is irrelevant to book state, so we stop."""
        subscribed(reconstructor, {"A"})
        reconstructor.handle_frame(snapshot_frame("A", seq=1), at(0))
        reconstructor.handle_frame(control_frame("brand_new_frame_type", seq=2), at(1))
        assert not view_of(reconstructor, "A").is_structurally_valid
        assert (
            view_of(reconstructor, "A").invalidation_reason
            is InvalidationReason.UNKNOWN_SEQUENCED_FRAME
        )
        assert reconstructor.health.unknown_sequenced_frames == 1

    def test_unknown_frame_without_seq_is_harmless(self, reconstructor):
        subscribed(reconstructor, {"A"})
        reconstructor.handle_frame(snapshot_frame("A", seq=1), at(0))
        reconstructor.handle_frame(json.dumps({"type": "something_new"}), at(1))
        assert view_of(reconstructor, "A").is_structurally_valid


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


class TestSnapshots:
    def test_snapshot_makes_a_waiting_book_valid(self, reconstructor):
        subscribed(reconstructor, {"A"})
        assert view_of(reconstructor, "A").integrity is BookIntegrity.WAITING_SNAPSHOT
        reconstructor.handle_frame(snapshot_frame("A", yes=[("0.5000", "10.00")]), at(0))
        assert view_of(reconstructor, "A").integrity is BookIntegrity.VALID

    def test_yes_only(self, reconstructor):
        subscribed(reconstructor, {"A"})
        reconstructor.handle_frame(snapshot_frame("A", yes=[("0.5000", "10.00")]), at(0))
        view = view_of(reconstructor, "A")
        assert len(view.yes_bids) == 1
        assert view.no_bids == ()

    def test_no_only(self, reconstructor):
        subscribed(reconstructor, {"A"})
        reconstructor.handle_frame(snapshot_frame("A", no=[("0.4000", "7.00")]), at(0))
        view = view_of(reconstructor, "A")
        assert view.yes_bids == ()
        assert view.no_bids[0].price == Price.from_value("0.4000")

    def test_both_sides(self, reconstructor):
        subscribed(reconstructor, {"A"})
        reconstructor.handle_frame(
            snapshot_frame("A", yes=[("0.5000", "10.00")], no=[("0.4000", "7.00")]), at(0)
        )
        view = view_of(reconstructor, "A")
        assert len(view.yes_bids) == 1
        assert len(view.no_bids) == 1

    def test_empty_snapshot_is_valid(self, reconstructor):
        # Observed live: a finished market's book is genuinely empty.
        subscribed(reconstructor, {"A"})
        reconstructor.handle_frame(snapshot_frame("A"), at(0))
        view = view_of(reconstructor, "A")
        assert view.integrity is BookIntegrity.VALID
        assert view.is_empty

    def test_levels_are_best_first(self, reconstructor):
        subscribed(reconstructor, {"A"})
        reconstructor.handle_frame(
            snapshot_frame("A", yes=[("0.1000", "1.00"), ("0.5000", "2.00"), ("0.3000", "3.00")]),
            at(0),
        )
        prices = [level.price.to_str() for level in view_of(reconstructor, "A").yes_bids]
        assert prices == ["0.5000", "0.3000", "0.1000"]

    def test_snapshot_replaces_rather_than_merges(self, reconstructor):
        """A merge would let a level the venue has dropped survive forever."""
        subscribed(reconstructor, {"A"})
        reconstructor.handle_frame(
            snapshot_frame("A", seq=1, yes=[("0.5000", "10.00"), ("0.4000", "5.00")]), at(0)
        )
        reconstructor.handle_frame(snapshot_frame("A", seq=2, yes=[("0.6000", "1.00")]), at(1))
        prices = [level.price.to_str() for level in view_of(reconstructor, "A").yes_bids]
        assert prices == ["0.6000"]

    def test_duplicate_price_level_is_rejected(self, reconstructor):
        subscribed(reconstructor, {"A"})
        reconstructor.handle_frame(
            snapshot_frame("A", yes=[("0.5000", "10.00"), ("0.5000", "3.00")]), at(0)
        )
        view = view_of(reconstructor, "A")
        assert view.integrity is BookIntegrity.INTEGRITY_UNKNOWN
        assert view.invalidation_reason is InvalidationReason.DUPLICATE_PRICE_LEVEL

    def test_several_markets_under_one_sid(self, reconstructor):
        subscribed(reconstructor, {"A", "B", "C"})
        for index, ticker in enumerate(["A", "B", "C"], start=1):
            reconstructor.handle_frame(
                snapshot_frame(ticker, seq=index, yes=[("0.5000", "1.00")]), at(index)
            )
        assert len(reconstructor.valid_books()) == 3

    def test_midstream_snapshot_does_not_reset_the_sequence(self, reconstructor):
        """A snapshot at seq 250 was observed live after add_markets."""
        subscribed(reconstructor, {"A", "B"})
        reconstructor.handle_frame(snapshot_frame("A", seq=1), at(0))
        reconstructor.handle_frame(delta_frame("A", seq=2), at(1))
        reconstructor.handle_frame(snapshot_frame("B", seq=3), at(2))
        reconstructor.handle_frame(delta_frame("A", seq=4), at(3))
        assert reconstructor.health.sequence_violations == 0
        assert len(reconstructor.valid_books()) == 2

    def test_zero_quantity_levels_are_dropped(self, reconstructor):
        subscribed(reconstructor, {"A"})
        reconstructor.handle_frame(
            snapshot_frame("A", yes=[("0.5000", "0.00"), ("0.4000", "5.00")]), at(0)
        )
        prices = [level.price.to_str() for level in view_of(reconstructor, "A").yes_bids]
        assert prices == ["0.4000"]

    def test_malformed_snapshot_leaves_previous_state_untouched(self, reconstructor):
        subscribed(reconstructor, {"A"})
        reconstructor.handle_frame(snapshot_frame("A", seq=1, yes=[("0.5000", "10.00")]), at(0))
        before = view_of(reconstructor, "A").yes_bids
        reconstructor.handle_frame(
            snapshot_frame("A", seq=2, yes=[("0.3000", "1.00"), ("0.3000", "2.00")]), at(1)
        )
        assert view_of(reconstructor, "A").yes_bids == before


# ---------------------------------------------------------------------------
# Deltas
# ---------------------------------------------------------------------------


class TestDeltas:
    def _valid_book(self, recon: OrderBookReconstructor) -> None:
        subscribed(recon, {"A"})
        recon.handle_frame(snapshot_frame("A", seq=1, yes=[("0.5000", "10.00")]), at(0))

    def test_creates_a_new_level(self, reconstructor):
        self._valid_book(reconstructor)
        reconstructor.handle_frame(delta_frame("A", seq=2, price="0.4000", delta="3.00"), at(1))
        prices = {level.price.to_str() for level in view_of(reconstructor, "A").yes_bids}
        assert prices == {"0.5000", "0.4000"}

    def test_increases_an_existing_level(self, reconstructor):
        self._valid_book(reconstructor)
        reconstructor.handle_frame(delta_frame("A", seq=2, price="0.5000", delta="5.00"), at(1))
        assert view_of(reconstructor, "A").yes_bids[0].quantity == Quantity.from_value("15.00")

    def test_decreases_an_existing_level(self, reconstructor):
        self._valid_book(reconstructor)
        reconstructor.handle_frame(delta_frame("A", seq=2, price="0.5000", delta="-4.00"), at(1))
        assert view_of(reconstructor, "A").yes_bids[0].quantity == Quantity.from_value("6.00")

    def test_exact_depletion_removes_the_level(self, reconstructor):
        self._valid_book(reconstructor)
        reconstructor.handle_frame(delta_frame("A", seq=2, price="0.5000", delta="-10.00"), at(1))
        assert view_of(reconstructor, "A").yes_bids == ()
        assert view_of(reconstructor, "A").is_structurally_valid

    def test_negative_result_fails_closed(self, reconstructor):
        """Never clamped: the book state is demonstrably wrong."""
        self._valid_book(reconstructor)
        reconstructor.handle_frame(delta_frame("A", seq=2, price="0.5000", delta="-11.00"), at(1))
        view = view_of(reconstructor, "A")
        assert view.integrity is BookIntegrity.INTEGRITY_UNKNOWN
        assert view.invalidation_reason is InvalidationReason.NEGATIVE_QUANTITY

    def test_delta_before_snapshot_cannot_create_a_valid_book(self, reconstructor):
        subscribed(reconstructor, {"A"})
        reconstructor.handle_frame(delta_frame("A", seq=1, delta="5.00"), at(0))
        view = view_of(reconstructor, "A")
        assert view.integrity is BookIntegrity.INTEGRITY_UNKNOWN
        assert view.invalidation_reason is InvalidationReason.DELTA_BEFORE_SNAPSHOT

    def test_no_side_delta(self, reconstructor):
        subscribed(reconstructor, {"A"})
        reconstructor.handle_frame(snapshot_frame("A", seq=1, no=[("0.4000", "5.00")]), at(0))
        reconstructor.handle_frame(
            delta_frame("A", seq=2, side="no", price="0.4000", delta="2.00"), at(1)
        )
        assert view_of(reconstructor, "A").no_bids[0].quantity == Quantity.from_value("7.00")

    def test_sides_are_independent(self, reconstructor):
        subscribed(reconstructor, {"A"})
        reconstructor.handle_frame(
            snapshot_frame("A", seq=1, yes=[("0.5000", "10.00")], no=[("0.4000", "5.00")]),
            at(0),
        )
        reconstructor.handle_frame(
            delta_frame("A", seq=2, side="no", price="0.4000", delta="-5.00"), at(1)
        )
        assert view_of(reconstructor, "A").no_bids == ()
        assert len(view_of(reconstructor, "A").yes_bids) == 1

    def test_fractional_quantity(self, reconstructor):
        self._valid_book(reconstructor)
        reconstructor.handle_frame(delta_frame("A", seq=2, price="0.5000", delta="0.29"), at(1))
        assert view_of(reconstructor, "A").yes_bids[0].quantity == Quantity.from_value("10.29")

    def test_sub_cent_price(self, reconstructor):
        self._valid_book(reconstructor)
        reconstructor.handle_frame(delta_frame("A", seq=2, price="0.0001", delta="14.29"), at(1))
        cheapest = view_of(reconstructor, "A").yes_bids[-1]
        assert cheapest.price == Price.from_value("0.0001")
        assert cheapest.quantity == Quantity.from_value("14.29")

    def test_malformed_side_fails_closed(self, reconstructor):
        self._valid_book(reconstructor)
        reconstructor.handle_frame(delta_frame("A", seq=2, side="sideways"), at(1))
        assert not view_of(reconstructor, "A").is_structurally_valid

    def test_malformed_price_fails_closed(self, reconstructor):
        self._valid_book(reconstructor)
        reconstructor.handle_frame(delta_frame("A", seq=2, price="0.12345"), at(1))
        assert not view_of(reconstructor, "A").is_structurally_valid


# ---------------------------------------------------------------------------
# Subscriptions, connection loss, journal
# ---------------------------------------------------------------------------


class TestSubscriptions:
    def test_initial_subscription_waits_for_snapshot(self, reconstructor):
        subscribed(reconstructor, {"A"})
        assert view_of(reconstructor, "A").integrity is BookIntegrity.WAITING_SNAPSHOT
        assert not reconstructor.scan_eligible("A", at(0))

    def test_same_channel_subscriptions_merge_into_one_sid(self, reconstructor):
        """Observed live: the server merges same-channel subscribes."""
        subscribed(reconstructor, {"A"}, sid=1)
        subscribed(reconstructor, {"B"}, sid=1)
        assert reconstructor.registry.markets_for_sid(1) == {"A", "B"}

    def test_ok_frame_adds_a_market(self, reconstructor):
        subscribed(reconstructor, {"A"})
        reconstructor.handle_frame(snapshot_frame("A", seq=1), at(0))
        reconstructor.handle_frame(control_frame("ok", seq=2, market_tickers=["A", "B"]), at(1))
        assert reconstructor.registry.markets_for_sid(1) == {"A", "B"}
        assert view_of(reconstructor, "B").integrity is BookIntegrity.WAITING_SNAPSHOT

    def test_added_market_becomes_valid_after_its_snapshot(self, reconstructor):
        subscribed(reconstructor, {"A"})
        reconstructor.handle_frame(snapshot_frame("A", seq=1), at(0))
        reconstructor.handle_frame(control_frame("ok", seq=2, market_tickers=["A", "B"]), at(1))
        reconstructor.handle_frame(snapshot_frame("B", seq=3), at(2))
        assert view_of(reconstructor, "B").integrity is BookIntegrity.VALID
        assert reconstructor.scan_eligible("B", at(3))

    def test_removed_market_becomes_ineligible(self, reconstructor):
        subscribed(reconstructor, {"A", "B"})
        reconstructor.handle_frame(snapshot_frame("A", seq=1), at(0))
        reconstructor.handle_frame(snapshot_frame("B", seq=2), at(1))
        reconstructor.handle_frame(control_frame("ok", seq=3, market_tickers=["A"]), at(2))
        assert view_of(reconstructor, "B").integrity is BookIntegrity.UNSUBSCRIBED
        assert not reconstructor.scan_eligible("B", at(3))
        assert reconstructor.scan_eligible("A", at(3))


class TestConnectionLoss:
    def test_disconnect_invalidates_every_book(self, reconstructor):
        subscribed(reconstructor, {"A", "B"})
        reconstructor.handle_frame(snapshot_frame("A", seq=1), at(0))
        reconstructor.handle_frame(snapshot_frame("B", seq=2), at(1))
        assert len(reconstructor.valid_books()) == 2
        reconstructor.close_connection(at(2), reason="dropped")
        assert reconstructor.valid_books() == {}
        assert view_of(reconstructor, "A").invalidation_reason is InvalidationReason.CONNECTION_LOST

    def test_reconnect_requires_fresh_snapshots(self, reconstructor):
        subscribed(reconstructor, {"A"})
        reconstructor.handle_frame(snapshot_frame("A", seq=1, yes=[("0.5000", "9.00")]), at(0))
        reconstructor.close_connection(at(1))
        epoch = reconstructor.open_connection(at(2))
        assert epoch == 2
        subscribed(reconstructor, {"A"})
        assert view_of(reconstructor, "A").integrity is BookIntegrity.WAITING_SNAPSHOT
        assert view_of(reconstructor, "A").is_empty  # old levels discarded
        reconstructor.handle_frame(snapshot_frame("A", seq=1, yes=[("0.6000", "1.00")]), at(3))
        assert view_of(reconstructor, "A").is_structurally_valid

    def test_sequence_authority_does_not_cross_connections(self, reconstructor):
        """seq restarts at 1 after reconnect; that must not read as a decrease."""
        subscribed(reconstructor, {"A"})
        reconstructor.handle_frame(snapshot_frame("A", seq=1), at(0))
        reconstructor.handle_frame(delta_frame("A", seq=2), at(1))
        reconstructor.close_connection(at(2))
        reconstructor.open_connection(at(3))
        subscribed(reconstructor, {"A"})
        reconstructor.handle_frame(snapshot_frame("A", seq=1), at(4))
        assert reconstructor.health.sequence_violations == 0
        assert view_of(reconstructor, "A").is_structurally_valid


class TestJournalFailure:
    def test_journal_failure_invalidates_and_blocks_eligibility(self, tmp_path: Path) -> None:
        """Losing a frame must not leave a confident book behind."""
        journal = RawFrameJournal(tmp_path / "j.jsonl", connection_epoch=1)
        recon = OrderBookReconstructor(journal=journal)
        recon.open_connection(T0)
        subscribed(recon, {"A"})
        recon.handle_frame(snapshot_frame("A", seq=1), at(0))
        assert recon.scan_eligible("A", at(1))

        journal.close()  # subsequent writes now fail; also releases the handle
        recon.handle_frame(delta_frame("A", seq=2), at(2))

        assert not recon.journal_healthy
        assert not recon.scan_eligible("A", at(3))
        assert recon.health.journal_failures == 1
        assert any(e.kind is EventKind.JOURNAL_FAILURE for e in recon.events)

    def test_journal_health_does_not_recover_on_its_own(self, tmp_path: Path) -> None:
        journal = RawFrameJournal(tmp_path / "j.jsonl", connection_epoch=1)
        recon = OrderBookReconstructor(journal=journal)  # journal closed below
        recon.open_connection(T0)
        subscribed(recon, {"A"})
        journal.close()
        recon.handle_frame(snapshot_frame("A", seq=1), at(0))
        assert not recon.journal_healthy
        # Even a later successful-looking call must not restore eligibility.
        assert not recon.scan_eligible("A", at(1))

    def test_unparseable_frame_is_journalled_and_ignored(
        self, reconstructor: OrderBookReconstructor
    ) -> None:
        subscribed(reconstructor, {"A"})
        reconstructor.handle_frame(snapshot_frame("A", seq=1), at(0))
        reconstructor.handle_frame("{not json", at(1))
        assert reconstructor.health.unparsed_frames == 1
        assert reconstructor.health.frames_journalled == 2
        assert view_of(reconstructor, "A").is_structurally_valid


# ---------------------------------------------------------------------------
# Provenance, liveness, events
# ---------------------------------------------------------------------------


class TestProvenance:
    def test_records_snapshot_and_latest_coordinates(self, reconstructor):
        subscribed(reconstructor, {"A"})
        reconstructor.handle_frame(snapshot_frame("A", seq=1, yes=[("0.5000", "1.00")]), at(0))
        reconstructor.handle_frame(delta_frame("A", seq=2), at(1))
        provenance = view_of(reconstructor, "A").provenance
        assert provenance.connection_epoch == 1
        assert provenance.sid == 1
        assert provenance.snapshot_seq == 1
        assert provenance.latest_seq == 2
        assert provenance.snapshot_raw_id
        assert provenance.latest_raw_id
        assert provenance.snapshot_raw_id != provenance.latest_raw_id
        assert provenance.applied_frames == 1

    def test_provenance_does_not_accumulate_every_frame(self, reconstructor):
        """Compact by design; the journal holds the full chain."""
        subscribed(reconstructor, {"A"})
        reconstructor.handle_frame(snapshot_frame("A", seq=1, yes=[("0.5000", "100.00")]), at(0))
        for seq in range(2, 40):
            reconstructor.handle_frame(
                delta_frame("A", seq=seq, price="0.5000", delta="1.00"), at(seq)
            )
        provenance = view_of(reconstructor, "A").provenance
        assert provenance.applied_frames == 38
        assert not any(
            isinstance(getattr(provenance, f), list | tuple)
            for f in provenance.__dataclass_fields__
        )


class TestLiveness:
    def test_book_change_time_is_not_a_health_signal(self, reconstructor):
        """A resting quote can sit unchanged and stay eligible."""
        subscribed(reconstructor, {"A"})
        reconstructor.handle_frame(snapshot_frame("A", seq=1, yes=[("0.5000", "1.00")]), at(0))
        much_later = T0 + timedelta(seconds=30)
        reconstructor.record_transport_signal(much_later)
        assert reconstructor.scan_eligible("A", much_later)

    def test_silent_connection_becomes_ineligible(self, reconstructor):
        subscribed(reconstructor, {"A"})
        reconstructor.handle_frame(snapshot_frame("A", seq=1), at(0))
        way_later = T0 + timedelta(minutes=10)
        assert not reconstructor.connection_is_healthy(way_later)
        assert not reconstructor.scan_eligible("A", way_later)

    def test_transport_signal_keeps_connection_healthy(self, reconstructor):
        subscribed(reconstructor, {"A"})
        reconstructor.handle_frame(snapshot_frame("A", seq=1), at(0))
        later = T0 + timedelta(minutes=10)
        reconstructor.record_transport_signal(later)
        assert reconstructor.connection_is_healthy(later)


class TestEvents:
    def test_key_events_are_emitted(self, reconstructor):
        subscribed(reconstructor, {"A"})
        reconstructor.handle_frame(snapshot_frame("A", seq=1), at(0))
        reconstructor.handle_frame(delta_frame("A", seq=2), at(1))
        reconstructor.handle_frame(control_frame("ok", seq=3, market_tickers=["A"]), at(2))
        reconstructor.handle_frame(delta_frame("A", seq=99), at(3))
        kinds = [e.kind for e in reconstructor.events]
        for expected in (
            EventKind.CONNECTION_OPENED,
            EventKind.SUBSCRIBED,
            EventKind.SNAPSHOT_APPLIED,
            EventKind.DELTA_APPLIED,
            EventKind.CONTROL_SEQ_CONSUMED,
            EventKind.SEQUENCE_VIOLATION,
            EventKind.BOOKS_INVALIDATED,
        ):
            assert expected in kinds

    def test_violation_event_carries_both_sequence_values(self, reconstructor):
        subscribed(reconstructor, {"A"})
        reconstructor.handle_frame(snapshot_frame("A", seq=1), at(0))
        reconstructor.handle_frame(delta_frame("A", seq=7), at(1))
        violation = next(e for e in reconstructor.events if e.kind is EventKind.SEQUENCE_VIOLATION)
        assert violation.expected_seq == 2
        assert violation.seq == 7
        assert violation.raw_id

    def test_event_log_fields_contain_no_payload(self, reconstructor):
        subscribed(reconstructor, {"A"})
        reconstructor.handle_frame(snapshot_frame("A", seq=1), at(0))
        for event in reconstructor.events:
            rendered = str(event.log_fields())
            assert "yes_dollars_fp" not in rendered
            assert "KALSHI-ACCESS" not in rendered

    def test_event_sink_receives_events(self, tmp_path: Path) -> None:
        seen: list[ReconstructionEvent] = []
        with RawFrameJournal(tmp_path / "j.jsonl", connection_epoch=1) as journal:
            recon = OrderBookReconstructor(journal=journal, event_sink=seen.append)
            recon.open_connection(T0)
        assert seen and seen[0].kind is EventKind.CONNECTION_OPENED


class TestNoDerivedAsks:
    def test_book_view_exposes_only_quoted_bids(self, reconstructor):
        subscribed(reconstructor, {"A"})
        reconstructor.handle_frame(
            snapshot_frame("A", yes=[("0.5000", "1.00")], no=[("0.4000", "1.00")]), at(0)
        )
        view = view_of(reconstructor, "A")
        for forbidden in ("yes_asks", "no_asks", "asks"):
            assert not hasattr(view, forbidden)
        assert all(not level.derived for level in view.yes_bids + view.no_bids)
