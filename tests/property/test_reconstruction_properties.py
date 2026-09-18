"""Property tests for order-book reconstruction.

Strategy: generate a valid frame stream *and* the book state it implies, using a
deliberately naive reference implementation. The reconstructor must agree with
the reference on every clean stream, and must fail closed on every corrupted
one.

The reference is intentionally dumb -- plain dicts, no sequence logic, no
integrity states. If it were as clever as the thing under test, agreeing with it
would prove much less.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from predarb.books.reconstruction import OrderBookReconstructor
from predarb.books.state import BookIntegrity
from predarb.ingest.raw_journal import RawFrameJournal

pytestmark = pytest.mark.property

T0 = datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC)
TICKERS = ["MKT-A", "MKT-B", "MKT-C"]

# Prices on the whole-cent grid; quantities in 0.01 units.
price_units = st.integers(min_value=1, max_value=100).map(lambda c: c * 100)
quantity_units = st.integers(min_value=1, max_value=5000)


def price_str(units: int) -> str:
    return f"{units // 10000}.{units % 10000:04d}"


def qty_str(units: int) -> str:
    return f"{units // 100}.{units % 100:02d}"


class ReferenceBook:
    """A deliberately naive book: two dicts, no safety machinery."""

    def __init__(self) -> None:
        self.yes: dict[int, int] = {}
        self.no: dict[int, int] = {}

    def snapshot(self, yes: list[tuple[int, int]], no: list[tuple[int, int]]) -> None:
        self.yes = {p: q for p, q in yes if q > 0}
        self.no = {p: q for p, q in no if q > 0}

    def delta(self, side: str, price: int, delta: int) -> None:
        book = self.yes if side == "yes" else self.no
        total = book.get(price, 0) + delta
        if total == 0:
            book.pop(price, None)
        else:
            book[price] = total


@contextmanager
def make_reconstructor() -> Iterator[OrderBookReconstructor]:
    """A fresh reconstructor per Hypothesis example.

    Deliberately not a pytest fixture: a function-scoped fixture is created once
    per test *function*, not per example, so Hypothesis would reuse a single
    reconstructor across every generated stream and its sequence state would
    leak between them. That is exactly what
    ``HealthCheck.function_scoped_fixture`` warns about, and suppressing the
    warning would have hidden a real defect in the test rather than fixed it.
    """
    with tempfile.TemporaryDirectory() as directory:
        journal = RawFrameJournal(Path(directory) / "j.jsonl", connection_epoch=1)
        try:
            recon = OrderBookReconstructor(journal=journal)
            recon.open_connection(T0)
            recon.note_subscribed(sid=1, channel="orderbook_delta", markets=set(TICKERS), at=T0)
            yield recon
        finally:
            journal.close()


type Stream = tuple[list[str], dict[str, ReferenceBook]]


@st.composite
def clean_stream(draw: st.DrawFn) -> tuple[list[str], dict[str, ReferenceBook]]:
    """A valid dense stream over one sid, plus the state it implies."""
    tickers = draw(st.lists(st.sampled_from(TICKERS), min_size=1, max_size=3, unique=True))
    reference = {t: ReferenceBook() for t in tickers}
    frames: list[str] = []
    seq = 1

    for ticker in tickers:
        yes = draw(
            st.lists(st.tuples(price_units, quantity_units), max_size=4, unique_by=lambda p: p[0])
        )
        no = draw(
            st.lists(st.tuples(price_units, quantity_units), max_size=4, unique_by=lambda p: p[0])
        )
        reference[ticker].snapshot(yes, no)
        frames.append(
            json.dumps(
                {
                    "type": "orderbook_snapshot",
                    "sid": 1,
                    "seq": seq,
                    "msg": {
                        "market_ticker": ticker,
                        "yes_dollars_fp": [[price_str(p), qty_str(q)] for p, q in yes],
                        "no_dollars_fp": [[price_str(p), qty_str(q)] for p, q in no],
                    },
                }
            )
        )
        seq += 1

    for _ in range(draw(st.integers(min_value=0, max_value=25))):
        ticker = draw(st.sampled_from(tickers))
        side = draw(st.sampled_from(["yes", "no"]))
        price = draw(price_units)
        book = reference[ticker].yes if side == "yes" else reference[ticker].no
        existing = book.get(price, 0)
        # Only generate deltas that keep the level non-negative: a negative
        # result is a corruption case, tested separately.
        delta = draw(st.integers(min_value=-existing, max_value=5000))
        assume(delta != 0)
        reference[ticker].delta(side, price, delta)
        frames.append(
            json.dumps(
                {
                    "type": "orderbook_delta",
                    "sid": 1,
                    "seq": seq,
                    "msg": {
                        "market_ticker": ticker,
                        "price_dollars": price_str(price),
                        "delta_fp": qty_str(delta) if delta > 0 else "-" + qty_str(-delta),
                        "side": side,
                    },
                }
            )
        )
        seq += 1

    return frames, reference


def feed(recon: OrderBookReconstructor, frames: list[str]) -> None:
    for index, frame in enumerate(frames):
        recon.handle_frame(frame, T0 + timedelta(milliseconds=index))


def as_maps(recon: OrderBookReconstructor, ticker: str) -> tuple[dict[int, int], dict[int, int]]:
    view = recon.book(ticker)
    assert view is not None
    return (
        {level.price.units: level.quantity.units for level in view.yes_bids},
        {level.price.units: level.quantity.units for level in view.no_bids},
    )


SETTINGS = settings(max_examples=60)


class TestCleanStreams:
    @SETTINGS
    @given(clean_stream())
    def test_matches_the_reference_implementation(self, stream: Stream) -> None:
        frames, reference = stream
        with make_reconstructor() as recon:
            feed(recon, frames)
            for ticker, expected in reference.items():
                yes, no = as_maps(recon, ticker)
                assert yes == expected.yes
                assert no == expected.no

    @SETTINGS
    @given(clean_stream())
    def test_clean_streams_never_violate(self, stream: Stream) -> None:
        frames, reference = stream
        with make_reconstructor() as recon:
            feed(recon, frames)
            assert recon.health.sequence_violations == 0
            assert recon.health.invariant_violations == 0
            for ticker in reference:
                view = recon.book(ticker)
                assert view is not None
                assert view.integrity is BookIntegrity.VALID

    @SETTINGS
    @given(clean_stream())
    def test_zero_quantity_levels_never_persist(self, stream: Stream) -> None:
        frames, reference = stream
        with make_reconstructor() as recon:
            feed(recon, frames)
            for ticker in reference:
                view = recon.book(ticker)
                assert view is not None
                assert all(not level.quantity.is_zero for level in view.yes_bids + view.no_bids)

    @SETTINGS
    @given(clean_stream())
    def test_reconstruction_is_deterministic(self, stream: Stream) -> None:
        """The same frames must always produce the same book."""
        frames, _ = stream
        results = []
        for _ in range(2):
            with make_reconstructor() as recon:
                feed(recon, frames)
                results.append({t: as_maps(recon, t) for t in TICKERS if recon.book(t)})
        assert results[0] == results[1]


class TestCorruptionFailsClosed:
    """The critical safety property.

    Once integrity is lost, no book from that epoch may be presented as VALID
    again without a fresh authoritative snapshot.
    """

    @SETTINGS
    @given(clean_stream(), st.data())
    def test_dropping_one_frame_invalidates(self, stream: Stream, data: st.DataObject) -> None:
        """Dropping an interior frame must be detected.

        The dropped frame must be neither the first (which merely establishes
        the baseline -- see ``test_dropping_the_first_frame_is_safe_though_undetected``)
        nor the last (whose absence nothing later can expose).
        """
        frames, _ = stream
        assume(len(frames) >= 3)
        index = data.draw(st.integers(min_value=1, max_value=len(frames) - 2))
        corrupted = frames[:index] + frames[index + 1 :]
        with make_reconstructor() as recon:
            feed(recon, corrupted)
            assert recon.health.sequence_violations >= 1
            assert recon.valid_books() == {}

    @SETTINGS
    @given(clean_stream())
    def test_dropping_the_first_frame_is_safe_though_undetected(self, stream: Stream) -> None:
        """Losing the opening frame cannot be detected, and does not need to be.

        The first frame a tracker sees establishes the baseline, so a missing
        predecessor is invisible -- deliberately, since a resubscribe may
        legitimately begin at any value.

        Safety does not depend on detecting it: the opening frames are the
        per-market snapshots, and a market whose snapshot was lost simply never
        receives one, so it stays WAITING_SNAPSHOT and never becomes scannable.
        What must not happen is a book going VALID without its own snapshot.
        """
        frames, _ = stream
        assume(len(frames) >= 2)
        first_ticker = json.loads(frames[0])["msg"]["market_ticker"]
        with make_reconstructor() as recon:
            feed(recon, frames[1:])
            view = recon.book(first_ticker)
            assert view is not None
            # It never got its snapshot, so it is not valid -- whether because
            # it is still waiting or because a stray delta invalidated it.
            assert view.integrity is not BookIntegrity.VALID
            assert first_ticker not in recon.valid_books()

    @SETTINGS
    @given(clean_stream(), st.data())
    def test_duplicating_one_frame_invalidates(self, stream: Stream, data: st.DataObject) -> None:
        frames, _ = stream
        assume(len(frames) >= 2)
        index = data.draw(st.integers(min_value=0, max_value=len(frames) - 1))
        corrupted = [*frames[: index + 1], frames[index], *frames[index + 1 :]]
        with make_reconstructor() as recon:
            feed(recon, corrupted)
            assert recon.health.sequence_violations >= 1
            assert recon.valid_books() == {}

    @SETTINGS
    @given(clean_stream(), st.data())
    def test_reordering_two_frames_invalidates(self, stream: Stream, data: st.DataObject) -> None:
        frames, _ = stream
        assume(len(frames) >= 3)
        index = data.draw(st.integers(min_value=0, max_value=len(frames) - 2))
        corrupted = list(frames)
        corrupted[index], corrupted[index + 1] = corrupted[index + 1], corrupted[index]
        with make_reconstructor() as recon:
            feed(recon, corrupted)
            assert recon.health.sequence_violations >= 1
            assert recon.valid_books() == {}

    @SETTINGS
    @given(clean_stream(), st.data())
    def test_altered_seq_invalidates(self, stream: Stream, data: st.DataObject) -> None:
        frames, _ = stream
        assume(len(frames) >= 2)
        index = data.draw(st.integers(min_value=1, max_value=len(frames) - 1))
        corrupted = list(frames)
        payload = json.loads(corrupted[index])
        payload["seq"] += data.draw(st.integers(min_value=2, max_value=50))
        corrupted[index] = json.dumps(payload)
        with make_reconstructor() as recon:
            feed(recon, corrupted)
            assert recon.health.sequence_violations >= 1

    @SETTINGS
    @given(clean_stream())
    def test_no_valid_book_survives_a_violation_without_a_new_snapshot(
        self, stream: Stream
    ) -> None:
        """The safety property stated plainly.

        After a violation, later well-formed frames must not quietly restore
        validity -- only a fresh authoritative snapshot may.
        """
        frames, _ = stream
        assume(len(frames) >= 2)
        broken = json.loads(frames[-1])
        broken["seq"] += 10
        with make_reconstructor() as recon:
            feed(recon, [*frames[:-1], json.dumps(broken)])
            assume(recon.health.sequence_violations >= 1)
            follow_up = json.loads(frames[-1])
            follow_up["seq"] = broken["seq"] + 1
            recon.handle_frame(json.dumps(follow_up), T0 + timedelta(seconds=1))
            assert recon.valid_books() == {}


class TestNegativeQuantity:
    @SETTINGS
    @given(clean_stream())
    def test_oversized_negative_delta_fails_closed(self, stream: Stream) -> None:
        frames, reference = stream
        with make_reconstructor() as recon:
            feed(recon, frames)
            assume(recon.health.sequence_violations == 0)
            ticker = next(iter(reference))
            view = recon.book(ticker)
            assume(view is not None and view.yes_bids)
            assert view is not None
            level = view.yes_bids[0]
            recon.handle_frame(
                json.dumps(
                    {
                        "type": "orderbook_delta",
                        "sid": 1,
                        "seq": (view.provenance.latest_seq or 0) + 1,
                        "msg": {
                            "market_ticker": ticker,
                            "price_dollars": level.price.to_str(),
                            "delta_fp": "-" + qty_str(level.quantity.units + 1),
                            "side": "yes",
                        },
                    }
                ),
                T0 + timedelta(seconds=2),
            )
            after = recon.book(ticker)
            assert after is not None
            assert after.integrity is BookIntegrity.INTEGRITY_UNKNOWN
