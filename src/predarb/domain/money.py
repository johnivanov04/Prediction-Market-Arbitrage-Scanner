"""Exact money, price and quantity arithmetic.

Phase 1 forbids binary floating point for anything that touches money. This
module is the only place scaled-integer representations are defined, and every
other module is expected to build on these types rather than on ``float``,
``Decimal`` or raw ``int``.

Why scaled integers rather than ``Decimal``
-------------------------------------------
Not because ``Decimal`` is inexact or nondeterministic -- it is neither, and
with explicit contexts it is perfectly reproducible. The choice follows from
the shape of the data: Kalshi documents prices to four decimal places and
quantities to two, so their product lands at six. Those scales are fixed and
known in advance, which makes scaled integers the closer fit:

* exactness is structural rather than contextual -- every operation below is
  plain integer arithmetic, with no precision or rounding mode to set
  correctly at each call site;
* invalid precision is easy to reject -- a five-decimal price simply fails to
  convert to an integer number of $0.0001 units, so it raises instead of
  silently rounding into a valid-looking off-tick price;
* serialisation is unambiguous -- one integer has exactly one wire form.

``Decimal`` is still used at the parsing/serialisation boundary, where it turns
an exact decimal string into an exact integer, and for genuinely real-valued
intermediates such as a fee model's ``raw_model_fee`` (see
:meth:`Money.ceil_from_decimal`).

The scales are chosen to match the Kalshi API as it is documented and as it
actually behaves (see ``docs/api_assumptions.md``):

==============  ========================  ==============================
Concept         Wire format               Representation here
==============  ========================  ==============================
Price           ``"0.4200"``  (4 dp USD)  ``Price``    -- units of $0.0001
Quantity        ``"1596.82"`` (2 dp)      ``Quantity`` -- units of 0.01 contracts
Money           fees quoted to 6 dp USD   ``Money``    -- units of $0.000001
==============  ========================  ==============================

The central identity this module exists to guarantee is::

    PRICE_SCALE * QUANTITY_SCALE == MONEY_SCALE
    (1e-4)      * (1e-2)         == 1e-6

Because of it, ``price * quantity`` is a single exact integer multiplication
with **no rounding whatsoever**, and Kalshi's documented ``ceil_6dp`` fee
rounding is exactly "round up to one unit of :class:`Money`". Gross notional is
therefore never approximated, and the only rounding in the whole pipeline is the
rounding the exchange itself documents.

Note on prices: Kalshi no longer quotes whole cents. Observed tick sizes are as
fine as $0.0001 near the book edges, so a cent-based representation would be
lossy. See ``docs/api_assumptions.md`` (A-04).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, InvalidOperation, localcontext
from typing import Final, Self

__all__ = [
    "MONEY_DECIMALS",
    "MONEY_SCALE",
    "PRICE_DECIMALS",
    "PRICE_SCALE",
    "QUANTITY_DECIMALS",
    "QUANTITY_SCALE",
    "FloatRejectedError",
    "InexactValueError",
    "Money",
    "MoneyError",
    "Price",
    "Quantity",
    "QuantityDelta",
]

PRICE_DECIMALS: Final[int] = 4
QUANTITY_DECIMALS: Final[int] = 2
MONEY_DECIMALS: Final[int] = 6

PRICE_SCALE: Final[int] = 10**PRICE_DECIMALS
QUANTITY_SCALE: Final[int] = 10**QUANTITY_DECIMALS
MONEY_SCALE: Final[int] = 10**MONEY_DECIMALS

# Precision used only for the string/Decimal boundary. Large enough that no
# realistic notional can overflow it, and never relied on for correctness:
# every conversion is checked for exactness afterwards.
_PARSE_PRECISION: Final[int] = 60

if PRICE_SCALE * QUANTITY_SCALE != MONEY_SCALE:  # pragma: no cover - structural invariant
    raise RuntimeError(
        "Scale invariant violated: PRICE_SCALE * QUANTITY_SCALE must equal MONEY_SCALE, "
        "otherwise price x quantity is not exact."
    )


class MoneyError(Exception):
    """Base class for exact-arithmetic failures."""


class FloatRejectedError(MoneyError, TypeError):
    """Raised when a binary float is offered where exact arithmetic is required.

    This is deliberately loud. A ``float`` reaching this layer means some caller
    has done inexact arithmetic upstream, and the result can no longer be
    trusted for an arbitrage claim.
    """


class InexactValueError(MoneyError, ValueError):
    """Raised when a value cannot be represented exactly at the required scale."""


def _to_decimal(value: object, *, field: str) -> Decimal:
    """Coerce an exact value to ``Decimal``, rejecting floats and non-finites.

    Accepts ``str``, ``int`` and ``Decimal``. The parameter is typed ``object``
    on purpose: this is a validating boundary, and a narrower annotation would
    make the runtime guards unreachable for the type checker while doing nothing
    to stop an untyped caller at runtime.
    """
    if isinstance(value, bool):
        raise FloatRejectedError(f"{field}: bool is not a valid exact value (got {value!r})")
    if isinstance(value, float):
        raise FloatRejectedError(
            f"{field}: refusing to build an exact value from float {value!r}. "
            "Pass a string, int or Decimal; binary floats cannot represent "
            "decimal prices exactly and must never touch money arithmetic."
        )
    if isinstance(value, Decimal):
        dec = value
    elif isinstance(value, int):
        dec = Decimal(value)
    elif isinstance(value, str):
        try:
            dec = Decimal(value.strip())
        except InvalidOperation as exc:
            raise InexactValueError(f"{field}: {value!r} is not a valid decimal string") from exc
    else:
        raise FloatRejectedError(
            f"{field}: unsupported type {type(value).__name__}; expected str, int or Decimal"
        )
    if not dec.is_finite():
        raise InexactValueError(f"{field}: {value!r} is not finite")
    return dec


def _units_from(value: object, *, scale: int, decimals: int, field: str) -> int:
    """Convert an exact decimal value into integer units of ``1/scale``.

    Raises :class:`InexactValueError` if the value carries more precision than
    the scale can hold. Truncating silently here would let a sub-tick price
    round into a fake edge, so this fails closed instead.
    """
    dec = _to_decimal(value, field=field)
    with localcontext() as ctx:
        ctx.prec = _PARSE_PRECISION
        scaled = dec * scale
        integral = scaled.to_integral_value()
        if scaled != integral:
            raise InexactValueError(
                f"{field}: {dec} needs more than {decimals} decimal places "
                f"and cannot be represented exactly"
            )
        return int(integral)


def _require_int(value: int, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FloatRejectedError(f"{field}: units must be a plain int, got {type(value).__name__}")
    return value


def _format_units(units: int, decimals: int) -> str:
    """Render scaled integer units as an exact fixed-point decimal string."""
    sign = "-" if units < 0 else ""
    whole, frac = divmod(abs(units), 10**decimals)
    return f"{sign}{whole}.{frac:0{decimals}d}"


@dataclass(frozen=True, slots=True, order=True)
class Price:
    """A price in units of $0.0001 (one hundredth of a cent).

    A price is a *rate*, not an amount of money: multiplying a price by a
    quantity yields :class:`Money`. Prices are non-negative. The upper bound is
    deliberately **not** enforced here, because it is the contract's
    ``notional_value_dollars`` and is therefore venue/instrument specific; that
    check belongs to the instrument layer, which knows the notional.
    """

    units: int

    def __post_init__(self) -> None:
        _require_int(self.units, field="Price.units")
        if self.units < 0:
            raise InexactValueError(f"Price may not be negative (got {self.units} units)")

    @classmethod
    def from_value(cls, value: object) -> Self:
        """Build from an exact ``str``/``int``/``Decimal`` dollar amount."""
        return cls(_units_from(value, scale=PRICE_SCALE, decimals=PRICE_DECIMALS, field="Price"))

    @classmethod
    def from_units(cls, units: int) -> Self:
        return cls(units)

    def as_decimal(self) -> Decimal:
        return Decimal(self.to_str())

    def to_str(self) -> str:
        """Render in the venue's wire format, e.g. ``"0.4200"``."""
        return _format_units(self.units, PRICE_DECIMALS)

    def __str__(self) -> str:
        return self.to_str()

    def complement(self, notional: Price) -> Price:
        """Return ``notional - self``.

        ``notional`` is required rather than defaulted to $1.00 on purpose. A
        Kalshi binary contract's payout is carried in
        ``notional_value_dollars``; assuming $1.00 is exactly the kind of silent
        assumption that turns a non-standard contract into a fake arbitrage.
        Callers must read the notional from instrument metadata and pass it.
        """
        if notional.units < self.units:
            raise InexactValueError(
                f"complement: price {self} exceeds notional {notional}; "
                "this instrument is not a standard complementary pair"
            )
        return Price(notional.units - self.units)

    def __mul__(self, quantity: Quantity) -> Money:
        """Exact price x quantity. No rounding occurs (see module docstring)."""
        if not isinstance(quantity, Quantity):
            return NotImplemented
        return Money(self.units * quantity.units)


