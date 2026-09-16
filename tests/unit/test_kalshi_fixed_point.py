"""Tests for exact parsing of Kalshi's fixed-point wire values."""

from __future__ import annotations

import ast
import inspect
from decimal import Decimal

import pytest

from predarb.domain import money
from predarb.domain.money import FloatRejectedError, InexactValueError, Money, Price, Quantity
from predarb.venues.kalshi import fixed_point
from predarb.venues.kalshi.fixed_point import (
    parse_fee_multiplier,
    parse_money_dollars,
    parse_optional_price_dollars,
    parse_optional_quantity_fp,
    parse_price_dollars,
    parse_quantity_delta_fp,
    parse_quantity_fp,
)

pytestmark = pytest.mark.unit


class TestPriceParsing:
    @pytest.mark.parametrize(
        ("wire", "units"),
        [
            ("0.0000", 0),
            ("0.0001", 1),
            ("1.0000", 10_000),
            ("0.4200", 4200),
            ("0.5600", 5600),
            ("0.7410", 7410),
            ("0.0010", 10),
            ("0.9900", 9900),
        ],
    )
    def test_parses_observed_wire_values(self, wire, units):
        assert parse_price_dollars(wire).units == units

    def test_round_trips_byte_for_byte(self):
        for wire in ("0.0000", "0.0001", "0.4200", "1.0000", "0.2180"):
            assert parse_price_dollars(wire).to_str() == wire

    def test_fewer_decimal_places_accepted(self):
        # The venue pads to 4 dp, but a shorter exact value is still exact.
        assert parse_price_dollars("0.42").units == 4200
        assert parse_price_dollars("1").units == 10_000
        assert parse_price_dollars("0.4").units == 4000

    def test_leading_and_trailing_zeros(self):
        assert parse_price_dollars("00.4200").units == 4200
        assert parse_price_dollars("0.420000").units == 4200

    def test_surrounding_whitespace_tolerated(self):
        assert parse_price_dollars("  0.4200 ").units == 4200

    def test_excessive_precision_rejected(self):
        # Rounding here would invent an off-tick price the venue never sent.
        with pytest.raises(InexactValueError):
            parse_price_dollars("0.12345")

    def test_negative_price_rejected(self):
        with pytest.raises(InexactValueError):
            parse_price_dollars("-0.0100")

    def test_non_string_rejected(self):
        # Kalshi encodes prices as strings; a number means the schema moved.
        for bad in (0.42, 42, Decimal("0.42"), None, ["0.42"]):
            with pytest.raises(InexactValueError):
                parse_price_dollars(bad)


class TestQuantityParsing:
    @pytest.mark.parametrize(
        ("wire", "units"),
        [("0.00", 0), ("0.01", 1), ("13.00", 1300), ("1596.82", 159_682), ("14.29", 1429)],
    )
    def test_parses_observed_wire_values(self, wire, units):
        assert parse_quantity_fp(wire).units == units

    def test_fractional_contracts_preserved(self):
        assert parse_quantity_fp("9358.02").as_decimal() == Decimal("9358.02")

    def test_large_quantity(self):
        assert parse_quantity_fp("5892940.00").units == 589_294_000

    def test_excessive_precision_rejected(self):
        with pytest.raises(InexactValueError):
            parse_quantity_fp("1.001")

    def test_negative_quantity_rejected(self):
        with pytest.raises(InexactValueError):
            parse_quantity_fp("-1.00")


class TestQuantityDeltaParsing:
    def test_negative_delta(self):
        assert parse_quantity_delta_fp("-54.00").units == -5400

    def test_positive_delta(self):
        assert parse_quantity_delta_fp("12.50").units == 1250

    def test_fractional_delta(self):
        assert parse_quantity_delta_fp("14.29").units == 1429

    def test_round_trip(self):
        for wire in ("-54.00", "12.50", "0.00", "-0.01"):
            assert parse_quantity_delta_fp(wire).to_str() == wire

    def test_excessive_precision_rejected(self):
        with pytest.raises(InexactValueError):
            parse_quantity_delta_fp("-1.001")


