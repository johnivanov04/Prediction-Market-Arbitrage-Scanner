"""Tests for the taker fee formula and the balance-precision grid.

The formula constant itself is a reviewer-supplied document finding (A-14), so
these tests pin the *mechanics* and the refusals, not the provenance of 0.07.
"""

from __future__ import annotations

import re
from decimal import Decimal

import pytest

from predarb.domain.enums import FeeType, Liquidity
from predarb.domain.fees import FeeConfiguration, FeeScope
from predarb.domain.money import Money, Price, Quantity
from predarb.venues.kalshi.fee_model import (
    MIN_QUANTITY_INCREMENT,
    QUADRATIC_TAKER_RATE,
    SUPPORTED_TAKER_FEE_TYPES,
    TRADE_FEE_STEP,
    BalancePrecision,
    UnsupportedFeeTypeError,
    max_fill_count,
    max_rounding_fee_per_fill,
    net_fee_bounds,
    net_fee_lower_bound,
    net_fee_upper_bound,
    raw_model_fee,
    signed_revenue_for_buy,
    taker_rate_for,
    trade_fee_from,
)

pytestmark = pytest.mark.unit


def config(fee_type: str = FeeType.QUADRATIC.value, multiplier: str = "1") -> FeeConfiguration:
    return FeeConfiguration(
        fee_type_raw=fee_type,
        multiplier=Decimal(multiplier),
        scope=FeeScope.SERIES,
        scope_ticker="KXTEST",
    )


class TestRawModelFee:
    def test_matches_the_documented_worked_example(self):
        """1 contract at $0.55: 0.07 * 1 * 0.55 * 0.45 = $0.0173250."""
        fee = raw_model_fee(
            price=Price.from_value("0.5500"),
            quantity=Quantity.from_value("1.00"),
            configuration=config(),
        )
        assert fee == Decimal("0.0173250")

    def test_keeps_precision_beyond_six_places(self):
        """The documented example carries eight places before the ceiling step.

        Rounding here would pre-empt ceil_6dp and change the charged fee.
        """
        fee = raw_model_fee(
            price=Price.from_value("0.0900"),
            quantity=Quantity.from_value("0.50"),
            configuration=config(),
        )
        assert fee == Decimal("0.00286650")
        exponent = fee.as_tuple().exponent
        assert isinstance(exponent, int)
        assert -exponent > 6

    def test_is_symmetric_about_one_half(self):
        """P(1-P) is symmetric, so yes at P costs the same as no at 1-P."""
        at_low = raw_model_fee(
            price=Price.from_value("0.3000"),
            quantity=Quantity.from_value("1.00"),
            configuration=config(),
        )
        at_high = raw_model_fee(
            price=Price.from_value("0.7000"),
            quantity=Quantity.from_value("1.00"),
            configuration=config(),
        )
        assert at_low == at_high

    def test_is_linear_in_quantity(self):
        """The property the whole fragmentation analysis rests on."""
        one = raw_model_fee(
            price=Price.from_value("0.1234"),
            quantity=Quantity.from_value("1.00"),
            configuration=config(),
        )
        ten = raw_model_fee(
            price=Price.from_value("0.1234"),
            quantity=Quantity.from_value("10.00"),
            configuration=config(),
        )
        assert ten == one * 10

    @pytest.mark.parametrize("multiplier", ["0", "0.5", "1"])
    def test_multiplier_scales_the_fee(self, multiplier):
        """0, 0.5 and 1 are all observed live; a zero multiplier means free."""
        fee = raw_model_fee(
            price=Price.from_value("0.5000"),
            quantity=Quantity.from_value("2.00"),
            configuration=config(multiplier=multiplier),
        )
        assert fee == Decimal(multiplier) * Decimal("0.07") * 2 * Decimal("0.25")

    def test_is_zero_at_the_price_extremes(self):
        for price in ("0.0000", "1.0000"):
            fee = raw_model_fee(
                price=Price.from_value(price),
                quantity=Quantity.from_value("100.00"),
                configuration=config(),
            )
            assert fee == 0


