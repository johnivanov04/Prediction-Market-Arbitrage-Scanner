"""Phase-2B: does ForecastEx have usable cross-market relations?

Structurally, yes. Economically the same-market answer was settled by inverse
pricing; here it is settled by something else, and the reason is worth stating
up front because it is the whole finding:

    Rule 604 offsetting applies only to "the same Forecast Market". A
    cross-market basket therefore has no netting escape and must be held to
    Resolution -- which is exactly where ForecastEx's one unresolved settlement
    path lives.

In Phase 2A the same-market pair was safe *because* Rule 604 nets it at $1.00
within the day, before Rules 414 and 415 can touch it. Cross-market baskets lose
that protection. The rule that rescued the first case is unavailable in the
second, and Rule 414(b)(3) -- the committee "fair allocation" capped at $1.00
with no floor -- becomes reachable for precisely the positions a relation basket
must hold.

The relation itself is clean
----------------------------
Every economic product examined asks "Will [X] exceed [threshold] in [date]?" and
lists several thresholds per expiration at the exchange's discretion. Two strikes
on one release give a mechanical implication: same Underlying, same Source
Agency, same Resolution Time, and for CPI explicitly "only the initial value from
the official release", so both contracts resolve on one and the same number.

What the ladder does *not* give is mutual exclusion or exhaustiveness. An
"exceed" ladder is monotone -- a high enough print turns every member YES at once
-- so AT_MOST_ONE fails, and no finite set of "exceed" strikes covers an
unbounded domain from below, so AT_LEAST_ONE fails. Neither is a gap in the
research; it is what the product structure is.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

from predarb.domain.money import Price
from predarb.semantics.cross_market_relation import (
    Comparison,
    RelationClass,
    RelationFeasibility,
    RelationProof,
    ThresholdContract,
    nested_implication,
    vertical_spread_economics,
)

__all__ = [
    "CPI_LADDER",
    "ECONOMIC_LADDER_FEASIBILITY",
    "NESTED_SPREAD_ECONOMICS",
    "ORDINARY_AND_REVIEW",
    "RULE_414_ACCELERATED",
    "cpi_ladder_relation",
]

ORDINARY_AND_REVIEW: Final[frozenset[str]] = frozenset(
    {"ordinary_settlement_603a", "event_review_415"}
)
"""Settlement paths on which a same-measurement implication still binds.

Rule 603(a) resolves both legs from the same observed value. Rule 415 has the
Event Review Committee "determine a final Outcome", and Outcome is defined as
"whether an Event Question resolves to 'Yes' or 'No'" -- a binary verdict that
feeds 603(a). Both leave the two legs tied to one number.
"""

RULE_414_ACCELERATED: Final = "rule_414_accelerated_settlement"
"""The path that breaks it.

Rule 414(b)(2) settles from "the most recent last prices of the Forecast
Contracts affected" -- each leg at its own price, no longer bound to a common
value. Rule 414(b)(3) hands an undetermined case to the Event Review Committee
for "a binding determination of fair allocation", capped only from above: "In no
event shall the combined payout for a single 'Yes' Position and a single 'No'
Position exceed $1.00." That cap is per Forecast Market, and a cross-market
basket holds one side of two different markets, so it does not even bound this
basket. Worst case is not the notional; it is zero.
"""

CPI_LADDER: Final[tuple[ThresholdContract, ...]] = (
    ThresholdContract(
        identifier="CPIUS-2026-11-320.00",
        underlying="US CPI index value, initial release",
        resolution_key="CPIUS|2026-11|BLS initial print|no revisions",
        threshold=Decimal("320.00"),
        comparison=Comparison.STRICTLY_ABOVE,
    ),
    ThresholdContract(
        identifier="CPIUS-2026-11-322.00",
        underlying="US CPI index value, initial release",
        resolution_key="CPIUS|2026-11|BLS initial print|no revisions",
        threshold=Decimal("322.00"),
        comparison=Comparison.STRICTLY_ABOVE,
    ),
)
"""An illustrative pair from the CPI ladder.