@dataclass(frozen=True, slots=True, order=True)
class Quantity:
    """A contract count in units of 0.01 contracts.

    Kalshi reports sizes as fixed-point strings with two decimals (for example
    ``"1596.82"``), so contract counts are genuinely fractional and must not be
    modelled as integers. See ``docs/api_assumptions.md`` (A-05).
    """

    units: int

    def __post_init__(self) -> None:
        _require_int(self.units, field="Quantity.units")
        if self.units < 0:
            raise InexactValueError(f"Quantity may not be negative (got {self.units} units)")

    @classmethod
    def from_value(cls, value: object) -> Self:
        return cls(
            _units_from(value, scale=QUANTITY_SCALE, decimals=QUANTITY_DECIMALS, field="Quantity")
        )

    @classmethod
    def from_units(cls, units: int) -> Self:
        return cls(units)

    @classmethod
    def zero(cls) -> Self:
        return cls(0)

    def as_decimal(self) -> Decimal:
        return Decimal(self.to_str())

    def to_str(self) -> str:
        """Render in the venue's wire format, e.g. ``"1596.82"``."""
        return _format_units(self.units, QUANTITY_DECIMALS)

    def __str__(self) -> str:
        return self.to_str()

    @property
    def is_zero(self) -> bool:
        return self.units == 0

    def __add__(self, other: Quantity) -> Quantity:
        if not isinstance(other, Quantity):
            return NotImplemented
        return Quantity(self.units + other.units)

    def __sub__(self, other: Quantity) -> Quantity:
        if not isinstance(other, Quantity):
            return NotImplemented
        return Quantity(self.units - other.units)

    def __mul__(self, price: Price) -> Money:
        if not isinstance(price, Price):
            return NotImplemented
        return Money(self.units * price.units)


