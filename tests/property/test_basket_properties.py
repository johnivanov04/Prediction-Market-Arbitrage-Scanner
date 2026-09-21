"""Property tests for AT_MOST_ONE basket payoff and profit bounds.

The payoff identities are asserted here rather than encoded in the detector.
That is the point: the detector derives everything from the certified per-member
payoff tables through the generic engine, and these tests check that the
familiar closed forms fall out of it. If the detector hardcoded
``q * (n - 1) * N`` it would be right for equal notionals and quietly wrong for
unequal ones.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from predarb.detectors.no_basket import JointMemberNoPayoff
from predarb.domain.money import Money, Price, Quantity
from predarb.domain.payoff import PortfolioPayoff, Position, evaluate_portfolio
from predarb.opportunities.models import ProfitInterval
from predarb.semantics.certificate import (
    CertificateStatus,
    SettlementCertificate,
    standard_binary_complement,
)
from predarb.semantics.fingerprint import synthetic_fingerprint
from predarb.semantics.relation import (
    MIN_BASKET_MEMBERS,
    NO_SELECTED_MEMBER_WINS,
    at_most_one_states,
    canonical_members,
)

pytestmark = pytest.mark.property

SETTINGS = settings(max_examples=60)
T0 = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)

member_counts = st.integers(min_value=MIN_BASKET_MEMBERS, max_value=8)
quantity_units = st.integers(min_value=1, max_value=50_000)
notional_units = st.integers(min_value=1, max_value=50_000)


def certificate_for(ticker: str, notional: Price) -> SettlementCertificate:
    return standard_binary_complement(
        market_ticker=ticker,
        evidence_fingerprint=synthetic_fingerprint(market=ticker),
        rules_hash="h" * 64,
        notional=notional,
        evidence="property fixture",
        verified_by="test",
        verification_method="fixture",
        verified_at=T0,
        valid_from=T0,
        status=CertificateStatus.VERIFIED,
    )


def no_basket_positions(notionals: list[Price], quantity: Quantity) -> list[Position]:
    """Build the lifted NO positions exactly as the detector does."""
    tickers = [f"M{i}" for i in range(len(notionals))]
    states = tuple(s.name for s in at_most_one_states(tickers))
    return [
        Position(
            label=ticker,
            quantity=quantity,
            payoff=JointMemberNoPayoff(
                ticker=ticker,
                certificate=certificate_for(ticker, notional),
                joint_states=states,
            ),
        )
        for ticker, notional in zip(tickers, notionals, strict=True)
    ]


def evaluate_basket(notionals: list[Price], quantity: Quantity) -> PortfolioPayoff:
    tickers = [f"M{i}" for i in range(len(notionals))]
    return evaluate_portfolio(no_basket_positions(notionals, quantity), at_most_one_states(tickers))


class TestStateSpace:
    @given(n=member_counts)
    @SETTINGS
    def test_there_are_exactly_n_plus_one_states(self, n):
        """Not 2**n filtered: the forbidden combinations are never constructed."""
        states = at_most_one_states([f"M{i}" for i in range(n)])
        assert len(states) == n + 1

    @given(n=member_counts)
    @SETTINGS
    def test_the_all_selected_no_state_is_always_present(self, n):
        states = at_most_one_states([f"M{i}" for i in range(n)])
        assert any(s.name == NO_SELECTED_MEMBER_WINS for s in states)

    @given(n=member_counts)
    @SETTINGS
    def test_every_member_has_exactly_one_winning_state(self, n):
        tickers = [f"M{i}" for i in range(n)]
        names = [s.name for s in at_most_one_states(tickers)]
        for ticker in tickers:
            assert names.count(ticker) == 1

    @given(n=member_counts, seed=st.integers(0, 10_000))
    @SETTINGS
    def test_member_order_does_not_change_the_state_space(self, n, seed):
        tickers = [f"M{i}" for i in range(n)]
        shuffled = list(tickers)
        random.Random(seed).shuffle(shuffled)
        assert at_most_one_states(tickers) == at_most_one_states(shuffled)

    @given(n=member_counts, seed=st.integers(0, 10_000))
    @SETTINGS
    def test_canonical_member_ordering_is_stable(self, n, seed):
        tickers = [f"M{i}" for i in range(n)]
        shuffled = list(tickers)
        random.Random(seed).shuffle(shuffled)
        assert canonical_members(tickers) == canonical_members(shuffled)


class TestEqualNotionalIdentity:
    @given(n=member_counts, quantity=quantity_units, notional=notional_units)
    @SETTINGS
    def test_worst_case_is_q_times_n_minus_one_times_notional(self, n, quantity, notional):
        """The familiar closed form, derived rather than hardcoded."""
        price = Price.from_units(notional)
        size = Quantity.from_units(quantity)
        payoff = evaluate_basket([price] * n, size)
        assert payoff.minimum == price * Quantity.from_units(quantity * (n - 1))

    @given(n=member_counts, quantity=quantity_units, notional=notional_units)
    @SETTINGS
    def test_best_case_is_every_leg_paying(self, n, quantity, notional):
        price = Price.from_units(notional)
        payoff = evaluate_basket([price] * n, Quantity.from_units(quantity))
        assert payoff.maximum == price * Quantity.from_units(quantity * n)

    @given(n=member_counts, quantity=quantity_units, notional=notional_units)
    @SETTINGS
    def test_the_all_no_state_attains_the_maximum(self, n, quantity, notional):
        price = Price.from_units(notional)
        payoff = evaluate_basket([price] * n, Quantity.from_units(quantity))
        assert any(s.name == NO_SELECTED_MEMBER_WINS for s in payoff.maximising_states)

    @given(n=member_counts, quantity=quantity_units, notional=notional_units)
    @SETTINGS
    def test_every_member_state_attains_the_minimum_when_equal(self, n, quantity, notional):
        price = Price.from_units(notional)
        payoff = evaluate_basket([price] * n, Quantity.from_units(quantity))
        assert len(payoff.minimising_states) == n


class TestUnequalNotionalIdentity:
    @given(
        notionals=st.lists(notional_units, min_size=MIN_BASKET_MEMBERS, max_size=6),
        quantity=quantity_units,
    )
    @SETTINGS
    def test_worst_case_is_sum_minus_max(self, notionals, quantity):
        """``q * (sum(N) - max(N))`` -- the general form.

        A detector that hardcoded the equal-notional shortcut would be wrong
        here, which is exactly why it does not.
        """
        prices = [Price.from_units(n) for n in notionals]
        size = Quantity.from_units(quantity)
        payoff = evaluate_basket(prices, size)
        expected = Money.from_units((sum(notionals) - max(notionals)) * quantity)
        assert payoff.minimum == expected

    @given(
        notionals=st.lists(notional_units, min_size=MIN_BASKET_MEMBERS, max_size=6),
        quantity=quantity_units,
    )
    @SETTINGS
    def test_the_largest_member_winning_is_the_worst_case(self, notionals, quantity):
        """Losing the biggest NO leg hurts most."""
        prices = [Price.from_units(n) for n in notionals]
        payoff = evaluate_basket(prices, Quantity.from_units(quantity))
        largest = f"M{notionals.index(max(notionals))}"
        assert any(s.name == largest for s in payoff.minimising_states)

    @given(
        notionals=st.lists(notional_units, min_size=MIN_BASKET_MEMBERS, max_size=6),
        quantity=quantity_units,
    )
    @SETTINGS
    def test_minimum_never_exceeds_maximum(self, notionals, quantity):
        prices = [Price.from_units(n) for n in notionals]
        payoff = evaluate_basket(prices, Quantity.from_units(quantity))
        assert payoff.minimum <= payoff.maximum

    @given(
        notionals=st.lists(notional_units, min_size=MIN_BASKET_MEMBERS, max_size=6),
        quantity=quantity_units,
        seed=st.integers(0, 10_000),
    )
    @SETTINGS
    def test_member_input_order_does_not_change_the_result(self, notionals, quantity, seed):
        prices = [Price.from_units(n) for n in notionals]
        shuffled = list(prices)
        random.Random(seed).shuffle(shuffled)
        size = Quantity.from_units(quantity)
        assert evaluate_basket(prices, size).minimum == evaluate_basket(shuffled, size).minimum


class TestProfitMonotonicity:
    money_units = st.integers(min_value=0, max_value=5_000_000)

    @given(payoff=money_units, gross=money_units, low=money_units, extra=money_units)
    @SETTINGS
    def test_lower_never_exceeds_upper(self, payoff, gross, low, extra):
        interval = ProfitInterval(
            worst_case_payoff=Money.from_units(payoff),
            gross_cost=Money.from_units(gross),
            fee_lower=Money.from_units(low),
            fee_upper=Money.from_units(low + extra),
        )
        assert interval.profit_lower_bound <= interval.profit_upper_bound

    @given(payoff=money_units, gross=money_units, low=money_units, extra=money_units)
    @SETTINGS
    def test_raising_any_leg_cost_cannot_improve_profit(self, payoff, gross, low, extra):
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
    def test_raising_a_fee_upper_bound_cannot_improve_the_guarantee(
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


class TestExactArithmetic:
    @given(
        notionals=st.lists(notional_units, min_size=MIN_BASKET_MEMBERS, max_size=6),
        quantity=quantity_units,
    )
    @SETTINGS
    def test_no_float_appears_in_any_payoff(self, notionals, quantity):
        prices = [Price.from_units(n) for n in notionals]
        payoff = evaluate_basket(prices, Quantity.from_units(quantity))
        assert isinstance(payoff.minimum, Money)
        assert isinstance(payoff.minimum.units, int)
        for entry in payoff.per_state:
            assert isinstance(entry.total, Money)
            for _, amount in entry.by_position:
                assert isinstance(amount, Money)

    @given(notional=notional_units)
    @SETTINGS
    def test_zero_quantity_pays_nothing_in_every_state(self, notional):
        prices = [Price.from_units(notional)] * 3
        payoff = evaluate_basket(prices, Quantity.zero())
        assert payoff.minimum == payoff.maximum == Money.zero()
