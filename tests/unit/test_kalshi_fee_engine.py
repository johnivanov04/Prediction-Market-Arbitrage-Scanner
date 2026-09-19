"""Tests for the exact per-fill fee mechanics and the per-order accumulator.

These are the ground-truth numbers. Everything in
:mod:`predarb.venues.kalshi.fees` is an estimate *of* these, so an error here
propagates silently into every later claim.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from predarb.domain.enums import FeeType, Liquidity
from predarb.domain.fees import FeeConfiguration, FeeScope
from predarb.domain.money import Money, Price, Quantity
from predarb.venues.kalshi import fee_engine as module_under_test
from predarb.venues.kalshi.fee_engine import (
    AccumulatorState,
    Fill,
    apply_fill,
    apply_fills,
    total_net_fee,
    total_trade_fee,
)
from predarb.venues.kalshi.fee_model import BalancePrecision, UnsupportedFeeTypeError

pytestmark = pytest.mark.unit

CONFIG = FeeConfiguration(
    fee_type_raw=FeeType.QUADRATIC.value,
    multiplier=Decimal(1),
    scope=FeeScope.SERIES,
    scope_ticker="KXTEST",
)
NON_DIRECT = BalancePrecision.non_direct_member()
DIRECT = BalancePrecision.direct_member()


def fill(price: str, quantity: str, multiplier: str = "1") -> Fill:
    configuration = (
        CONFIG
        if multiplier == "1"
        else FeeConfiguration(
            fee_type_raw=FeeType.QUADRATIC.value,
            multiplier=Decimal(multiplier),
            scope=FeeScope.SERIES,
            scope_ticker="KXTEST",
        )
    )
    return Fill(
        price=Price.from_value(price),
        quantity=Quantity.from_value(quantity),
        configuration=configuration,
    )


class TestDocumentedSingleFill:
    """The worked example from the fee documentation, step by step.

    1 contract at $0.055 for a non-direct member:
        model fee      = 0.07 * 1 * 0.055 * 0.945 = $0.00363825
        trade fee      = ceil_6dp(...)            = $0.003639
        signed revenue = -$0.055000
        unaligned      = -$0.058639
        aligned        = floor_cent(...)          = -$0.060000
        rounding fee   = $0.001361
    """

    @pytest.fixture
    def result(self):
        return apply_fill(fill("0.0550", "1.00"), AccumulatorState.new_order(), NON_DIRECT)

    def test_raw_model_fee(self, result):
        assert result.raw_model_fee == Decimal("0.00363825")

    def test_trade_fee_is_the_ceiling(self, result):
        assert result.trade_fee == Money.from_value("0.003639")

    def test_signed_revenue_is_negative(self, result):
        assert result.signed_revenue == Money.from_value("-0.055000")

    def test_aligned_change_floors_away_from_zero(self, result):
        assert result.aligned_change == Money.from_value("-0.060000")

    def test_rounding_fee_is_the_realignment(self, result):
        assert result.rounding_fee == Money.from_value("0.001361")

    def test_no_rebate_on_the_first_fill(self, result):
        """Nothing has accumulated yet, so there is nothing to return."""
        assert result.rebate == Money.zero()

    def test_net_fee_combines_trade_and_rounding(self, result):
        assert result.net_fee == Money.from_value("0.005000")

    def test_balance_change_equals_the_aligned_change(self, result):
        assert result.balance_change == Money.from_value("-0.060000")

    def test_the_identity_holds(self, result):
        """net_fee == trade_fee + rounding_fee - rebate, exactly."""
        assert result.net_fee == result.trade_fee + result.rounding_fee - result.rebate

    def test_accumulator_carries_the_rounding_forward(self, result):
        assert result.accumulator_before.accumulated == Money.zero()
        assert result.accumulator_after.accumulated == Money.from_value("0.001361")
        assert result.accumulator_after.fills_applied == 1


class TestAccumulatorProgression:
    """Three identical fills of 5 contracts at $0.20, non-direct member.

    Each contributes $0.004 of rounding, so the accumulator runs
    0.004 -> 0.008 -> 0.012, crosses a cent on the third fill, rebates $0.010
    and carries $0.002 forward. This is the mechanism that keeps rounding from
    accruing to the venue indefinitely.
    """

    @pytest.fixture
    def run(self):
        return apply_fills([fill("0.2000", "5.00")] * 3, NON_DIRECT)

    def test_each_fill_rounds_by_four_tenths_of_a_cent(self, run):
        results, _ = run
        assert [r.rounding_fee for r in results] == [Money.from_value("0.004000")] * 3

    def test_rebate_arrives_only_when_a_whole_step_accumulates(self, run):
        results, _ = run
        assert [r.rebate for r in results] == [
            Money.zero(),
            Money.zero(),
            Money.from_value("0.010000"),
        ]

    def test_accumulator_path(self, run):
        results, final = run
        assert [r.accumulator_after.accumulated for r in results] == [
            Money.from_value("0.004000"),
            Money.from_value("0.008000"),
            Money.from_value("0.002000"),
        ]
        assert final.accumulated == Money.from_value("0.002000")

    def test_the_rebate_reduces_that_fills_net_fee(self, run):
        results, _ = run
        assert [r.net_fee for r in results] == [
            Money.from_value("0.060000"),
            Money.from_value("0.060000"),
            Money.from_value("0.050000"),
        ]

    def test_rebated_cash_returns_to_the_balance(self, run):
        results, _ = run
        assert results[2].balance_change == Money.from_value("-1.050000")
        assert results[0].balance_change == Money.from_value("-1.060000")

    def test_accumulated_rounding_is_conserved(self, run):
        """Every cent of rounding is either still held or was rebated."""
        results, final = run
        charged = sum(r.rounding_fee.units for r in results)
        rebated = sum(r.rebate.units for r in results)
        assert charged - rebated == final.accumulated.units


class TestRebateCap:
    def test_net_fee_never_goes_negative(self):
        """A rebate is capped by the fee it offsets, not by what is accrued."""
        # A large accumulator carried into a nearly free fill.
        carried = AccumulatorState(accumulated=Money.from_value("1.000000"), fills_applied=99)
        result = apply_fill(fill("0.0001", "0.01"), carried, NON_DIRECT)
        assert result.net_fee.units >= 0
        assert result.rebate <= result.trade_fee + result.rounding_fee

    def test_rebate_lands_on_the_precision_grid(self):
        carried = AccumulatorState(accumulated=Money.from_value("0.099000"), fills_applied=9)
        result = apply_fill(fill("0.5000", "10.00"), carried, NON_DIRECT)
        assert result.rebate.units % NON_DIRECT.step.units == 0

    def test_partial_steps_are_never_rebated(self):
        """$0.009 accrued against a $0.01 grid returns nothing yet.

        The fill is chosen to land exactly on the cent grid so it contributes
        no rounding of its own: a fill's rounding joins the accumulator before
        the rebate is computed, which would otherwise tip it over a step.
        """
        carried = AccumulatorState(accumulated=Money.from_value("0.009000"), fills_applied=1)
        result = apply_fill(fill("0.5000", "20.00"), carried, NON_DIRECT)
        assert result.rounding_fee == Money.zero()
        assert result.rebate == Money.zero()

    def test_a_negative_accumulator_is_rejected(self):
        with pytest.raises(ValueError, match="must not be negative"):
            AccumulatorState(accumulated=Money.from_value("-0.001000"))


class TestOrderingAndScope:
    def test_fills_are_never_reordered(self):
        """Rebate timing depends on accumulator value when each fill lands."""
        fills = [fill("0.2000", "5.00"), fill("0.9900", "1.00"), fill("0.2000", "5.00")]
        forward, _ = apply_fills(fills, NON_DIRECT)
        reverse, _ = apply_fills(list(reversed(fills)), NON_DIRECT)
        assert [r.fill.price for r in forward] == [f.price for f in fills]
        assert [r.fill.price for r in reverse] == [f.price for f in reversed(fills)]

    def test_a_fresh_order_does_not_inherit_an_accumulator(self):
        _, final = apply_fills([fill("0.2000", "5.00")] * 2, NON_DIRECT)
        assert final.accumulated == Money.from_value("0.008000")
        fresh, _ = apply_fills([fill("0.2000", "5.00")], NON_DIRECT)
        assert fresh[0].accumulator_before.accumulated == Money.zero()

    def test_an_order_can_be_continued_with_its_prior_state(self):
        """Fills may arrive in batches; the accumulator must survive."""
        whole, whole_final = apply_fills([fill("0.2000", "5.00")] * 3, NON_DIRECT)
        first, carried = apply_fills([fill("0.2000", "5.00")], NON_DIRECT)
        rest, rest_final = apply_fills([fill("0.2000", "5.00")] * 2, NON_DIRECT, state=carried)
        assert total_net_fee(whole) == total_net_fee(first + rest)
        assert whole_final.accumulated == rest_final.accumulated
        assert whole_final.fills_applied == rest_final.fills_applied == 3

    def test_empty_fill_list_is_a_no_op(self):
        results, final = apply_fills([], NON_DIRECT)
        assert results == ()
        assert final.accumulated == Money.zero()
        assert total_net_fee(results) == Money.zero()


class TestMemberPrecision:
    """The same trade costs a non-direct member materially more."""

    def test_direct_member_rounds_far_less(self):
        one = apply_fill(fill("0.0550", "1.00"), AccumulatorState.new_order(), DIRECT)
        other = apply_fill(fill("0.0550", "1.00"), AccumulatorState.new_order(), NON_DIRECT)
        assert one.rounding_fee == Money.from_value("0.000061")
        assert other.rounding_fee == Money.from_value("0.001361")
        assert one.net_fee < other.net_fee

    def test_trade_fee_is_independent_of_member_class(self):
        """Only the rounding differs; the modelled fee is the same trade."""
        one = apply_fill(fill("0.0550", "1.00"), AccumulatorState.new_order(), DIRECT)
        other = apply_fill(fill("0.0550", "1.00"), AccumulatorState.new_order(), NON_DIRECT)
        assert one.trade_fee == other.trade_fee
        assert one.raw_model_fee == other.raw_model_fee


class TestUnsupportedConfigurations:
    def test_an_unknown_fee_type_raises_rather_than_estimating(self):
        bad = Fill(
            price=Price.from_value("0.5000"),
            quantity=Quantity.from_value("1.00"),
            configuration=FeeConfiguration(
                fee_type_raw="margin_market_maker_program_fees",
                multiplier=Decimal(1),
                scope=FeeScope.SERIES,
                scope_ticker="KXTEST",
            ),
        )
        with pytest.raises(UnsupportedFeeTypeError):
            apply_fill(bad, AccumulatorState.new_order(), NON_DIRECT)

    def test_a_maker_fill_raises(self):
        maker = Fill(
            price=Price.from_value("0.5000"),
            quantity=Quantity.from_value("1.00"),
            configuration=CONFIG,
            liquidity=Liquidity.MAKER,
        )
        with pytest.raises(UnsupportedFeeTypeError, match="only taker fees"):
            apply_fill(maker, AccumulatorState.new_order(), NON_DIRECT)


class TestTotals:
    def test_totals_sum_their_components(self):
        results, _ = apply_fills([fill("0.2000", "5.00")] * 3, NON_DIRECT)
        assert total_trade_fee(results) == Money.from_value("0.168000")
        assert total_net_fee(results) == Money.from_value("0.170000")

    def test_net_total_is_at_least_the_trade_total(self):
        """Rounding can only add; rebates only return what rounding took."""
        results, _ = apply_fills([fill("0.2000", "5.00")] * 3, NON_DIRECT)
        assert total_net_fee(results) >= total_trade_fee(results)


class TestNoProfitLogic:
    """Step 6 is fee mechanics only."""

    def test_module_exposes_no_profitability_surface(self):
        for forbidden in (
            "is_arbitrage",
            "profit",
            "edge",
            "roi",
            "guaranteed_payoff",
            "max_profitable_quantity",
        ):
            assert not hasattr(module_under_test, forbidden)


class TestStrandedRounding:
    """Dust fills can strand rounding indefinitely.

    Found by the property suite, and kept as an example because it is the
    mechanism behind the fragmentation finding rather than an edge case: the
    rebate cap is per-fill, so a fill whose own fee is below one balance step
    returns nothing even when a whole step is owed.
    """

    def test_two_dust_fills_leave_a_whole_step_unrebated(self):
        """Even for a direct member, on the finest documented grid."""
        dust = fill("0.0001", "0.01")
        results, final = apply_fills([dust, dust], DIRECT)
        assert all(r.rebate == Money.zero() for r in results)
        assert final.accumulated == Money.from_value("0.000196")
        assert final.accumulated > DIRECT.step

    def test_the_rounding_dwarfs_the_actual_trade(self):
        """$0.000001 of contracts carrying $0.000099 of fee."""
        result = apply_fill(fill("0.0001", "0.01"), AccumulatorState.new_order(), DIRECT)
        assert result.signed_revenue == Money.from_value("-0.000001")
        assert result.trade_fee == Money.from_value("0.000001")
        assert result.net_fee == Money.from_value("0.000099")
