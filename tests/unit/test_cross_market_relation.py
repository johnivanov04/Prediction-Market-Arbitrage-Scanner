"""Relations between distinct contracts, and whether a basket over them locks.

Two things are kept strictly apart here, because conflating them is how a
research pass talks itself into a venue:

* whether the relation is **mechanically provable** from contract language;
* whether it **survives every reachable settlement path**.

A relation true only under ordinary settlement is not a relation a basket may
rely on. Nothing in this module accepts correlation, adjacency, naming or
apparent ranges as evidence of anything.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from predarb.domain.money import Price
from predarb.semantics.cross_market_relation import (
    BasketEconomics,
    Comparison,
    RelationClass,
    RelationFeasibility,
    RelationProof,
    ThresholdContract,
    at_most_one_over,
    covers_domain,
    nested_implication,
    relation_members_key,
    vertical_spread_economics,
)
from predarb.venues.forecastex.relation_findings import (
    CPI_LADDER,
    ECONOMIC_LADDER_FEASIBILITY,
    NESTED_SPREAD_ECONOMICS,
    RULE_414_ACCELERATED,
    cpi_ladder_relation,
)

pytestmark = pytest.mark.unit

N = Price.from_value("1.0000")
KEY = "CPIUS|2026-11|BLS initial print|no revisions"


def contract(
    name: str,
    threshold: str,
    *,
    key: str = KEY,
    comparison: Comparison = Comparison.STRICTLY_ABOVE,
    underlying: str = "US CPI index value, initial release",
) -> ThresholdContract:
    return ThresholdContract(
        identifier=name,
        underlying=underlying,
        resolution_key=key,
        threshold=Decimal(threshold),
        comparison=comparison,
    )


LOW = contract("low", "320.00")
HIGH = contract("high", "322.00")


class TestNestedThresholdImplication:
    def test_a_higher_strike_implies_a_lower_one(self):
        proof = nested_implication(LOW, HIGH)
        assert proof.relation is RelationClass.NESTED_IMPLICATION

    def test_the_implication_holds_at_every_value(self):
        """Checked directly rather than argued."""
        for raw in ("319.99", "320.00", "320.01", "321.99", "322.00", "322.01", "400.00"):
            value = Decimal(raw)
            if HIGH.yes_at(value):
                assert LOW.yes_at(value), raw

    def test_reversing_the_pair_is_not_an_implication(self):
        assert nested_implication(HIGH, LOW).relation is RelationClass.NONE

    def test_equal_thresholds_are_not_nested(self):
        assert nested_implication(LOW, contract("same", "320.00")).relation is (RelationClass.NONE)

    def test_a_different_underlying_implies_nothing(self):
        other = contract("other", "322.00", underlying="Euro area CPI index value")
        assert nested_implication(LOW, other).relation is RelationClass.NONE

    def test_a_different_period_implies_nothing(self):
        """The trap: two contracts that look identical but measure different months."""
        other = contract("other", "322.00", key="CPIUS|2026-12|BLS initial print|no revisions")
        proof = nested_implication(LOW, other)
        assert proof.relation is RelationClass.NONE
        assert "same observed number" in proof.reasoning

    def test_a_different_revision_policy_implies_nothing(self):
        other = contract("other", "322.00", key="CPIUS|2026-11|BLS revised print|revisions")
        assert nested_implication(LOW, other).relation is RelationClass.NONE

    def test_downward_ladders_nest_the_other_way(self):
        below_hi = contract("bh", "322.00", comparison=Comparison.STRICTLY_BELOW)
        below_lo = contract("bl", "320.00", comparison=Comparison.STRICTLY_BELOW)
        assert nested_implication(below_hi, below_lo).relation is (RelationClass.NESTED_IMPLICATION)
        assert nested_implication(below_lo, below_hi).relation is RelationClass.NONE

    def test_mixed_comparisons_do_not_relate(self):
        below = contract("b", "322.00", comparison=Comparison.STRICTLY_BELOW)
        assert nested_implication(LOW, below).relation is RelationClass.NONE


class TestThresholdLaddersGiveNoMutualExclusion:
    def test_a_monotone_ladder_is_not_at_most_one(self):
        proof = at_most_one_over([LOW, HIGH, contract("higher", "324.00")])
        assert proof.relation is RelationClass.NONE
        assert "every member YES" in proof.reasoning

    def test_a_high_value_makes_every_member_yes(self):
        ladder = [LOW, HIGH, contract("higher", "324.00")]
        assert all(c.yes_at(Decimal("400.00")) for c in ladder)

    def test_a_single_member_is_not_a_relation(self):
        assert at_most_one_over([LOW]).relation is RelationClass.NONE


class TestExhaustivenessNeedsDomainCoverage:
    def test_an_unbounded_domain_is_never_covered(self):
        assert not covers_domain([LOW, HIGH], domain_low=None, domain_high=None)

    def test_a_domain_starting_below_the_lowest_strike_is_not_covered(self):
        """A print under every strike leaves all members NO."""
        assert not covers_domain(
            [LOW, HIGH], domain_low=Decimal("0.00"), domain_high=Decimal("500.00")
        )

    def test_a_domain_starting_above_the_lowest_strike_is_covered(self):
        assert covers_domain(
            [LOW, HIGH], domain_low=Decimal("321.00"), domain_high=Decimal("500.00")
        )

    def test_a_strict_comparison_needs_the_bound_strictly_above(self):
        """X > 320 is NO at exactly 320, so a domain starting at 320 is not covered."""
        assert not covers_domain([LOW], domain_low=Decimal("320.00"), domain_high=Decimal("400.00"))

    def test_an_inclusive_comparison_is_satisfied_at_the_bound(self):
        inclusive = contract("inc", "320.00", comparison=Comparison.AT_OR_ABOVE)
        assert covers_domain(
            [inclusive], domain_low=Decimal("320.00"), domain_high=Decimal("400.00")
        )

    def test_no_members_covers_nothing(self):
        assert not covers_domain([], domain_low=Decimal("0"), domain_high=Decimal("1"))


class TestAlternateSettlementCanInvalidateARelation:
    def test_a_relation_broken_on_any_path_does_not_survive(self):
        proof = cpi_ladder_relation()
        assert proof.relation is RelationClass.NESTED_IMPLICATION
        assert not proof.survives_all_paths

    def test_the_breaking_path_is_named(self):
        assert RULE_414_ACCELERATED in cpi_ladder_relation().broken_by_settlement_paths

    def test_the_surviving_paths_are_named(self):
        surviving = cpi_ladder_relation().survives_settlement_paths
        assert "ordinary_settlement_603a" in surviving
        assert "event_review_415" in surviving

    def test_ordinary_settlement_alone_is_not_sufficient(self):
        """The standard: a relation true only under ordinary settlement fails."""
        proof = cpi_ladder_relation()
        assert proof.survives_settlement_paths
        assert not proof.survives_all_paths

    def test_a_relation_with_no_broken_paths_does_survive(self):
        clean = RelationProof(
            relation=RelationClass.NESTED_IMPLICATION,
            members=("a", "b"),
            reasoning="test",
            survives_settlement_paths=frozenset({"ordinary"}),
        )
        assert clean.survives_all_paths

    def test_no_relation_never_survives_however_clean_the_paths(self):
        none = RelationProof(relation=RelationClass.NONE, members=("a",), reasoning="no relation")
        assert not none.survives_all_paths


class TestSameMarketOffsetDoesNotImplyCrossMarketOffset:
    def test_the_two_legs_are_different_forecast_markets(self):
        """Rule 604 offsets only within one Forecast Market, so a cross-market
        basket has no netting escape and must be held to Resolution."""
        assert CPI_LADDER[0].identifier != CPI_LADDER[1].identifier
        assert CPI_LADDER[0].threshold != CPI_LADDER[1].threshold

    def test_the_basket_is_therefore_exposed_to_the_resolution_path(self):
        assert RULE_414_ACCELERATED in cpi_ladder_relation().broken_by_settlement_paths

    def test_the_legs_share_a_measurement_but_not_a_market(self):
        """Same number decides both, which is what makes the implication work;
        different markets, which is what removes the netting protection."""
        assert CPI_LADDER[0].same_measurement_as(CPI_LADDER[1])


class TestSymbolicBasketEconomics:
    def test_the_vertical_spread_worst_case_is_the_notional(self):
        assert NESTED_SPREAD_ECONOMICS.worst_case_payoff == N

    def test_the_required_inequality_is_cost_under_the_worst_case(self):
        assert "< 1.0000" in NESTED_SPREAD_ECONOMICS.required_inequality

    def test_the_worst_case_is_verified_over_every_region(self):
        """Buy YES(low) and NO(high); check all three terminal regions."""
        one = N.units
        for raw in ("319.00", "321.00", "323.00"):
            value = Decimal(raw)
            yes_low = one if LOW.yes_at(value) else 0
            no_high = 0 if HIGH.yes_at(value) else one
            assert yes_low + no_high >= one, raw

    def test_the_middle_region_pays_twice(self):
        value = Decimal("321.00")
        assert LOW.yes_at(value) and not HIGH.yes_at(value)

    def test_a_two_leg_basket_is_recorded_as_two_legs(self):
        assert NESTED_SPREAD_ECONOMICS.legs == 2

    def test_fees_enter_the_inequality_when_supplied(self):
        with_fee = vertical_spread_economics(N, per_leg_fee=Price.from_value("0.0100"))
        assert "2 x 0.0100" in with_fee.required_inequality


class TestVenueImposedAlgebraicImpossibility:
    def test_a_cost_floor_at_or_above_the_payoff_is_impossible(self):
        """The ForecastEx same-market case: inverse pricing fixes a pair at one
        cent over the notional, so no quote can rescue it."""
        same_market = BasketEconomics(
            relation=RelationClass.EXACTLY_ONE,
            legs=2,
            notional=N,
            worst_case_payoff=N,
        )
        assert same_market.is_algebraically_impossible(
            minimum_total_cost=Price.from_value("1.0100")
        )

    def test_a_cost_floor_below_the_payoff_is_not_impossible(self):
        """Cross-market: nothing constrains the sum of prices across markets."""
        assert not NESTED_SPREAD_ECONOMICS.is_algebraically_impossible(
            minimum_total_cost=Price.from_value("0.9900")
        )

    def test_equality_counts_as_impossible(self):
        """Paying exactly the worst case locks in zero, which is not an edge."""
        assert NESTED_SPREAD_ECONOMICS.is_algebraically_impossible(minimum_total_cost=N)

    def test_the_cross_market_verdict_is_semantics_not_economics(self):
        """Not venue elimination and not an absent relation: the implication is
        real and the edge is algebraically reachable. What is unresolved is a
        settlement path."""
        assert ECONOMIC_LADDER_FEASIBILITY is RelationFeasibility.SEMANTICS_UNRESOLVED

    def test_the_relation_behind_that_verdict_is_a_real_one(self):
        assert cpi_ladder_relation().relation is RelationClass.NESTED_IMPLICATION

    def test_and_the_edge_is_not_algebraically_closed_off(self):
        assert not NESTED_SPREAD_ECONOMICS.is_algebraically_impossible(
            minimum_total_cost=Price.from_value("0.9800")
        )
        assert cpi_ladder_relation().relation is RelationClass.NESTED_IMPLICATION


class TestMemberOrderingInvariance:
    def test_a_proof_sorts_its_members(self):
        assert nested_implication(LOW, HIGH).members == ("high", "low")

    def test_the_member_key_is_order_independent(self):
        assert relation_members_key(["b", "a", "c"]) == relation_members_key(["c", "b", "a"])

    def test_the_member_key_deduplicates(self):
        assert relation_members_key(["a", "a", "b"]) == ("a", "b")

    def test_two_proofs_over_the_same_pair_agree_on_members(self):
        first = nested_implication(LOW, HIGH)
        second = nested_implication(contract("low", "320.00"), contract("high", "322.00"))
        assert first.members == second.members
