"""Tests for Kalshi wire -> domain normalisation."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from predarb.books.levels import BookSource, BookTransport
from predarb.domain.enums import MarketSide, SettlementKind, VenueId
from predarb.domain.money import Price, Quantity
from predarb.venues.kalshi.models import (
    KalshiEventEnvelope,
    KalshiMarket,
    KalshiMarketEnvelope,
    KalshiOrderbook,
    KalshiOrderbookEnvelope,
    KalshiSeriesEnvelope,
    KalshiWsEnvelope,
)
from predarb.venues.kalshi.normalize import (
    EXCLUSION_MULTIVARIATE,
    EXCLUSION_NO_NOTIONAL,
    EXCLUSION_NON_BINARY,
    EXCLUSION_PROVISIONAL,
    normalize_event,
    normalize_market,
    normalize_orderbook,
    normalize_series,
    normalize_ws_snapshot,
    rules_hash_for,
    settlement_kind_for,
)
from tests.conftest import load_payload

pytestmark = pytest.mark.unit

RECEIVED = datetime(2026, 9, 15, 12, 0, 0, tzinfo=UTC)


def source(ticker: str = "TEST") -> BookSource:
    return BookSource(
        transport=BookTransport.REST_SNAPSHOT, instrument_ticker=ticker, received_at=RECEIVED
    )


def market_from(path: str) -> KalshiMarket:
    return KalshiMarketEnvelope.model_validate(load_payload(path)).market


class TestSettlementKindFailsClosed:
    def test_binary_maps_to_binary(self):
        assert settlement_kind_for("binary") is SettlementKind.BINARY

    def test_scalar_maps_to_scalar(self):
        assert settlement_kind_for("scalar") is SettlementKind.SCALAR

    @pytest.mark.parametrize("raw", ["", "spread", "brand_new_type", None, "BINARY_V2"])
    def test_unrecognised_maps_to_unknown(self, raw):
        # A new venue value must never be silently treated as a standard binary.
        assert settlement_kind_for(raw) is SettlementKind.UNKNOWN

    def test_case_and_whitespace_tolerated(self):
        assert settlement_kind_for(" Binary ") is SettlementKind.BINARY


class TestRulesHash:
    def test_stable_for_same_input(self):
        assert rules_hash_for("a", "b") == rules_hash_for("a", "b")

    def test_changes_when_rules_change(self):
        assert rules_hash_for("a", "b") != rules_hash_for("a", "c")

    def test_moving_text_between_fields_changes_hash(self):
        # The NUL separator stops "ab"+"" colliding with "a"+"b".
        assert rules_hash_for("ab", "") != rules_hash_for("a", "b")

    def test_none_when_no_rules_text(self):
        assert rules_hash_for(None, None) is None


class TestNormalizeMarket:
    def test_identifiers_preserved(self):
        market = market_from("rest/market_linear_cent.json")
        instrument = normalize_market(market)
        assert instrument.ticker == market.ticker
        assert instrument.event_ticker == market.event_ticker
        assert instrument.venue is VenueId.KALSHI

    def test_exact_values_survive(self):
        market = market_from("rest/market_linear_cent.json")
        instrument = normalize_market(market)
        assert instrument.notional_value == market.notional_value_dollars
        assert instrument.yes_bid == market.yes_bid_dollars

    def test_settlement_metadata_preserved(self):
        instrument = normalize_market(market_from("rest/market_settled.json"))
        assert instrument.result_raw is not None
        assert instrument.settlement_value is not None
        assert instrument.status_raw

    def test_price_grid_built_from_ranges(self):
        instrument = normalize_market(market_from("rest/market_tapered_deci_cent.json"))
        assert instrument.price_grid is not None
        assert instrument.price_grid.structure_name == "tapered_deci_cent"
        assert len(instrument.price_grid.bands) == 3

    def test_rules_hash_recorded(self):
        instrument = normalize_market(market_from("rest/market_linear_cent.json"))
        assert instrument.rules_hash is not None
        assert len(instrument.rules_hash) == 64

    def test_normalisation_is_idempotent(self):
        market = market_from("rest/market_linear_cent.json")
        assert normalize_market(market) == normalize_market(market)

    def test_no_notional_is_represented_not_invented(self):
        market = KalshiMarket.model_validate(
            {"ticker": "X", "event_ticker": "E", "market_type": "binary", "status": "active"}
        )
        instrument = normalize_market(market)
        assert instrument.notional_value is None
        assert EXCLUSION_NO_NOTIONAL in instrument.exclusion_reasons


class TestExclusions:
    def test_multivariate_market_excluded(self):
        instrument = normalize_market(market_from("rest/market_center_deci_edge_centi_cent.json"))
        assert EXCLUSION_MULTIVARIATE in instrument.exclusion_reasons
        assert instrument.is_excluded

    def test_provisional_market_excluded(self):
        market = KalshiMarket.model_validate(
            {
                "ticker": "X",
                "event_ticker": "E",
                "market_type": "binary",
                "status": "active",
                "is_provisional": True,
            }
        )
        assert EXCLUSION_PROVISIONAL in normalize_market(market).exclusion_reasons

    def test_scalar_market_excluded(self):
        market = KalshiMarket.model_validate(
            {"ticker": "X", "event_ticker": "E", "market_type": "scalar", "status": "active"}
        )
        instrument = normalize_market(market)
        assert instrument.settlement_kind is SettlementKind.SCALAR
        assert EXCLUSION_NON_BINARY in instrument.exclusion_reasons

    def test_all_reasons_are_reported_not_just_the_first(self):
        market = KalshiMarket.model_validate(
            {
                "ticker": "X",
                "event_ticker": "E",
                "market_type": "scalar",
                "status": "active",
                "is_provisional": True,
            }
        )
        reasons = normalize_market(market).exclusion_reasons
        assert EXCLUSION_PROVISIONAL in reasons
        assert EXCLUSION_NON_BINARY in reasons

    def test_ordinary_binary_market_is_not_excluded(self):
        instrument = normalize_market(market_from("rest/market_linear_cent.json"))
        assert instrument.exclusion_reasons == ()
        assert not instrument.is_excluded


class TestNormalizeEventAndSeries:
    def test_mutually_exclusive_preserved_exactly(self):
        event = KalshiEventEnvelope.model_validate(
            load_payload("rest/event_mutually_exclusive.json")
        ).event
        normalized = normalize_event(event)
        assert normalized.mutually_exclusive is True

    def test_non_mutually_exclusive_preserved(self):
        event = KalshiEventEnvelope.model_validate(
            load_payload("rest/event_not_mutually_exclusive.json")
        ).event
        assert normalize_event(event).mutually_exclusive is False

    def test_collateral_return_type_kept_raw(self):
        event = KalshiEventEnvelope.model_validate(
            load_payload("rest/event_mutually_exclusive.json")
        ).event
        # MECNET semantics are unresolved (A-16), so it stays an opaque string.
        assert normalize_event(event).collateral_return_type == "MECNET"

    def test_normalisation_creates_no_relation(self):
        event = KalshiEventEnvelope.model_validate(
            load_payload("rest/event_mutually_exclusive.json")
        ).event
        normalized = normalize_event(event)
        # mutually_exclusive is evidence, never an automatic relation, and can
        # never imply EXACTLY_ONE.
        assert not hasattr(normalized, "relation")
        assert not hasattr(normalized, "relation_type")

    def test_series_fee_metadata_preserved(self):
        series = KalshiSeriesEnvelope.model_validate(
            load_payload("rest/series_KXHIGHNY.json")
        ).series
        normalized = normalize_series(series)
        assert normalized.fee_type_raw == "quadratic"
        assert series.fee_multiplier == Decimal(1)

    def test_settlement_sources_preserved(self):
        series = KalshiSeriesEnvelope.model_validate(
            load_payload("rest/series_KXHIGHNY.json")
        ).series
        normalized = normalize_series(series)
        assert normalized.settlement_source_names
        assert normalized.contract_terms_url


class TestNormalizeOrderbook:
    def test_levels_sorted_best_first(self):
        book = KalshiOrderbookEnvelope.model_validate(
            load_payload("rest/orderbook_linear_cent.json")
        ).orderbook_fp
        normalized = normalize_orderbook(book, source())
        yes_prices = [level.price.units for level in normalized.yes_bids]
        no_prices = [level.price.units for level in normalized.no_bids]
        assert yes_prices == sorted(yes_prices, reverse=True)
        assert no_prices == sorted(no_prices, reverse=True)

    def test_live_ascending_input_is_reordered(self):
        """Regression for the documented-vs-observed ordering discrepancy (A-07).

        The venue's docs say "best to worst"; live responses are ascending,
        which for a bid book is worst-to-best. Trusting the array order would
        present the worst price as the best.
        """
        book = KalshiOrderbookEnvelope.model_validate(
            load_payload("rest/orderbook_linear_cent.json")
        ).orderbook_fp
        wire_prices = [price.units for price, _ in book.no_dollars]
        assert wire_prices == sorted(wire_prices), "fixture should be ascending as observed live"
        normalized = normalize_orderbook(book, source())
        assert normalized.no_bids[0].price.units == max(wire_prices)
        assert normalized.best_no_bid is not None
        assert normalized.best_no_bid.price.units == max(wire_prices)

    def test_sides_are_tagged_correctly(self):
        book = KalshiOrderbookEnvelope.model_validate(
            load_payload("rest/orderbook_center_deci_edge_centi_cent.json")
        ).orderbook_fp
        normalized = normalize_orderbook(book, source())
        assert all(level.side is MarketSide.YES for level in normalized.yes_bids)
        assert all(level.side is MarketSide.NO for level in normalized.no_bids)

    def test_no_derived_levels_are_created(self):
        book = KalshiOrderbookEnvelope.model_validate(
            load_payload("rest/orderbook_linear_cent.json")
        ).orderbook_fp
        normalized = normalize_orderbook(book, source())
        assert all(not level.derived for level in normalized.yes_bids + normalized.no_bids)

    def test_empty_book(self):
        book = KalshiOrderbookEnvelope.model_validate(
            load_payload("rest/orderbook_empty.json")
        ).orderbook_fp
        normalized = normalize_orderbook(book, source())
        assert normalized.is_empty
        assert normalized.best_yes_bid is None

    def test_one_empty_side(self):
        book = KalshiOrderbook.model_validate(
            {"yes_dollars": [["0.4200", "5.00"]], "no_dollars": []}
        )
        normalized = normalize_orderbook(book, source())
        assert len(normalized.yes_bids) == 1
        assert normalized.no_bids == ()

    def test_zero_size_levels_dropped(self):
        book = KalshiOrderbook.model_validate(
            {"yes_dollars": [["0.4200", "0.00"], ["0.4100", "5.00"]], "no_dollars": []}
        )
        normalized = normalize_orderbook(book, source())
        assert [level.price.to_str() for level in normalized.yes_bids] == ["0.4100"]

    def test_duplicate_price_level_is_rejected(self):
        book = KalshiOrderbook.model_validate(
            {"yes_dollars": [["0.4200", "5.00"], ["0.4200", "3.00"]], "no_dollars": []}
        )
        with pytest.raises(ValueError, match="duplicate"):
            normalize_orderbook(book, source())

    def test_quantities_preserved_exactly(self):
        book = KalshiOrderbook.model_validate(
            {"yes_dollars": [["0.0001", "14.29"]], "no_dollars": []}
        )
        normalized = normalize_orderbook(book, source())
        assert normalized.yes_bids[0].quantity == Quantity.from_value("14.29")
        assert normalized.yes_bids[0].price == Price.from_value("0.0001")

    def test_provenance_preserved(self):
        book = KalshiOrderbookEnvelope.model_validate(
            load_payload("rest/orderbook_linear_cent.json")
        ).orderbook_fp
        normalized = normalize_orderbook(book, source("ABC"))
        assert normalized.source.instrument_ticker == "ABC"
        assert normalized.source.received_at == RECEIVED
        assert normalized.source.transport is BookTransport.REST_SNAPSHOT

    def test_idempotent(self):
        book = KalshiOrderbookEnvelope.model_validate(
            load_payload("rest/orderbook_linear_cent.json")
        ).orderbook_fp
        assert normalize_orderbook(book, source()) == normalize_orderbook(book, source())

    def test_total_depth_is_unchanged(self):
        book = KalshiOrderbookEnvelope.model_validate(
            load_payload("rest/orderbook_linear_cent.json")
        ).orderbook_fp
        normalized = normalize_orderbook(book, source())
        assert sum(q.units for _, q in book.no_dollars) == sum(
            level.quantity.units for level in normalized.no_bids
        )


class TestNormalizeWsSnapshot:
    def test_produces_the_same_shape_as_rest(self):
        snapshot = KalshiWsEnvelope.model_validate(
            load_payload("websocket/synthetic_orderbook_snapshot.json")
        ).as_snapshot()
        normalized = normalize_ws_snapshot(snapshot, source("FED-23DEC-T3.00"))
        # Documented snapshot is ascending; best NO bid is 0.5600, not 0.5400.
        assert normalized.best_no_bid is not None
        assert normalized.best_no_bid.price == Price.from_value("0.5600")
        assert normalized.best_yes_bid is not None
        assert normalized.best_yes_bid.price == Price.from_value("0.2200")
