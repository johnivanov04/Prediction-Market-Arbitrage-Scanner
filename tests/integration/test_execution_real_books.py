"""Execution curves derived from real production books.

Real captured frames are replayed through the Step 4 reconstructor, and the
resulting authoritative books are quoted. The point is to confirm the complement
arithmetic and provenance hold on data the venue actually sent, not just on
constructed examples.
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

from predarb.books.execution import ExecutionContext, ExecutionCurve, build_execution_curve
from predarb.books.orderbook import BookView
from predarb.books.reconstruction import OrderBookReconstructor
from predarb.domain.enums import MarketSide
from predarb.domain.models import VenueInstrument
from predarb.domain.money import Money, Price, Quantity
from predarb.ingest.raw_journal import RawFrameJournal
from predarb.venues.kalshi.models import KalshiMarket
from predarb.venues.kalshi.normalize import normalize_market

pytestmark = pytest.mark.integration

REAL_DIR = Path(__file__).parent.parent / "fixtures" / "kalshi" / "websocket_real"
T0 = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)
NOTIONAL = Price.from_value("1.0000")
CONTEXT = ExecutionContext(current_connection_epoch=1)


def real_frames(prefix: str = "experiment_a") -> list[tuple[int, str, dict[str, Any]]]:
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
def reconstructed_books() -> Iterator[dict[str, BookView]]:
    """Replay the real frames and hand back the resulting books."""
    frames = real_frames()
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
            for index, (_, text, _) in enumerate(frames):
                recon.handle_frame(text, T0 + timedelta(milliseconds=index * 10))
            yield recon.books()
        finally:
            journal.close()


def instrument_for(ticker: str) -> VenueInstrument:
    return normalize_market(
        KalshiMarket.model_validate(
            {
                "ticker": ticker,
                "event_ticker": "EVT",
                "market_type": "binary",
                "status": "active",
                "notional_value_dollars": "1.0000",
            }
        )
    )


def curve_for(view: BookView, outcome: MarketSide) -> ExecutionCurve:
    return build_execution_curve(view, instrument_for(view.market_ticker), CONTEXT, outcome, at=T0)


class TestRealBooksQuote:
    def test_fixtures_present(self) -> None:
        assert real_frames(), "no real frames captured"

    def test_every_valid_book_produces_a_curve(self) -> None:
        with reconstructed_books() as books:
            valid = [view for view in books.values() if view.is_structurally_valid]
            assert valid
            for view in valid:
                for outcome in (MarketSide.YES, MarketSide.NO):
                    curve_for(view, outcome)  # must not raise

    def test_buy_yes_depth_equals_total_no_bid_depth(self) -> None:
        """BUY YES liquidity is exactly the NO bids, nothing more or less."""
        with reconstructed_books() as books:
            checked = 0
            for view in books.values():
                if not view.is_structurally_valid:
                    continue
                curve = curve_for(view, MarketSide.YES)
                expected = sum(level.quantity.units for level in view.no_bids)
                assert curve.max_fillable_quantity.units == expected
                checked += 1
            assert checked

    def test_buy_no_depth_equals_total_yes_bid_depth(self) -> None:
        with reconstructed_books() as books:
            for view in books.values():
                if not view.is_structurally_valid:
                    continue
                curve = curve_for(view, MarketSide.NO)
                expected = sum(level.quantity.units for level in view.yes_bids)
                assert curve.max_fillable_quantity.units == expected

    def test_derived_asks_are_exact_complements(self) -> None:
        with reconstructed_books() as books:
            checked = 0
            for view in books.values():
                if not view.is_structurally_valid or not view.no_bids:
                    continue
                curve = curve_for(view, MarketSide.YES)
                for level in curve.levels:
                    assert (
                        level.execution_price.units + level.source_bid_price.units == NOTIONAL.units
                    )
                    checked += 1
            assert checked, "expected at least one derived level"

    def test_best_ask_derives_from_best_bid(self) -> None:
        with reconstructed_books() as books:
            for view in books.values():
                if not view.is_structurally_valid or not view.no_bids:
                    continue
                curve = curve_for(view, MarketSide.YES)
                best_bid = view.no_bids[0].price  # best bid is the highest
                assert curve.best_execution_price == best_bid.complement(NOTIONAL)

    def test_quantities_match_the_source_levels(self) -> None:
        with reconstructed_books() as books:
            for view in books.values():
                if not view.is_structurally_valid or not view.no_bids:
                    continue
                curve = curve_for(view, MarketSide.YES)
                by_source = {
                    level.source_bid_price: level.available_quantity for level in curve.levels
                }
                for source in view.no_bids:
                    if source.quantity.is_zero:
                        continue
                    assert by_source[source.price] == source.quantity

    def test_provenance_points_at_the_real_book(self) -> None:
        with reconstructed_books() as books:
            for ticker, view in books.items():
                if not view.is_structurally_valid or not view.no_bids:
                    continue
                curve = curve_for(view, MarketSide.YES)
                for level in curve.levels:
                    assert level.liquidity.market_ticker == ticker
                    assert level.liquidity.connection_epoch == view.provenance.connection_epoch
                    assert level.liquidity.sid == view.provenance.sid
                    assert level.liquidity.book_seq == view.provenance.latest_seq
                    assert level.derived


class TestRealBookEdgeCases:
    def test_empty_book_gives_zero_depth_without_error(self) -> None:
        """Two captured markets had finished trading; their books are empty."""
        with reconstructed_books() as books:
            empty = [v for v in books.values() if v.is_structurally_valid and v.is_empty]
            if not empty:
                pytest.skip("no empty book in this fixture set")
            for view in empty:
                curve = curve_for(view, MarketSide.YES)
                assert curve.is_empty
                assert curve.max_fillable_quantity.is_zero
                quote = curve.quote_up_to(Quantity.from_value("5.00"))
                assert quote.filled_quantity.is_zero
                assert not quote.fully_fillable

    def test_one_sided_book_quotes_one_side_only(self) -> None:
        with reconstructed_books() as books:
            one_sided = [
                v
                for v in books.values()
                if v.is_structurally_valid and bool(v.yes_bids) != bool(v.no_bids)
            ]
            if not one_sided:
                pytest.skip("no one-sided book captured")
            for view in one_sided:
                yes_curve = curve_for(view, MarketSide.YES)
                no_curve = curve_for(view, MarketSide.NO)
                assert yes_curve.is_empty != no_curve.is_empty

    def test_multi_level_book_costs_more_at_depth(self) -> None:
        with reconstructed_books() as books:
            deep = [v for v in books.values() if v.is_structurally_valid and len(v.no_bids) >= 3]
            if not deep:
                pytest.skip("no multi-level book captured")
            view = deep[0]
            curve = curve_for(view, MarketSide.YES)
            shallow_quantity = curve.breakpoints[0].cumulative_quantity
            deeper_quantity = curve.breakpoints[2].cumulative_quantity
            shallow_vwap = curve.vwap(shallow_quantity)
            deeper_vwap = curve.vwap(deeper_quantity)
            assert shallow_vwap is not None and deeper_vwap is not None
            assert deeper_vwap >= shallow_vwap

    def test_fractional_quantities_survive(self) -> None:
        with reconstructed_books() as books:
            found = False
            for view in books.values():
                if not view.is_structurally_valid:
                    continue
                curve = curve_for(view, MarketSide.YES)
                for level in curve.levels:
                    if level.available_quantity.units % 100 != 0:
                        found = True
                        # exact: price (4dp) * quantity (2dp) = money (6dp)
                        assert level.full_cost == Money.from_units(
                            level.execution_price.units * level.available_quantity.units
                        )
            if not found:
                pytest.skip("no fractional quantity in this fixture set")

    def test_no_precision_is_lost_against_the_source_book(self) -> None:
        with reconstructed_books() as books:
            for view in books.values():
                if not view.is_structurally_valid or not view.no_bids:
                    continue
                curve = curve_for(view, MarketSide.YES)
                total_source = sum(level.quantity.units for level in view.no_bids)
                total_curve = sum(level.available_quantity.units for level in curve.levels)
                assert total_source == total_curve
