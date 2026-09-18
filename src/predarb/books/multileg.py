"""Gross cost across several acquisition legs at a common quantity.

Every Phase 1 structure buys the *same* quantity in each of several contracts:
`q` YES and `q` NO for a binary complement, `q` NO in each of N markets for an
AT_MOST_ONE basket. This module answers what that costs, before fees, at each
quantity where any leg's cost changes.

It decides nothing. There is no profit here, and no comparison against a payoff
-- that requires fees and settlement semantics, which belong to later layers.
What this produces is the input those layers consume.

Liquidity collisions
--------------------
Two legs can silently target the same resting contracts. On Kalshi a YES ask is
derived from a NO bid, so "buy YES" and "sell NO"-shaped strategies -- or two
legs on the same market -- can both be reaching for one level. Summing their
costs would treat one pool of liquidity as two, and report a size that cannot
actually be executed.

:func:`detect_liquidity_collisions` finds those overlaps by identity rather than
by re-deriving arithmetic, and :func:`multi_leg_costs` refuses to aggregate when
one exists. Phase 5 does not resolve collisions -- no reservation or netting --
it just makes ignoring them impossible.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from predarb.books.execution import ExecutionCurve
from predarb.books.liquidity import BookLiquidityId, ExecutableLevel
from predarb.domain.average_price import AveragePrice
from predarb.domain.money import Money, Quantity

_MIN_DISPLAY_POINTS: Final = 2
"""A display curve needs at least its endpoints to mean anything."""

_MAX_COLLISIONS_SHOWN: Final = 5
"""Cap on collisions named in an error message; the full tuple is on the error."""

__all__ = [
    "LiquidityCollisionError",
    "MultiLegCostPoint",
    "common_fillable_quantity",
    "detect_liquidity_collisions",
    "multi_leg_costs",
    "thin_for_display",
]


class LiquidityCollisionError(Exception):
    """Two legs would consume the same resting liquidity.

    Raised rather than returning a summed cost, because the sum would be a
    number that looks executable and is not.
    """

    def __init__(self, collisions: tuple[BookLiquidityId, ...]) -> None:
        self.collisions = collisions
        shown = collisions[:_MAX_COLLISIONS_SHOWN]
        detail = ", ".join(identity.describe() for identity in shown)
        hidden = len(collisions) - len(shown)
        more = f" (+{hidden} more)" if hidden else ""
        super().__init__(
            f"{len(collisions)} source level(s) are claimed by more than one leg: "
            f"{detail}{more}. Summing these legs would count the same resting "
            "contracts twice."
        )


@dataclass(frozen=True, slots=True)
class MultiLegCostPoint:
    """Total gross cost of acquiring ``quantity`` in every leg."""

    quantity: Quantity
    total_gross_cost: Money
    leg_costs: tuple[Money, ...]
    leg_vwaps: tuple[AveragePrice | None, ...]
    leg_tickers: tuple[str, ...]
    liquidity_ids: tuple[BookLiquidityId, ...]
    """Every source level consumed across all legs at this quantity."""

    @property
    def combined_vwap(self) -> AveragePrice | None:
        """Average cost per *set* of legs, not per contract of one leg."""
        if self.quantity.is_zero:
            return None
        return AveragePrice(self.total_gross_cost, self.quantity)

    def describe(self) -> str:
        return (
            f"q={self.quantity} total_gross={self.total_gross_cost} "
            f"legs={len(self.leg_costs)} (fees excluded)"
        )


def common_fillable_quantity(curves: Sequence[ExecutionCurve]) -> Quantity:
    """Largest quantity every leg can fill.

    The minimum across legs: a structure needing `q` in each contract is
    limited by its thinnest. One empty leg makes the common depth zero, which is
    the correct answer, not an error.
    """
    if not curves:
        return Quantity.zero()
    return Quantity.from_units(min(curve.max_fillable_quantity.units for curve in curves))


def detect_liquidity_collisions(
    curves: Sequence[ExecutionCurve], *, up_to: Quantity | None = None
) -> tuple[BookLiquidityId, ...]:
    """Source levels that more than one leg would consume.

    ``up_to`` restricts the check to the levels actually reached at that
    quantity; without it, every level in every curve is considered. The
    narrower check matters because two legs may overlap only in depth neither
    of them reaches.
    """
    seen: dict[BookLiquidityId, int] = {}
    for curve in curves:
        reachable = curve.levels if up_to is None else _levels_up_to(curve, up_to)
        for identity in {level.liquidity for level in reachable}:
            seen[identity] = seen.get(identity, 0) + 1
    return tuple(sorted(identity for identity, count in seen.items() if count > 1))


def _levels_up_to(curve: ExecutionCurve, quantity: Quantity) -> tuple[ExecutableLevel, ...]:
    """Levels a fill of ``quantity`` would touch."""
    touched: list[ExecutableLevel] = []
    remaining = quantity.units
    for level in curve.levels:
        if remaining <= 0:
            break
        touched.append(level)
        remaining -= level.available_quantity.units
    return tuple(touched)


def multi_leg_costs(curves: Sequence[ExecutionCurve]) -> tuple[MultiLegCostPoint, ...]:
    """Total gross cost at **every** quantity where any leg's marginal cost changes.

    This is the canonical economic curve, and it is deliberately complete: the
    breakpoints are the full union of the legs' own breakpoints, capped at the
    common fillable quantity. Nothing is sampled, thinned or summarised.

    Completeness is not a nicety here. Everything downstream is discontinuous at
    exactly these points -- the fee model is non-linear in price, fee rounding is
    a ceiling, and guaranteed payoff is quantity-dependent -- so profitability
    can flip at a single breakpoint. Omitting one interior point could miss a
    real candidate, invent a maximum profitable quantity, or report the wrong
    limiting quantity. Use :func:`thin_for_display` when a human or a chart
    needs fewer rows, and never feed the result of that to a detector.

    Raises :class:`LiquidityCollisionError` if two legs claim the same resting
    level.
    """
    if not curves:
        return ()

    common = common_fillable_quantity(curves)
    if common.is_zero:
        return ()

    collisions = detect_liquidity_collisions(curves, up_to=common)
    if collisions:
        raise LiquidityCollisionError(collisions)

    quantities = {
        point.cumulative_quantity.units
        for curve in curves
        for point in curve.breakpoints
        if 0 < point.cumulative_quantity.units <= common.units
    }
    quantities.add(common.units)

    return tuple(_cost_point(curves, Quantity.from_units(units)) for units in sorted(quantities))


def _cost_point(curves: Sequence[ExecutionCurve], quantity: Quantity) -> MultiLegCostPoint:
    leg_costs = tuple(curve.gross_cost(quantity) for curve in curves)
    return MultiLegCostPoint(
        quantity=quantity,
        total_gross_cost=Money.from_units(sum(cost.units for cost in leg_costs)),
        leg_costs=leg_costs,
        leg_vwaps=tuple(curve.vwap(quantity) for curve in curves),
        leg_tickers=tuple(curve.market_ticker for curve in curves),
        liquidity_ids=tuple(
            sorted(
                {level.liquidity for curve in curves for level in _levels_up_to(curve, quantity)}
            )
        ),
    )


def thin_for_display(
    points: Sequence[MultiLegCostPoint], *, max_points: int = 40
) -> tuple[MultiLegCostPoint, ...]:
    """Reduce a cost curve for terminal output, charts or dashboards.

    **Presentation only.** No detector, fee engine or payoff engine may consume
    the result: a thinned curve is missing exactly the interior breakpoints where
    profitability can change, so a decision made from one can be wrong in a way
    that leaves no trace.

    Purely a filter over an already-computed exact curve -- it never recomputes
    anything, and the first and last points are always kept so the cheapest and
    deepest executable sets remain visible.
    """
    if max_points < _MIN_DISPLAY_POINTS:
        raise ValueError(f"max_points must be at least {_MIN_DISPLAY_POINTS}, got {max_points}")
    if len(points) <= max_points:
        return tuple(points)
    step = (len(points) - 1) / (max_points - 1)
    indices = sorted({round(index * step) for index in range(max_points)} | {len(points) - 1})
    return tuple(points[index] for index in indices)
