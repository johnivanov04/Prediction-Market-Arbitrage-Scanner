"""Relations among Rothera contracts, and the regime that removes their floor.

Two relation families are provable here from filed text alone.

**Soccer is a real partition.** The Payment Criterion makes a winner someone who
"scored more goals (a strictly greater number) than its opponent at the
conclusion of regulation time (90 minutes plus stoppage time only)", and the
Exchange may list a "tie" iteration where "each <soccer team> must have scored
an equal number of goals (including 0-0 draws)". Over one number -- the
regulation-time score -- those three are mutually exclusive and exhaustive.
That is a partition established by text, not by ticker names or by counting
what happens to be listed.

**Core PCE is a threshold ladder.** Its criterion runs over "the month-to-month
change in the monthly Core PCE Price Index for <month>" with comparison
operators and a threshold, and it pins the measurement hard: "Only the
seasonally adjusted figure from the first official release is used for
settlement", with revisions and other sources expressly excluded. Two strikes on
that same number nest.

And then both die the same death. Rothera's cancellation path settles each
contract at *its own* fair market price. Every contract still conserves
internally -- long + short is one notional, exactly, for any price -- but the
partition's floor is a statement about the *sum across members*, and three
independently determined prices are constrained by nothing. Buy all three legs
of a soccer match and have it abandoned, and the basket returns
``p_home + p_away + p_tie``, which no rule requires to be a dollar.

This is the ForecastEx phase-2B failure mode arriving by a different route, and
it is why :mod:`predarb.semantics.payout_relation` separates within-contract
conservation from across-contract coupling. Rothera has the first and not the
second.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

from predarb.domain.money import Price
from predarb.semantics.cross_market_relation import (
    Comparison,
    RelationFeasibility,
    RelationProof,
    ThresholdContract,
    nested_implication,
)
from predarb.semantics.payout_relation import (
    PartitionBasket,
    PriceCoupling,
    SettlementRegime,
)

__all__ = [
    "BASEBALL_PAIR",
    "CORE_PCE_HIGH",
    "CORE_PCE_LOW",
    "EMERGENCY",
    "FAIR_MARKET_DELAY",
    "NOTIONAL",
    "ORDINARY",
    "REACHABLE_REGIMES",
    "ROTHERA_FEASIBILITY",
    "SOCCER_TRIPLE",
    "core_pce_ladder",
    "hypothetical_coupled_partition",
]

NOTIONAL: Final = Price.from_value("1.0000")

_PCE_KEY: Final = "CorePCE|<month>|BEA first official release|seasonally adjusted|no revisions"

ORDINARY: Final = "ordinary_binary"
FAIR_MARKET_DELAY: Final = "fair_market_price_source_delay"
EMERGENCY: Final = "emergency_rule_1_11"

REACHABLE_REGIMES: Final = frozenset(
    {SettlementRegime.BINARY_OUTCOME, SettlementRegime.RESIDUAL_PRICE}
)
"""Both are reachable on any listed match: ordinary result, or abandonment.

``INDEPENDENT_PAYOUTS`` is deliberately absent. No Rothera clause sets the two
sides separately -- the residual construction covers every fair-market path that
states a split, and the emergency power is recorded as a mechanism rather than a
regime because it can change the terms themselves rather than settle under them.
"""

SOCCER_TRIPLE: Final = PartitionBasket(
    members=("soccer:home_win", "soccer:away_win", "soccer:tie"),
    notional=NOTIONAL,
    partition_proven=True,
    reachable_regimes=REACHABLE_REGIMES,
    price_coupling=PriceCoupling.INDEPENDENT,
    reasoning=(
        "Regulation-time goals decide all three: strictly greater for either "
        "team, equal for the tie iteration. Mutually exclusive and exhaustive "
        "over one number, so exactly one is satisfied in every ordinary "
        "terminal state. On abandonment each member settles at its own fair "
        "market price and no rule ties the three prices to a dollar."
    ),
    notes=(
        "Extra time and penalties are excluded from the winner determination, "
        "so a match decided on penalties settles the tie iteration Yes. That "
        "sharpens the partition rather than breaking it.",
        "The coupling is INDEPENDENT rather than UNKNOWN because the clause is "
        "explicit about what it does -- it fixes a price per Contract -- and "
        "silent about any relationship between Contracts.",
    ),
)

BASEBALL_PAIR: Final = PartitionBasket(
    members=("baseball:team_a", "baseball:team_b"),
    notional=NOTIONAL,
    partition_proven=True,
    reachable_regimes=REACHABLE_REGIMES,
    price_coupling=PriceCoupling.INDEPENDENT,
    reasoning=(
        "Baseball admits no tie: the winner scored strictly more runs 'at the "
        "conclusion of the full game, including any extra innings', so exactly "
        "one team contract is Yes. A forfeit is handled explicitly and "
        "preserves the partition -- the forfeiting team resolves No and the "
        "opponent Yes. Abandonment does not."
    ),
    notes=(
        "Disqualification before first pitch settles at a fair market price "
        "whose split the certification does not state, so that path is doubly "
        "unresolved: no floor across the pair, and no stated complement within "
        "either contract.",
    ),
)


def _pce(
    name: str, threshold: str, comparison: Comparison = Comparison.STRICTLY_ABOVE
) -> ThresholdContract:
    return ThresholdContract(
        identifier=name,
        underlying="month-to-month change in the monthly Core PCE Price Index",
        resolution_key=_PCE_KEY,
        threshold=Decimal(threshold),
        comparison=comparison,
    )


CORE_PCE_LOW: Final = _pce("pce_above_0.20", "0.20")
CORE_PCE_HIGH: Final = _pce("pce_above_0.30", "0.30")


def core_pce_ladder() -> RelationProof:
    """The nested implication two Core PCE strikes give.

    Recorded as surviving ordinary settlement and broken by the fair-market
    path, for the same reason the partitions are: the implication constrains
    which contracts are Yes, and in that path none of them is.
    """
    base = nested_implication(CORE_PCE_LOW, CORE_PCE_HIGH)
    return RelationProof(
        relation=base.relation,
        members=base.members,
        reasoning=(
            base.reasoning + ". The certification pins the measurement to the "
            "seasonally adjusted figure from the BEA's first official release "
            "and expressly excludes revisions and other sources, so one number "
            "decides both strikes."
        ),
        survives_settlement_paths=frozenset({ORDINARY}),
        broken_by_settlement_paths=frozenset({FAIR_MARKET_DELAY, EMERGENCY}),
    )


def hypothetical_coupled_partition(basket: PartitionBasket) -> PartitionBasket:
    """The same basket if Rothera coupled its members' prices to the notional.

    Not a finding -- a control. It shows the machinery is not returning zero for
    everything, and it names exactly what one sentence in a future amendment
    would have to say for these baskets to become usable.
    """
    return PartitionBasket(
        members=basket.members,
        notional=basket.notional,
        partition_proven=basket.partition_proven,
        reachable_regimes=basket.reachable_regimes,
        price_coupling=PriceCoupling.COUPLED_TO_NOTIONAL,
        reasoning=(
            "Hypothetical: assumes text requiring the fair market prices of a "
            "partition's members to sum to the Settlement Value. No such text "
            "exists at Rothera."
        ),
    )


ROTHERA_FEASIBILITY: Final = RelationFeasibility.SEMANTICS_UNRESOLVED
"""The relations are proven and the venue imposes no pricing identity, so this
is neither NO_USEFUL_RELATION nor venue-eliminated. What blocks is that one
reachable settlement regime removes the basket floor while leaving every
individual contract perfectly conserved."""
