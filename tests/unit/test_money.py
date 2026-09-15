"""Unit tests for exact money arithmetic."""

from __future__ import annotations

from decimal import Decimal

import pytest

from predarb.domain.money import (
    MONEY_SCALE,
    PRICE_SCALE,
    QUANTITY_SCALE,
    FloatRejectedError,
    InexactValueError,
    Money,
    Price,
    Quantity,
)

pytestmark = pytest.mark.unit


class TestScaleInvariant:
    def test_price_times_quantity_scale_equals_money_scale(self):
        # The whole design rests on this: it is why price * quantity never rounds.
        assert PRICE_SCALE * QUANTITY_SCALE == MONEY_SCALE


class TestExactRepresentation:
    @pytest.mark.parametrize(
        ("wire", "units"),
        [
            ("0.4200", 4200),
            ("0.0001", 1),
            ("0.0000", 0),
            ("1.0000", 10_000),
            ("0.9900", 9900),
            ("0.0010", 10),
        ],
    )
    def test_price_from_wire_format(self, wire, units):
        assert Price.from_value(wire).units == units

    @pytest.mark.parametrize(
        ("wire", "units"),
        [
            ("1596.82", 159_682),
            ("13.00", 1300),
            ("0.01", 1),
            ("14.29", 1429),
            ("0.00", 0),
        ],
    )
    def test_quantity_from_wire_format(self, wire, units):
        # Kalshi counts are genuinely fractional; "14.29" must not become 14.
        assert Quantity.from_value(wire).units == units

    def test_quantity_preserves_fractional_contracts(self):
        assert Quantity.from_value("14.29").as_decimal() == Decimal("14.29")

    def test_accepts_int_and_decimal(self):
        assert Price.from_value(1).units == 10_000
        assert Price.from_value(Decimal("0.42")).units == 4200
        assert Money.from_value(Decimal("0.003639")).units == 3639


class TestRoundTripSerialization:
    @pytest.mark.parametrize("wire", ["0.4200", "0.0001", "1.0000", "0.5600"])
    def test_price_round_trip(self, wire):
        assert Price.from_value(wire).to_str() == wire

    @pytest.mark.parametrize("wire", ["1596.82", "13.00", "0.01"])
    def test_quantity_round_trip(self, wire):
        assert Quantity.from_value(wire).to_str() == wire

    @pytest.mark.parametrize("wire", ["0.003639", "-0.055000", "0.000001", "0.000000"])
    def test_money_round_trip(self, wire):
        assert Money.from_value(wire).to_str() == wire

    def test_money_renders_negative_sub_unit_correctly(self):
        assert Money.from_units(-1).to_str() == "-0.000001"


class TestFloatRejection:
    @pytest.mark.parametrize("ctor", [Price.from_value, Quantity.from_value, Money.from_value])
    def test_float_is_rejected(self, ctor):
        with pytest.raises(FloatRejectedError):
            ctor(0.42)

    @pytest.mark.parametrize("ctor", [Price.from_value, Quantity.from_value, Money.from_value])
    def test_bool_is_rejected(self, ctor):
        # bool is an int subclass; True must not silently become 1.0000.
        with pytest.raises(FloatRejectedError):
            ctor(True)

    def test_float_units_rejected(self):
        with pytest.raises(FloatRejectedError):
            Price(4200.0)  # type: ignore[arg-type]

    def test_nan_and_inf_rejected(self):
        for bad in ("NaN", "Infinity", "-Infinity"):
            with pytest.raises(InexactValueError):
                Money.from_value(bad)

    def test_garbage_string_rejected(self):
        with pytest.raises(InexactValueError):
            Price.from_value("not-a-price")


class TestPrecisionFailsClosed:
    def test_price_with_five_decimals_is_rejected(self):
        # Truncating here would invent an off-tick price and a fake edge.
        with pytest.raises(InexactValueError):
            Price.from_value("0.12345")

    def test_quantity_with_three_decimals_is_rejected(self):
        with pytest.raises(InexactValueError):
            Quantity.from_value("1.001")

    def test_money_with_seven_decimals_is_rejected(self):
        with pytest.raises(InexactValueError):
            Money.from_value("0.0000001")

    def test_negative_price_rejected(self):
        with pytest.raises(InexactValueError):
            Price.from_value("-0.0100")

    def test_negative_quantity_rejected(self):
        with pytest.raises(InexactValueError):
            Quantity.from_value("-1.00")

    def test_money_may_be_negative(self):
        assert Money.from_value("-0.055000").units == -55_000


class TestMultiplication:
    def test_price_times_quantity_is_exact(self):
        cost = Price.from_value("0.4200") * Quantity.from_value("100.00")
        assert cost.to_str() == "42.000000"

    def test_multiplication_is_commutative(self):
        p, q = Price.from_value("0.3300"), Quantity.from_value("7.77")
        assert p * q == q * p

    def test_sub_cent_price_times_fractional_quantity(self):
        # 0.0001 * 14.29 = 0.001429 exactly; no rounding anywhere.
        cost = Price.from_value("0.0001") * Quantity.from_value("14.29")
        assert cost.to_str() == "0.001429"
        assert cost.as_decimal() == Decimal("0.0001") * Decimal("14.29")

    def test_multiplying_price_by_price_is_not_allowed(self):
        with pytest.raises(TypeError):
            _ = Price.from_value("0.42") * Price.from_value("0.42")  # type: ignore[operator]


