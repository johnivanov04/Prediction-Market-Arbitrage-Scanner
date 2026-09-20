"""Terminal payoff of a portfolio, by explicit enumeration of settlement states.

Venue-neutral and arithmetic-only. This module knows nothing about Kalshi,
order books, fees or arbitrage. It answers exactly one question:

    given these positions, and given that exactly one of these states occurs,
    what does the portfolio pay in each state, and what is the worst case?

No probabilities
----------------
There is no probability, weighting or expected value anywhere here, and there
will not be. A state-independent payoff is a statement about *every* allowed
state, so a state's likelihood is irrelevant to it. Introducing a probability
would turn a proof into an estimate.

Unknown is not impossible
-------------------------
The engine refuses to evaluate a portfolio whose positions do not specify a
payoff for every allowed state. A missing state is not a zero-payoff state and
not a zero-probability state -- it is an unproven one, and a worst case computed
over a subset of states is not a worst case at all.

Broader than the venue adapter
------------------------------
The mathematics here supports any notional and any finite set of states,
including scalar-style tables, even where a particular venue adapter currently
refuses to price such a market. Domain mathematics is deliberately kept wider
than venue support, so that a future settlement model needs no new engine.

Enumeration is an implementation, not the interface
---------------------------------------------------
:class:`PortfolioSolver` is the boundary. Finite enumeration is sufficient for
Phase 1's two-state markets; an LP/MILP/SAT solver can replace it later without
any detector changing, provided it returns the same worst case.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from predarb.domain.money import Money, Price, Quantity

__all__ = [
    "EnumerationSolver",
    "IncompletePayoffSpecificationError",
    "PayoffModel",
    "PortfolioPayoff",
    "PortfolioSolver",
    "Position",
    "SettlementState",
    "StatePayoff",
    "TabulatedPayoff",
    "evaluate_portfolio",
]


class IncompletePayoffSpecificationError(Exception):
    """A position does not say what it pays in some allowed state.

    Raised rather than defaulting the missing state to zero. A portfolio whose
    worst case was computed over only the states someone remembered to list is
    not proven; it is merely untested against the rest.
    """


@dataclass(frozen=True, slots=True, order=True)
class SettlementState:
    """One terminal state a market can settle into.

    Identified by name. States are compared and sorted by name so that results
    do not depend on the order the caller happened to supply them in.
    """

    name: str

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("settlement state needs a non-empty name")

    def __str__(self) -> str:
        return self.name


@runtime_checkable
class PayoffModel(Protocol):
    """What one contract pays, per contract, in a given state."""

    @property
    def covered_states(self) -> frozenset[str]:
        """Every state this model can answer for."""
        ...

    def payoff_per_contract(self, state: SettlementState) -> Price:
        """Payout for a single contract if ``state`` occurs."""
        ...


@dataclass(frozen=True, slots=True)
class TabulatedPayoff:
    """An explicit, complete payoff table: state name -> payout per contract.

    Per-contract payouts are :class:`Price` rather than :class:`Money` so that
    ``payout * quantity`` is exact by construction: a 4dp price times a 2dp
    quantity is exactly 6dp, the scale invariant the whole codebase rests on.
    Nothing here rounds.
    """

    per_contract: Mapping[str, Price]

    def __post_init__(self) -> None:
        if not self.per_contract:
            raise ValueError("a payoff table must cover at least one state")
        object.__setattr__(self, "per_contract", dict(self.per_contract))

    @classmethod
    def binary(cls, *, winning_state: str, losing_state: str, notional: Price) -> TabulatedPayoff:
        """The standard two-state table: full notional in one state, nothing in the other."""
        if winning_state == losing_state:
            raise ValueError(f"winning and losing state are both {winning_state!r}")
        return cls({winning_state: notional, losing_state: Price.from_units(0)})

    @property
    def covered_states(self) -> frozenset[str]:
        return frozenset(self.per_contract)

    def payoff_per_contract(self, state: SettlementState) -> Price:
        try:
            return self.per_contract[state.name]
        except KeyError:
            raise IncompletePayoffSpecificationError(
                f"no payoff specified for state {state.name!r}; "
                f"this table covers {sorted(self.per_contract)}"
            ) from None


@dataclass(frozen=True, slots=True)
class Position:
    """A held quantity of one contract, with how that contract pays."""

    label: str
    quantity: Quantity
    payoff: PayoffModel

    def payoff_in(self, state: SettlementState) -> Money:
        """Total payout for this position if ``state`` occurs. Exact."""
        return self.payoff.payoff_per_contract(state) * self.quantity


@dataclass(frozen=True, slots=True)
class StatePayoff:
    """What the whole portfolio pays in one state, and what each leg contributed."""

    state: SettlementState
    by_position: tuple[tuple[str, Money], ...]
    total: Money


@dataclass(frozen=True, slots=True)
class PortfolioPayoff:
    """Payoff in every allowed state, with the extremes and where they occur."""

    states: tuple[SettlementState, ...]
    per_state: tuple[StatePayoff, ...]
    minimum: Money
    maximum: Money
    minimising_states: tuple[SettlementState, ...]
    maximising_states: tuple[SettlementState, ...]

    def __post_init__(self) -> None:
        if self.minimum > self.maximum:
            raise ValueError(f"minimum {self.minimum} exceeds maximum {self.maximum}")

    @property
    def is_state_independent(self) -> bool:
        """True when every allowed state pays the same. The provable case."""
        return self.minimum == self.maximum

    @property
    def worst_case(self) -> Money:
        """The only payoff figure a contractual proof may rely on."""
        return self.minimum

    def payoff_in(self, state: SettlementState) -> Money:
        for entry in self.per_state:
            if entry.state == state:
                return entry.total
        raise KeyError(f"state {state.name!r} was not evaluated")

    def describe(self) -> str:
        rows = ", ".join(f"{entry.state.name}={entry.total.to_str()}" for entry in self.per_state)
        return f"worst={self.minimum.to_str()} best={self.maximum.to_str()} [{rows}]"


@runtime_checkable
class PortfolioSolver(Protocol):
    """Computes a portfolio's payoff extremes over a set of allowed states.

    The seam that lets enumeration be swapped for an optimiser later. Any
    replacement must return the identical worst case, not a bound on it.
    """

    def solve(
        self, positions: Sequence[Position], states: Sequence[SettlementState]
    ) -> PortfolioPayoff: ...


class EnumerationSolver:
    """Evaluates every state explicitly.

    Sufficient while portfolios are small and state sets are two-element. It is
    exhaustive by construction, which is why it is the Phase 1 default: there is
    no search heuristic to be wrong.
    """

    def solve(
        self, positions: Sequence[Position], states: Sequence[SettlementState]
    ) -> PortfolioPayoff:
        if not states:
            raise IncompletePayoffSpecificationError(
                "no settlement states supplied; a worst case over an empty state set "
                "is vacuous, not safe"
            )

        ordered = tuple(sorted(set(states)))
        if len(ordered) != len(states):
            duplicates = sorted({s.name for s in states if list(states).count(s) > 1})
            raise ValueError(f"duplicate settlement states supplied: {duplicates}")

        _require_total_coverage(positions, ordered)

        per_state: list[StatePayoff] = []
        for state in ordered:
            contributions = tuple((p.label, p.payoff_in(state)) for p in positions)
            total = Money.from_units(sum(amount.units for _, amount in contributions))
            per_state.append(StatePayoff(state=state, by_position=contributions, total=total))

        totals = [entry.total for entry in per_state]
        minimum, maximum = min(totals), max(totals)
        return PortfolioPayoff(
            states=ordered,
            per_state=tuple(per_state),
            minimum=minimum,
            maximum=maximum,
            minimising_states=tuple(e.state for e in per_state if e.total == minimum),
            maximising_states=tuple(e.state for e in per_state if e.total == maximum),
        )


def _require_total_coverage(
    positions: Sequence[Position], states: Sequence[SettlementState]
) -> None:
    """Every position must answer for every state, or nothing is evaluated."""
    names = {state.name for state in states}
    for position in positions:
        missing = names - position.payoff.covered_states
        if missing:
            raise IncompletePayoffSpecificationError(
                f"position {position.label!r} has no payoff for state(s) "
                f"{sorted(missing)}; an unspecified state is unproven, not zero"
            )


_DEFAULT_SOLVER = EnumerationSolver()


def evaluate_portfolio(
    positions: Sequence[Position],
    states: Sequence[SettlementState],
    *,
    solver: PortfolioSolver | None = None,
) -> PortfolioPayoff:
    """Payoff of ``positions`` across ``states``.

    An empty position list is legal and pays zero in every state -- that is a
    real answer, unlike an empty *state* list, which is refused.
    """
    return (solver or _DEFAULT_SOLVER).solve(positions, states)
