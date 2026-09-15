"""Property-based tests for exact money arithmetic.

These assert the algebraic laws the rest of the system silently relies on. If
any of them can be broken, an arbitrage claim computed from these types can be
wrong in a way no example-based test would catch.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from predarb.domain.money import (
    MONEY_DECIMALS,
    PRICE_SCALE,
    Money,
    Price,
    Quantity,
)

pytestmark = pytest.mark.property

# Price units span $0.0000 - $1.0000 for a standard $1 notional contract.
price_units = st.integers(min_value=0, max_value=PRICE_SCALE)
# Quantities up to 1,000,000 contracts, in 0.01 steps.
quantity_units = st.integers(min_value=0, max_value=100_000_000)
money_units = st.integers(min_value=-(10**12), max_value=10**12)

prices = price_units.map(Price.from_units)
quantities = quantity_units.map(Quantity.from_units)
monies = money_units.map(Money.from_units)


class TestSerializationRoundTrip:
    @given(prices)
    def test_price_round_trips_through_string(self, price):
        assert Price.from_value(price.to_str()) == price

    @given(quantities)
    def test_quantity_round_trips_through_string(self, quantity):
        assert Quantity.from_value(quantity.to_str()) == quantity

    @given(monies)
    def test_money_round_trips_through_string(self, money):
        assert Money.from_value(money.to_str()) == money

    @given(monies)
    def test_money_round_trips_through_decimal(self, money):
        assert Money.from_value(money.as_decimal()) == money


class TestMultiplicationExactness:
    @given(prices, quantities)
    def test_product_matches_decimal_arithmetic_exactly(self, price, quantity):
        # The core claim: scaled-integer multiplication equals exact decimal
        # multiplication, with no rounding, for every representable input.
        assert (price * quantity).as_decimal() == price.as_decimal() * quantity.as_decimal()

    @given(prices, quantities)
    def test_product_is_commutative(self, price, quantity):
        assert price * quantity == quantity * price

    @given(prices, quantities, quantities)
    def test_distributes_over_quantity_addition(self, price, q1, q2):
        assert price * (q1 + q2) == (price * q1) + (price * q2)

    @given(prices)
    def test_zero_quantity_costs_nothing(self, price):
        assert (price * Quantity.zero()).is_zero


class TestMoneyGroupLaws:
    @given(monies, monies)
    def test_addition_commutes(self, a, b):
        assert a + b == b + a

    @given(monies, monies, monies)
    def test_addition_associates(self, a, b, c):
        assert (a + b) + c == a + (b + c)

    @given(monies)
    def test_negation_is_an_inverse(self, a):
        assert (a + (-a)).is_zero

    @given(monies, monies)
    def test_subtraction_is_addition_of_negation(self, a, b):
        assert a - b == a + (-b)


class TestRoundingLaws:
    @given(monies, st.integers(min_value=0, max_value=MONEY_DECIMALS))
    def test_ceil_is_never_less_than_input(self, money, decimals):
        assert money.ceil_to_decimals(decimals) >= money

    @given(monies, st.integers(min_value=0, max_value=MONEY_DECIMALS))
    def test_floor_is_never_greater_than_input(self, money, decimals):
        assert money.floor_to_decimals(decimals) <= money

    @given(monies, st.integers(min_value=0, max_value=MONEY_DECIMALS))
    def test_ceil_and_floor_differ_by_exactly_zero_or_one_step(self, money, decimals):
        # On the grid the two agree; off it they bracket the value with exactly
        # one step between them. Anything else would mean rounding skipped or
        # overshot a representable amount.
        step = 10 ** (MONEY_DECIMALS - decimals)
        gap = money.ceil_to_decimals(decimals).units - money.floor_to_decimals(decimals).units
        on_grid = money.units % step == 0
        assert gap == (0 if on_grid else step)

    @given(monies, st.integers(min_value=0, max_value=MONEY_DECIMALS))
    def test_rounding_is_idempotent(self, money, decimals):
        once = money.ceil_to_decimals(decimals)
        assert once.ceil_to_decimals(decimals) == once

    @given(monies, st.integers(min_value=0, max_value=MONEY_DECIMALS))
    def test_results_land_on_the_grid(self, money, decimals):
        step = 10 ** (MONEY_DECIMALS - decimals)
        assert money.ceil_to_decimals(decimals).units % step == 0
        assert money.floor_to_decimals(decimals).units % step == 0


class TestCeilFromDecimal:
    @given(st.decimals(min_value=Decimal("-1000"), max_value=Decimal("1000"), places=8))
    def test_never_understates_the_fee(self, value):
        # A fee must never be rounded down: that would overstate profit.
        assert Money.ceil_from_decimal(value).as_decimal() >= value

    @given(st.decimals(min_value=Decimal("-1000"), max_value=Decimal("1000"), places=8))
    def test_within_one_micro_dollar(self, value):
        assert Money.ceil_from_decimal(value).as_decimal() - value < Decimal("0.000001")

    @given(monies)
    def test_agrees_with_from_value_on_representable_inputs(self, money):
        assert Money.ceil_from_decimal(money.as_decimal()) == money


class TestComplement:
    @given(prices, prices)
    def test_complement_is_an_involution(self, price, notional):
        assume(price <= notional)
        assert price.complement(notional).complement(notional) == price

    @given(prices, prices)
    def test_complement_pair_sums_to_notional(self, price, notional):
        assume(price <= notional)
        assert price.units + price.complement(notional).units == notional.units


class TestFloatRejection:
    @given(st.floats(allow_nan=False, allow_infinity=False, min_value=0, max_value=1))
    def test_no_float_ever_becomes_a_price(self, value):
        with pytest.raises(TypeError):
            Price.from_value(value)
