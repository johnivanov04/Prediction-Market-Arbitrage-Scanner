"""Exact average prices.

Why this is not a :class:`~predarb.domain.money.Price`
------------------------------------------------------
A VWAP is ``total_cost / total_quantity``. That ratio is very often not
representable on the venue's 4-decimal price grid: buying 3 contracts at
$0.4200 and 1 at $0.4300 averages $0.425, and buying 3 at $0.0001 and 1 at
$0.0002 averages $0.000125 -- finer than a price can express.

Rounding it into a ``Price`` would be wrong twice over. It loses value, and it
implies the number is something you could put on an order, which it is not. A
VWAP is an **analytical ratio**; an order price is a grid point. Keeping the
types distinct means the two can never be confused at a call site.

So the ratio is stored exactly, as its numerator and denominator, and rendered
to a decimal only when a human needs to read it -- with the precision stated
explicitly at that moment rather than baked into storage.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from fractions import Fraction
from typing import Final

from predarb.domain.money import Money, Quantity

__all__ = ["DEFAULT_DISPLAY_PLACES", "AveragePrice"]

DEFAULT_DISPLAY_PLACES: Final = 8
"""Places used when rendering for humans.

Deeper than a price (4) or money (6) so that a rendered VWAP does not silently
collapse two genuinely different averages into the same string."""

_RENDER_PRECISION: Final = 60


@dataclass(frozen=True, slots=True)
class AveragePrice:
    """An exact ``cost / quantity`` ratio.

    Comparable and hashable, and compared by *value* rather than by numerator
    and denominator, so two averages that are arithmetically equal compare equal
    even when they came from different fills.
    """

    total_cost: Money
    total_quantity: Quantity

    def __post_init__(self) -> None:
        if self.total_quantity.is_zero:
            raise ValueError(
                "an average price needs a non-zero quantity; a zero-quantity fill "
                "has no average, which is why callers get None rather than a value"
            )

    @property
    def ratio(self) -> Fraction:
        """The exact ratio, in dollars per contract.

        ``Money`` is micro-dollars and ``Quantity`` is hundredths of a contract,
        so the unit ratio must be scaled by 10**-4 to be dollars per contract.
        """
        return Fraction(self.total_cost.units, self.total_quantity.units * 10_000)

    def as_decimal(self, places: int = DEFAULT_DISPLAY_PLACES) -> Decimal:
        """Render for display. Rounded, and only here.

        Banker's rounding, so repeated rendering does not drift upward.
        """
        if places < 0:
            raise ValueError(f"places must not be negative, got {places}")
        with localcontext() as ctx:
            ctx.prec = _RENDER_PRECISION
            exact = Decimal(self.ratio.numerator) / Decimal(self.ratio.denominator)
            return exact.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_EVEN)

    @property
    def is_exact_on_price_grid(self) -> bool:
        """Whether this average happens to land on the 4 dp price scale.

        Useful for diagnostics. It is **not** a licence to convert: even an
        average that lands on the grid is still an average, not an order price.
        """
        return (self.ratio * 10_000).denominator == 1

    def cost_for(self, quantity: Quantity) -> Fraction:
        """Exact cost of ``quantity`` at this average, in dollars.

        Returns a ``Fraction`` rather than ``Money`` because the result need not
        be representable in micro-dollars, and silently rounding it here would
        reintroduce the error this type exists to avoid.
        """
        return self.ratio * Fraction(quantity.units, 100)

    def __str__(self) -> str:
        return str(self.as_decimal())

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, AveragePrice):
            return NotImplemented
        return self.ratio == other.ratio

    def __lt__(self, other: AveragePrice) -> bool:
        if not isinstance(other, AveragePrice):
            return NotImplemented
        return self.ratio < other.ratio

    def __le__(self, other: AveragePrice) -> bool:
        return self == other or self < other

    def __hash__(self) -> int:
        return hash(self.ratio)
