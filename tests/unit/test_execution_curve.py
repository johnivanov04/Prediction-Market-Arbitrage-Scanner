"""Tests for the breakpoint curve, immutability and multi-leg aggregation."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from predarb.books.execution import (
    ExecutionCurve,
    InsufficientDepthError,
    build_execution_curve,
)
from predarb.books.multileg import (
    LiquidityCollisionError,
    common_fillable_quantity,
    detect_liquidity_collisions,
    multi_leg_costs,
    thin_for_display,
)
from predarb.books.orderbook import MutableOrderBook
from predarb.domain.enums import MarketSide
from predarb.domain.money import Money, Quantity, QuantityDelta
from tests.unit.test_execution import CONTEXT, book, curve_for, instrument, p, q

pytestmark = pytest.mark.unit

T0 = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)


def three_level_curve(ticker: str = "MKT") -> ExecutionCurve:
    """NO bids 0.60/0.50/0.40 -> YES asks 0.40/0.50/0.60 with 10/20/30 depth."""
    return build_execution_curve(
        book(no=[("0.6000", "10.00"), ("0.5000", "20.00"), ("0.4000", "30.00")], ticker=ticker),
        instrument(ticker=ticker),
        CONTEXT,
        MarketSide.YES,
        at=T0,
    )


class TestBreakpoints:
    def test_one_breakpoint_per_level(self) -> None:
        curve = three_level_curve()
        assert len(curve.breakpoints) == 3

    def test_breakpoints_are_cumulative(self) -> None:
        curve = three_level_curve()
        assert [b.cumulative_quantity.to_str() for b in curve.breakpoints] == [
            "10.00",
            "30.00",
            "60.00",
        ]

    def test_cumulative_costs_are_exact(self) -> None:
        curve = three_level_curve()
        assert [b.cumulative_cost.to_str() for b in curve.breakpoints] == [
            "4.000000",
            "14.000000",
            "32.000000",
        ]

    def test_marginal_price_names_the_level_being_consumed(self) -> None:
        curve = three_level_curve()
        assert [b.marginal_price.to_str() for b in curve.breakpoints] == [
            "0.4000",
            "0.5000",
            "0.6000",
        ]

    def test_breakpoints_carry_liquidity_identity(self) -> None:
        curve = three_level_curve()
        assert all(b.liquidity.market_ticker == "MKT" for b in curve.breakpoints)
        assert len({b.liquidity for b in curve.breakpoints}) == 3


class TestCurveEvaluation:
    def test_zero_costs_nothing(self) -> None:
        assert three_level_curve().gross_cost(q("0.00")).is_zero

    def test_at_each_breakpoint(self) -> None:
        curve = three_level_curve()
        for point in curve.breakpoints:
            assert curve.gross_cost(point.cumulative_quantity) == point.cumulative_cost

    def test_inside_a_level(self) -> None:
        curve = three_level_curve()
        # 10 @ 0.40 = 4.00, then 5 more @ 0.50 = 2.50
        assert curve.gross_cost(q("15.00")) == Money.from_value("6.500000")

    def test_at_total_depth(self) -> None:
        curve = three_level_curve()
        assert curve.gross_cost(q("60.00")) == Money.from_value("32.000000")

    def test_beyond_depth_raises(self) -> None:
        """gross_cost answers exactly; quote_up_to answers partially."""
        with pytest.raises(InsufficientDepthError, match="displayed"):
            three_level_curve().gross_cost(q("61.00"))

    def test_fractional_quantity_inside_a_level(self) -> None:
        curve = three_level_curve()
        # 10 @ 0.40 = 4.00, then 0.29 @ 0.50 = 0.145
        assert curve.gross_cost(q("10.29")) == Money.from_value("4.145000")

    def test_vwap_at_a_quantity(self) -> None:
        curve = three_level_curve()
        vwap = curve.vwap(q("30.00"))
        assert vwap is not None
        assert vwap.as_decimal(6) == Decimal("0.466667")

    def test_vwap_at_zero_is_none(self) -> None:
        assert three_level_curve().vwap(q("0.00")) is None

    def test_marginal_price_progression(self) -> None:
        curve = three_level_curve()
        assert curve.marginal_price(q("0.00")) == p("0.4000")
        assert curve.marginal_price(q("10.00")) == p("0.5000")
        assert curve.marginal_price(q("30.00")) == p("0.6000")
        assert curve.marginal_price(q("60.00")) is None  # nothing left

    def test_empty_curve_evaluates_safely(self) -> None:
        empty = curve_for(book(), MarketSide.YES)
        assert empty.gross_cost(q("0.00")).is_zero
        assert empty.marginal_price(q("0.00")) is None
        assert empty.max_fillable_quantity.is_zero


class TestCurveMonotonicity:
    def test_cumulative_quantity_increases(self) -> None:
        curve = three_level_curve()
        units = [b.cumulative_quantity.units for b in curve.breakpoints]
        assert units == sorted(units)
        assert len(set(units)) == len(units)

    def test_cumulative_cost_increases(self) -> None:
        curve = three_level_curve()
        costs = [b.cumulative_cost.units for b in curve.breakpoints]
        assert costs == sorted(costs)

    def test_marginal_price_never_improves_with_depth(self) -> None:
        """Acquisition gets worse, never better, as cheap levels are exhausted."""
        curve = three_level_curve()
        prices = [b.marginal_price.units for b in curve.breakpoints]
        assert prices == sorted(prices)

    def test_gross_cost_is_non_decreasing(self) -> None:
        curve = three_level_curve()
        previous = Money.zero()
        for units in range(0, 6001, 250):
            cost = curve.gross_cost(Quantity.from_units(units))
            assert cost >= previous
            previous = cost


class TestImmutability:
    def test_mutating_the_live_book_does_not_change_an_existing_curve(self) -> None:
        live = MutableOrderBook("MKT", connection_epoch=1, sid=1)
        live.apply_snapshot(
            yes_levels=(),
            no_levels=((p("0.6000"), q("10.00")),),
            seq=1,
            raw_id="raw-1",
            received_at=T0,
        )
        curve = build_execution_curve(live.view(), instrument(), CONTEXT, MarketSide.YES, at=T0)
        before_depth = curve.max_fillable_quantity
        before_cost = curve.gross_cost(q("10.00"))

        live.apply_delta(
            side=MarketSide.NO,
            price=p("0.6000"),
            delta=QuantityDelta.from_value("-5.00"),
            seq=2,
            raw_id="raw-2",
            received_at=T0,
        )

        assert curve.max_fillable_quantity == before_depth
        assert curve.gross_cost(q("10.00")) == before_cost

    def test_curve_is_pinned_to_a_book_coordinate(self) -> None:
        curve = three_level_curve()
        assert curve.connection_epoch == 1
        assert curve.sid == 1
        assert curve.book_seq == 10

    def test_levels_are_an_immutable_tuple(self) -> None:
        curve = three_level_curve()
        assert isinstance(curve.levels, tuple)
        assert isinstance(curve.breakpoints, tuple)

    def test_same_view_produces_the_same_curve(self) -> None:
        view = book(no=[("0.6000", "10.00"), ("0.5000", "20.00")])
        first = build_execution_curve(view, instrument(), CONTEXT, MarketSide.YES, at=T0)
        second = build_execution_curve(view, instrument(), CONTEXT, MarketSide.YES, at=T0)
        assert [lv.execution_price for lv in first.levels] == [
            lv.execution_price for lv in second.levels
        ]
        assert first.gross_cost(q("25.00")) == second.gross_cost(q("25.00"))


# ---------------------------------------------------------------------------
# Multi-leg
# ---------------------------------------------------------------------------


def leg(
    ticker: str, bids: list[tuple[str, str]], outcome: MarketSide = MarketSide.YES
) -> ExecutionCurve:
    # Depth for buying YES comes from NO bids, and vice versa.
    view = (
        book(ticker=ticker, no=bids) if outcome is MarketSide.YES else book(ticker=ticker, yes=bids)
    )
    return build_execution_curve(
        view,
        instrument(ticker=ticker),
        CONTEXT,
        outcome,
        at=T0,
    )


class TestCommonDepth:
    def test_common_quantity_is_the_minimum(self) -> None:
        legs = [leg("A", [("0.6000", "10.00")]), leg("B", [("0.6000", "4.00")])]
        assert common_fillable_quantity(legs) == q("4.00")

    def test_empty_leg_gives_zero_common_depth(self) -> None:
        legs = [leg("A", [("0.6000", "10.00")]), leg("B", [])]
        assert common_fillable_quantity(legs).is_zero

    def test_no_legs_gives_zero(self) -> None:
        assert common_fillable_quantity([]).is_zero

    def test_n_legs(self) -> None:
        legs = [leg(f"M{i}", [("0.6000", f"{i + 5}.00")]) for i in range(4)]
        assert common_fillable_quantity(legs) == q("5.00")


class TestMultiLegCosts:
    def test_two_legs_sum_at_each_breakpoint(self) -> None:
        legs = [leg("A", [("0.6000", "10.00")]), leg("B", [("0.7000", "10.00")])]
        points = multi_leg_costs(legs)
        assert points
        final = points[-1]
        assert final.quantity == q("10.00")
        # A: 10 @ 0.40 = 4.00 ; B: 10 @ 0.30 = 3.00
        assert final.total_gross_cost == Money.from_value("7.000000")
        assert final.leg_costs == (Money.from_value("4.000000"), Money.from_value("3.000000"))

    def test_breakpoints_are_the_union_across_legs(self) -> None:
        legs = [
            leg("A", [("0.6000", "10.00"), ("0.5000", "10.00")]),
            leg("B", [("0.7000", "5.00"), ("0.6000", "15.00")]),
        ]
        points = multi_leg_costs(legs)
        quantities = [point.quantity.to_str() for point in points]
        assert "5.00" in quantities  # B's first level
        assert "10.00" in quantities  # A's first level
        assert quantities[-1] == "20.00"  # common depth

    def test_shallow_leg_limits_the_set(self) -> None:
        legs = [leg("A", [("0.6000", "100.00")]), leg("B", [("0.6000", "3.00")])]
        points = multi_leg_costs(legs)
        assert points[-1].quantity == q("3.00")

    def test_empty_leg_produces_no_points(self) -> None:
        assert multi_leg_costs([leg("A", [("0.6000", "10.00")]), leg("B", [])]) == ()

    def test_no_legs_produces_no_points(self) -> None:
        assert multi_leg_costs([]) == ()

    def test_points_carry_per_leg_vwaps_and_tickers(self) -> None:
        legs = [leg("A", [("0.6000", "10.00")]), leg("B", [("0.7000", "10.00")])]
        point = multi_leg_costs(legs)[-1]
        assert point.leg_tickers == ("A", "B")
        assert all(vwap is not None for vwap in point.leg_vwaps)

    def test_points_carry_every_consumed_liquidity_id(self) -> None:
        legs = [leg("A", [("0.6000", "10.00")]), leg("B", [("0.7000", "10.00")])]
        point = multi_leg_costs(legs)[-1]
        assert len(point.liquidity_ids) == 2

    def test_no_profit_or_fee_fields_exist(self) -> None:
        """Profitability needs fees and payoff; neither belongs in this layer."""
        legs = [leg("A", [("0.6000", "10.00")]), leg("B", [("0.7000", "10.00")])]
        point = multi_leg_costs(legs)[-1]
        for forbidden in ("profit", "edge", "net_cost", "fees", "payoff", "is_profitable"):
            assert not hasattr(point, forbidden)


class TestLiquidityCollisions:
    def _same_market_both_sides(self) -> tuple[ExecutionCurve, ExecutionCurve]:
        """Buying YES and NO on one market draws on two different bid sides.

        Those are genuinely different resting levels, so this must *not* collide.
        """
        view = book(yes=[("0.4000", "10.00")], no=[("0.6000", "10.00")])
        yes_leg = build_execution_curve(view, instrument(), CONTEXT, MarketSide.YES, at=T0)
        no_leg = build_execution_curve(view, instrument(), CONTEXT, MarketSide.NO, at=T0)
        return yes_leg, no_leg

    def test_opposite_sides_of_one_market_do_not_collide(self) -> None:
        yes_leg, no_leg = self._same_market_both_sides()
        assert detect_liquidity_collisions([yes_leg, no_leg]) == ()
        assert multi_leg_costs([yes_leg, no_leg])

    def test_the_same_leg_twice_collides(self) -> None:
        """Two legs reaching for one pool of contracts is not executable twice."""
        curve = leg("A", [("0.6000", "10.00")])
        collisions = detect_liquidity_collisions([curve, curve])
        assert collisions
        with pytest.raises(LiquidityCollisionError, match="more than one leg"):
            multi_leg_costs([curve, curve])

    def test_collision_error_names_the_levels(self) -> None:
        curve = leg("A", [("0.6000", "10.00")])
        with pytest.raises(LiquidityCollisionError) as exc_info:
            multi_leg_costs([curve, curve])
        assert "A:NO@0.6000" in str(exc_info.value)
        assert exc_info.value.collisions

    def test_collision_check_respects_reachable_depth(self) -> None:
        """Levels neither leg reaches cannot collide."""
        deep = leg("A", [("0.6000", "10.00"), ("0.5000", "10.00")])
        shallow = leg("B", [("0.6000", "2.00")])
        # Only A's first level is reachable at the common depth of 2.00.
        reachable = detect_liquidity_collisions([deep, shallow], up_to=q("2.00"))
        assert reachable == ()

    def test_distinct_markets_never_collide(self) -> None:
        legs = [leg("A", [("0.6000", "10.00")]), leg("B", [("0.6000", "10.00")])]
        assert detect_liquidity_collisions(legs) == ()


class TestCanonicalCurveIsComplete:
    """The economic curve must never be thinned.

    Everything downstream is discontinuous at breakpoints -- the fee model is
    non-linear in price, fee rounding is a ceiling, payoff is quantity-dependent
    -- so a missing interior point could hide a real candidate or invent a
    maximum profitable quantity.
    """

    def _deep_legs(self, levels: int = 60) -> list[ExecutionCurve]:
        first = [(f"0.{9000 - index:04d}", "1.00") for index in range(levels)]
        second = [(f"0.{8000 - index:04d}", "1.00") for index in range(levels)]
        return [leg("A", first), leg("B", second)]

    def test_every_leg_breakpoint_appears_in_the_canonical_curve(self) -> None:
        legs = self._deep_legs()
        points = multi_leg_costs(legs)
        common = common_fillable_quantity(legs)
        expected = {
            point.cumulative_quantity.units
            for curve in legs
            for point in curve.breakpoints
            if 0 < point.cumulative_quantity.units <= common.units
        }
        actual = {point.quantity.units for point in points}
        assert expected <= actual

    def test_canonical_curve_takes_no_thinning_argument(self) -> None:
        signature = inspect.signature(multi_leg_costs)
        for forbidden in ("max_points", "limit", "sample", "thin"):
            assert forbidden not in signature.parameters

    def test_deep_books_are_not_truncated(self) -> None:
        points = multi_leg_costs(self._deep_legs(levels=120))
        # One breakpoint per level in the shallower-priced leg, all preserved.
        assert len(points) >= 120


class TestDisplayThinningIsPresentationOnly:
    def test_thinning_does_not_alter_the_underlying_points(self) -> None:
        legs = [
            leg("A", [(f"0.{9000 - i:04d}", "1.00") for i in range(50)]),
            leg("B", [("0.5000", "500.00")]),
        ]
        exact = multi_leg_costs(legs)
        thinned = thin_for_display(exact, max_points=5)
        # Every displayed point is an unmodified member of the exact curve.
        assert all(point in exact for point in thinned)
        assert len(thinned) <= 6  # endpoints are always retained

    def test_thinning_returns_a_new_sequence(self) -> None:
        legs = [
            leg("A", [("0.6000", "10.00"), ("0.5000", "10.00")]),
            leg("B", [("0.6000", "20.00")]),
        ]
        exact = multi_leg_costs(legs)
        before = [point.total_gross_cost.units for point in exact]
        thin_for_display(exact, max_points=2)
        assert [point.total_gross_cost.units for point in exact] == before

    def test_endpoints_are_always_kept(self) -> None:
        legs = [
            leg("A", [(f"0.{9000 - i:04d}", "1.00") for i in range(30)]),
            leg("B", [("0.5000", "500.00")]),
        ]
        exact = multi_leg_costs(legs)
        thinned = thin_for_display(exact, max_points=3)
        assert thinned[0] == exact[0]
        assert thinned[-1] == exact[-1]

    def test_short_curves_pass_through_unchanged(self) -> None:
        legs = [leg("A", [("0.6000", "10.00")]), leg("B", [("0.6000", "20.00")])]
        exact = multi_leg_costs(legs)
        assert thin_for_display(exact, max_points=40) == exact

    def test_too_few_points_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least"):
            thin_for_display((), max_points=1)
