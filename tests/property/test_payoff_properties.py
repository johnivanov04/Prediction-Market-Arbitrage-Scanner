"""Property tests for payoff arithmetic and profit intervals.

The laws here are the ones the detector leans on without rechecking. If any can
be broken, a classification computed downstream can be wrong in a way no example
test would reveal.
"""

from __future__ import annotations

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from predarb.domain.money import Money, Price, Quantity
from predarb.domain.payoff import (
    Position,
    SettlementState,
    TabulatedPayoff,
    evaluate_portfolio,
)
from predarb.opportunities.models import PayoffStatus, ProfitInterval

pytestmark = pytest.mark.property

SETTINGS = settings(max_examples=60)
YES = SettlementState("YES")
NO = SettlementState("NO")
STATES = (YES, NO)

quantity_units = st.integers(min_value=0, max_value=200_000)
positive_quantity_units = st.integers(min_value=1, max_value=200_000)
notional_units = st.integers(min_value=1, max_value=100_000)
money_units = st.integers(min_value=0, max_value=5_000_000)


def yes_position(units: int, notional: Price) -> Position:
    return Position(
        label="YES",
        quantity=Quantity.from_units(units),
        payoff=TabulatedPayoff.binary(winning_state="YES", losing_state="NO", notional=notional),
    )


def no_position(units: int, notional: Price) -> Position:
    return Position(
        label="NO",
        quantity=Quantity.from_units(units),
        payoff=TabulatedPayoff.binary(winning_state="NO", losing_state="YES", notional=notional),
    )


class TestComplementPayoff:
    @given(quantity=quantity_units, notional=notional_units)
    @SETTINGS
    def test_equal_legs_pay_quantity_times_notional_in_every_state(self, quantity, notional):
        """The invariant the whole detector rests on."""
        price = Price.from_units(notional)
        payoff = evaluate_portfolio(
            [yes_position(quantity, price), no_position(quantity, price)], STATES
        )
        expected = price * Quantity.from_units(quantity)
        assert payoff.minimum == expected
        assert payoff.maximum == expected
        assert payoff.is_state_independent

    @given(yes_units=quantity_units, no_units=quantity_units, notional=notional_units)
    @SETTINGS
    def test_unequal_legs_pay_the_smaller_leg_in_the_worst_case(
        self, yes_units, no_units, notional
    ):
        price = Price.from_units(notional)
        payoff = evaluate_portfolio(
            [yes_position(yes_units, price), no_position(no_units, price)], STATES
        )
        assert payoff.minimum == price * Quantity.from_units(min(yes_units, no_units))
        assert payoff.maximum == price * Quantity.from_units(max(yes_units, no_units))

    @given(yes_units=quantity_units, no_units=quantity_units, notional=notional_units)
    @SETTINGS
    def test_minimum_never_exceeds_maximum(self, yes_units, no_units, notional):
        price = Price.from_units(notional)
        payoff = evaluate_portfolio(
            [yes_position(yes_units, price), no_position(no_units, price)], STATES
        )
        assert payoff.minimum <= payoff.maximum

    @given(yes_units=quantity_units, no_units=quantity_units, notional=notional_units)
    @SETTINGS
    def test_state_order_does_not_change_the_answer(self, yes_units, no_units, notional):
        price = Price.from_units(notional)
        positions = [yes_position(yes_units, price), no_position(no_units, price)]
        forward = evaluate_portfolio(positions, (YES, NO))
        reverse = evaluate_portfolio(positions, (NO, YES))
        assert forward.minimum == reverse.minimum
        assert forward.maximum == reverse.maximum

    @given(units=quantity_units, notional=notional_units)
    @SETTINGS
    def test_duplicating_a_position_doubles_its_payoff(self, units, notional):
        price = Price.from_units(notional)
        single = evaluate_portfolio([yes_position(units, price)], STATES)
        doubled = evaluate_portfolio(
            [yes_position(units, price), yes_position(units, price)], STATES
        )
        assert doubled.maximum.units == single.maximum.units * 2

    @given(units=quantity_units, notional=notional_units)
    @SETTINGS
    def test_every_monetary_output_is_exact(self, units, notional):
        price = Price.from_units(notional)
        payoff = evaluate_portfolio([yes_position(units, price), no_position(units, price)], STATES)
        assert isinstance(payoff.minimum, Money)
        assert isinstance(payoff.minimum.units, int)
        for entry in payoff.per_state:
            assert isinstance(entry.total, Money)


