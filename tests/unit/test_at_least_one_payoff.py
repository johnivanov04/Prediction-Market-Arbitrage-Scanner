"""The singleton-witness theorem, cross-checked against real enumeration.

The symbolic solver claims a minimum without building the state space. These
tests build it anyway, for small n, and check the two agree -- which is the only
way to know the shortcut is a theorem and not an assumption.
"""

from __future__ import annotations

from collections.abc import Iterable
from itertools import combinations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from predarb.domain.at_least_one_payoff import (
    MemberPayoffSpec,
    PayoffTheorem,
    PreconditionStatus,
    UnprovablePayoffError,
    prove_at_least_one_floor,
)
from predarb.domain.money import InexactValueError, Money, Price, Quantity

pytestmark = pytest.mark.unit

FORBIDDEN = "ALL_SELECTED_MEMBERS_NO"


def spec(ticker: str, notional: str, *, losing: str = "0.0000") -> MemberPayoffSpec:
    return MemberPayoffSpec(
        ticker=ticker,
        winning_payoff_per_contract=Price.from_value(notional),
        losing_payoff_per_contract=Price.from_value(losing),
    )


def enumerate_minimum(specs: list[MemberPayoffSpec], quantity: Quantity) -> Money:
    """The honest, exponential answer: every permitted state, explicitly.

    Permitted means every non-empty winner subset -- ``2**n - 1`` of them. Used
    only here, only for small n, and only to check the shortcut.
    """
    tickers = [s.ticker for s in specs]
    payoff = {s.ticker: s.winning_payoff_per_contract for s in specs}
    best: int | None = None
    for size in range(1, len(tickers) + 1):
        for winners in combinations(tickers, size):
            total = sum(payoff[t].units for t in winners)
            best = total if best is None else min(best, total)
    assert best is not None
    return Price.from_units(best) * quantity


class TestTheoremMatchesEnumeration:
    @pytest.mark.parametrize("n", [2, 3, 4, 5, 6])
    def test_equal_notionals(self, n: int) -> None:
        specs = [spec(f"M{i}", "1.0000") for i in range(n)]
        quantity = Quantity.from_value("1.00")
        proof = prove_at_least_one_floor(specs, quantity=quantity, forbidden_state=FORBIDDEN)
        assert proof.worst_case_payoff == enumerate_minimum(specs, quantity)

    @pytest.mark.parametrize("n", [2, 3, 4, 5])
    def test_differing_notionals(self, n: int) -> None:
        specs = [spec(f"M{i}", f"{1 + i}.0000") for i in range(n)]
        quantity = Quantity.from_value("3.00")
        proof = prove_at_least_one_floor(specs, quantity=quantity, forbidden_state=FORBIDDEN)
        assert proof.worst_case_payoff == enumerate_minimum(specs, quantity)

    def test_the_enumeration_really_covers_two_to_the_n_minus_one_states(self):
        """Guards the cross-check itself: a smaller state set would prove nothing."""
        specs = [spec(f"M{i}", "1.0000") for i in range(4)]
        states = sum(1 for size in range(1, 5) for _ in combinations(range(4), size))
        assert states == 2**4 - 1
        proof = prove_at_least_one_floor(
            specs, quantity=Quantity.from_value("1.00"), forbidden_state=FORBIDDEN
        )
        assert proof.permitted_state_count == states


class TestTheoremProperties:
    @given(
        notionals=st.lists(st.integers(min_value=1, max_value=50_000), min_size=2, max_size=8),
        quantity_units=st.integers(min_value=1, max_value=10_000),
    )
    def test_the_floor_is_quantity_times_the_smallest_notional(
        self, notionals: list[int], quantity_units: int
    ) -> None:
        specs = [
            MemberPayoffSpec(
                ticker=f"M{i}",
                winning_payoff_per_contract=Price.from_units(n),
                losing_payoff_per_contract=Price.from_units(0),
            )
            for i, n in enumerate(notionals)
        ]
        quantity = Quantity.from_units(quantity_units)
        proof = prove_at_least_one_floor(specs, quantity=quantity, forbidden_state=FORBIDDEN)
        assert proof.worst_case_payoff == Price.from_units(min(notionals)) * quantity

    @given(notionals=st.lists(st.integers(min_value=1, max_value=20_000), min_size=2, max_size=6))
    def test_ordering_of_members_does_not_matter(self, notionals: list[int]) -> None:
        def build(order: Iterable[int]) -> list[MemberPayoffSpec]:
            return [
                MemberPayoffSpec(
                    ticker=f"M{i}",
                    winning_payoff_per_contract=Price.from_units(notionals[i]),
                    losing_payoff_per_contract=Price.from_units(0),
                )
                for i in order
            ]

        quantity = Quantity.from_value("1.00")
        forward = prove_at_least_one_floor(
            build(range(len(notionals))), quantity=quantity, forbidden_state=FORBIDDEN
        )
        backward = prove_at_least_one_floor(
            build(reversed(range(len(notionals)))), quantity=quantity, forbidden_state=FORBIDDEN
        )
        assert forward.worst_case_payoff == backward.worst_case_payoff
        assert forward.members == backward.members
        assert forward.witnesses == backward.witnesses

    @given(notionals=st.lists(st.integers(min_value=1, max_value=20_000), min_size=2, max_size=6))
    def test_adding_a_winner_never_reduces_the_payoff(self, notionals: list[int]) -> None:
        """Monotonicity, which is what puts the minimum at a singleton."""
        payoff = {f"M{i}": n for i, n in enumerate(notionals)}
        tickers = list(payoff)
        for size in range(1, len(tickers)):
            for winners in combinations(tickers, size):
                base = sum(payoff[t] for t in winners)
                for extra in set(tickers) - set(winners):
                    assert base + payoff[extra] >= base

    @given(
        notionals=st.lists(st.integers(min_value=1, max_value=20_000), min_size=2, max_size=5),
        quantity_units=st.integers(min_value=1, max_value=500),
    )
    def test_symbolic_minimum_equals_enumeration_minimum(
        self, notionals: list[int], quantity_units: int
    ) -> None:
        specs = [
            MemberPayoffSpec(
                ticker=f"M{i}",
                winning_payoff_per_contract=Price.from_units(n),
                losing_payoff_per_contract=Price.from_units(0),
            )
            for i, n in enumerate(notionals)
        ]
        quantity = Quantity.from_units(quantity_units)
        proof = prove_at_least_one_floor(specs, quantity=quantity, forbidden_state=FORBIDDEN)
        assert proof.worst_case_payoff == enumerate_minimum(specs, quantity)


