"""Relations over *payout states*, not over YES/NO labels.

Phases 1 and 2 modelled a relation as a statement about which contracts resolve
YES. That works only while every contract pays the whole notional to one side.
Rothera breaks the assumption in a way that is worth taking seriously, because
it breaks it *favourably*: its product terms say, for a cancelled event,

    long  = fair market price
    short = $1 - fair market price

which is the residual construction this project has repeatedly failed to prove
elsewhere. Single-contract conservation holds for any price the exchange picks.
A YES/NO relation cannot express that, because in this state no contract is
YES or NO -- both sides are paid something.

The distinction that matters
----------------------------
A partition of three soccer contracts (home win / away win / tie) pays exactly
one notional under ordinary settlement, whatever the outcome. Under residual
settlement each member pays its own price, and the basket returns

    p_home + p_away + p_tie

Conservation says nothing about that sum. Each contract conserves *within
itself* -- long plus short is one notional -- while the sum across contracts is
whatever three independent discretionary determinations happen to be. The floor
is therefore zero, not one, and it is zero however well each individual pair
conserves.

That is the ForecastEx phase-2B failure in a new dress, and the reason this
module exists: to make the two questions structurally impossible to conflate.
:class:`PartitionBasket` answers them separately and its floor is the minimum
over every regime that is actually reachable.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from predarb.domain.money import Price

__all__ = [
    "MIN_PARTITION_MEMBERS",
    "PartitionBasket",
    "PriceCoupling",
    "SettlementRegime",
    "basket_cost_symbol",
    "residual_pair_conserves",
]

MIN_PARTITION_MEMBERS: Final = 2


class SettlementRegime(StrEnum):
    """How a terminal state assigns money to the two sides of one contract."""

    BINARY_OUTCOME = "BINARY_OUTCOME"
    """One side receives the whole notional, the other nothing."""

    RESIDUAL_PRICE = "RESIDUAL_PRICE"
    """A price ``p`` is chosen and the sides receive ``p`` and ``notional - p``.

    Conserves exactly, by construction, for every ``p``. Who chooses ``p`` and
    how does not affect that -- which is precisely why discretion over ``p`` is
    harmless and discretion over two payouts is not."""

    INDEPENDENT_PAYOUTS = "INDEPENDENT_PAYOUTS"
    """The two sides are set separately, with no stated relationship. Conserves
    only by coincidence, so a basket may assume nothing."""

    @property
    def conserves_within_contract(self) -> bool:
        return self is not SettlementRegime.INDEPENDENT_PAYOUTS


class PriceCoupling(StrEnum):
    """Whether the prices chosen for *different* contracts are tied together.

    The question a partition lives or dies on, and a completely separate one
    from within-contract conservation.
    """

    COUPLED_TO_NOTIONAL = "COUPLED_TO_NOTIONAL"
    """Governing text requires the members' prices to sum to the notional."""

    INDEPENDENT = "INDEPENDENT"
    """Each member's price is determined on its own."""

    UNKNOWN = "UNKNOWN"
    """Not established. Fails closed, and is the honest default -- an exchange
    that settles related markets sensibly in practice has still promised
    nothing."""

    @property
    def supports_a_floor(self) -> bool:
        return self is PriceCoupling.COUPLED_TO_NOTIONAL


def residual_pair_conserves(notional: Price, price: Price) -> bool:
    """Whether ``long = p`` and ``short = notional - p`` sum to the notional.

    Exact in scaled integers, so this is a real check rather than a restatement:
    it is what fails first if a venue ever rounds the two legs independently.
    """
    if price.units > notional.units:
        return False
    return price.units + (notional.units - price.units) == notional.units


@dataclass(frozen=True, slots=True)
class PartitionBasket:
    """A set of contracts claimed to pay exactly one notional between them.

    Holding the long side of every member is the basket. Whether that basket has
    a floor depends on the regime each reachable settlement path uses, and on
    whether the members' prices are coupled -- never on the relation label
    alone.
    """

    members: tuple[str, ...]
    notional: Price
    partition_proven: bool
    """Whether governing text establishes that exactly one member is satisfied
    in every ordinary terminal state."""

    reachable_regimes: frozenset[SettlementRegime]
    price_coupling: PriceCoupling = PriceCoupling.UNKNOWN
    reasoning: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "members", tuple(sorted(set(self.members))))
        if len(self.members) < MIN_PARTITION_MEMBERS:
            raise ValueError("a partition needs at least two distinct members")
        if not self.reachable_regimes:
            raise ValueError(
                f"{self.members}: no reachable settlement regime was recorded; an "
                "empty set would read as a proof when it is an omission"
            )

    def floor_under(self, regime: SettlementRegime) -> Price:
        """Guaranteed total payout to the long side of every member.

        ``BINARY_OUTCOME``  -- one notional, if the partition is proven.
        ``RESIDUAL_PRICE``  -- one notional only when the prices are coupled to
        it; otherwise zero, since nothing stops every member settling near zero.
        ``INDEPENDENT_PAYOUTS`` -- zero, since not even one contract is pinned.
        """
        zero = Price.from_value("0")
        if regime is SettlementRegime.BINARY_OUTCOME:
            return self.notional if self.partition_proven else zero
        if regime is SettlementRegime.RESIDUAL_PRICE:
            return self.notional if self.price_coupling.supports_a_floor else zero
        return zero

    @property
    def guaranteed_floor(self) -> Price:
        """The worst case over every reachable regime -- what may be relied on."""
        return min(
            (self.floor_under(r) for r in self.reachable_regimes),
            key=lambda p: p.units,
        )

    @property
    def survives_all_regimes(self) -> bool:
        return self.guaranteed_floor.units == self.notional.units

    @property
    def breaking_regimes(self) -> tuple[SettlementRegime, ...]:
        """Which reachable regimes drop the floor below the notional."""
        return tuple(
            sorted(
                (r for r in self.reachable_regimes if self.floor_under(r) != self.notional),
                key=lambda r: r.value,
            )
        )

    def describe(self) -> str:
        lines = [
            f"{' + '.join(self.members)}",
            f"  partition proven   : {self.partition_proven}",
            f"  price coupling     : {self.price_coupling.value}",
            f"  reachable regimes  : {', '.join(sorted(r.value for r in self.reachable_regimes))}",
            f"  guaranteed floor   : {self.guaranteed_floor} of {self.notional}",
            f"  survives all       : {self.survives_all_regimes}",
        ]
        if self.breaking_regimes:
            lines.append(
                f"  broken by          : {', '.join(r.value for r in self.breaking_regimes)}"
            )
        return "\n".join(lines)


def basket_cost_symbol(members: Sequence[str] | Iterable[str]) -> str:
    """Symbolic total cost of buying the long side of every member."""
    return " + ".join(f"p({m})" for m in sorted(set(members)))