class TestMoneyParsing:
    def test_six_decimal_places(self):
        assert parse_money_dollars("0.003639").units == 3639

    def test_four_decimal_wire_value(self):
        # liquidity_dollars arrives at 4 dp; the value is preserved exactly even
        # though Money canonically renders 6.
        parsed = parse_money_dollars("0.0000")
        assert parsed.as_decimal() == Decimal("0.0000")
        assert parsed.to_str() == "0.000000"

    def test_excessive_precision_rejected(self):
        with pytest.raises(InexactValueError):
            parse_money_dollars("0.0000001")


class TestOptionalParsers:
    def test_none_stays_none(self):
        # Absent is not zero: a market with no bid is not bidding $0.00.
        assert parse_optional_price_dollars(None) is None
        assert parse_optional_quantity_fp(None) is None

    def test_present_value_parsed(self):
        assert parse_optional_price_dollars("0.4200") == Price.from_value("0.4200")
        assert parse_optional_quantity_fp("13.00") == Quantity.from_value("13.00")


class TestFeeMultiplierParsing:
    """The multiplier is a JSON *number*, unlike every other financial field."""

    @pytest.mark.parametrize("raw", [0, 1, 2])
    def test_integers(self, raw):
        assert parse_fee_multiplier(raw) == Decimal(raw)

    def test_decimal_preserved(self):
        # 0.5 and 0 were both observed live.
        assert parse_fee_multiplier(Decimal("0.5")) == Decimal("0.5")

    def test_float_rejected(self):
        # 0.5 is binary-exact so a float would survive today; 0.1 would not.
        # Rejecting the type is what stops the silent case ever arising.
        with pytest.raises(InexactValueError):
            parse_fee_multiplier(0.5)

    def test_bool_rejected(self):
        with pytest.raises(InexactValueError):
            parse_fee_multiplier(True)

    def test_string_accepted(self):
        assert parse_fee_multiplier("0.0175") == Decimal("0.0175")


class TestNoFloatPath:
    """The parsers must never route a value through binary floating point."""

    @pytest.mark.parametrize("module", [fixed_point, money])
    def test_no_float_call_in_executable_code(self, module):
        """No ``float(...)`` call anywhere in the exact-arithmetic modules.

        Checked via AST rather than text search, so prose in the docstrings --
        which necessarily discusses ``float(...)`` to explain why it is banned --
        cannot trip the test, and a real call cannot hide behind formatting.
        """
        tree = ast.parse(inspect.getsource(module))
        offenders = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "float"
        ]
        assert offenders == [], (
            f"float() called in {module.__name__} at lines {offenders}; "
            "exact arithmetic must never route through binary floating point"
        )

    @pytest.mark.parametrize(
        ("wire", "expected_units"),
        [("0.0003", 3), ("0.0006", 6), ("0.0012", 12), ("0.0029", 29), ("0.0093", 93)],
    )
    def test_prices_that_a_float_path_would_corrupt(self, wire, expected_units):
        # int(float("0.0003") * 10000) == 2, not 3. At the $0.0001 edge grid that
        # is a full tick of error. 573 of the 10,001 representable prices are
        # wrong via the float path.
        assert int(float(wire) * 10_000) != expected_units
        assert parse_price_dollars(wire).units == expected_units

    @pytest.mark.parametrize(
        ("wire", "expected_units"),
        [("0.29", 29), ("0.57", 57), ("1.13", 113), ("2.01", 201)],
    )
    def test_quantities_that_a_float_path_would_corrupt(self, wire, expected_units):
        assert int(float(wire) * 100) != expected_units
        assert parse_quantity_fp(wire).units == expected_units

    def test_money_ceil_uses_decimal_not_float(self):
        assert Money.ceil_from_decimal(Decimal("0.00363825")).to_str() == "0.003639"


class TestErrorTypes:
    def test_float_rejection_is_a_type_error(self):
        # FloatRejectedError subclasses TypeError so callers can catch broadly.
        assert issubclass(FloatRejectedError, TypeError)

    def test_inexact_is_a_value_error(self):
        assert issubclass(InexactValueError, ValueError)