class TestComplement:
    def test_complement_against_one_dollar_notional(self):
        notional = Price.from_value("1.0000")
        assert Price.from_value("0.5600").complement(notional).to_str() == "0.4400"

    def test_complement_requires_explicit_notional(self):
        # No default: assuming $1.00 is how a non-standard contract becomes a
        # fake arbitrage.
        with pytest.raises(TypeError):
            Price.from_value("0.5600").complement()  # type: ignore[call-arg]

    def test_complement_rejects_price_above_notional(self):
        with pytest.raises(InexactValueError):
            Price.from_value("1.5000").complement(Price.from_value("1.0000"))


class TestRounding:
    def test_ceil_to_six_decimals_is_identity(self):
        assert Money.from_value("0.003639").ceil_to_decimals(6).to_str() == "0.003639"

    def test_ceil_rounds_up_for_positive(self):
        assert Money.from_value("0.003639").ceil_to_decimals(2).to_str() == "0.010000"

    def test_ceil_is_exact_on_boundary(self):
        assert Money.from_value("0.010000").ceil_to_decimals(2).to_str() == "0.010000"

    def test_ceil_rounds_toward_zero_for_negative(self):
        # Ceiling of -0.055 at cent precision is -0.05, not -0.06.
        assert Money.from_value("-0.055000").ceil_to_decimals(2).to_str() == "-0.050000"

    def test_floor_rounds_down_for_negative(self):
        # This is the aligned_change step in Kalshi's documented rounding.
        assert Money.from_value("-0.058639").floor_to_decimals(2).to_str() == "-0.060000"

    def test_floor_rounds_down_for_positive(self):
        assert Money.from_value("0.019999").floor_to_decimals(2).to_str() == "0.010000"

    @pytest.mark.parametrize("decimals", [-1, 7, 100])
    def test_invalid_decimals_rejected(self, decimals):
        with pytest.raises(InexactValueError):
            Money.from_value("1.000000").ceil_to_decimals(decimals)

    def test_float_decimals_rejected(self):
        with pytest.raises(FloatRejectedError):
            Money.from_value("1.000000").ceil_to_decimals(2.0)  # type: ignore[arg-type]


class TestKalshiDocumentedRoundingExample:
    """Reproduces the worked example on Kalshi's fee-rounding page.

    Documented values: trade fee = ceil_6dp($0.00363825) = $0.003639;
    aligned change = floor_cent(-$0.055 - $0.003639) = -$0.060000;
    rounding fee = (-$0.055 - $0.003639) - (-$0.06) = $0.001361.

    Note the input $0.00363825 has 8 decimal places, which is finer than
    ``Money`` represents. That is correct and intentional: the *model fee* is a
    real-valued intermediate, and 6 dp is the precision at which it becomes
    money. The intermediate is carried as ``Decimal`` and lands in ``Money``
    exactly once, at the documented ceiling step.
    """

    def test_worked_example(self):
        model_fee = Decimal("0.00363825")
        trade_fee = Money.ceil_from_decimal(model_fee)
        assert trade_fee.to_str() == "0.003639"

        revenue = Money.from_value("-0.055000")
        net = revenue - trade_fee
        assert net.to_str() == "-0.058639"

        aligned_change = net.floor_to_decimals(2)
        assert aligned_change.to_str() == "-0.060000"

        rounding_fee = net - aligned_change
        assert rounding_fee.to_str() == "0.001361"


class TestOrderingAndArithmetic:
    def test_money_addition_and_subtraction(self):
        a, b = Money.from_value("1.500000"), Money.from_value("0.250000")
        assert (a + b).to_str() == "1.750000"
        assert (a - b).to_str() == "1.250000"
        assert (-a).to_str() == "-1.500000"
        assert abs(Money.from_value("-2.000000")).to_str() == "2.000000"

    def test_scale_by_int(self):
        # The (N - 1) payoff multiplier in an AT_MOST_ONE basket.
        assert Money.from_value("1.000000").scale_by_int(4).to_str() == "4.000000"

    def test_scale_by_float_rejected(self):
        with pytest.raises(FloatRejectedError):
            Money.from_value("1.000000").scale_by_int(2.0)  # type: ignore[arg-type]

    def test_ordering(self):
        assert Money.from_value("1.000000") > Money.from_value("0.999999")
        assert Price.from_value("0.4200") < Price.from_value("0.4201")
        assert Quantity.from_value("1.00") <= Quantity.from_value("1.00")

    def test_cross_type_comparison_is_not_allowed(self):
        with pytest.raises(TypeError):
            _ = Money.from_value("1.000000") < Price.from_value("1.0000")  # type: ignore[operator]

    def test_hashable_and_frozen(self):
        p = Price.from_value("0.4200")
        assert len({p, Price.from_value("0.4200")}) == 1
        with pytest.raises(AttributeError):
            p.units = 5  # type: ignore[misc]
