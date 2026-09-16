"""Exact parsing of Kalshi's fixed-point wire values.

Kalshi encodes financial quantities as **decimal strings**, not JSON numbers:

=====================  ==================  =========================
Wire field pattern     Example             Parsed as
=====================  ==================  =========================
``*_dollars``          ``"0.4200"``        :class:`Price` (4 dp)
``*_dollars`` (money)  ``"0.003639"``      :class:`Money` (6 dp)
``*_fp``               ``"1596.82"``       :class:`Quantity` (2 dp)
``delta_fp``           ``"-54.00"``        :class:`QuantityDelta` (signed)
=====================  ==================  =========================

Why every parser here goes string -> Decimal -> int, never through ``float``
----------------------------------------------------------------------------
This is not a stylistic preference. Measured against the real grids:

* **573 of the 10,001** representable prices in ``[0.0000, 1.0000]`` are
  corrupted by ``int(float(s) * 10000)``. ``"0.0003"`` yields ``2`` instead of
  ``3`` -- a whole tick wrong, on exactly the ``$0.0001`` grid that the
  ``center_deci_edge_centi_cent`` structure uses at the book edges.
* **9,174 of the first 200,000** representable quantities are corrupted by
  ``int(float(s) * 100)``. ``"0.29"`` yields ``28`` instead of ``29``.

A single such error changes a price by a tick or a size by a contract, which is
more than enough to invent or erase an apparent edge. So ``float`` appears
nowhere in this module, and ``tests/unit/test_kalshi_fixed_point.py`` asserts
that the source contains no float conversion.

Precision is rejected rather than rounded
-----------------------------------------
A price with five decimals, or a quantity with three, raises. Kalshi does not
document rounding at this boundary, so rounding here would be us inventing a
value the exchange never sent. See ``docs/api_assumptions.md`` A-04/A-05.
"""

from __future__ import annotations

from decimal import Decimal

from predarb.domain.money import (
    InexactValueError,
    Money,
    Price,
    Quantity,
    QuantityDelta,
)

__all__ = [
    "parse_fee_multiplier",
    "parse_money_dollars",
    "parse_optional_money_dollars",
    "parse_optional_price_dollars",
    "parse_optional_quantity_fp",
    "parse_price_dollars",
    "parse_quantity_delta_fp",
    "parse_quantity_fp",
]


def _require_wire_string(value: object, *, field: str) -> str:
    """Require a genuine string, the way Kalshi actually encodes these values.

    A JSON number arriving where a fixed-point string is expected means either
    the schema changed or something upstream already passed the value through a
    float. Both are loud failures, not values to coerce.
    """
    if isinstance(value, str):
        return value.strip()
    raise InexactValueError(
        f"{field}: expected a fixed-point decimal string, got "
        f"{type(value).__name__} ({value!r}). Kalshi encodes this field as a "
        "string; a number here means the schema changed or a float crept in."
    )


def parse_price_dollars(value: object, *, field: str = "price") -> Price:
    """Parse a ``*_dollars`` price string (4 dp) into an exact :class:`Price`."""
    return Price.from_value(_require_wire_string(value, field=field))


def parse_quantity_fp(value: object, *, field: str = "quantity") -> Quantity:
    """Parse a ``*_fp`` count string (2 dp) into an exact :class:`Quantity`."""
    return Quantity.from_value(_require_wire_string(value, field=field))


def parse_quantity_delta_fp(value: object, *, field: str = "delta_fp") -> QuantityDelta:
    """Parse a signed ``delta_fp`` string into an exact :class:`QuantityDelta`."""
    return QuantityDelta.from_value(_require_wire_string(value, field=field))


def parse_money_dollars(value: object, *, field: str = "money") -> Money:
    """Parse a dollar string into :class:`Money` (up to 6 dp)."""
    return Money.from_value(_require_wire_string(value, field=field))


def parse_optional_price_dollars(value: object, *, field: str = "price") -> Price | None:
    """As :func:`parse_price_dollars`, but ``None``/absent stays ``None``.

    Absence is preserved rather than defaulted to zero: a market with no quote
    is not a market quoting $0.00, and collapsing the two would create a
    free-money artefact at the top of the book.
    """
    if value is None:
        return None
    return parse_price_dollars(value, field=field)


def parse_optional_quantity_fp(value: object, *, field: str = "quantity") -> Quantity | None:
    """As :func:`parse_quantity_fp`, but ``None``/absent stays ``None``.

    Observed live: ``no_bid_size_fp`` is absent on markets quoting only one
    side. Absent is not the same as ``"0.00"`` and is kept distinct.
    """
    if value is None:
        return None
    return parse_quantity_fp(value, field=field)


def parse_optional_money_dollars(value: object, *, field: str = "money") -> Money | None:
    if value is None:
        return None
    return parse_money_dollars(value, field=field)


def parse_fee_multiplier(value: object, *, field: str = "fee_multiplier") -> Decimal:
    """Parse ``fee_multiplier`` / ``fee_multiplier_override``.

    This one is different from every other parser here, and the difference is a
    trap. Unlike prices and sizes, Kalshi sends the fee multiplier as a **JSON
    number**, not a string -- observed values ``0``, ``0.5`` and ``1``.

    A JSON number is decoded by ``json.loads`` into a ``float`` before any model
    ever sees it, so exactness must be preserved *at decode time*, by decoding
    with ``parse_float=Decimal`` (see
    :func:`predarb.venues.kalshi.models.decode_json`). By the time a bare
    ``float`` reaches here the damage may already be done, so a ``float`` is
    rejected rather than converted.

    ``0.5`` happens to be binary-exact, so a float round-trip would survive
    today. A future multiplier of ``0.1`` would not, and the failure would be
    silent and tiny -- the worst possible kind in a fee calculation.
    """
    if isinstance(value, bool):
        raise InexactValueError(f"{field}: bool is not a valid multiplier ({value!r})")
    if isinstance(value, float):
        raise InexactValueError(
            f"{field}: received a float ({value!r}). Decode the JSON with "
            "parse_float=Decimal so the multiplier keeps its exact decimal value; "
            "by the time it is a float the original digits may already be lost."
        )
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise InexactValueError(f"{field}: {value!r} is not finite")
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        return Decimal(value.strip())
    raise InexactValueError(f"{field}: unsupported type {type(value).__name__} ({value!r})")
