"""Relations between *distinct* contracts, and whether a basket over them locks.

Phase 1 built AT_MOST_ONE and AT_LEAST_ONE over the members of one event. This
adds the relation a threshold ladder gives instead: two contracts on the **same
measured value** at different strikes, where one outcome implies the other.

    "Will CPI exceed 320.00 in Nov 2026?"     -- low strike
    "Will CPI exceed 322.00 in Nov 2026?"     -- high strike

    YES(X > 322) implies YES(X > 320)

That implication is mechanical, not statistical, and it follows from the contract
language alone: same Underlying, same Source Agency, same Resolution Time, same
single authoritative value. It is emphatically not correlation, and nothing in
this module will accept correlation as a relation.

What an implication buys you
----------------------------
Buying YES on the low strike and NO on the high strike pays at least the notional
in every ordinary terminal state:

    X > T_hi        YES(lo)=N, NO(hi)=0   -> N
    T_lo < X <= T_hi YES(lo)=N, NO(hi)=N  -> 2N
    X <= T_lo       YES(lo)=0, NO(hi)=N   -> N

so the worst case is N and the basket locks whenever the two bids together cost
less than N.

The part that is easy to get wrong
----------------------------------
A relation proven under ordinary settlement is not proven. Every venue keeps
some discretionary path -- cancellation, committee allocation, accelerated
settlement -- and if any reachable path can settle the two legs *independently*,
the implication stops binding them and the worst case collapses. So a relation
carries the settlement paths it survives, and
:meth:`RelationProof.survives_all_paths` is what a basket may actually rely on.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final

from predarb.domain.money import Price

_MIN_RELATION_MEMBERS: Final = 2

__all__ = [
    "BasketEconomics",
    "Comparison",
    "RelationClass",
    "RelationFeasibility",
    "RelationProof",
    "ThresholdContract",
    "at_most_one_over",
    "covers_domain",
    "nested_implication",
]


class Comparison(StrEnum):
    """How a threshold contract compares the measured value to its strike."""

    STRICTLY_ABOVE = "STRICTLY_ABOVE"
    """YES iff value > threshold. ForecastEx's "exceed" wording."""

    AT_OR_ABOVE = "AT_OR_ABOVE"
    """YES iff value >= threshold."""

    STRICTLY_BELOW = "STRICTLY_BELOW"
    AT_OR_BELOW = "AT_OR_BELOW"

    @property
    def is_upward(self) -> bool:
        return self in {Comparison.STRICTLY_ABOVE, Comparison.AT_OR_ABOVE}


class RelationClass(StrEnum):
    NESTED_IMPLICATION = "NESTED_IMPLICATION"
    AT_MOST_ONE = "AT_MOST_ONE"
    AT_LEAST_ONE = "AT_LEAST_ONE"
    EXACTLY_ONE = "EXACTLY_ONE"
    NONE = "NONE"


class RelationFeasibility(StrEnum):
    """What a family is worth, once semantics and venue mechanics are both in."""

    PROVEN_AND_ECONOMICALLY_POSSIBLE = "RELATION_PROVEN_AND_ECONOMICALLY_POSSIBLE"
    PROVEN_BUT_VENUE_MECHANICS_ELIMINATE_EDGE = "RELATION_PROVEN_BUT_VENUE_MECHANICS_ELIMINATE_EDGE"
    SEMANTICS_UNRESOLVED = "RELATION_SEMANTICS_UNRESOLVED"
    NO_USEFUL_RELATION = "NO_USEFUL_RELATION"


@dataclass(frozen=True, slots=True)
class ThresholdContract:
    """One listed contract resolving on a threshold over a measured value."""

    identifier: str
    underlying: str
    """The measured quantity. Two contracts relate only if this matches exactly --
    same series, same period, same source, same revision policy."""

    resolution_key: str
    """Everything else that must match for the values to be the same number:
    period, Source Agency, Resolution Time, revision treatment. Two contracts
    with different keys measure different things however similar they look."""

    threshold: Decimal
    comparison: Comparison = Comparison.STRICTLY_ABOVE

    def same_measurement_as(self, other: ThresholdContract) -> bool:
        """Whether both contracts resolve on one and the same observed number."""
        return (
            self.underlying == other.underlying
            and self.resolution_key == other.resolution_key
            and self.comparison is other.comparison
        )

    def yes_at(self, value: Decimal) -> bool:
        match self.comparison:
            case Comparison.STRICTLY_ABOVE:
                return value > self.threshold
            case Comparison.AT_OR_ABOVE:
                return value >= self.threshold
            case Comparison.STRICTLY_BELOW:
                return value < self.threshold
            case Comparison.AT_OR_BELOW:
                return value <= self.threshold


