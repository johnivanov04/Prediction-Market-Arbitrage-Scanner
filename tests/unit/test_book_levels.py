"""Tests for normalised book levels, ordering and the quoted/derived split."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from predarb.books.levels import (
    BookLevel,
    BookSource,
    BookTransport,
    QuotedBook,
    sort_levels_best_first,
)
from predarb.domain.enums import MarketSide
from predarb.domain.money import Price, Quantity, QuantityDelta

pytestmark = pytest.mark.unit

RECEIVED = datetime(2026, 9, 15, 12, 0, 0, tzinfo=UTC)


def level(
    price: str,
    qty: str,
    side: MarketSide = MarketSide.YES,
    *,
    derived: bool = False,
    derived_from_price: Price | None = None,
) -> BookLevel:
    return BookLevel(
        price=Price.from_value(price),
        quantity=Quantity.from_value(qty),
        side=side,
        derived=derived,
        derived_from_price=derived_from_price,
    )


def src() -> BookSource:
    return BookSource(
        transport=BookTransport.REST_SNAPSHOT, instrument_ticker="X", received_at=RECEIVED
    )


class TestSorting:
    def test_sorts_descending_by_price(self):
        levels = [level("0.0100", "1.00"), level("0.5000", "2.00"), level("0.2500", "3.00")]
        ordered = sort_levels_best_first(levels)
        assert [x.price.to_str() for x in ordered] == ["0.5000", "0.2500", "0.0100"]

    def test_already_sorted_input_unchanged(self):
        levels = [level("0.5000", "1.00"), level("0.1000", "1.00")]
        assert sort_levels_best_first(levels) == tuple(levels)

    def test_empty(self):
        assert sort_levels_best_first([]) == ()


class TestQuotedBookInvariants:
    def test_rejects_ascending_levels(self):
        # Guards the A-07 inversion: ascending input must be sorted first.
        with pytest.raises(ValueError, match="descending"):
            QuotedBook(
                source=src(),
                yes_bids=(level("0.1000", "1.00"), level("0.5000", "1.00")),
                no_bids=(),
            )

    def test_rejects_duplicate_prices(self):
        with pytest.raises(ValueError, match="duplicate"):
            QuotedBook(
                source=src(),
                yes_bids=(level("0.5000", "1.00"), level("0.5000", "2.00")),
                no_bids=(),
            )

    def test_rejects_wrong_side_in_a_bucket(self):
        with pytest.raises(ValueError, match="contains a NO level"):
            QuotedBook(source=src(), yes_bids=(level("0.5000", "1.00", MarketSide.NO),), no_bids=())

    def test_rejects_derived_levels(self):
        derived = level(
            "0.5000", "1.00", derived=True, derived_from_price=Price.from_value("0.5000")
        )
        with pytest.raises(ValueError, match="quotes only"):
            QuotedBook(source=src(), yes_bids=(derived,), no_bids=())

    def test_best_bids(self):
        book = QuotedBook(
            source=src(),
            yes_bids=(level("0.5000", "1.00"), level("0.4000", "2.00")),
            no_bids=(level("0.4500", "3.00", MarketSide.NO),),
        )
        assert book.best_yes_bid is not None
        assert book.best_yes_bid.price.to_str() == "0.5000"
        assert book.best_no_bid is not None
        assert book.best_no_bid.price.to_str() == "0.4500"
        assert not book.is_empty

    def test_empty_book(self):
        book = QuotedBook(source=src(), yes_bids=(), no_bids=())
        assert book.is_empty
        assert book.best_yes_bid is None
        assert book.best_no_bid is None


class TestDerivedProvenance:
    def test_derived_level_must_record_its_source(self):
        # Losing this link would let derived depth be double-counted against
        # the quoted level it came from.
        with pytest.raises(ValueError, match="must record the quoted price"):
            BookLevel(
                price=Price.from_value("0.4400"),
                quantity=Quantity.from_value("1.00"),
                side=MarketSide.YES,
                derived=True,
            )

    def test_quoted_level_must_not_claim_a_source(self):
        with pytest.raises(ValueError, match="must not claim a derivation source"):
            BookLevel(
                price=Price.from_value("0.4400"),
                quantity=Quantity.from_value("1.00"),
                side=MarketSide.YES,
                derived=False,
                derived_from_price=Price.from_value("0.5600"),
            )

    def test_valid_derived_level(self):
        derived = BookLevel(
            price=Price.from_value("0.4400"),
            quantity=Quantity.from_value("1.00"),
            side=MarketSide.YES,
            derived=True,
            derived_from_price=Price.from_value("0.5600"),
        )
        assert derived.derived
        assert derived.derived_from_price == Price.from_value("0.5600")


class TestBookSource:
    def test_absent_exchange_timestamp_preserved_as_none(self):
        assert src().exchange_ts is None

    def test_sequence_and_subscription_recorded(self):
        source = BookSource(
            transport=BookTransport.WS_SNAPSHOT,
            instrument_ticker="X",
            received_at=RECEIVED,
            sequence=42,
            subscription_id=2,
        )
        assert source.sequence == 42
        assert source.subscription_id == 2


class TestQuantityDelta:
    def test_applies_positive_delta(self):
        assert QuantityDelta.from_value("5.00").apply_to(Quantity.from_value("10.00")).to_str() == (
            "15.00"
        )

    def test_applies_negative_delta(self):
        assert QuantityDelta.from_value("-4.00").apply_to(
            Quantity.from_value("10.00")
        ).to_str() == ("6.00")

    def test_depletes_level_to_zero(self):
        result = QuantityDelta.from_value("-20.00").apply_to(Quantity.from_value("20.00"))
        assert result.is_zero

    def test_negative_result_is_an_error_not_a_clamp(self):
        # Clamping would hide a missed message; the book state is genuinely
        # inconsistent and must surface.
        with pytest.raises(ValueError, match="inconsistent"):
            QuantityDelta.from_value("-30.00").apply_to(Quantity.from_value("20.00"))

    def test_fractional_delta_is_exact(self):
        result = QuantityDelta.from_value("0.29").apply_to(Quantity.from_value("0.00"))
        assert result.to_str() == "0.29"
