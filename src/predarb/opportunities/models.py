"""The opportunity record, and the vocabulary for classifying one.

Four independent axes, never collapsed
--------------------------------------
A single confidence score would hide exactly the distinctions that matter. A
portfolio can have a proven state-independent payoff *and* be unexecutable; a
fee interval can be sound *and* too wide to conclude anything. So each question
is answered separately:

``semantic_status``   do we know what this contract pays in every state?
``payoff_status``     given that, is the worst case above the cost?
``cost_status``       is the cost exact, bounded, or unavailable?
``execution_status``  could this actually be acquired as priced?

Terminology is deliberately awkward to misuse
---------------------------------------------
There is no ``profit`` field and no ``fee`` field. When fees are bounded rather
than exact, the honest quantities are intervals, and a scalar named ``profit``
would be read as a number someone can bank. The fields are
``profit_lower_bound`` / ``profit_upper_bound``, which cannot be misread.

Contractual proof is not execution safety
-----------------------------------------
:attr:`Classification.PROVEN_CONTRACTUAL_ARBITRAGE` means: *if every leg fills
at the quoted depth, the payoff exceeds the cost in every allowed settlement
state.* It says nothing about whether both legs will fill. Two acquisitions on
a live venue are not atomic, so the execution axis stays ``RACE_EXPOSED``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from predarb.clock import ensure_utc
from predarb.domain.money import Money, Quantity

__all__ = [
    "Classification",
    "CostStatus",
    "ExecutionStatus",
    "PayoffStatus",
    "ProfitInterval",
    "SemanticStatus",
]


class SemanticStatus(StrEnum):
    """Do we have a proven payoff table for every allowed state?"""

    VERIFIED = "VERIFIED"
    BLOCKED = "BLOCKED"


class PayoffStatus(StrEnum):
    """What the payoff-versus-cost comparison established."""

    STATE_INDEPENDENT_PROFIT = "STATE_INDEPENDENT_PROFIT"
    """Worst-case payoff strictly exceeds the worst-case cost."""

    NON_POSITIVE = "NON_POSITIVE"
    """Even the most optimistic cost leaves no positive margin."""

    INDETERMINATE = "INDETERMINATE"
    """The cost interval straddles the payoff. Not a finding either way."""

    NOT_EVALUATED = "NOT_EVALUATED"
    """Blocked before the comparison could be made."""


class CostStatus(StrEnum):
    """How precisely the acquisition cost is known."""

    EXACT = "EXACT"
    """Gross cost and fees both exact -- only possible from real fill records."""

    BOUNDED = "BOUNDED"
    """Gross cost exact, fees proven to lie in an interval."""

    UNAVAILABLE = "UNAVAILABLE"
    """Fee semantics unestablished. No cost figure exists; it is not zero."""


class ExecutionStatus(StrEnum):
    """Whether the quoted acquisition is actually achievable."""

    DEPTH_VERIFIED = "DEPTH_VERIFIED"
    """Depth existed for every leg at the quoted instant. Not a fill guarantee."""

    RACE_EXPOSED = "RACE_EXPOSED"
    """Legs are acquired by separate, non-atomic orders. The book can move
    between them, so one leg may fill and the other may not."""

    STALE_OR_INVALID = "STALE_OR_INVALID"
    """The book could not be trusted at the quoted instant."""


class Classification(StrEnum):
    """The single headline verdict, derived from the four axes.

    Only the first value is an arbitrage claim, and it requires a *strictly*
    positive guaranteed profit. Zero is not arbitrage.
    """

    PROVEN_CONTRACTUAL_ARBITRAGE = "PROVEN_CONTRACTUAL_ARBITRAGE"
    """Worst-case payoff exceeds worst-case cost, under verified settlement
    semantics and a proven fee upper bound. Subject to execution race risk."""

    PROVEN_NOT_PROFITABLE = "PROVEN_NOT_PROFITABLE"
    """Even with the most favourable admissible fee, profit is not positive."""

    INDETERMINATE_COST_BOUNDS = "INDETERMINATE_COST_BOUNDS"
    """Profitable under optimistic fees, unprofitable under pessimistic ones.
    Fee uncertainty prevents a proof. Deliberately not called arbitrage."""

    BLOCKED_SETTLEMENT_SEMANTICS = "BLOCKED_SETTLEMENT_SEMANTICS"
    BLOCKED_FEE_SEMANTICS = "BLOCKED_FEE_SEMANTICS"
    BLOCKED_LIQUIDITY_COLLISION = "BLOCKED_LIQUIDITY_COLLISION"
    BLOCKED_BOOK_INTEGRITY = "BLOCKED_BOOK_INTEGRITY"
    INSUFFICIENT_DEPTH = "INSUFFICIENT_DEPTH"

    @property
    def is_arbitrage_claim(self) -> bool:
        return self is Classification.PROVEN_CONTRACTUAL_ARBITRAGE

    @property
    def is_blocked(self) -> bool:
        return self.value.startswith("BLOCKED_") or self is Classification.INSUFFICIENT_DEPTH


@dataclass(frozen=True, slots=True)
class ProfitInterval:
    """Guaranteed-profit bounds implied by a cost interval and a worst case.

    ``lower`` uses the fee **upper** bound and is the only figure a proof may
    rest on. ``upper`` uses the fee lower bound and describes the best case
    that is still admissible -- useful for triage, never for a claim.
    """

    worst_case_payoff: Money
    gross_cost: Money
    fee_lower: Money
    fee_upper: Money

    def __post_init__(self) -> None:
        if self.fee_lower > self.fee_upper:
            raise ValueError(
                f"fee lower bound {self.fee_lower} exceeds upper bound {self.fee_upper}"
            )
        if self.fee_lower.units < 0 or self.gross_cost.units < 0:
            raise ValueError("gross cost and fees must not be negative")

    @property
    def total_cost_lower(self) -> Money:
        return self.gross_cost + self.fee_lower

    @property
    def total_cost_upper(self) -> Money:
        return self.gross_cost + self.fee_upper

    @property
    def profit_lower_bound(self) -> Money:
        """Worst case: guaranteed payoff minus the most fees can be."""
        return self.worst_case_payoff - self.total_cost_upper

    @property
    def profit_upper_bound(self) -> Money:
        """Best admissible case: guaranteed payoff minus the least fees can be."""
        return self.worst_case_payoff - self.total_cost_lower

    @property
    def is_proven_profitable(self) -> bool:
        """Strictly positive. A zero-profit portfolio is not an arbitrage."""
        return self.profit_lower_bound.units > 0

    @property
    def is_proven_unprofitable(self) -> bool:
        return self.profit_upper_bound.units <= 0

    @property
    def is_indeterminate(self) -> bool:
        return not self.is_proven_profitable and not self.is_proven_unprofitable

    def payoff_status(self) -> PayoffStatus:
        if self.is_proven_profitable:
            return PayoffStatus.STATE_INDEPENDENT_PROFIT
        if self.is_proven_unprofitable:
            return PayoffStatus.NON_POSITIVE
        return PayoffStatus.INDETERMINATE

    def describe(self) -> str:
        return (
            f"payoff {self.worst_case_payoff.to_str()} - cost ["
            f"{self.total_cost_lower.to_str()}, {self.total_cost_upper.to_str()}] "
            f"=> profit [{self.profit_lower_bound.to_str()}, "
            f"{self.profit_upper_bound.to_str()}]"
        )


@dataclass(frozen=True, slots=True)
class OpportunityIdentity:
    """Everything needed to find this observation again in the raw journal."""

    detected_at: datetime
    market_ticker: str
    quantity: Quantity
    connection_epoch: int
    sid: int
    book_seq: int | None
    snapshot_raw_id: str | None
    latest_raw_id: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "detected_at", ensure_utc(self.detected_at))

    def describe(self) -> str:
        return (
            f"{self.market_ticker} q={self.quantity} @ epoch={self.connection_epoch} "
            f"sid={self.sid} seq={self.book_seq} raw={self.latest_raw_id}"
        )
