"""Replay real production frames through the reconstructor.

The bridge from synthetic tests to actual exchange behaviour: these are frames
Kalshi really sent, captured during the A-09 experiments, fed through the Step 4
reconstructor in stream order.

Two claims are checked. Clean real data must reconstruct cleanly, and the same
data with exactly one frame removed must fail closed before the following frame
can touch a book.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from predarb.books.events import EventKind
from predarb.books.reconstruction import OrderBookReconstructor
from predarb.books.sequence import SequenceViolation
from predarb.books.state import BookIntegrity
from predarb.ingest.raw_journal import RawFrameJournal, read_journal

pytestmark = pytest.mark.integration

REAL_DIR = Path(__file__).parent.parent / "fixtures" / "kalshi" / "websocket_real"
T0 = datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC)

type RealFrame = tuple[int, str, dict[str, Any]]
"""(seq, raw text, parsed payload) for one captured production frame."""


def real_stream(prefix: str) -> list[RealFrame]:
    """Frames from one experiment, ordered by sequence.

    Sequence order *is* arrival order within a sid: the counter is dense and
    assigned on send, so sorting by it reconstructs the order the venue used.
    """
    frames = []
    for path in REAL_DIR.glob(f"{prefix}_*.json"):
        text = path.read_text().strip()
        payload = json.loads(text)
        if payload.get("seq") is None:
            continue
        frames.append((payload["seq"], text, payload))
    frames.sort(key=lambda row: row[0])
    return frames


@contextmanager
def reconstructor_for(frames: list[RealFrame]) -> Iterator[OrderBookReconstructor]:
    tickers = {
        payload["msg"]["market_ticker"]
        for _, _, payload in frames
        if isinstance(payload.get("msg"), dict) and payload["msg"].get("market_ticker")
    }
    with tempfile.TemporaryDirectory() as directory:
        journal = RawFrameJournal(Path(directory) / "j.jsonl", connection_epoch=1)
        try:
            recon = OrderBookReconstructor(journal=journal)
            recon.open_connection(T0)
            recon.note_subscribed(sid=1, channel="orderbook_delta", markets=tickers, at=T0)
            yield recon
        finally:
            journal.close()


def feed(recon: OrderBookReconstructor, frames: list[RealFrame]) -> None:
    for index, (_, text, _) in enumerate(frames):
        recon.handle_frame(text, T0 + timedelta(milliseconds=index * 10))


class TestRealStreamReconstructs:
    def test_fixtures_are_present(self) -> None:
        assert real_stream("experiment_a"), "no real experiment_a frames captured"

    @pytest.mark.parametrize("prefix", ["experiment_a", "experiment_b"])
    def test_clean_real_stream_has_no_violations(self, prefix: str) -> None:
        frames = real_stream(prefix)
        if len(frames) < 2:
            pytest.skip(f"not enough {prefix} frames captured")
        with reconstructor_for(frames) as recon:
            feed(recon, frames)
            assert recon.health.sequence_violations == 0
            assert recon.health.invariant_violations == 0
            assert recon.health.unknown_sequenced_frames == 0

    def test_real_snapshots_and_deltas_apply(self) -> None:
        frames = real_stream("experiment_a")
        with reconstructor_for(frames) as recon:
            feed(recon, frames)
            assert recon.health.snapshots_applied >= 1
            assert recon.health.deltas_applied >= 1
            assert recon.valid_books()

    def test_real_book_contents_are_exact(self) -> None:
        """Levels reconstruct to the exact fixed-point values the venue sent."""
        frames = real_stream("experiment_a")
        snapshot = next(
            (
                p
                for _, _, p in frames
                if p["type"] == "orderbook_snapshot" and p["msg"].get("yes_dollars_fp")
            ),
            None,
        )
        if snapshot is None:
            pytest.skip("no non-empty snapshot captured")
        ticker = snapshot["msg"]["market_ticker"]
        with reconstructor_for(frames) as recon:
            # Feed only up to and including that snapshot, so no delta has moved it.
            upto = [f for f in frames if f[0] <= snapshot["seq"]]
            feed(recon, upto)
            view = recon.book(ticker)
            assert view is not None
            expected = dict(snapshot["msg"]["yes_dollars_fp"])
            actual = {level.price.to_str(): level.quantity.to_str() for level in view.yes_bids}
            assert actual == expected

    def test_every_real_frame_is_journalled_losslessly(self) -> None:
        frames = real_stream("experiment_a")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "j.jsonl"
            journal = RawFrameJournal(path, connection_epoch=1)
            try:
                recon = OrderBookReconstructor(journal=journal)
                recon.open_connection(T0)
                feed(recon, frames)
            finally:
                journal.close()
            records = read_journal(path)
        assert len(records) == len(frames)
        assert [r.payload for r in records] == [text for _, text, _ in frames]
        assert all(r.verify() for r in records)


class TestFaultInjectionOnRealData:
    """Corrupt a real stream locally. Never provoke loss against the venue."""

    def test_dropping_one_real_frame_is_detected(self) -> None:
        frames = real_stream("experiment_a")
        if len(frames) < 4:
            pytest.skip("not enough frames to drop an interior one")
        # Drop an interior frame: the following frame exposes the hole.
        drop_at = len(frames) // 2
        dropped_seq = frames[drop_at][0]
        corrupted = frames[:drop_at] + frames[drop_at + 1 :]
        with reconstructor_for(corrupted) as recon:
            feed(recon, corrupted)
            assert recon.health.sequence_violations >= 1
            assert recon.valid_books() == {}
            # Match on kind, not reason: BOOKS_INVALIDATED carries the same
            # reason but not the sequence values.
            violation = next(e for e in recon.events if e.kind is EventKind.SEQUENCE_VIOLATION)
            assert violation.expected_seq == dropped_seq
            assert violation.violation is SequenceViolation.SKIP

    def test_violating_frame_does_not_reach_a_book(self) -> None:
        """The frame after the hole must not mutate anything."""
        frames = real_stream("experiment_a")
        if len(frames) < 4:
            pytest.skip("not enough frames")
        drop_at = len(frames) // 2
        corrupted = frames[:drop_at] + frames[drop_at + 1 :]
        with reconstructor_for(corrupted) as recon:
            feed(recon, corrupted[:drop_at])
            before = {t: v.yes_bids for t, v in recon.books().items()}
            feed(recon, corrupted[drop_at : drop_at + 1])
            after = {t: v.yes_bids for t, v in recon.books().items()}
            assert before == after

    def test_duplicating_one_real_frame_is_detected(self) -> None:
        frames = real_stream("experiment_a")
        if len(frames) < 3:
            pytest.skip("not enough frames")
        index = len(frames) // 2
        corrupted = [*frames[: index + 1], frames[index], *frames[index + 1 :]]
        with reconstructor_for(corrupted) as recon:
            feed(recon, corrupted)
            assert recon.health.sequence_violations >= 1
            assert recon.valid_books() == {}

    def test_reordering_two_real_frames_is_detected(self) -> None:
        frames = real_stream("experiment_a")
        if len(frames) < 4:
            pytest.skip("not enough frames")
        index = len(frames) // 2
        corrupted = list(frames)
        corrupted[index], corrupted[index + 1] = corrupted[index + 1], corrupted[index]
        with reconstructor_for(corrupted) as recon:
            feed(recon, corrupted)
            assert recon.health.sequence_violations >= 1
            assert recon.valid_books() == {}

    def test_recovery_requires_a_fresh_snapshot(self) -> None:
        """After a real-data violation, only a new epoch plus snapshot restores validity."""
        frames = real_stream("experiment_a")
        if len(frames) < 4:
            pytest.skip("not enough frames")
        drop_at = len(frames) // 2
        corrupted = frames[:drop_at] + frames[drop_at + 1 :]
        with reconstructor_for(corrupted) as recon:
            feed(recon, corrupted)
            assert recon.valid_books() == {}

            # Reconnect and replay the original opening snapshots.
            recon.close_connection(T0 + timedelta(seconds=5))
            recon.open_connection(T0 + timedelta(seconds=6))
            snapshots = [f for f in frames if f[2]["type"] == "orderbook_snapshot"]
            tickers = {p["msg"]["market_ticker"] for _, _, p in snapshots}
            recon.note_subscribed(
                sid=1,
                channel="orderbook_delta",
                markets=tickers,
                at=T0 + timedelta(seconds=6),
            )
            assert all(
                recon.books()[t].integrity is BookIntegrity.WAITING_SNAPSHOT for t in tickers
            )
            feed(recon, snapshots)
            assert len(recon.valid_books()) == len(tickers)
