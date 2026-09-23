"""The worst-case payoff of a long-YES basket under AT_LEAST_ONE, proven symbolically.

The state space is the problem. ``AT_LEAST_ONE`` over n members permits
``2**n - 1`` joint outcomes -- everything except all-NO -- and one live event has
300 members. Enumerating that is not a calculation, it is an outage.

The theorem
-----------
For equal quantity ``q`` of YES in every selected member, where each member's
YES pays ``N_i`` when that member settles YES and ``0`` when it settles NO:

    worst_case_payoff = q * min_i(N_i)

Proof. Let ``W`` be the set of members settling YES in some permitted state.
AT_LEAST_ONE forbids only ``W = {}``, so every permitted state has ``|W| >= 1``.
The portfolio pays ``q * sum(N_i for i in W)``. Since every ``N_i >= 0``,
dropping a member from ``W`` cannot increase the sum, so the minimum over
permitted states is attained at some ``|W| = 1``. Among singletons the payoff is
``q * N_i``, minimised at ``min_i(N_i)``. QED

The argument uses two things about the payoff model, and both are checked rather
than assumed:

**Nonnegativity.** Every ``N_i >= 0``. A negative winning payout would make
adding a winner *reduce* the total, and the minimum could then sit at
``|W| = n`` instead. This one is discharged by the type system --
:class:`~predarb.domain.money.Price` refuses a negative value at construction --
so the check below is unreachable through ``Price`` and exists for a future
payoff model that uses a signed representation.

**Monotonicity.** A member settling YES never reduces any *other* member's
payout. Long binary YES positions satisfy this trivially -- each leg pays on its
own outcome alone. A clawback, a cross-market liability or a state-dependent
transfer would not, and the theorem would not apply.

Why not "just enumerate the singletons"
----------------------------------------
Feeding only the n singleton states into the enumeration solver returns the same
number, and would be a lie about the state space: it would record that the
portfolio was evaluated over n states when the relation permits ``2**n - 1``.
A later reader -- or a later detector reusing that payoff object -- would see a
state set that says "exactly one member wins", which is AT_MOST_ONE, a
guarantee nobody proved here.

So this module returns a symbolic result that says what it actually did: which
theorem, which preconditions held, which member witnesses the minimum, and over
what state family. Tests cross-check it against real enumeration for small n.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from predarb.domain.money import Money, Price, Quantity

__all__ = [
    "AtLeastOnePayoffProof",
    "MemberPayoffSpec",
    "PayoffTheorem",
    "PreconditionStatus",
    "UnprovablePayoffError",
    "prove_at_least_one_floor",
]


class UnprovablePayoffError(Exception):
    """The theorem's preconditions do not hold, so no floor can be claimed."""


class PayoffTheorem(StrEnum):
    SINGLETON_WITNESS_MINIMUM = "SINGLETON_WITNESS_MINIMUM"
    """The minimum over all permitted states is attained at a single-winner
    state, so it equals ``q * min_i(N_i)``."""


class PreconditionStatus(StrEnum):
    SATISFIED = "SATISFIED"
    VIOLATED_NEGATIVE_WINNING_PAYOFF = "VIOLATED_NEGATIVE_WINNING_PAYOFF"
    """Unreachable while payouts are :class:`Price`, which cannot be negative.
    Retained so a signed payoff representation cannot quietly bypass the
    theorem's first precondition."""
    VIOLATED_NONZERO_LOSING_PAYOFF = "VIOLATED_NONZERO_LOSING_PAYOFF"
    """A member pays something when it settles NO. The theorem's arithmetic
    assumes a losing leg contributes nothing; a nonzero losing payout means the
    minimum is no longer at a singleton and may not even be at an extreme."""

    VIOLATED_CROSS_MEMBER_DEPENDENCE = "VIOLATED_CROSS_MEMBER_DEPENDENCE"
    """One member's payout depends on another member's outcome."""

    @property
    def permits_proof(self) -> bool:
        return self is PreconditionStatus.SATISFIED


