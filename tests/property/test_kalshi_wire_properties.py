"""Property tests for the Kalshi wire boundary.

These assert the invariants the whole pipeline leans on: that a value written
by the venue survives parsing unchanged, and that the price grid behaves like a
grid for every representable price rather than only the ones a fixture happened
to contain.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from predarb.books.levels import BookLevel, BookSource, BookTransport, sort_levels_best_first
from predarb.domain.enums import MarketSide
from predarb.domain.models import PriceBand, PriceGrid
from predarb.domain.money import InexactValueError, Price, Quantity, QuantityDelta
from predarb.venues.kalshi.fixed_point import (
    parse_price_dollars,
    parse_quantity_delta_fp,
    parse_quantity_fp,
)
from predarb.venues.kalshi.models import KalshiOrderbook
from predarb.venues.kalshi.normalize import normalize_orderbook

pytestmark = pytest.mark.property

price_units = st.integers(min_value=0, max_value=10_000)
quantity_units = st.integers(min_value=0, max_value=100_000_000)
delta_units = st.integers(min_value=-100_000_000, max_value=100_000_000)


def band(start: str, end: str, step: str) -> PriceBand:
    return PriceBand(
        start=Price.from_value(start), end=Price.from_value(end), step=Price.from_value(step)
    )


GRIDS = [
    PriceGrid(bands=(band("0.0000", "1.0000", "0.0100"),), structure_name="linear_cent"),
    PriceGrid(
        bands=(
            band("0.0000", "0.1000", "0.0010"),
            band("0.1000", "0.9000", "0.0100"),
            band("0.9000", "1.0000", "0.0010"),
        ),
        structure_name="tapered_deci_cent",
    ),
    PriceGrid(
        bands=(
            band("0.0000", "0.0100", "0.0001"),
            band("0.0100", "0.9900", "0.0010"),
            band("0.9900", "1.0000", "0.0001"),
        ),
        structure_name="center_deci_edge_centi_cent",
    ),
]


def _valid_units(grid: PriceGrid) -> set[int]:
    """Every on-grid price for a grid, as raw units."""
    return {
        units for b in grid.bands for units in range(b.start.units, b.end.units + 1, b.step.units)
    }


class TestWireRoundTrip:
    @given(price_units)
    def test_every_price_round_trips_through_the_wire_format(self, units):
        wire = Price.from_units(units).to_str()
        assert parse_price_dollars(wire).units == units

    @given(quantity_units)
    def test_every_quantity_round_trips(self, units):
        wire = Quantity.from_units(units).to_str()
        assert parse_quantity_fp(wire).units == units

    @given(delta_units)
    def test_every_delta_round_trips(self, units):
        wire = QuantityDelta.from_units(units).to_str()
        assert parse_quantity_delta_fp(wire).units == units

    @given(price_units)
    def test_parsed_price_equals_decimal_value_of_the_string(self, units):
        wire = Price.from_units(units).to_str()
        assert parse_price_dollars(wire).as_decimal() == Decimal(wire)

    @given(st.integers(min_value=0, max_value=10_000))
    def test_parsing_never_agrees_with_a_corrupting_float_path(self, units):
        """Where the float path differs, the exact parser must be the right one."""
        wire = Price.from_units(units).to_str()
        assert parse_price_dollars(wire).units == units
        naive = int(float(wire) * 10_000)
        assert naive in (units, units - 1)  # float either matches or under-counts


class TestPrecisionRejection:
    @given(st.integers(min_value=0, max_value=99_999))
    def test_five_decimal_prices_are_rejected(self, raw):
        value = f"0.{raw:05d}"
        assume(not value.endswith("0"))  # otherwise it is really a 4 dp value
        with pytest.raises(InexactValueError):
            parse_price_dollars(value)

    @given(st.integers(min_value=0, max_value=999))
    def test_three_decimal_quantities_are_rejected(self, raw):
        value = f"0.{raw:03d}"
        assume(not value.endswith("0"))
        with pytest.raises(InexactValueError):
            parse_quantity_fp(value)


class TestPriceGridProperties:
    @pytest.mark.parametrize("grid", GRIDS)
    @given(price_units)
    def test_snap_down_is_valid_and_not_above_input(self, grid, units):
        price = Price.from_units(units)
        snapped = grid.snap_down(price)
        assert snapped is not None
        assert snapped <= price
        assert grid.is_valid_price(snapped)

    @pytest.mark.parametrize("grid", GRIDS)
    @given(price_units)
    def test_snap_up_is_valid_and_not_below_input(self, grid, units):
        price = Price.from_units(units)
        snapped = grid.snap_up(price)
        assert snapped is not None
        assert snapped >= price
        assert grid.is_valid_price(snapped)

    @pytest.mark.parametrize("grid", GRIDS)
    @given(st.data())
    def test_snapping_is_identity_on_valid_prices(self, grid, data):
        # Valid prices are drawn from the grid rather than filtered out of the
        # full range: on the linear-cent grid only 101 of 10,001 prices are
        # valid, so filtering would discard ~99% of generated inputs.
        valid = sorted(_valid_units(grid))
        price = Price.from_units(data.draw(st.sampled_from(valid)))
        assert grid.snap_down(price) == price
        assert grid.snap_up(price) == price

    @pytest.mark.parametrize("grid", GRIDS)
    @given(price_units)
    def test_no_valid_price_lies_strictly_between_the_snaps(self, grid, units):
        price = Price.from_units(units)
        low, high = grid.snap_down(price), grid.snap_up(price)
        assert low is not None and high is not None
        for candidate in range(low.units + 1, high.units):
            assert not grid.is_valid_price(Price.from_units(candidate))

    @pytest.mark.parametrize("grid", GRIDS)
    def test_observed_grids_are_internally_consistent(self, grid):
        assert grid.has_consistent_boundaries()
        assert grid.is_contiguous()


class TestOrderbookNormalisationProperties:
    @given(
        st.lists(
            st.tuples(price_units, st.integers(min_value=1, max_value=1_000_000)),
            min_size=0,
            max_size=40,
            unique_by=lambda pair: pair[0],
        )
    )
    def test_sorting_is_descending_and_lossless(self, raw_levels):
        levels = [
            BookLevel(
                price=Price.from_units(p),
                quantity=Quantity.from_units(q),
                side=MarketSide.YES,
            )
            for p, q in raw_levels
        ]
        ordered = sort_levels_best_first(levels)
        assert len(ordered) == len(levels)
        assert [x.price.units for x in ordered] == sorted(
            [x.price.units for x in levels], reverse=True
        )
        assert sum(x.quantity.units for x in ordered) == sum(x.quantity.units for x in levels)

    @given(
        st.lists(
            st.tuples(price_units, st.integers(min_value=1, max_value=100_000)),
            min_size=1,
            max_size=25,
            unique_by=lambda pair: pair[0],
        )
    )
    def test_wire_order_never_affects_the_result(self, raw_levels):
        """Shuffling the venue's array must not change the normalised book.

        This is the structural defence against A-07: whichever ordering Kalshi
        emits, the best bid we compute is the same.
        """
        ascending = sorted(raw_levels)
        descending = list(reversed(ascending))

        def build(pairs: list[tuple[int, int]]) -> KalshiOrderbook:
            return KalshiOrderbook.model_validate(
                {
                    "yes_dollars": [
                        [Price.from_units(p).to_str(), Quantity.from_units(q).to_str()]
                        for p, q in pairs
                    ],
                    "no_dollars": [],
                }
            )

        source = BookSource(
            transport=BookTransport.REST_SNAPSHOT,
            instrument_ticker="X",
            received_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        first = normalize_orderbook(build(ascending), source)
        second = normalize_orderbook(build(descending), source)
        assert first == second
        assert first.best_yes_bid is not None
        assert first.best_yes_bid.price.units == max(p for p, _ in raw_levels)