@dataclass(frozen=True, slots=True, order=True)
class QuantityDelta:
    """A **signed** change in contract count, in units of 0.01 contracts.

    Kalshi's ``orderbook_delta`` messages carry ``delta_fp`` as a signed
    fixed-point string (``"-54.00"``), so a delta cannot be represented by
    :class:`Quantity`, which is non-negative by construction. Keeping them as
    separate types means a delta can never be mistaken for a resting size.
    """

    units: int

    def __post_init__(self) -> None:
        _require_int(self.units, field="QuantityDelta.units")

    @classmethod
    def from_value(cls, value: object) -> Self:
        return cls(
            _units_from(
                value, scale=QUANTITY_SCALE, decimals=QUANTITY_DECIMALS, field="QuantityDelta"
            )
        )

    @classmethod
    def from_units(cls, units: int) -> Self:
        return cls(units)

    def as_decimal(self) -> Decimal:
        return Decimal(self.to_str())

    def to_str(self) -> str:
        """Render in the venue's wire format, e.g. ``"-54.00"``."""
        return _format_units(self.units, QUANTITY_DECIMALS)

    def __str__(self) -> str:
        return self.to_str()

    @property
    def is_negative(self) -> bool:
        return self.units < 0

    def apply_to(self, quantity: Quantity) -> Quantity:
        """Apply this delta to a resting size.

        Raises if the result would be negative. A book level cannot hold a
        negative size, so that outcome means the book state is wrong -- a
        missed message or a misapplied delta -- and must surface rather than be
        clamped to zero, which would silently hide the gap.
        """
        total = quantity.units + self.units
        if total < 0:
            raise InexactValueError(
                f"applying delta {self} to size {quantity} would give a negative "
                f"quantity ({total} units); the book state is inconsistent"
            )
        return Quantity(total)


