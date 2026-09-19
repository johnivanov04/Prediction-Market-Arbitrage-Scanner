"""Exact fee mechanics for a known sequence of fills.

This is the ground-truth primitive. Given an *actual* ordered fill stream and
the member's balance precision, it reproduces the documented mechanics exactly
and deterministically: the same stream always yields the same numbers.

It is not, and cannot be, a pre-trade calculator. An L2 price level is aggregate
depth that may execute as one fill or many, and the mechanics are per-fill with
an order-scoped accumulator, so the final fee depends on fill segmentation that
the book does not reveal. :mod:`predarb.venues.kalshi.fees` handles that
uncertainty explicitly; this module assumes the segmentation is already known.

Per-order accumulator
---------------------
The accumulator is scoped to **one order** and persists across all of that
order's fills. Not per market, per price, per fill, or global -- the
documentation is specific, and each of those alternatives would produce
different rebate timing.

Order matters. Fills are applied in sequence and never reordered: the
accumulator's value when a fill arrives determines whether that fill triggers a
rebate.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal

from predarb.domain.enums import Liquidity
from predarb.domain.fees import FeeConfiguration
from predarb.domain.money import Money, Price, Quantity
from predarb.venues.kalshi.fee_model import (
    BalancePrecision,
    raw_model_fee,
    signed_revenue_for_buy,
    trade_fee_from,
)

__all__ = [
    "AccumulatorState",
    "Fill",
    "FillFeeResult",
    "apply_fill",
    "apply_fills",
    "total_net_fee",
    "total_trade_fee",
]


@dataclass(frozen=True, slots=True)
class Fill:
    """One exchange fill. Not an L2 price level.

    The distinction is the whole reason this module exists: a level of 100
    contracts may execute as one fill or as fifty, and the fee mechanics differ
    between those cases.
    """

    price: Price
    quantity: Quantity
    configuration: FeeConfiguration
    liquidity: Liquidity = Liquidity.TAKER

    def gross_cost(self) -> Money:
        """Exact cash out for this fill, before fees."""
        return self.price * self.quantity


@dataclass(frozen=True, slots=True)
class AccumulatorState:
    """Per-order rounding accumulator.

    Holds overpaid rounding that has not yet reached the member's balance
    precision. Immutable: each fill returns a new state, so an audit can show
    the value before and after every step.
    """

    accumulated: Money = field(default_factory=Money.zero)
    fills_applied: int = 0

    def __post_init__(self) -> None:
        if self.accumulated.units < 0:
            raise ValueError(
                f"accumulator must not be negative, got {self.accumulated}; it holds "
                "overpayment awaiting rebate, which cannot be less than nothing"
            )

    @classmethod
    def new_order(cls) -> AccumulatorState:
        """A fresh accumulator. One per order."""
        return cls()


@dataclass(frozen=True, slots=True)
class FillFeeResult:
    """Every documented intermediate for one fill.

    Deliberately exhaustive. An audit has to be able to re-derive the number
    without trusting this code, which means every step is visible rather than
    only the total.
    """

    fill: Fill
    signed_revenue: Money
    raw_model_fee: Decimal
    trade_fee: Money
    aligned_change: Money
    rounding_fee: Money
    rebate: Money
    net_fee: Money
    balance_change: Money
    accumulator_before: AccumulatorState
    accumulator_after: AccumulatorState

    def __post_init__(self) -> None:
        if self.net_fee.units < 0:
            raise ValueError(
                f"net fee is negative ({self.net_fee}); the documented rebate cap "
                "exists precisely to prevent this"
            )

    def describe(self) -> str:
        return (
            f"{self.fill.quantity} @ {self.fill.price}: "
            f"model={self.raw_model_fee} trade={self.trade_fee} "
            f"rounding={self.rounding_fee} rebate={self.rebate} net={self.net_fee}"
        )


def apply_fill(fill: Fill, state: AccumulatorState, precision: BalancePrecision) -> FillFeeResult:
    """Apply the documented mechanics to one fill.

    Steps, in the documented order::

        trade_fee     = ceil_6dp(model_fee)
        aligned_change= floor_precision(signed_revenue - trade_fee)
        rounding_fee  = (signed_revenue - trade_fee) - aligned_change
        accumulator  += rounding_fee
        rebate        = whole precision steps available, capped so net_fee >= 0
    """
    model_fee = raw_model_fee(
        price=fill.price,
        quantity=fill.quantity,
        configuration=fill.configuration,
        liquidity=fill.liquidity,
    )
    trade_fee = trade_fee_from(model_fee)
    signed_revenue = signed_revenue_for_buy(fill.gross_cost())

    # Negative for a buy, so this floors toward negative infinity: the
    # documented floor_cent(-$0.058639) = -$0.060000.
    unaligned = signed_revenue - trade_fee
    aligned_change = precision.floor(unaligned)
    rounding_fee = unaligned - aligned_change

    accumulated = state.accumulated + rounding_fee
    rebate = _rebate_for(accumulated, trade_fee + rounding_fee, precision)
    net_fee = trade_fee + rounding_fee - rebate

    return FillFeeResult(
        fill=fill,
        signed_revenue=signed_revenue,
        raw_model_fee=model_fee,
        trade_fee=trade_fee,
        aligned_change=aligned_change,
        rounding_fee=rounding_fee,
        rebate=rebate,
        net_fee=net_fee,
        # Cash actually leaving the account: the aligned change plus whatever
        # was rebated. Kept separate from net_fee, because "the fee" and "the
        # cash this fill costs" are different questions.
        balance_change=aligned_change + rebate,
        accumulator_before=state,
        accumulator_after=replace(
            state, accumulated=accumulated - rebate, fills_applied=state.fills_applied + 1
        ),
    )


def _rebate_for(accumulated: Money, fee_before_rebate: Money, precision: BalancePrecision) -> Money:
    """Whole precision steps of accumulated overpayment, capped.

    Two constraints, both documented: rebates are returned in whole increments
    of the member's balance precision, and a fill's net fee cannot become
    negative. The cap is applied to the *step count* rather than to the amount,
    so the rebate always lands on the precision grid.
    """
    step = precision.step.units
    whole_steps = accumulated.units // step
    if whole_steps <= 0:
        return Money.zero()
    affordable_steps = fee_before_rebate.units // step
    granted = min(whole_steps, max(0, affordable_steps))
    return Money.from_units(granted * step)


def apply_fills(
    fills: Sequence[Fill],
    precision: BalancePrecision,
    *,
    state: AccumulatorState | None = None,
) -> tuple[tuple[FillFeeResult, ...], AccumulatorState]:
    """Apply a whole order's fills in order, threading one accumulator.

    Returns every per-fill result plus the final accumulator, so a caller can
    continue the same order later if more fills arrive.

    The sequence is consumed as given and never sorted: rebate timing depends
    on the accumulator's value when each fill lands.
    """
    current = state or AccumulatorState.new_order()
    results: list[FillFeeResult] = []
    for fill in fills:
        result = apply_fill(fill, current, precision)
        results.append(result)
        current = result.accumulator_after
    return tuple(results), current


def total_net_fee(results: Sequence[FillFeeResult]) -> Money:
    """Sum of net fees across an order's fills."""
    return Money.from_units(sum(result.net_fee.units for result in results))


def total_trade_fee(results: Sequence[FillFeeResult]) -> Money:
    return Money.from_units(sum(result.trade_fee.units for result in results))
