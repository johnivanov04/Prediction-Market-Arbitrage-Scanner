"""The Polymarket US combinatorial product, and the state its own legs can reach.

The Combinatoric Athletic Outcome Contract is the strongest relation language
found anywhere in phases 1 and 2. It does not merely imply a relation, it states
one as a biconditional:

    "Every outcome must be satisfied for the Contract to resolve to $1.00. The
     Contract resolves to $1.00 if and only if every leg is satisfied. If any
     single leg is not satisfied, the Contract resolves to $0.00, regardless of
     the outcomes of any remaining unsettled legs."

From that alone -- no correlation, no price assumption -- holding the
combination long against a short in each leg is an AT_LEAST_ONE set: the
all-satisfied state pays the combination, and any single failure pays that
leg's short. Worst case one notional, and nothing at this venue fixes the sum
of the prices, so unlike ForecastEx the edge is not closed off by algebra.

Then the legs are read. CAOC describes its constituents as settling
"$1.00/$0.00", and its Underlying as "whether all the specified constituent
Contracts, settle to the specified side". But the athletic contracts those legs
are have a third state: Rule 9.101(D) pays **both** sides $0.50 on a tie. In
that state the combination pays $0.00 while the short leg pays only $0.50, and
the worst case falls to half the notional. The relation is real; the basket
built on it is not.

This is not a drafting quibble to be resolved by picking the sensible reading.
Either the tie makes the leg "not satisfied" -- and then the arithmetic above
holds and the basket floor is $0.50 -- or the Payout Condition does not cover
the state at all, and the combination has no stated outcome. Both fail.
"""

from __future__ import annotations

from itertools import product
from typing import Final

from predarb.domain.money import Price
from predarb.semantics.cross_market_relation import (
    BasketEconomics,
    CombinationContract,
    RelationClass,
    RelationFeasibility,
    RelationProof,
    combination_and,
    combination_basket_economics,
)

__all__ = [
    "CAOC",
    "CAOC_BASKET_ECONOMICS",
    "CAOC_FEASIBILITY",
    "CAOC_TIE_FLOOR",
    "ORDINARY_BINARY_9101D",
    "PAIR_COST_FLOOR",
    "PRODUCT_CANCELLATION_9101K",
    "SAME_MARKET_PAIR",
    "TIE_BROKEN_BASKET",
    "TIE_SPLIT_9101D",
    "LegOutcome",
    "basket_payoff",
    "caoc_leg_implications",
    "caoc_relation",
    "worst_case_over",
]

NOTIONAL: Final = Price.from_value("1.0000")
TIE_PAYOUT: Final = Price.from_value("0.5000")

ORDINARY_BINARY_9101D: Final = "ordinary_binary_9101D"
TIE_SPLIT_9101D: Final = "tie_split_9101D"
PRODUCT_CANCELLATION_9101K: Final = "product_cancellation_9101K"
OUTCOME_REVIEW_104: Final = "outcome_review_10_4"

CAOC: Final = CombinationContract(
    identifier="CAOC:two-leg",
    legs=("aec:leg_a", "aec:leg_b"),
)
"""A two-leg combination over athletic event contracts.

Two legs because that is the minimum the certification allows ("two or more"),
and because the break is already visible there -- more legs add states, none of
which repair it.
"""


class LegOutcome:
    """The three states Rule 9.101(D) gives an athletic leg."""

    OCCURS: Final = "occurs"
    DOES_NOT_OCCUR: Final = "does_not_occur"
    TIE: Final = "tie"

    ALL: Final = (OCCURS, DOES_NOT_OCCUR, TIE)


def _leg_payouts(outcome: str) -> tuple[Price, Price]:
    """``(long, short)`` for one athletic leg, from Rule 9.101(D)."""
    if outcome == LegOutcome.OCCURS:
        return NOTIONAL, Price.from_value("0")
    if outcome == LegOutcome.DOES_NOT_OCCUR:
        return Price.from_value("0"), NOTIONAL
    if outcome == LegOutcome.TIE:
        return TIE_PAYOUT, TIE_PAYOUT
    raise ValueError(f"unknown leg outcome {outcome!r}")


def caoc_relation() -> RelationProof:
    """The AT_LEAST_ONE set the combination's biconditional gives.

    Recorded with the settlement paths it survives, because that is the part a
    basket may rely on -- and it does not survive the tie.
    """
    return combination_and(
        CAOC,
        survives=(ORDINARY_BINARY_9101D, OUTCOME_REVIEW_104),
        broken_by=(TIE_SPLIT_9101D, PRODUCT_CANCELLATION_9101K),
    )


CAOC_BASKET_ECONOMICS: Final = combination_basket_economics(CAOC, NOTIONAL)
"""What the basket would be worth if the legs really were two-state."""

CAOC_TIE_FLOOR: Final = TIE_PAYOUT
"""What its worst case actually is, once Rule 9.101(D)'s tie is admitted."""


