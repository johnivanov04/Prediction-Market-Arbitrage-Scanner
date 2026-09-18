"""Tests for the live collector's loop behaviour.

No socket is opened. A fake client scripts frames, idleness and failures so the
loop's decisions can be checked deterministically.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from predarb.books.reconstruction import OrderBookReconstructor
from predarb.books.state import BookIntegrity
from predarb.config import Settings
from predarb.ingest.book_collector import BookCollector, default_journal_path
from predarb.ingest.raw_journal import RawFrameJournal
from predarb.venues.kalshi.models import KalshiWsEnvelope, decode_json
from predarb.venues.kalshi.websocket import ObservedFrame

pytestmark = pytest.mark.unit

T0 = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)


def frame(payload: dict[str, Any]) -> ObservedFrame:
    text = json.dumps(payload)
    return ObservedFrame(
        received_at=T0,
        raw=text,
        envelope=KalshiWsEnvelope.model_validate(decode_json(text)),
    )


@contextmanager
def harness(
    tmp_path: Path, markets: list[str]
) -> Iterator[tuple[BookCollector, OrderBookReconstructor]]:
    """A collector plus a live reconstructor, with the journal closed on exit.

    Closing matters: an unclosed handle raises a ResourceWarning, and this suite
    treats warnings as errors.
    """
    collector = BookCollector(
        settings=Settings(), markets=markets, journal_path=tmp_path / "j.jsonl"
    )
    journal = RawFrameJournal(tmp_path / "j.jsonl", connection_epoch=1)
    try:
        recon = OrderBookReconstructor(journal=journal)
        recon.open_connection(T0)
        yield collector, recon
    finally:
        journal.close()


class TestSubscriptionRegistration:
    """The sid comes from the server's acknowledgement, never from assumption."""

    def test_registers_from_the_subscribed_ack(self, tmp_path: Path) -> None:
        with harness(tmp_path, ["A", "B"]) as (collector, recon):
            ack = frame(
                {"type": "subscribed", "id": 1, "msg": {"channel": "orderbook_delta", "sid": 7}}
            )
            assert collector._register_subscription(recon, ack) is True
            assert recon.registry.markets_for_sid(7) == {"A", "B"}
            view = recon.book("A")
            assert view is not None
            assert view.integrity is BookIntegrity.WAITING_SNAPSHOT

    def test_ignores_non_ack_frames(self, tmp_path: Path) -> None:
        with harness(tmp_path, ["A"]) as (collector, recon):
            other = frame({"type": "orderbook_delta", "sid": 1, "seq": 1, "msg": {}})
            assert collector._register_subscription(recon, other) is False

    def test_ignores_an_ack_without_a_usable_sid(self, tmp_path: Path) -> None:
        with harness(tmp_path, ["A"]) as (collector, recon):
            bad = frame({"type": "subscribed", "id": 1, "msg": {"channel": "orderbook_delta"}})
            assert collector._register_subscription(recon, bad) is False


class TestBookCapture:
    def test_captures_state_before_teardown(self, tmp_path: Path) -> None:
        """Closing a connection invalidates every book, correctly.

        A report taken after teardown would therefore show INTEGRITY_UNKNOWN
        everywhere and say nothing about how the run actually went, so the
        capture happens while the connection is still live.
        """
        with harness(tmp_path, ["A"]) as (collector, recon):
            recon.note_subscribed(sid=1, channel="orderbook_delta", markets={"A"}, at=T0)
            recon.handle_frame(
                json.dumps(
                    {
                        "type": "orderbook_snapshot",
                        "sid": 1,
                        "seq": 1,
                        "msg": {
                            "market_ticker": "A",
                            "yes_dollars_fp": [["0.5000", "10.00"]],
                            "no_dollars_fp": [],
                        },
                    }
                ),
                T0,
            )
            live = collector._capture_books(recon)
            recon.close_connection(T0, reason="teardown")
            after = collector._capture_books(recon)

        assert live[0]["integrity"] == "VALID"
        assert after[0]["integrity"] == "INTEGRITY_UNKNOWN"

    def test_capture_includes_provenance_and_eligibility(self, tmp_path: Path) -> None:
        collector = BookCollector(
            settings=Settings(), markets=["A"], journal_path=tmp_path / "j.jsonl"
        )
        journal = RawFrameJournal(tmp_path / "j.jsonl", connection_epoch=1)
        try:
            recon = OrderBookReconstructor(journal=journal)
            recon.open_connection(T0)
            recon.note_subscribed(sid=1, channel="orderbook_delta", markets={"A"}, at=T0)
            captured = collector._capture_books(recon)
        finally:
            journal.close()
        row = captured[0]
        for key in ("ticker", "integrity", "latest_seq", "applied_frames", "scan_eligible"):
            assert key in row


class TestConstruction:
    def test_requires_at_least_one_market(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="at least one market"):
            BookCollector(settings=Settings(), markets=[], journal_path=tmp_path / "j.jsonl")

    def test_journal_path_is_per_run(self) -> None:
        """One run's evidence must never interleave with another's."""
        first = default_journal_path(Path("/tmp"), now=datetime(2026, 1, 1, tzinfo=UTC))
        second = default_journal_path(Path("/tmp"), now=datetime(2026, 1, 2, tzinfo=UTC))
        assert first != second
        assert first.suffix == ".jsonl"

    def test_no_trading_surface(self) -> None:
        for forbidden in ("place_order", "submit", "send_order", "cancel"):
            assert not hasattr(BookCollector, forbidden)


class TestSummary:
    def test_summary_reports_counters(self, tmp_path: Path) -> None:
        collector = BookCollector(
            settings=Settings(), markets=["A"], journal_path=tmp_path / "j.jsonl"
        )
        summary = collector.summary()
        for key in ("connections", "reconnects", "frames", "event_counts"):
            assert key in summary