class TestFailingClosed:
    """An unknown fee type must never fall back to the quadratic formula."""

    def test_unrecognised_fee_type_raises(self):
        with pytest.raises(UnsupportedFeeTypeError, match="not in the documented enum"):
            taker_rate_for(config(fee_type="margin_market_maker_program_fees"))

    @pytest.mark.parametrize(
        "fee_type",
        [FeeType.FLAT, FeeType.QUADRATIC_WITH_MAKER_FEES, FeeType.QUADRATIC_WITH_COMBO_MAKER_FEES],
    )
    def test_documented_but_unspecified_types_raise(self, fee_type):
        """Named in the enum, but with no taker rate we can point to."""
        assert fee_type not in SUPPORTED_TAKER_FEE_TYPES
        with pytest.raises(UnsupportedFeeTypeError, match="no established taker formula"):
            taker_rate_for(config(fee_type=fee_type.value))

    def test_only_quadratic_is_supported(self):
        assert set(SUPPORTED_TAKER_FEE_TYPES) == {FeeType.QUADRATIC}
        assert taker_rate_for(config()) == QUADRATIC_TAKER_RATE

    def test_maker_liquidity_is_refused(self):
        with pytest.raises(UnsupportedFeeTypeError, match="only taker fees"):
            raw_model_fee(
                price=Price.from_value("0.5000"),
                quantity=Quantity.from_value("1.00"),
                configuration=config(),
                liquidity=Liquidity.MAKER,
            )

    def test_non_dollar_notional_is_refused(self):
        """(1 - P) is written against a $1 payout; generalising it is a guess."""
        with pytest.raises(UnsupportedFeeTypeError, match=re.escape("not $1.0000")):
            raw_model_fee(
                price=Price.from_value("0.5000"),
                quantity=Quantity.from_value("1.00"),
                configuration=config(),
                notional=Price.from_value("0.5000"),
            )

    def test_explicit_dollar_notional_is_accepted(self):
        fee = raw_model_fee(
            price=Price.from_value("0.5000"),
            quantity=Quantity.from_value("1.00"),
            configuration=config(),
            notional=Price.from_value("1.0000"),
        )
        assert fee == Decimal("0.0175")

    def test_float_multiplier_is_rejected_at_construction(self):
        with pytest.raises(TypeError, match="must not be a float"):
            FeeConfiguration(
                fee_type_raw=FeeType.QUADRATIC.value,
                multiplier=0.5,  # type: ignore[arg-type]
                scope=FeeScope.SERIES,
                scope_ticker="KXTEST",
            )


class TestTradeFee:
    def test_documented_ceiling_example(self):
        """ceil_6dp($0.00363825) = $0.003639."""
        assert trade_fee_from(Decimal("0.00363825")) == Money.from_value("0.003639")

    def test_rounds_up_never_down(self):
        assert trade_fee_from(Decimal("0.0000001")) == Money.from_value("0.000001")

    def test_exact_six_place_value_is_unchanged(self):
        assert trade_fee_from(Decimal("0.017325")) == Money.from_value("0.017325")

    def test_zero_stays_zero(self):
        assert trade_fee_from(Decimal(0)) == Money.zero()


class TestBalancePrecision:
    def test_documented_member_grids(self):
        assert BalancePrecision.direct_member().step == Money.from_value("0.000100")
        assert BalancePrecision.non_direct_member().step == Money.from_value("0.010000")

    def test_floors_a_negative_change_away_from_zero(self):
        """floor_cent(-$0.058639) = -$0.060000, not -$0.050000."""
        precision = BalancePrecision.non_direct_member()
        assert precision.floor(Money.from_value("-0.058639")) == Money.from_value("-0.060000")

    def test_floors_a_positive_change_toward_zero(self):
        precision = BalancePrecision.non_direct_member()
        assert precision.floor(Money.from_value("0.058639")) == Money.from_value("0.050000")

    def test_a_value_already_on_the_grid_is_unchanged(self):
        precision = BalancePrecision.non_direct_member()
        for value in ("-0.060000", "0.000000", "1.230000"):
            assert precision.floor(Money.from_value(value)) == Money.from_value(value)

    def test_direct_member_grid_is_finer(self):
        """The same change rounds less against a direct member."""
        amount = Money.from_value("-0.058639")
        direct = BalancePrecision.direct_member().floor(amount)
        non_direct = BalancePrecision.non_direct_member().floor(amount)
        assert direct == Money.from_value("-0.058700")
        assert direct > non_direct

    def test_a_non_positive_step_is_rejected(self):
        for step in ("0.000000", "-0.010000"):
            with pytest.raises(ValueError, match="must be positive"):
                BalancePrecision(step=Money.from_value(step))

    def test_there_is_no_default_member_class(self):
        """Guessing would make half of all users' fees silently wrong."""
        with pytest.raises(TypeError):
            BalancePrecision()  # type: ignore[call-arg]