Thresholds are listed "at ForecastEx's discretion" per expiration, so the exact
strikes are a live-data question; the *structure* is what the terms establish.
The resolution key encodes what must match for the two legs to resolve on one
number, including the terms' own restriction to "the initial CPI value from the
official release".
"""


def cpi_ladder_relation() -> RelationProof:
    """The implication the CPI ladder supports, with the paths it survives."""
    base = nested_implication(CPI_LADDER[0], CPI_LADDER[1])
    return RelationProof(
        relation=base.relation,
        members=base.members,
        reasoning=(
            base.reasoning + ". Both contracts name the same Underlying, Source Agency and "
            "Resolution Time, and the CPI terms restrict resolution to the initial "
            "value from the official release, so a single number decides both legs."
        ),
        survives_settlement_paths=ORDINARY_AND_REVIEW,
        broken_by_settlement_paths=frozenset({RULE_414_ACCELERATED}),
    )


NESTED_SPREAD_ECONOMICS: Final = vertical_spread_economics(
    notional=Price.from_value("1.0000"), per_leg_fee=None
)
"""Buy YES on the near strike, buy NO on the far strike.

Worst case is $1.00 under ordinary settlement, so the basket locks whenever the
two bids together cost under a dollar. Inverse pricing turns that into a
requirement that the YES price on the *further* strike exceed the YES price on
the nearer one by more than a tick -- a monotonicity violation of more than one
cent. Unlike the same-market case, nothing in the matching rules makes that
impossible: Rule 401(d) constrains the two sides of one Forecast Market and says
nothing about prices across markets.
"""

ECONOMIC_LADDER_FEASIBILITY: Final = RelationFeasibility.SEMANTICS_UNRESOLVED
"""The verdict for ForecastEx economic threshold ladders.

Not NO_USEFUL_RELATION: the implication is real and mechanically provable.
Not PROVEN_BUT_VENUE_MECHANICS_ELIMINATE_EDGE: cross-market prices are
unconstrained by inverse pricing, so a positive edge is algebraically reachable.

SEMANTICS_UNRESOLVED, because Rule 414 is reachable for a position that cannot
be netted away, and on that path the two legs settle independently and the worst
case falls to zero. This is the same single gap that left Phase 2A's complement
claim unproven, arriving by a different route.
"""

FAMILY_SUMMARY: Final[tuple[tuple[str, str, RelationClass, RelationFeasibility], ...]] = (
    (
        "CPI[region]",
        "Will [region]'s CPI exceed [XXX.XX] in [date]?",
        RelationClass.NESTED_IMPLICATION,
        RelationFeasibility.SEMANTICS_UNRESOLVED,
    ),
    (
        "GDP[country]",
        "Will [Country]'s quarterly change in real GDP exceed [#.#]% in [date]?",
        RelationClass.NESTED_IMPLICATION,
        RelationFeasibility.SEMANTICS_UNRESOLVED,
    ),
    (
        "IJC",
        "Will US Initial Jobless Claims exceed [number] for the week ending [date]?",
        RelationClass.NESTED_IMPLICATION,
        RelationFeasibility.SEMANTICS_UNRESOLVED,
    ),
    (
        "HS",
        "Will US Housing Starts exceed [number] in [month][year]?",
        RelationClass.NESTED_IMPLICATION,
        RelationFeasibility.SEMANTICS_UNRESOLVED,
    ),
    (
        "DEBT[type]",
        "Will [debt type] exceed $[###] in [quarter] [year]?",
        RelationClass.NESTED_IMPLICATION,
        RelationFeasibility.SEMANTICS_UNRESOLVED,
    ),
    (
        "FES",
        "Will [expiration] S&P 500 Index Futures settle above [$####.##] on [date]?",
        RelationClass.NESTED_IMPLICATION,
        RelationFeasibility.SEMANTICS_UNRESOLVED,
    ),
    (
        "FF",
        "Will the US Federal Funds Target Rate be set above [#.##%] at the FOMC "
        "meeting ending [date]?",
        RelationClass.NESTED_IMPLICATION,
        RelationFeasibility.SEMANTICS_UNRESOLVED,
    ),
    (
        "Success",
        "[Varies] -- sports and achievement outcomes",
        RelationClass.NONE,
        RelationFeasibility.NO_USEFUL_RELATION,
    ),
)
"""Product families examined, by structural class.

Every economic and financial family shares one pattern: a monotone "exceed"
ladder over a single authoritative release. The Success family is the only one
carrying Participant Split and Fair Price, and it lists distinct achievement
questions rather than a ladder, so it offers no mechanical relation at all.
"""