@dataclass(frozen=True, slots=True)
class MemberPayoffSpec:
    """One member's certified YES payoff, reduced to what the theorem needs.

    Built from a verified settlement certificate, never from a market's
    displayed fields. ``losing_payoff`` is carried explicitly rather than
    assumed zero: a certificate that says otherwise must break the proof, not
    be silently rounded into it.
    """

    ticker: str
    winning_payoff_per_contract: Price
    losing_payoff_per_contract: Price
    cross_member_dependent: bool = False
    """Whether this member's payout depends on any other member's outcome."""

    def precondition_status(self) -> PreconditionStatus:
        if self.winning_payoff_per_contract.units < 0:
            return PreconditionStatus.VIOLATED_NEGATIVE_WINNING_PAYOFF
        if self.losing_payoff_per_contract.units != 0:
            return PreconditionStatus.VIOLATED_NONZERO_LOSING_PAYOFF
        if self.cross_member_dependent:
            return PreconditionStatus.VIOLATED_CROSS_MEMBER_DEPENDENCE
        return PreconditionStatus.SATISFIED


@dataclass(frozen=True, slots=True)
class AtLeastOnePayoffProof:
    """A proven lower bound, plus everything needed to audit how it was reached."""

    theorem: PayoffTheorem
    members: tuple[str, ...]
    quantity: Quantity
    worst_case_payoff: Money
    witnesses: tuple[str, ...]
    """The member(s) whose singleton-YES state attains the minimum."""

    preconditions: Mapping[str, PreconditionStatus]
    permitted_state_count: int
    """``2 ** n - 1``. Recorded, never materialised -- the number is the honest
    description of the state family the bound covers."""

    forbidden_state: str
    best_case_payoff: Money
    """Attained when every member settles YES, which AT_LEAST_ONE permits."""

    @property
    def preconditions_hold(self) -> bool:
        return all(status.permits_proof for status in self.preconditions.values())

    @property
    def is_state_independent(self) -> bool:
        """Only when n == 1. With two or more members the payoff genuinely varies."""
        return self.worst_case_payoff == self.best_case_payoff

    def describe(self) -> str:
        return (
            f"{self.theorem.value} over {len(self.members)} member(s): floor "
            f"{self.worst_case_payoff.to_str()} witnessed by {list(self.witnesses)}, "
            f"ceiling {self.best_case_payoff.to_str()}, across "
            f"{self.permitted_state_count} permitted state(s) "
            f"(forbidden: {self.forbidden_state})"
        )


def prove_at_least_one_floor(
    specs: Sequence[MemberPayoffSpec],
    *,
    quantity: Quantity,
    forbidden_state: str,
) -> AtLeastOnePayoffProof:
    """Prove the guaranteed payoff floor for equal-quantity long YES.

    Raises :class:`UnprovablePayoffError` when a precondition fails. There is no
    "best effort" bound: a floor that might not hold is not a floor, and
    returning one would put a number into a profit interval that the contract
    does not guarantee.
    """
    if not specs:
        raise UnprovablePayoffError("no members supplied; there is nothing to bound")
    if quantity.units <= 0:
        raise UnprovablePayoffError(f"quantity must be positive, got {quantity.to_str()}")

    tickers = [spec.ticker for spec in specs]
    if len(set(tickers)) != len(tickers):
        raise UnprovablePayoffError(f"duplicate members supplied: {sorted(tickers)}")

    preconditions = {spec.ticker: spec.precondition_status() for spec in specs}
    violated = {t: s for t, s in preconditions.items() if not s.permits_proof}
    if violated:
        detail = "; ".join(f"{t}: {s.value}" for t, s in sorted(violated.items()))
        raise UnprovablePayoffError(
            f"the singleton-witness theorem does not apply ({detail}); its minimum "
            "sits at a single-winner state only because a losing leg contributes "
            "nothing and a winning leg never contributes less than nothing"
        )

    floor_price = min(spec.winning_payoff_per_contract for spec in specs)
    witnesses = tuple(
        sorted(spec.ticker for spec in specs if spec.winning_payoff_per_contract == floor_price)
    )
    ceiling_units = sum(spec.winning_payoff_per_contract.units for spec in specs)

    return AtLeastOnePayoffProof(
        theorem=PayoffTheorem.SINGLETON_WITNESS_MINIMUM,
        members=tuple(sorted(tickers)),
        quantity=quantity,
        worst_case_payoff=floor_price * quantity,
        witnesses=witnesses,
        preconditions=dict(sorted(preconditions.items())),
        # Computed, not enumerated. For n = 300 this is a large integer and
        # nothing anywhere tries to build the set it counts.
        permitted_state_count=(1 << len(specs)) - 1,
        forbidden_state=forbidden_state,
        best_case_payoff=Price.from_units(ceiling_units) * quantity,
    )