class TestSignedRevenue:
    def test_a_buy_is_negative(self):
        assert signed_revenue_for_buy(Money.from_value("0.055000")) == Money.from_value("-0.055000")

    def test_negative_gross_cost_is_rejected(self):
        with pytest.raises(ValueError, match="must not be negative"):
            signed_revenue_for_buy(Money.from_value("-0.010000"))


class TestMaxFillCount:
    """Fill fragmentation is finite because quantity granularity is documented."""

    @pytest.mark.parametrize(
        ("quantity", "expected"),
        [
            ("0.00", 0),
            ("0.01", 1),
            ("0.02", 2),
            ("0.50", 50),
            ("1.00", 100),
            ("3.00", 300),
            ("7.35", 735),
            ("100.00", 10_000),
        ],
    )
    def test_counts_minimum_increments(self, quantity, expected):
        assert max_fill_count(Quantity.from_value(quantity)) == expected

    def test_the_increment_is_the_documented_one(self):
        assert Quantity.from_value("0.01") == MIN_QUANTITY_INCREMENT

    def test_a_fractional_quantity_is_exact(self):
        """0.07 contracts is seven fills at most, not six or eight."""
        assert max_fill_count(Quantity.from_value("0.07")) == 7

    def test_zero_quantity_permits_no_fills(self):
        assert max_fill_count(Quantity.zero()) == 0

    def test_it_equals_quantity_over_the_increment(self):
        for value in ("0.01", "0.33", "2.50", "19.99"):
            quantity = Quantity.from_value(value)
            expected = quantity.as_decimal() / MIN_QUANTITY_INCREMENT.as_decimal()
            assert max_fill_count(quantity) == expected


class TestMaxRoundingFeePerFill:
    """One fill can lose at most one balance step, less one micro-dollar."""

    def test_direct_member(self):
        assert max_rounding_fee_per_fill(BalancePrecision.direct_member()) == Money.from_value(
            "0.000099"
        )

    def test_non_direct_member(self):
        assert max_rounding_fee_per_fill(BalancePrecision.non_direct_member()) == Money.from_value(
            "0.009999"
        )

    def test_it_is_one_micro_dollar_below_the_step(self):
        for precision in (BalancePrecision.direct_member(), BalancePrecision.non_direct_member()):
            assert max_rounding_fee_per_fill(precision) == precision.step - TRADE_FEE_STEP


class TestNetFeeBounds:
    """The proven interval a total net fee must lie in."""

    def test_lower_bound_is_the_ceiling_of_the_model_fee(self):
        assert net_fee_lower_bound(Decimal("0.00363825")) == Money.from_value("0.003639")

    def test_lower_bound_of_zero_is_zero(self):
        assert net_fee_lower_bound(Decimal(0)) == Money.zero()

    def test_upper_bound_formula(self):
        """ceil_6dp(F) + k_max*B - one micro-dollar."""
        bound = net_fee_upper_bound(
            total_raw_model_fee=Decimal("0.00363825"),
            quantity=Quantity.from_value("1.00"),
            precision=BalancePrecision.non_direct_member(),
        )
        assert bound == Money.from_value("0.003639") + Money.from_value(
            "1.000000"
        ) - Money.from_units(1)

    def test_upper_bound_with_a_single_permitted_fill(self):
        """At Q = 0.01 the only possible execution is one fill."""
        bound = net_fee_upper_bound(
            total_raw_model_fee=Decimal("0.001"),
            quantity=Quantity.from_value("0.01"),
            precision=BalancePrecision.direct_member(),
        )
        assert bound == Money.from_value("0.001000") + Money.from_value("0.000099")

    def test_zero_quantity_bounds_to_zero(self):
        assert (
            net_fee_upper_bound(
                total_raw_model_fee=Decimal(0),
                quantity=Quantity.zero(),
                precision=BalancePrecision.non_direct_member(),
            )
            == Money.zero()
        )

    def test_a_coarser_grid_gives_a_wider_bound(self):
        fee, quantity = Decimal("0.01"), Quantity.from_value("1.00")
        direct = net_fee_upper_bound(
            total_raw_model_fee=fee,
            quantity=quantity,
            precision=BalancePrecision.direct_member(),
        )
        non_direct = net_fee_upper_bound(
            total_raw_model_fee=fee,
            quantity=quantity,
            precision=BalancePrecision.non_direct_member(),
        )
        assert non_direct > direct

    def test_the_unknown_member_bound_covers_both_classes(self):
        """Safe direction: over-states for a direct member, never under-states."""
        fee, quantity = Decimal("0.01"), Quantity.from_value("2.00")
        unknown = net_fee_upper_bound(
            total_raw_model_fee=fee,
            quantity=quantity,
            precision=BalancePrecision.unknown_member(),
        )
        for precision in (BalancePrecision.direct_member(), BalancePrecision.non_direct_member()):
            assert (
                net_fee_upper_bound(total_raw_model_fee=fee, quantity=quantity, precision=precision)
                <= unknown
            )

    def test_bounds_are_ordered(self):
        for quantity in ("0.01", "1.00", "25.00"):
            lower = net_fee_lower_bound(Decimal("0.02"))
            upper = net_fee_upper_bound(
                total_raw_model_fee=Decimal("0.02"),
                quantity=Quantity.from_value(quantity),
                precision=BalancePrecision.non_direct_member(),
            )
            assert lower <= upper

    def test_a_negative_quantity_cannot_reach_the_bound(self):
        """Quantity refuses negatives, so the count is non-negative by type."""
        with pytest.raises(ValueError, match="may not be negative"):
            Quantity.from_units(-1)


