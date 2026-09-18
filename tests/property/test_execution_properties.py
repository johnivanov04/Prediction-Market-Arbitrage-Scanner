"""Property tests for executable depth and cost.

The laws here are the ones every later layer will lean on without checking:
that the curve is ordered, that cost is monotone, and that VWAP and gross cost
agree exactly. If any of these can be broken, a profitability number computed
downstream can be wrong in a way no example test would reveal.
"""

from __future__ import annotations

from datetime import UTC, datetime
from fractions import Fraction

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from predarb.books.execution import ExecutionContext, ExecutionCurve, build_execution_curve
from predarb.books.levels import BookLevel
from predarb.books.multileg import common_fillable_quantity, multi_leg_costs
from predarb.books.orderbook import BookView
from predarb.books.state import BookIntegrity, BookProvenance
from predarb.domain.enums import MarketSide, SettlementKind, VenueId
from predarb.domain.models import VenueInstrument
from predarb.domain.money import Money, Price, Quantity

pytestmark = pytest.mark.property

T0 = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)
NOTIONAL = Price.from_value("1.0000")
CONTEXT = ExecutionContext(current_connection_epoch=1)
SETTINGS = settings(max_examples=60)

# Bid prices strictly inside (0, notional), so every complement is a real price.
bid_price_units = st.integers(min_value=1, max_value=9_999)
quantity_units = st.integers(min_value=1, max_value=500_000)

bid_book = st.lists(
    st.tuples(bid_price_units, quantity_units),
    min_size=0,
    max_size=12,
    unique_by=lambda pair: pair[0],
)


def make_instrument(ticker: str = "MKT") -> VenueInstrument:
    return VenueInstrument(
        venue=VenueId.KALSHI,
        ticker=ticker,
        event_ticker="EVT",
        title="",
        status_raw="active",
        settlement_kind=SettlementKind.BINARY,
        market_type_raw="binary",
        notional_value=NOTIONAL,
        price_grid=None,
        yes_sub_title=None,
        no_sub_title=None,
        rules_primary=None,
        rules_secondary=None,
        rules_hash=None,
        open_time=None,
        close_time=None,
        expected_expiration_time=None,
        latest_expiration_time=None,
        settlement_timer_seconds=None,
        result_raw=None,
        settlement_value=None,
        can_close_early=None,
        yes_bid=None,
        yes_ask=None,
        no_bid=None,
        no_ask=None,
        yes_bid_size=None,
        yes_ask_size=None,
    )


def make_curve(bids: list[tuple[int, int]], ticker: str = "MKT") -> ExecutionCurve:
    """A BUY YES curve derived from the given NO bids (best bid first)."""
    levels = tuple(
        BookLevel(
            price=Price.from_units(price),
            quantity=Quantity.from_units(quantity),
            side=MarketSide.NO,
        )
        for price, quantity in sorted(bids, reverse=True)
    )
    view = BookView(
        market_ticker=ticker,
        integrity=BookIntegrity.VALID,
        yes_bids=(),
        no_bids=levels,
        provenance=BookProvenance(connection_epoch=1, sid=1, latest_seq=5),
    )
    return build_execution_curve(view, make_instrument(ticker), CONTEXT, MarketSide.YES, at=T0)


class TestCurveOrdering:
    @SETTINGS
    @given(bid_book)
    def test_executable_asks_are_cheapest_first(self, bids: list[tuple[int, int]]) -> None:
        curve = make_curve(bids)
        prices = [level.execution_price.units for level in curve.levels]
        assert prices == sorted(prices)

    @SETTINGS
    @given(bid_book)
    def test_source_order_does_not_matter(self, bids: list[tuple[int, int]]) -> None:
        """Shuffling the source book must not change the curve."""
        assume(bids)
        ascending = make_curve(sorted(bids))
        descending = make_curve(sorted(bids, reverse=True))
        assert [lv.execution_price.units for lv in ascending.levels] == [
            lv.execution_price.units for lv in descending.levels
        ]

    @SETTINGS
    @given(bid_book)
    def test_every_ask_is_within_bounds(self, bids: list[tuple[int, int]]) -> None:
        curve = make_curve(bids)
        for level in curve.levels:
            assert 0 <= level.execution_price.units <= NOTIONAL.units

    @SETTINGS
    @given(bid_book)
    def test_complement_is_exact(self, bids: list[tuple[int, int]]) -> None:
        curve = make_curve(bids)
        for level in curve.levels:
            assert level.execution_price.units + level.source_bid_price.units == NOTIONAL.units


class TestFillLaws:
    @SETTINGS
    @given(bid_book, quantity_units)
    def test_filled_never_exceeds_requested(
        self, bids: list[tuple[int, int]], requested: int
    ) -> None:
        quote = make_curve(bids).quote_up_to(Quantity.from_units(requested))
        assert quote.filled_quantity.units <= requested

    @SETTINGS
    @given(bid_book, quantity_units)
    def test_filled_never_exceeds_depth(self, bids: list[tuple[int, int]], requested: int) -> None:
        curve = make_curve(bids)
        quote = curve.quote_up_to(Quantity.from_units(requested))
        assert quote.filled_quantity.units <= curve.max_fillable_quantity.units

    @SETTINGS
    @given(bid_book, quantity_units)
    def test_filled_plus_unfilled_is_requested(
        self, bids: list[tuple[int, int]], requested: int
    ) -> None:
        quote = make_curve(bids).quote_up_to(Quantity.from_units(requested))
        assert quote.filled_quantity.units + quote.unfilled_quantity.units == requested

    @SETTINGS
    @given(bid_book, quantity_units)
    def test_gross_cost_is_the_sum_of_slices(
        self, bids: list[tuple[int, int]], requested: int
    ) -> None:
        quote = make_curve(bids).quote_up_to(Quantity.from_units(requested))
        assert quote.gross_cost == Money.from_units(
            sum(slice_.gross_cost.units for slice_ in quote.slices)
        )

    @SETTINGS
    @given(bid_book, quantity_units)
    def test_slices_never_exceed_their_level(
        self, bids: list[tuple[int, int]], requested: int
    ) -> None:
        curve = make_curve(bids)
        available = {level.liquidity: level.available_quantity.units for level in curve.levels}
        quote = curve.quote_up_to(Quantity.from_units(requested))
        for slice_ in quote.slices:
            assert slice_.quantity.units <= available[slice_.liquidity]


