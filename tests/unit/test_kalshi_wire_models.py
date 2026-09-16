"""Tests for the Kalshi wire models and the forward-compatibility policy."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from pydantic import BaseModel, ValidationError

from predarb.domain.money import Price, Quantity, QuantityDelta
from predarb.venues.kalshi.models import (
    KalshiEventEnvelope,
    KalshiEventFeeChangesResponse,
    KalshiEventsPage,
    KalshiExchangeStatus,
    KalshiMarket,
    KalshiMarketEnvelope,
    KalshiMarketsPage,
    KalshiOrderbookEnvelope,
    KalshiSeriesEnvelope,
    KalshiSeriesFeeChangesResponse,
    KalshiWsEnvelope,
    decode_json,
    unknown_top_level_fields,
)
from tests.conftest import load_payload, load_raw

pytestmark = pytest.mark.unit

REST_MODELS: dict[str, type[BaseModel]] = {
    "rest/series_KXHIGHNY.json": KalshiSeriesEnvelope,
    "rest/event_mutually_exclusive.json": KalshiEventEnvelope,
    "rest/event_not_mutually_exclusive.json": KalshiEventEnvelope,
    "rest/events_list.json": KalshiEventsPage,
    "rest/markets_list.json": KalshiMarketsPage,
    "rest/market_linear_cent.json": KalshiMarketEnvelope,
    "rest/market_tapered_deci_cent.json": KalshiMarketEnvelope,
    "rest/market_center_deci_edge_centi_cent.json": KalshiMarketEnvelope,
    "rest/market_settled.json": KalshiMarketEnvelope,
    "rest/orderbook_linear_cent.json": KalshiOrderbookEnvelope,
    "rest/orderbook_tapered_deci_cent.json": KalshiOrderbookEnvelope,
    "rest/orderbook_center_deci_edge_centi_cent.json": KalshiOrderbookEnvelope,
    "rest/orderbook_empty.json": KalshiOrderbookEnvelope,
    "rest/series_fee_changes.json": KalshiSeriesFeeChangesResponse,
    "rest/series_fee_changes_empty.json": KalshiSeriesFeeChangesResponse,
    "rest/events_fee_changes.json": KalshiEventFeeChangesResponse,
    "rest/exchange_status.json": KalshiExchangeStatus,
}


class TestRealFixturesParse:
    @pytest.mark.parametrize(("path", "model"), list(REST_MODELS.items()))
    def test_every_captured_fixture_parses(self, path, model):
        assert model.model_validate(load_payload(path)) is not None


class TestDecoding:
    def test_decode_json_keeps_numbers_exact(self):
        payload = decode_json(b'{"fee_multiplier": 0.5, "floor_strike": 70.5}')
        assert isinstance(payload["fee_multiplier"], Decimal)
        assert payload["fee_multiplier"] == Decimal("0.5")
        assert isinstance(payload["floor_strike"], Decimal)

    def test_plain_json_loads_would_produce_floats(self):
        # Demonstrates exactly what decode_json exists to prevent.
        assert isinstance(json.loads('{"fee_multiplier": 0.5}')["fee_multiplier"], float)

    def test_float_strike_rejected_when_decoded_carelessly(self):
        careless = json.loads(
            '{"ticker":"X","event_ticker":"E","market_type":"binary","status":"active",'
            '"floor_strike": 70.5}'
        )
        with pytest.raises(ValidationError, match="float"):
            KalshiMarket.model_validate(careless)


class TestForwardCompatibility:
    """Unknown fields tolerated; malformed known financial fields fatal."""

    def test_unknown_field_does_not_break_parsing(self):
        payload = dict(load_payload("rest/market_linear_cent.json"))
        payload["market"] = {**payload["market"], "brand_new_kalshi_field": {"nested": [1, 2]}}
        assert KalshiMarketEnvelope.model_validate(payload).market.ticker

    def test_unknown_fields_are_reported_for_observability(self):
        payload = load_payload("rest/events_list.json")
        unknown = unknown_top_level_fields(KalshiEventsPage, payload)
        # 'milestones' is genuinely present in the live response and is not
        # modelled: ignored, but visible rather than silently dropped.
        assert "milestones" in unknown

    def test_no_unknown_top_level_fields_on_modelled_payloads(self):
        for path in (
            "rest/series_KXHIGHNY.json",
            "rest/market_linear_cent.json",
            "rest/orderbook_linear_cent.json",
        ):
            model = REST_MODELS[path]
            assert unknown_top_level_fields(model, load_payload(path)) == ()

    def test_undocumented_fee_type_is_accepted(self):
        # margin_market_maker_program_fees is absent from the documented enum
        # but appears on 24 live series. A closed enum would crash ingestion.
        response = KalshiSeriesFeeChangesResponse.model_validate(
            load_payload("rest/series_fee_changes.json")
        )
        observed = {change.fee_type for change in response.series_fee_change_arr}
        assert "margin_market_maker_program_fees" in observed

    def test_unknown_market_status_is_accepted(self):
        payload = dict(load_payload("rest/market_linear_cent.json"))
        payload["market"] = {**payload["market"], "status": "some_new_status"}
        assert KalshiMarketEnvelope.model_validate(payload).market.status == "some_new_status"

    @pytest.mark.parametrize("bad", ["not-a-price", "0.12345", "-0.5000", 0.42, 42, True])
    def test_malformed_price_is_fatal(self, bad):
        payload = dict(load_payload("rest/market_linear_cent.json"))
        payload["market"] = {**payload["market"], "yes_bid_dollars": bad}
        with pytest.raises(ValidationError):
            KalshiMarketEnvelope.model_validate(payload)

    @pytest.mark.parametrize("bad", ["1.001", "-1.00", 13, 13.0])
    def test_malformed_quantity_is_fatal(self, bad):
        payload = dict(load_payload("rest/market_linear_cent.json"))
        payload["market"] = {**payload["market"], "yes_bid_size_fp": bad}
        with pytest.raises(ValidationError):
            KalshiMarketEnvelope.model_validate(payload)

    def test_missing_required_identifier_is_fatal(self):
        with pytest.raises(ValidationError):
            KalshiMarket.model_validate({"event_ticker": "E", "market_type": "binary"})

    def test_missing_optional_quote_stays_none(self):
        market = KalshiMarket.model_validate(
            {"ticker": "X", "event_ticker": "E", "market_type": "binary", "status": "active"}
        )
        # Absent must not become zero: a market with no bid is not bidding $0.
        assert market.yes_bid_dollars is None
        assert market.yes_bid_size_fp is None
        assert market.notional_value_dollars is None


class TestExactValuesSurviveParsing:
    def test_market_prices_are_exact_types(self):
        market = KalshiMarketEnvelope.model_validate(
            load_payload("rest/market_linear_cent.json")
        ).market
        assert isinstance(market.notional_value_dollars, Price)
        assert market.notional_value_dollars.to_str() == "1.0000"

    def test_serialisation_returns_the_wire_string(self):
        market = KalshiMarketEnvelope.model_validate(
            load_payload("rest/market_linear_cent.json")
        ).market
        dumped = market.model_dump(mode="json")
        assert dumped["notional_value_dollars"] == "1.0000"

    def test_fee_multiplier_is_decimal_not_float(self):
        series = KalshiSeriesEnvelope.model_validate(
            load_payload("rest/series_KXHIGHNY.json")
        ).series
        assert isinstance(series.fee_multiplier, Decimal)


class TestOrderbookWireModel:
    def test_yes_and_no_bids_parse(self):
        book = KalshiOrderbookEnvelope.model_validate(
            load_payload("rest/orderbook_center_deci_edge_centi_cent.json")
        ).orderbook_fp
        assert book.yes_dollars == ((Price.from_value("0.2180"), Quantity.from_value("10.00")),)
        assert book.no_dollars == ((Price.from_value("0.7410"), Quantity.from_value("10.00")),)

    def test_empty_book_parses(self):
        book = KalshiOrderbookEnvelope.model_validate(
            load_payload("rest/orderbook_empty.json")
        ).orderbook_fp
        assert book.yes_dollars == ()
        assert book.no_dollars == ()

    def test_fractional_quantities_preserved(self):
        book = KalshiOrderbookEnvelope.model_validate(
            load_payload("rest/orderbook_linear_cent.json")
        ).orderbook_fp
        quantities = [q.to_str() for _, q in book.no_dollars]
        assert any("." in q and not q.endswith(".00") for q in quantities)

    def test_sub_cent_prices_preserved(self):
        # A sub-cent price is one off the whole-cent grid, i.e. not a multiple
        # of 100 price units. "0.0010" qualifies and would be destroyed by any
        # cent-based representation.
        book = KalshiOrderbookEnvelope.model_validate(
            load_payload("rest/orderbook_tapered_deci_cent.json")
        ).orderbook_fp
        sub_cent = [p.to_str() for p, _ in book.no_dollars if p.units % 100 != 0]
        assert sub_cent, "expected at least one sub-cent level in the tapered book"

    def test_no_asks_are_invented(self):
        book = KalshiOrderbookEnvelope.model_validate(
            load_payload("rest/orderbook_linear_cent.json")
        ).orderbook_fp
        assert not hasattr(book, "yes_asks")
        assert set(type(book).model_fields) == {"yes_dollars", "no_dollars"}


class TestWebSocketModels:
    def test_snapshot_parses(self):
        envelope = KalshiWsEnvelope.model_validate(
            load_payload("websocket/synthetic_orderbook_snapshot.json")
        )
        assert envelope.type == "orderbook_snapshot"
        assert envelope.sid == 2
        assert envelope.seq == 2
        snapshot = envelope.as_snapshot()
        assert snapshot.market_ticker == "FED-23DEC-T3.00"
        assert snapshot.yes_dollars_fp[0] == (
            Price.from_value("0.0800"),
            Quantity.from_value("300.00"),
        )

    def test_delta_parses_with_negative_value(self):
        envelope = KalshiWsEnvelope.model_validate(
            load_payload("websocket/synthetic_orderbook_delta_yes_negative.json")
        )
        delta = envelope.as_delta()
        assert delta.delta_fp == QuantityDelta.from_value("-54.00")
        assert delta.side == "yes"
        assert delta.price_dollars == Price.from_value("0.9600")

    def test_delta_positive_on_no_side(self):
        delta = KalshiWsEnvelope.model_validate(
            load_payload("websocket/synthetic_orderbook_delta_no_positive.json")
        ).as_delta()
        assert delta.delta_fp.units == 1250
        assert delta.side == "no"

    def test_fractional_delta(self):
        delta = KalshiWsEnvelope.model_validate(
            load_payload("websocket/synthetic_orderbook_delta_fractional.json")
        ).as_delta()
        assert delta.delta_fp.to_str() == "14.29"
        assert delta.price_dollars.to_str() == "0.0001"

    def test_sequence_and_sid_preserved_exactly(self):
        for name, seq in (
            ("websocket/synthetic_orderbook_snapshot.json", 2),
            ("websocket/synthetic_orderbook_delta_yes_negative.json", 3),
            ("websocket/synthetic_orderbook_delta_no_positive.json", 4),
            ("websocket/synthetic_orderbook_delta_level_depleted.json", 6),
        ):
            envelope = KalshiWsEnvelope.model_validate(load_payload(name))
            assert envelope.seq == seq
            assert envelope.sid == 2

    def test_timestamp_preserved(self):
        delta = KalshiWsEnvelope.model_validate(
            load_payload("websocket/synthetic_orderbook_delta_yes_negative.json")
        ).as_delta()
        assert delta.ts_ms == 1669149841000

    def test_absent_timestamp_stays_none(self):
        # Kalshi omits ts_ms on some messages; that absence is information.
        delta = KalshiWsEnvelope.model_validate(
            load_payload("websocket/synthetic_orderbook_delta_no_timestamp.json")
        ).as_delta()
        assert delta.ts_ms is None

    def test_error_frame_parses(self):
        envelope = KalshiWsEnvelope.model_validate(load_payload("websocket/synthetic_error.json"))
        error = envelope.as_error()
        assert error.code == 2
        assert envelope.id == 123

    def test_subscribed_ack_parses(self):
        envelope = KalshiWsEnvelope.model_validate(
            load_payload("websocket/synthetic_subscribed_ack.json")
        )
        assert envelope.type == "subscribed"
        assert envelope.id == 1

    def test_wrong_frame_type_is_refused(self):
        envelope = KalshiWsEnvelope.model_validate(
            load_payload("websocket/synthetic_orderbook_snapshot.json")
        )
        with pytest.raises(ValueError, match="expected a 'orderbook_delta' frame"):
            envelope.as_delta()

    def test_frame_without_msg_is_refused(self):
        envelope = KalshiWsEnvelope.model_validate({"type": "orderbook_delta", "sid": 1, "seq": 1})
        with pytest.raises(ValueError, match="no msg body"):
            envelope.as_delta()

    def test_malformed_delta_is_fatal(self):
        envelope = KalshiWsEnvelope.model_validate(
            {
                "type": "orderbook_delta",
                "sid": 1,
                "seq": 1,
                "msg": {
                    "market_ticker": "X",
                    "price_dollars": "0.12345",
                    "delta_fp": "1.00",
                    "side": "yes",
                },
            }
        )
        with pytest.raises(ValidationError):
            envelope.as_delta()

    def test_seq_is_not_interpreted(self):
        # Scoping is unverified (A-09); the wire layer must not assume one.
        envelope = KalshiWsEnvelope.model_validate({"type": "x", "seq": 99, "sid": 7})
        assert envelope.seq == 99
        assert envelope.sid == 7


class TestRawByteFidelity:
    def test_fixtures_are_stored_unmodified(self):
        # Raw-byte regression depends on the files not being pretty-printed.
        raw = load_raw("rest/orderbook_empty.json")
        assert raw == b'{"orderbook_fp":{"no_dollars":[],"yes_dollars":[]}}'