_ZERO_QUANTITY_PRECISIONS = [
    pytest.param(BalancePrecision.direct_member(), id="direct"),
    pytest.param(BalancePrecision.non_direct_member(), id="non-direct"),
    pytest.param(BalancePrecision.unknown_member(), id="unknown-conservative"),
]


class TestZeroQuantityBounds:
    """An order that acquires nothing costs exactly nothing.

    Not "approximately nothing", and not the positive-quantity formula
    evaluated at k_max = 0 -- that would give ceil_6dp(F) - one micro-dollar,
    a negative bound. Every one of the five fee quantities is zero here.
    """

    @pytest.mark.parametrize("precision", _ZERO_QUANTITY_PRECISIONS)
    def test_both_bounds_are_exactly_zero(self, precision):
        lower, upper = net_fee_bounds(
            total_raw_model_fee=Decimal(0), quantity=Quantity.zero(), precision=precision
        )
        assert lower == Money.zero()
        assert upper == Money.zero()

    @pytest.mark.parametrize("precision", _ZERO_QUANTITY_PRECISIONS)
    def test_the_upper_bound_is_not_negative(self, precision):
        """The formula's trailing -1 micro-dollar must not leak through."""
        upper = net_fee_upper_bound(
            total_raw_model_fee=Decimal(0), quantity=Quantity.zero(), precision=precision
        )
        assert upper == Money.zero()
        assert upper.units >= 0

    @pytest.mark.parametrize("multiplier", ["0", "1"])
    @pytest.mark.parametrize("precision", _ZERO_QUANTITY_PRECISIONS)
    def test_zero_quantity_at_any_multiplier(self, multiplier, precision):
        """The multiplier cannot matter: the model fee is linear in quantity."""
        model_fee = raw_model_fee(
            price=Price.from_value("0.5000"),
            quantity=Quantity.zero(),
            configuration=config(multiplier=multiplier),
        )
        assert model_fee == 0
        assert net_fee_bounds(
            total_raw_model_fee=model_fee, quantity=Quantity.zero(), precision=precision
        ) == (Money.zero(), Money.zero())

    def test_no_fills_are_permitted(self):
        assert max_fill_count(Quantity.zero()) == 0

    def test_a_fee_paired_with_zero_quantity_is_refused(self):
        """Contradictory input: a linear-in-quantity fee cannot be non-zero here."""
        with pytest.raises(ValueError, match="zero quantity carries a non-zero raw model fee"):
            net_fee_bounds(
                total_raw_model_fee=Decimal("0.01"),
                quantity=Quantity.zero(),
                precision=BalancePrecision.non_direct_member(),
            )

    def test_the_smallest_positive_quantity_still_uses_the_formula(self):
        """The zero case is a boundary, not a range: 0.01 behaves normally."""
        lower, upper = net_fee_bounds(
            total_raw_model_fee=Decimal("0.001"),
            quantity=Quantity.from_value("0.01"),
            precision=BalancePrecision.direct_member(),
        )
        assert lower == Money.from_value("0.001000")
        assert upper == Money.from_value("0.001099")
        assert upper > lower
