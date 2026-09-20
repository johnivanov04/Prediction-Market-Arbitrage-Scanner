"""Tests for the venue-neutral payoff engine.

Arithmetic only. No probabilities appear anywhere, and none should: a
state-independent payoff is a claim about every state, so likelihood is
irrelevant to it.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from predarb.domain import payoff as payoff_module
from predarb.domain.money import Money, Price, Quantity
from predarb.domain.payoff import (
    EnumerationSolver,
    IncompletePayoffSpecificationError,
    PortfolioPayoff,
    Position,
    SettlementState,
    TabulatedPayoff,
    evaluate_portfolio,
)

pytestmark = pytest.mark.unit

YES = SettlementState("YES")
NO = SettlementState("NO")
STATES = (YES, NO)
DOLLAR = Price.from_value("1.0000")


def q(value: str) -> Quantity:
    return Quantity.from_value(value)


def money(value: str) -> Money:
    return Money.from_value(value)


def yes_payoff(notional: Price = DOLLAR) -> TabulatedPayoff:
    return TabulatedPayoff.binary(winning_state="YES", losing_state="NO", notional=notional)


def no_payoff(notional: Price = DOLLAR) -> TabulatedPayoff:
    return TabulatedPayoff.binary(winning_state="NO", losing_state="YES", notional=notional)


def yes_position(quantity: str, notional: Price = DOLLAR) -> Position:
    return Position(label="YES", quantity=q(quantity), payoff=yes_payoff(notional))


def no_position(quantity: str, notional: Price = DOLLAR) -> Position:
    return Position(label="NO", quantity=q(quantity), payoff=no_payoff(notional))


class TestSinglePositionPayoff:
    def test_yes_position_in_the_yes_state_pays_notional(self):
        assert yes_position("1.00").payoff_in(YES) == money("1.000000")

    def test_yes_position_in_the_no_state_pays_nothing(self):
        assert yes_position("1.00").payoff_in(NO) == money("0.000000")

    def test_no_position_in_the_yes_state_pays_nothing(self):
        assert no_position("1.00").payoff_in(YES) == money("0.000000")

    def test_no_position_in_the_no_state_pays_notional(self):
        assert no_position("1.00").payoff_in(NO) == money("1.000000")

    def test_payoff_scales_with_quantity(self):
        assert yes_position("7.00").payoff_in(YES) == money("7.000000")

    def test_fractional_quantity_is_exact(self):
        """0.01 contracts at $1 is exactly one cent, not a rounded approximation."""
        assert yes_position("0.01").payoff_in(YES) == money("0.010000")
        assert yes_position("3.33").payoff_in(YES) == money("3.330000")

    def test_zero_quantity_pays_nothing_in_every_state(self):
        position = yes_position("0.00")
        assert position.payoff_in(YES) == Money.zero()
        assert position.payoff_in(NO) == Money.zero()


class TestNonDollarNotional:
    """Domain mathematics is deliberately wider than venue support.

    The Kalshi fee engine refuses non-$1 notionals because the published fee
    formula is written against a $1 payout. That is a venue restriction; the
    payoff arithmetic itself has no such limit.
    """

    def test_a_five_dollar_contract_pays_five_dollars(self):
        notional = Price.from_value("5.0000")
        assert yes_position("2.00", notional).payoff_in(YES) == money("10.000000")

    def test_complement_still_sums_to_notional(self):
        notional = Price.from_value("2.5000")
        payoff = evaluate_portfolio(
            [yes_position("4.00", notional), no_position("4.00", notional)], STATES
        )
        assert payoff.minimum == payoff.maximum == money("10.000000")

    def test_a_sub_dollar_notional_works(self):
        notional = Price.from_value("0.2500")
        assert yes_position("8.00", notional).payoff_in(YES) == money("2.000000")


class TestPortfolioExtremes:
    def test_equal_yes_and_no_is_state_independent(self):
        payoff = evaluate_portfolio([yes_position("3.00"), no_position("3.00")], STATES)
        assert payoff.minimum == payoff.maximum == money("3.000000")
        assert payoff.is_state_independent
        assert payoff.worst_case == money("3.000000")

    def test_unequal_quantities_pay_the_smaller_leg_in_the_worst_case(self):
        payoff = evaluate_portfolio([yes_position("5.00"), no_position("2.00")], STATES)
        assert payoff.minimum == money("2.000000")
        assert payoff.maximum == money("5.000000")
        assert not payoff.is_state_independent

    def test_the_minimising_state_is_reported(self):
        payoff = evaluate_portfolio([yes_position("5.00"), no_position("2.00")], STATES)
        assert payoff.minimising_states == (NO,)
        assert payoff.maximising_states == (YES,)

    def test_both_states_attain_the_extreme_when_balanced(self):
        payoff = evaluate_portfolio([yes_position("3.00"), no_position("3.00")], STATES)
        assert set(payoff.minimising_states) == {YES, NO}
        assert set(payoff.maximising_states) == {YES, NO}

    def test_multiple_positions_add_exactly(self):
        payoff = evaluate_portfolio(
            [yes_position("1.00"), yes_position("2.00"), no_position("3.00")], STATES
        )
        assert payoff.payoff_in(YES) == money("3.000000")
        assert payoff.payoff_in(NO) == money("3.000000")

    def test_duplicated_positions_add_rather_than_dedupe(self):
        single = evaluate_portfolio([yes_position("2.00")], STATES)
        doubled = evaluate_portfolio([yes_position("2.00"), yes_position("2.00")], STATES)
        assert doubled.payoff_in(YES).units == single.payoff_in(YES).units * 2

    def test_per_state_contributions_are_itemised(self):
        payoff = evaluate_portfolio([yes_position("3.00"), no_position("3.00")], STATES)
        entry = next(e for e in payoff.per_state if e.state == YES)
        assert dict(entry.by_position) == {"YES": money("3.000000"), "NO": Money.zero()}

    def test_an_empty_portfolio_pays_zero(self):
        """A real answer, unlike an empty state set."""
        payoff = evaluate_portfolio([], STATES)
        assert payoff.minimum == payoff.maximum == Money.zero()

    def test_zero_quantity_portfolio_is_state_independent_at_zero(self):
        payoff = evaluate_portfolio([yes_position("0.00"), no_position("0.00")], STATES)
        assert payoff.is_state_independent
        assert payoff.worst_case == Money.zero()


class TestStateOrderIndependence:
    def test_supplying_states_in_either_order_gives_the_same_result(self):
        forward = evaluate_portfolio([yes_position("5.00"), no_position("2.00")], (YES, NO))
        reverse = evaluate_portfolio([yes_position("5.00"), no_position("2.00")], (NO, YES))
        assert forward.minimum == reverse.minimum
        assert forward.maximum == reverse.maximum
        assert forward.states == reverse.states
        assert forward.minimising_states == reverse.minimising_states

    def test_states_are_reported_in_a_deterministic_order(self):
        payoff = evaluate_portfolio([yes_position("1.00")], (NO, YES))
        assert [s.name for s in payoff.states] == ["NO", "YES"]


class TestFailingClosed:
    def test_a_missing_state_is_refused_not_defaulted_to_zero(self):
        """An unspecified state is unproven, not worth zero."""
        partial = Position(
            label="PARTIAL",
            quantity=q("1.00"),
            payoff=TabulatedPayoff({"YES": DOLLAR}),
        )
        with pytest.raises(IncompletePayoffSpecificationError, match="no payoff for state"):
            evaluate_portfolio([partial], STATES)

    def test_an_empty_state_set_is_refused(self):
        """A worst case over no states is vacuous, not safe."""
        with pytest.raises(IncompletePayoffSpecificationError, match="vacuous"):
            evaluate_portfolio([yes_position("1.00")], [])

    def test_duplicate_states_are_refused(self):
        with pytest.raises(ValueError, match="duplicate settlement states"):
            evaluate_portfolio([yes_position("1.00")], (YES, NO, YES))

    def test_an_unnamed_state_is_refused(self):
        for name in ("", "   "):
            with pytest.raises(ValueError, match="non-empty name"):
                SettlementState(name)

    def test_an_empty_payoff_table_is_refused(self):
        with pytest.raises(ValueError, match="at least one state"):
            TabulatedPayoff({})

    def test_a_binary_table_needs_two_distinct_states(self):
        with pytest.raises(ValueError, match="both"):
            TabulatedPayoff.binary(winning_state="X", losing_state="X", notional=DOLLAR)

    def test_reading_an_unevaluated_state_raises(self):
        payoff = evaluate_portfolio([yes_position("1.00")], STATES)
        with pytest.raises(KeyError):
            payoff.payoff_in(SettlementState("VOID"))


class TestScalarStyleTables:
    """Three-state tables work; Phase 1 simply never certifies one."""

    def test_a_three_state_table_enumerates_correctly(self):
        states = (YES, NO, SettlementState("VOID"))
        spec = TabulatedPayoff(
            {"YES": DOLLAR, "NO": Price.from_units(0), "VOID": Price.from_value("0.5000")}
        )
        payoff = evaluate_portfolio([Position("P", q("2.00"), spec)], states)
        assert payoff.payoff_in(SettlementState("VOID")) == money("1.000000")
        assert payoff.minimum == Money.zero()
        assert payoff.maximum == money("2.000000")

    def test_a_void_state_breaks_the_complement_guarantee(self):
        """Exactly why an unmodelled third state must block a proof.

        YES+NO looks state-independent across {YES, NO}, and stops being so the
        moment a refund state exists that the table did not account for.
        """
        states = (YES, NO, SettlementState("VOID"))
        yes_spec = TabulatedPayoff(
            {"YES": DOLLAR, "NO": Price.from_units(0), "VOID": Price.from_value("0.3000")}
        )
        no_spec = TabulatedPayoff(
            {"YES": Price.from_units(0), "NO": DOLLAR, "VOID": Price.from_value("0.3000")}
        )
        payoff = evaluate_portfolio(
            [Position("YES", q("1.00"), yes_spec), Position("NO", q("1.00"), no_spec)], states
        )
        assert payoff.maximum == money("1.000000")
        assert payoff.minimum == money("0.600000")
        assert not payoff.is_state_independent


class TestSolverBoundary:
    def test_the_default_solver_is_enumeration(self):
        direct = EnumerationSolver().solve([yes_position("1.00")], STATES)
        via_helper = evaluate_portfolio([yes_position("1.00")], STATES)
        assert direct.minimum == via_helper.minimum

    def test_a_custom_solver_is_used_when_supplied(self):
        """The seam that lets an optimiser replace enumeration later."""
        calls: list[int] = []

        class CountingSolver:
            def solve(
                self,
                positions: Sequence[Position],
                states: Sequence[SettlementState],
            ) -> PortfolioPayoff:
                calls.append(len(positions))
                return EnumerationSolver().solve(positions, states)

        evaluate_portfolio([yes_position("1.00")], STATES, solver=CountingSolver())
        assert calls == [1]


class TestNoProbabilitySurface:
    def test_the_module_exposes_no_probability_concepts(self):
        for forbidden in (
            "probability",
            "expected_value",
            "ev",
            "weight",
            "likelihood",
            "distribution",
        ):
            assert not hasattr(payoff_module, forbidden)

    def test_payoff_results_expose_no_expected_value(self):
        payoff = evaluate_portfolio([yes_position("1.00")], STATES)
        for forbidden in ("expected", "mean", "probability", "ev"):
            assert not hasattr(payoff, forbidden)