@dataclass(frozen=True, slots=True)
class RelationProof:
    """A relation between named contracts, and the settlement paths it survives."""

    relation: RelationClass
    members: tuple[str, ...]
    reasoning: str
    survives_settlement_paths: frozenset[str] = frozenset()
    broken_by_settlement_paths: frozenset[str] = frozenset()
    """Paths on which the relation stops holding. Non-empty means the basket
    cannot rely on it, however clean the ordinary case looks."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "members", tuple(sorted(self.members)))

    @property
    def survives_all_paths(self) -> bool:
        return self.relation is not RelationClass.NONE and not self.broken_by_settlement_paths

    def describe(self) -> str:
        broken = ", ".join(sorted(self.broken_by_settlement_paths)) or "none"
        return f"{self.relation.value} over {self.members}; broken by: {broken}"


def nested_implication(low: ThresholdContract, high: ThresholdContract) -> RelationProof:
    """Whether ``YES(high)`` implies ``YES(low)`` mechanically.

    Requires the same measurement -- same underlying, resolution key and
    comparison -- and an ordered pair of thresholds. Two contracts on merely
    similar quantities imply nothing about each other, and the resolution key is
    what keeps "similar" from passing as "same".
    """
    if not low.same_measurement_as(high):
        return RelationProof(
            relation=RelationClass.NONE,
            members=(low.identifier, high.identifier),
            reasoning=(
                "different measurements: the two contracts do not resolve on one "
                "and the same observed number, so neither outcome constrains the other"
            ),
        )
    ordered = (
        low.threshold < high.threshold
        if low.comparison.is_upward
        else low.threshold > high.threshold
    )
    if not ordered:
        return RelationProof(
            relation=RelationClass.NONE,
            members=(low.identifier, high.identifier),
            reasoning="thresholds are not ordered so that one outcome implies the other",
        )
    direction = "above" if low.comparison.is_upward else "below"
    return RelationProof(
        relation=RelationClass.NESTED_IMPLICATION,
        members=(low.identifier, high.identifier),
        reasoning=(
            f"same measurement and nested thresholds: any value {direction} "
            f"{high.threshold} is also {direction} {low.threshold}, so YES on the "
            "further strike implies YES on the nearer one"
        ),
    )


def at_most_one_over(contracts: Sequence[ThresholdContract]) -> RelationProof:
    """Whether at most one member can be YES.

    Threshold ladders never satisfy this: an "exceed" ladder is monotone, so a
    high enough value turns every member YES at once. Returned as NONE with the
    reason, because "no relation here" is a finding worth recording rather than
    an empty result.
    """
    names = tuple(c.identifier for c in contracts)
    if len(contracts) < _MIN_RELATION_MEMBERS:
        return RelationProof(RelationClass.NONE, names, "fewer than two members")
    if all(contracts[0].same_measurement_as(c) for c in contracts[1:]):
        return RelationProof(
            relation=RelationClass.NONE,
            members=names,
            reasoning=(
                "a monotone threshold ladder over one measurement is not mutually "
                "exclusive: a value beyond the furthest strike makes every member YES"
            ),
        )
    return RelationProof(
        relation=RelationClass.NONE,
        members=names,
        reasoning="mutual exclusion is not established by threshold structure alone",
    )


def covers_domain(
    contracts: Sequence[ThresholdContract],
    *,
    domain_low: Decimal | None,
    domain_high: Decimal | None,
) -> bool:
    """Whether the members leave no value uncovered.

    A threshold ladder covers the domain only if the domain is bounded below and
    the lowest strike sits at or under that bound -- otherwise a value beneath
    every strike makes all members NO, and AT_LEAST_ONE fails. An unbounded
    domain (``None``) can never be covered by finitely many "exceed" strikes.
    """
    if not contracts or domain_low is None or domain_high is None:
        return False
    upward = [c for c in contracts if c.comparison.is_upward]
    if not upward:
        return False
    lowest = min(c.threshold for c in upward)
    if any(c.comparison is Comparison.STRICTLY_ABOVE for c in upward):
        return domain_low > lowest
    return domain_low >= lowest


@dataclass(frozen=True, slots=True)
class BasketEconomics:
    """The inequality a basket must satisfy, before any quote is known.

    Written symbolically so a venue's matching rules can be checked for making
    it unsatisfiable. That check is the point: if the rules put the requirement
    out of reach for every possible quote, no amount of market data helps.
    """

    relation: RelationClass
    legs: int
    notional: Price
    worst_case_payoff: Price
    total_bid_cost_symbol: str = "sum(b_i)"
    per_leg_fee: Price | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def required_inequality(self) -> str:
        fee = f" + {self.legs} x {self.per_leg_fee}" if self.per_leg_fee else ""
        return f"{self.total_bid_cost_symbol}{fee} < {self.worst_case_payoff}"

    @property
    def max_affordable_cost(self) -> Price:
        """The most a basket may cost and still lock, ignoring fees."""
        return self.worst_case_payoff

    def is_algebraically_impossible(self, *, minimum_total_cost: Price) -> bool:
        """Whether venue rules force the cost above the worst-case payoff.

        The ForecastEx same-market case: inverse pricing fixes a YES+NO pair at
        one cent over the notional, so the cost floor exceeds the payoff ceiling
        and no quote can rescue it.
        """
        return minimum_total_cost.units >= self.worst_case_payoff.units

    def payload(self) -> dict[str, Any]:
        return {
            "relation": self.relation.value,
            "legs": self.legs,
            "notional": str(self.notional),
            "worst_case_payoff": str(self.worst_case_payoff),
            "required_inequality": self.required_inequality,
            "per_leg_fee": str(self.per_leg_fee) if self.per_leg_fee else None,
            "notes": list(self.notes),
        }


def vertical_spread_economics(
    notional: Price, *, per_leg_fee: Price | None = None
) -> BasketEconomics:
    """The two-leg nested-implication basket: buy YES(low), buy NO(high)."""
    return BasketEconomics(
        relation=RelationClass.NESTED_IMPLICATION,
        legs=2,
        notional=notional,
        worst_case_payoff=notional,
        total_bid_cost_symbol="b(YES,low) + b(NO,high)",
        per_leg_fee=per_leg_fee,
        notes=(
            "Worst case is the notional, not zero: the only state paying once is "
            "one where the other leg pays nothing, and the middle state pays twice.",
            "Under inverse pricing where a market's two bids must total the notional "
            "plus one tick, the condition becomes: the YES price on the further "
            "strike must exceed the YES price on the nearer strike by more than one "
            "tick -- that is, a monotonicity violation larger than the tick.",
        ),
    )


def relation_members_key(members: Iterable[str]) -> tuple[str, ...]:
    """Canonical member ordering, so the same basket built two ways is one basket."""
    return tuple(sorted(set(members)))