class TestProfitIntervalOrdering:
    @given(payoff=money_units, gross=money_units, low=money_units, extra=money_units)
    @SETTINGS
    def test_lower_bound_never_exceeds_upper_bound(self, payoff, gross, low, extra):
        interval = ProfitInterval(
            worst_case_payoff=Money.from_units(payoff),
            gross_cost=Money.from_units(gross),
            fee_lower=Money.from_units(low),
            fee_upper=Money.from_units(low + extra),
        )
        assert interval.profit_lower_bound <= interval.profit_upper_bound

    @given(payoff=money_units, gross=money_units, low=money_units, extra=money_units)
    @SETTINGS
    def test_raising_the_fee_upper_bound_cannot_raise_the_profit_floor(
        self, payoff, gross, low, extra
    ):
        base = ProfitInterval(
            worst_case_payoff=Money.from_units(payoff),
            gross_cost=Money.from_units(gross),
            fee_lower=Money.from_units(low),
            fee_upper=Money.from_units(low + extra),
        )
        worse = ProfitInterval(
            worst_case_payoff=Money.from_units(payoff),
            gross_cost=Money.from_units(gross),
            fee_lower=Money.from_units(low),
            fee_upper=Money.from_units(low + extra + 1),
        )
        assert worse.profit_lower_bound <= base.profit_lower_bound

    @given(payoff=money_units, gross=money_units, low=money_units, extra=money_units)
    @SETTINGS
    def test_raising_gross_cost_cannot_improve_either_bound(self, payoff, gross, low, extra):
        base = ProfitInterval(
            worst_case_payoff=Money.from_units(payoff),
            gross_cost=Money.from_units(gross),
            fee_lower=Money.from_units(low),
            fee_upper=Money.from_units(low + extra),
        )
        dearer = ProfitInterval(
            worst_case_payoff=Money.from_units(payoff),
            gross_cost=Money.from_units(gross + 1),
            fee_lower=Money.from_units(low),
            fee_upper=Money.from_units(low + extra),
        )
        assert dearer.profit_lower_bound <= base.profit_lower_bound
        assert dearer.profit_upper_bound <= base.profit_upper_bound

    @given(payoff=money_units, gross=money_units, low=money_units, extra=money_units)
    @SETTINGS
    def test_raising_the_guaranteed_payoff_cannot_worsen_either_bound(
        self, payoff, gross, low, extra
    ):
        base = ProfitInterval(
            worst_case_payoff=Money.from_units(payoff),
            gross_cost=Money.from_units(gross),
            fee_lower=Money.from_units(low),
            fee_upper=Money.from_units(low + extra),
        )
        better = ProfitInterval(
            worst_case_payoff=Money.from_units(payoff + 1),
            gross_cost=Money.from_units(gross),
            fee_lower=Money.from_units(low),
            fee_upper=Money.from_units(low + extra),
        )
        assert better.profit_lower_bound >= base.profit_lower_bound
        assert better.profit_upper_bound >= base.profit_upper_bound


class TestProfitIntervalClassification:
    @given(payoff=money_units, gross=money_units, low=money_units, extra=money_units)
    @SETTINGS
    def test_a_proven_floor_holds_for_every_fee_in_the_interval(self, payoff, gross, low, extra):
        """If the floor is positive, no admissible fee can erase the profit.

        This is what licenses the word "proven": the claim is about the whole
        interval, not about the estimate inside it.
        """
        interval = ProfitInterval(
            worst_case_payoff=Money.from_units(payoff),
            gross_cost=Money.from_units(gross),
            fee_lower=Money.from_units(low),
            fee_upper=Money.from_units(low + extra),
        )
        assume(interval.is_proven_profitable)
        for fee in (low, low + extra, low + extra // 2):
            assert payoff - (gross + fee) > 0

    @given(payoff=money_units, gross=money_units, low=money_units, extra=money_units)
    @SETTINGS
    def test_exactly_one_classification_applies(self, payoff, gross, low, extra):
        interval = ProfitInterval(
            worst_case_payoff=Money.from_units(payoff),
            gross_cost=Money.from_units(gross),
            fee_lower=Money.from_units(low),
            fee_upper=Money.from_units(low + extra),
        )
        flags = [
            interval.is_proven_profitable,
            interval.is_proven_unprofitable,
            interval.is_indeterminate,
        ]
        assert sum(flags) == 1

    @given(gross=money_units, low=money_units)
    @SETTINGS
    def test_zero_profit_is_never_proven_profitable(self, gross, low):
        """Arbitrage requires strictly positive guaranteed profit.

        Constructed exactly on the boundary rather than filtered onto it: a
        payoff that precisely equals the cost is the case that must not be
        called arbitrage, and it is too rare to reach by sampling.
        """
        interval = ProfitInterval(
            worst_case_payoff=Money.from_units(gross + low),
            gross_cost=Money.from_units(gross),
            fee_lower=Money.from_units(low),
            fee_upper=Money.from_units(low),
        )
        assert interval.profit_lower_bound == Money.zero()
        assert not interval.is_proven_profitable
        assert interval.payoff_status() is PayoffStatus.NON_POSITIVE

    @given(gross=money_units, low=money_units)
    @SETTINGS
    def test_one_micro_dollar_above_the_boundary_is_proven(self, gross, low):
        """The boundary is strict and exactly one micro-dollar wide."""
        interval = ProfitInterval(
            worst_case_payoff=Money.from_units(gross + low + 1),
            gross_cost=Money.from_units(gross),
            fee_lower=Money.from_units(low),
            fee_upper=Money.from_units(low),
        )
        assert interval.profit_lower_bound == Money.from_units(1)
        assert interval.is_proven_profitable

    @given(payoff=money_units, gross=money_units, low=money_units, extra=money_units)
    @SETTINGS
    def test_an_exact_fee_collapses_the_profit_interval(self, payoff, gross, low, extra):
        del extra
        interval = ProfitInterval(
            worst_case_payoff=Money.from_units(payoff),
            gross_cost=Money.from_units(gross),
            fee_lower=Money.from_units(low),
            fee_upper=Money.from_units(low),
        )
        assert interval.profit_lower_bound == interval.profit_upper_bound

    @given(low=money_units, extra=positive_quantity_units)
    @SETTINGS
    def test_an_inverted_fee_interval_is_refused(self, low, extra):
        with pytest.raises(ValueError, match="exceeds upper bound"):
            ProfitInterval(
                worst_case_payoff=Money.zero(),
                gross_cost=Money.zero(),
                fee_lower=Money.from_units(low + extra),
                fee_upper=Money.from_units(low),
            )