class TestCostLaws:
    @SETTINGS
    @given(bid_book, st.integers(min_value=0, max_value=500_000))
    def test_cost_is_non_decreasing_in_quantity(
        self, bids: list[tuple[int, int]], units: int
    ) -> None:
        curve = make_curve(bids)
        depth = curve.max_fillable_quantity.units
        assume(depth > 0)
        first = min(units, depth)
        second = min(units + 1, depth)
        assert curve.gross_cost(Quantity.from_units(first)) <= curve.gross_cost(
            Quantity.from_units(second)
        )

    @SETTINGS
    @given(bid_book)
    def test_cost_at_breakpoints_matches_cumulative(self, bids: list[tuple[int, int]]) -> None:
        curve = make_curve(bids)
        for point in curve.breakpoints:
            assert curve.gross_cost(point.cumulative_quantity) == point.cumulative_cost

    @SETTINGS
    @given(bid_book)
    def test_marginal_price_never_improves(self, bids: list[tuple[int, int]]) -> None:
        """Consuming cheap depth can only make the next contract dearer."""
        curve = make_curve(bids)
        prices = [point.marginal_price.units for point in curve.breakpoints]
        assert prices == sorted(prices)

    @SETTINGS
    @given(bid_book)
    def test_full_depth_cost_equals_summed_levels(self, bids: list[tuple[int, int]]) -> None:
        curve = make_curve(bids)
        assume(not curve.is_empty)
        expected = sum(level.full_cost.units for level in curve.levels)
        assert curve.gross_cost(curve.max_fillable_quantity) == Money.from_units(expected)


class TestVwapLaws:
    @SETTINGS
    @given(bid_book, quantity_units)
    def test_vwap_times_quantity_recovers_cost_exactly(
        self, bids: list[tuple[int, int]], requested: int
    ) -> None:
        """No rounding anywhere: the ratio is exact, so the product is too."""
        quote = make_curve(bids).quote_up_to(Quantity.from_units(requested))
        assume(not quote.filled_quantity.is_zero)
        assert quote.vwap is not None
        assert quote.vwap.cost_for(quote.filled_quantity) == Fraction(
            quote.gross_cost.units, 1_000_000
        )

    @SETTINGS
    @given(bid_book, quantity_units)
    def test_vwap_lies_between_best_and_worst_fill_price(
        self, bids: list[tuple[int, int]], requested: int
    ) -> None:
        quote = make_curve(bids).quote_up_to(Quantity.from_units(requested))
        assume(quote.slices)
        assert quote.best_execution_price is not None
        assert quote.worst_execution_price is not None
        ratio = quote.vwap.ratio if quote.vwap else Fraction(0)
        assert Fraction(quote.best_execution_price.units, 10_000) <= ratio
        assert ratio <= Fraction(quote.worst_execution_price.units, 10_000)


class TestDeterminism:
    @SETTINGS
    @given(bid_book, quantity_units)
    def test_same_book_gives_the_same_quote(
        self, bids: list[tuple[int, int]], requested: int
    ) -> None:
        quantity = Quantity.from_units(requested)
        first = make_curve(bids).quote_up_to(quantity)
        second = make_curve(bids).quote_up_to(quantity)
        assert first.gross_cost == second.gross_cost
        assert first.filled_quantity == second.filled_quantity
        assert [s.execution_price for s in first.slices] == [
            s.execution_price for s in second.slices
        ]


class TestMultiLegLaws:
    @SETTINGS
    @given(bid_book, bid_book)
    def test_common_depth_is_the_minimum(
        self, first: list[tuple[int, int]], second: list[tuple[int, int]]
    ) -> None:
        legs = [make_curve(first, "A"), make_curve(second, "B")]
        assert common_fillable_quantity(legs).units == min(
            leg.max_fillable_quantity.units for leg in legs
        )

    @SETTINGS
    @given(bid_book, bid_book)
    def test_total_cost_is_the_sum_of_leg_costs(
        self, first: list[tuple[int, int]], second: list[tuple[int, int]]
    ) -> None:
        legs = [make_curve(first, "A"), make_curve(second, "B")]
        for point in multi_leg_costs(legs):
            assert point.total_gross_cost == Money.from_units(
                sum(cost.units for cost in point.leg_costs)
            )

    @SETTINGS
    @given(bid_book, bid_book)
    def test_points_never_exceed_common_depth(
        self, first: list[tuple[int, int]], second: list[tuple[int, int]]
    ) -> None:
        legs = [make_curve(first, "A"), make_curve(second, "B")]
        common = common_fillable_quantity(legs)
        for point in multi_leg_costs(legs):
            assert point.quantity.units <= common.units

    @SETTINGS
    @given(bid_book, bid_book)
    def test_total_cost_is_non_decreasing(
        self, first: list[tuple[int, int]], second: list[tuple[int, int]]
    ) -> None:
        legs = [make_curve(first, "A"), make_curve(second, "B")]
        points = multi_leg_costs(legs)
        costs = [point.total_gross_cost.units for point in points]
        assert costs == sorted(costs)