@dataclass(frozen=True, slots=True, order=True)
class Money:
    """A signed cash amount in units of $0.000001 (one micro-dollar).

    Six decimal places is not arbitrary: it is the precision at which Kalshi
    documents its fee rounding (``trade_fee = ceil_6dp(model_fee)``), and it is
    exactly the product of the price and quantity scales.
    """

    units: int

    def __post_init__(self) -> None:
        _require_int(self.units, field="Money.units")

    @classmethod
    def from_value(cls, value: object) -> Self:
        return cls(_units_from(value, scale=MONEY_SCALE, decimals=MONEY_DECIMALS, field="Money"))

    @classmethod
    def from_units(cls, units: int) -> Self:
        return cls(units)

    @classmethod
    def zero(cls) -> Self:
        return cls(0)

    @classmethod
    def ceil_from_decimal(cls, value: object) -> Self:
        """Bring a real-valued intermediate into :class:`Money`, rounding **up**.

        This is Kalshi's documented ``ceil_6dp``: the fee model produces a
        real-valued ``model_fee`` (the published worked example uses
        ``$0.00363825``, eight decimal places), and the exchange rounds it up to
        the nearest ``$0.000001`` to obtain the charged trade fee.

        This is the *only* sanctioned way for a value with more than six decimal
        places to become money. :meth:`from_value` still rejects such a value,
        because everywhere else the extra precision means a bug rather than a
        documented rounding step.
        """
        dec = _to_decimal(value, field="Money.ceil_from_decimal")
        with localcontext() as ctx:
            ctx.prec = _PARSE_PRECISION
            scaled = dec * MONEY_SCALE
            return cls(int(scaled.to_integral_value(rounding=ROUND_CEILING)))

    def as_decimal(self) -> Decimal:
        return Decimal(self.to_str())

    def to_str(self) -> str:
        """Render with full six-decimal precision, e.g. ``"0.003639"``."""
        return _format_units(self.units, MONEY_DECIMALS)

    def __str__(self) -> str:
        return self.to_str()

    @property
    def is_zero(self) -> bool:
        return self.units == 0

    @property
    def is_positive(self) -> bool:
        return self.units > 0

    def __add__(self, other: Money) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        return Money(self.units + other.units)

    def __sub__(self, other: Money) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        return Money(self.units - other.units)

    def __neg__(self) -> Money:
        return Money(-self.units)

    def __abs__(self) -> Money:
        return Money(abs(self.units))

    def scale_by_int(self, factor: int) -> Money:
        """Multiply by an exact integer (e.g. the ``N - 1`` in an AT_MOST_ONE payoff)."""
        _require_int(factor, field="Money.scale_by_int factor")
        return Money(self.units * factor)

    def ceil_to_decimals(self, decimals: int) -> Money:
        """Round **up** to ``decimals`` places. ``ceil_to_decimals(6)`` is a no-op."""
        step = self._step(decimals)
        return Money(-(-self.units // step) * step)

    def floor_to_decimals(self, decimals: int) -> Money:
        """Round **down** to ``decimals`` places."""
        step = self._step(decimals)
        return Money((self.units // step) * step)

    @staticmethod
    def _step(decimals: int) -> int:
        if not isinstance(decimals, int) or isinstance(decimals, bool):
            raise FloatRejectedError(f"decimals must be an int, got {type(decimals).__name__}")
        if not 0 <= decimals <= MONEY_DECIMALS:
            raise InexactValueError(
                f"decimals must be between 0 and {MONEY_DECIMALS}, got {decimals}"
            )
        return int(10 ** (MONEY_DECIMALS - decimals))