def basket_payoff(leg_outcomes: dict[str, str]) -> Price:
    """Total payoff of: long the combination, short every leg.

    The combination's own words decide its side -- it pays the notional if and
    only if every leg is satisfied -- and Rule 9.101(D) decides each short's.
    A tie is read as the leg's specified Outcome not occurring, which is the
    reading the certification's "if any single leg is not satisfied" language
    supports; the alternative is that the state is unaddressed entirely.
    """
    combo_pays = CAOC.yes_given({leg: leg_outcomes[leg] == LegOutcome.OCCURS for leg in CAOC.legs})
    units = NOTIONAL.units if combo_pays else 0
    units += sum(_leg_payouts(leg_outcomes[leg])[1].units for leg in CAOC.legs)
    return Price(units)


def worst_case_over(outcomes: tuple[str, ...]) -> Price:
    """Minimum basket payoff over every assignment drawn from ``outcomes``."""
    return min(
        basket_payoff(dict(zip(CAOC.legs, assignment, strict=True)))
        for assignment in product(outcomes, repeat=len(CAOC.legs))
    )


CAOC_FEASIBILITY: Final = RelationFeasibility.SEMANTICS_UNRESOLVED
"""Not NO_USEFUL_RELATION: the relation is proven, and stated more clearly than
anywhere else in this research. Not economically eliminated either -- no venue
mechanic fixes the sum of a combination's price and its legs'. What blocks is
that the legs' own certification reaches a state the combination's does not
model, and the precedence clause that would settle which document wins is
scoped to 'trading in' the Contract. See
:mod:`predarb.venues.polymarket_us.settlement_findings`."""

TIE_BROKEN_BASKET: Final = BasketEconomics(
    relation=RelationClass.AT_LEAST_ONE,
    legs=1 + len(CAOC.legs),
    notional=NOTIONAL,
    worst_case_payoff=CAOC_TIE_FLOOR,
    total_bid_cost_symbol="p(combo) + p(short:leg_a) + p(short:leg_b)",
    notes=(
        "Worst case is half the notional, in the state where one leg ties and "
        "the other is satisfied: the combination pays nothing, the tied leg's "
        "short pays $0.50, and the satisfied leg's short pays nothing.",
        "A three-leg basket costing under $0.50 would still lock, but the two "
        "shorts alone are worth more than that in any market where the "
        "combination is not near-certain, so the inequality is not reachable in "
        "practice rather than merely unattractive.",
    ),
)


def caoc_leg_implications() -> tuple[RelationProof, ...]:
    """One implication per leg, read straight off the biconditional.

    "if and only if every leg is satisfied" gives both directions; this is the
    forward one, combination implies leg. Weaker than the AT_LEAST_ONE set and
    exposed to exactly the same tie state, but recorded separately because it
    is the relation that survives if only one leg can be traded.
    """
    return tuple(
        RelationProof(
            relation=RelationClass.NESTED_IMPLICATION,
            members=(CAOC.identifier, leg),
            reasoning=(
                f"The combination pays only if every leg is satisfied, so "
                f"{CAOC.identifier} paying entails {leg} paying. The converse "
                "does not hold: the other legs may fail."
            ),
            survives_settlement_paths=frozenset({ORDINARY_BINARY_9101D, OUTCOME_REVIEW_104}),
            broken_by_settlement_paths=frozenset({TIE_SPLIT_9101D, PRODUCT_CANCELLATION_9101K}),
        )
        for leg in CAOC.legs
    )


PAIR_COST_FLOOR: Final = Price.from_value("0.0020")
"""Cheapest a long and a short in one athletic contract may be quoted, together.

Rule 9.101(F) bans orders outside $0.001 to $0.999 and sets the increment at
$0.001; nothing anywhere ties the two sides' prices to each other. This is a
statement about what the rules permit, not about what a book will show.
"""

SAME_MARKET_PAIR: Final = BasketEconomics(
    relation=RelationClass.EXACTLY_ONE,
    legs=2,
    notional=NOTIONAL,
    worst_case_payoff=NOTIONAL,
    total_bid_cost_symbol="p(long) + p(short)",
    notes=(
        "Recorded to answer one question: does a venue mechanic close the edge "
        "before any quote is seen, as ForecastEx Rule 401(d) does by fixing an "
        "inverse pair at $1.01? Here, no. The rules constrain each side to "
        "$0.001-$0.999 independently and impose no identity between them.",
        "A tie pays both sides $0.50, so the pair's worst case is the notional "
        "in all three of Rule 9.101(D)'s states -- the tie conserves, and does "
        "not damage a same-market pair the way it damages a combination basket.",
        "Fees are charged to the balance at execution rather than netted out of "
        "settlement, so they are an addition to the cost side of this "
        "inequality and never reduce the payoff.",
    ),
)