class TestPreconditions:
    def test_a_nonzero_losing_payoff_breaks_the_theorem(self):
        """The arithmetic assumes a losing leg contributes nothing."""
        specs = [spec("A", "1.0000"), spec("B", "1.0000", losing="0.2000")]
        with pytest.raises(UnprovablePayoffError, match="VIOLATED_NONZERO_LOSING_PAYOFF"):
            prove_at_least_one_floor(
                specs, quantity=Quantity.from_value("1.00"), forbidden_state=FORBIDDEN
            )

    def test_nonnegativity_is_discharged_by_the_type_system(self):
        """The theorem's first precondition cannot be violated through ``Price``.

        A negative winning payout would let adding a winner *reduce* the total,
        moving the minimum away from a singleton. ``Price`` refuses to hold one
        at all, so the precondition is structural rather than checked at
        runtime. The enum member is retained for a future signed payoff
        representation, which must not bypass the check silently.
        """
        with pytest.raises(InexactValueError, match="may not be negative"):
            Price.from_units(-1)
        assert PreconditionStatus.VIOLATED_NEGATIVE_WINNING_PAYOFF.permits_proof is False

    def test_cross_member_dependence_breaks_the_theorem(self):
        specs = [
            spec("A", "1.0000"),
            MemberPayoffSpec(
                ticker="B",
                winning_payoff_per_contract=Price.from_value("1.0000"),
                losing_payoff_per_contract=Price.from_units(0),
                cross_member_dependent=True,
            ),
        ]
        with pytest.raises(UnprovablePayoffError, match="VIOLATED_CROSS_MEMBER_DEPENDENCE"):
            prove_at_least_one_floor(
                specs, quantity=Quantity.from_value("1.00"), forbidden_state=FORBIDDEN
            )

    def test_every_precondition_is_reported_per_member(self):
        specs = [spec("A", "1.0000"), spec("B", "2.0000")]
        proof = prove_at_least_one_floor(
            specs, quantity=Quantity.from_value("1.00"), forbidden_state=FORBIDDEN
        )
        assert proof.preconditions == {
            "A": PreconditionStatus.SATISFIED,
            "B": PreconditionStatus.SATISFIED,
        }
        assert proof.preconditions_hold

    def test_no_members_is_refused(self):
        with pytest.raises(UnprovablePayoffError, match="nothing to bound"):
            prove_at_least_one_floor(
                [], quantity=Quantity.from_value("1.00"), forbidden_state=FORBIDDEN
            )

    def test_duplicate_members_are_refused(self):
        with pytest.raises(UnprovablePayoffError, match="duplicate members"):
            prove_at_least_one_floor(
                [spec("A", "1.0000"), spec("A", "1.0000")],
                quantity=Quantity.from_value("1.00"),
                forbidden_state=FORBIDDEN,
            )

    def test_a_non_positive_quantity_is_refused(self):
        with pytest.raises(UnprovablePayoffError, match="must be positive"):
            prove_at_least_one_floor(
                [spec("A", "1.0000"), spec("B", "1.0000")],
                quantity=Quantity.from_units(0),
                forbidden_state=FORBIDDEN,
            )


class TestNoExponentialWork:
    def test_three_hundred_members_is_instant(self):
        """The count is computed, never enumerated. 2**300 has 91 digits."""
        specs = [spec(f"M{i:03d}", "1.0000") for i in range(300)]
        proof = prove_at_least_one_floor(
            specs, quantity=Quantity.from_value("1.00"), forbidden_state=FORBIDDEN
        )
        assert proof.permitted_state_count == 2**300 - 1
        assert len(str(proof.permitted_state_count)) == 91
        assert proof.worst_case_payoff == Money.from_value("1.000000")

    def test_the_proof_names_its_theorem_and_forbidden_state(self):
        proof = prove_at_least_one_floor(
            [spec("A", "1.0000"), spec("B", "1.0000")],
            quantity=Quantity.from_value("1.00"),
            forbidden_state=FORBIDDEN,
        )
        assert proof.theorem is PayoffTheorem.SINGLETON_WITNESS_MINIMUM
        assert proof.forbidden_state == FORBIDDEN
        assert "permitted state(s)" in proof.describe()

    def test_state_independence_only_at_n_equals_one(self):
        """With two or more members the payoff genuinely varies across states."""
        single = prove_at_least_one_floor(
            [spec("A", "1.0000")], quantity=Quantity.from_value("1.00"), forbidden_state=FORBIDDEN
        )
        assert single.is_state_independent
        pair = prove_at_least_one_floor(
            [spec("A", "1.0000"), spec("B", "1.0000")],
            quantity=Quantity.from_value("1.00"),
            forbidden_state=FORBIDDEN,
        )
        assert not pair.is_state_independent
