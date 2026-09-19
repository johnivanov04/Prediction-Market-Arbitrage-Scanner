"""Property tests for the fee mechanics.

The fragmentation laws are the reason Step 6 exists in its current shape, so
they are proven over generated inputs rather than demonstrated on examples. An
example test would not have found that the non-direct case has no useful bound.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from predarb.domain.enums import FeeType
from predarb.domain.fees import FeeConfiguration, FeeScope
from predarb.domain.money import Money, Price, Quantity
from predarb.venues.kalshi.fee_engine import (
    AccumulatorState,
    Fill,
    apply_fill,
    apply_fills,
    total_net_fee,
    total_trade_fee,
)
from predarb.venues.kalshi.fee_model import (
    BalancePrecision,
    net_fee_lower_bound,
    net_fee_upper_bound,
    raw_model_fee,
    trade_fee_from,
)

pytestmark = pytest.mark.property

SETTINGS = settings(max_examples=60)
DIRECT = BalancePrecision.direct_member()
NON_DIRECT = BalancePrecision.non_direct_member()

price_units = st.integers(min_value=1, max_value=9_999)
quantity_units = st.integers(min_value=1, max_value=100_000)
multipliers = st.sampled_from([Decimal(0), Decimal("0.5"), Decimal(1)])
precisions = st.sampled_from([DIRECT, NON_DIRECT])


def configuration(multiplier: Decimal = Decimal(1)) -> FeeConfiguration:
    return FeeConfiguration(
        fee_type_raw=FeeType.QUADRATIC.value,
        multiplier=multiplier,
        scope=FeeScope.SERIES,
        scope_ticker="KXTEST",
    )


@st.composite
def fills(draw: st.DrawFn, *, multiplier: Decimal | None = None) -> Fill:
    return Fill(
        price=Price.from_units(draw(price_units)),
        quantity=Quantity.from_units(draw(quantity_units)),
        configuration=configuration(multiplier if multiplier is not None else draw(multipliers)),
    )


class TestModelFee:
    @given(price=price_units, quantity=quantity_units, multiplier=multipliers)
    @SETTINGS
    def test_is_never_negative(self, price, quantity, multiplier):
        fee = raw_model_fee(
            price=Price.from_units(price),
            quantity=Quantity.from_units(quantity),
            configuration=configuration(multiplier),
        )
        assert fee >= 0

    @given(price=price_units, quantity=quantity_units)
    @SETTINGS
    def test_is_symmetric_about_one_half(self, price, quantity):
        """P(1-P) means a $0.30 yes and a $0.70 no cost the same to trade."""
        notional = Price.from_value("1.0000")
        low = Price.from_units(price)
        high = low.complement(notional)
        quantity_value = Quantity.from_units(quantity)
        assert raw_model_fee(
            price=low, quantity=quantity_value, configuration=configuration()
        ) == raw_model_fee(price=high, quantity=quantity_value, configuration=configuration())

    @given(price=price_units, first=quantity_units, second=quantity_units)
    @SETTINGS
    def test_is_exactly_additive_in_quantity(self, price, first, second):
        """The core fragmentation law, at the model level.

        Splitting a quantity across fills cannot change the total model fee,
        because the formula is linear in C at fixed P. Everything the estimator
        claims about fragmentation rests on this.
        """
        price_value = Price.from_units(price)
        config = configuration()

        def fee(units: int) -> Decimal:
            return raw_model_fee(
                price=price_value, quantity=Quantity.from_units(units), configuration=config
            )

        assert fee(first) + fee(second) == fee(first + second)

    @given(quantity=quantity_units)
    @SETTINGS
    def test_a_zero_multiplier_means_no_fee(self, quantity):
        """Observed live; a fee-free series must compute as free, not as cheap."""
        fee = raw_model_fee(
            price=Price.from_value("0.5000"),
            quantity=Quantity.from_units(quantity),
            configuration=configuration(Decimal(0)),
        )
        assert fee == 0


class TestTradeFee:
    @given(value=st.decimals(min_value=0, max_value=100, places=10))
    @SETTINGS
    def test_ceiling_never_undercharges(self, value):
        charged = trade_fee_from(value)
        assert Fraction(charged.units, 1_000_000) >= Fraction(value)

    @given(value=st.decimals(min_value=0, max_value=100, places=10))
    @SETTINGS
    def test_ceiling_overshoots_by_less_than_one_micro_dollar(self, value):
        charged = trade_fee_from(value)
        assert Fraction(charged.units, 1_000_000) - Fraction(value) < Fraction(1, 1_000_000)


class TestPerFillInvariants:
    @given(fill=fills(), precision=precisions)
    @SETTINGS
    def test_net_fee_is_never_negative(self, fill, precision):
        """The rebate cap exists to guarantee this; a negative fee would be
        free money and would read downstream as profit."""
        result = apply_fill(fill, AccumulatorState.new_order(), precision)
        assert result.net_fee.units >= 0

    @given(fill=fills(), precision=precisions)
    @SETTINGS
    def test_the_net_fee_identity_holds(self, fill, precision):
        result = apply_fill(fill, AccumulatorState.new_order(), precision)
        assert result.net_fee == result.trade_fee + result.rounding_fee - result.rebate

    @given(fill=fills(), precision=precisions)
    @SETTINGS
    def test_rounding_fee_is_under_one_precision_step(self, fill, precision):
        """Realignment moves to the nearest grid point below, never further."""
        result = apply_fill(fill, AccumulatorState.new_order(), precision)
        assert 0 <= result.rounding_fee.units < precision.step.units

    @given(fill=fills(), precision=precisions)
    @SETTINGS
    def test_the_balance_change_lands_on_the_precision_grid(self, fill, precision):
        """The whole point of the alignment step."""
        result = apply_fill(fill, AccumulatorState.new_order(), precision)
        assert result.aligned_change.units % precision.step.units == 0

    @given(fill=fills(), precision=precisions)
    @SETTINGS
    def test_trade_fee_is_at_least_the_model_fee(self, fill, precision):
        result = apply_fill(fill, AccumulatorState.new_order(), precision)
        assert Fraction(result.trade_fee.units, 1_000_000) >= Fraction(result.raw_model_fee)


class TestAccumulatorInvariants:
    @given(
        fill_list=st.lists(fills(multiplier=Decimal(1)), min_size=1, max_size=12),
        precision=precisions,
    )
    @SETTINGS
    def test_the_accumulator_never_goes_negative(self, fill_list, precision):
        results, final = apply_fills(fill_list, precision)
        assert all(r.accumulator_after.accumulated.units >= 0 for r in results)
        assert final.accumulated.units >= 0

    @given(
        fill_list=st.lists(fills(multiplier=Decimal(1)), min_size=1, max_size=12),
        precision=precisions,
    )
    @SETTINGS
    def test_rounding_is_conserved(self, fill_list, precision):
        """Every unit of rounding charged is either rebated or still held.

        If this can break, the venue either keeps rounding it should have
        returned or returns money it never took.
        """
        results, final = apply_fills(fill_list, precision)
        charged = sum(r.rounding_fee.units for r in results)
        rebated = sum(r.rebate.units for r in results)
        assert charged - rebated == final.accumulated.units

    @given(
        fill_list=st.lists(fills(multiplier=Decimal(1)), min_size=1, max_size=12),
        precision=precisions,
    )
    @SETTINGS
    def test_a_stranded_step_means_the_fill_could_not_afford_it(self, fill_list, precision):
        """The accumulator may hold a whole step, but only for one reason.

        The obvious invariant -- that carried rounding is always less than one
        step -- is **false**, and the exception is the mechanism behind the
        whole fragmentation finding. A rebate is capped by the fee it offsets,
        so a fill whose own fee is below one step returns nothing even when a
        full step is owed. Dust fills therefore strand rounding indefinitely.

        What does hold: whenever the accumulator is left holding a step or
        more, that fill's fee was too small to return it.
        """
        results, _ = apply_fills(fill_list, precision)
        step = precision.step.units
        for result in results:
            if result.accumulator_after.accumulated.units >= step:
                affordable = (result.trade_fee + result.rounding_fee).units
                assert affordable < step or result.rebate.units > 0

    @given(
        fill_list=st.lists(fills(multiplier=Decimal(1)), min_size=1, max_size=12),
        precision=precisions,
    )
    @SETTINGS
    def test_a_fill_that_can_afford_a_rebate_always_gets_one(self, fill_list, precision):
        """The converse: rounding is never withheld from a fill that can take it."""
        results, _ = apply_fills(fill_list, precision)
        step = precision.step.units
        for result in results:
            owed = result.accumulator_before.accumulated + result.rounding_fee
            affordable = (result.trade_fee + result.rounding_fee).units
            if owed.units >= step and affordable >= step:
                assert result.rebate.units >= step

    @given(
        fill_list=st.lists(fills(multiplier=Decimal(1)), min_size=1, max_size=12),
        precision=precisions,
    )
    @SETTINGS
    def test_net_total_is_at_least_the_trade_total(self, fill_list, precision):
        results, _ = apply_fills(fill_list, precision)
        assert total_net_fee(results).units >= total_trade_fee(results).units

    @given(
        fill_list=st.lists(fills(multiplier=Decimal(1)), min_size=2, max_size=8),
        precision=precisions,
    )
    @SETTINGS
    def test_splitting_an_order_in_two_never_costs_less(self, fill_list, precision):
        """Running fills as two orders resets the accumulator, which can only
        delay or lose a rebate -- never produce one earlier."""
        whole, _ = apply_fills(fill_list, precision)
        midpoint = len(fill_list) // 2
        first, _ = apply_fills(fill_list[:midpoint], precision)
        second, _ = apply_fills(fill_list[midpoint:], precision)
        assert (
            total_net_fee(first).units + total_net_fee(second).units >= total_net_fee(whole).units
        )


class TestTheIdentityBehindTheLowerBound:
    """``net_total == sum(trade_fee) + final_accumulator``.

    The whole lower-bound proof rests on this rearrangement, so it is checked
    directly rather than inferred.
    """

    @given(
        fill_list=st.lists(fills(multiplier=Decimal(1)), min_size=1, max_size=10),
        precision=precisions,
    )
    @SETTINGS
    def test_net_total_is_trade_total_plus_leftover_accumulator(self, fill_list, precision):
        results, final = apply_fills(fill_list, precision)
        assert total_net_fee(results) == total_trade_fee(results) + final.accumulated


class TestProvenBounds:
    """Every execution must land inside the interval the estimator publishes."""

    @given(
        price=price_units,
        quantity=st.integers(min_value=1, max_value=60),
        parts=st.integers(min_value=1, max_value=12),
        precision=precisions,
        multiplier=multipliers,
    )
    @SETTINGS
    def test_any_fragmentation_lies_within_the_bounds(
        self, price, quantity, parts, precision, multiplier
    ):
        assume(quantity % parts == 0)
        config = configuration(multiplier)
        price_value = Price.from_units(price)
        total = Quantity.from_units(quantity)
        pieces = [
            Fill(
                price=price_value,
                quantity=Quantity.from_units(quantity // parts),
                configuration=config,
            )
            for _ in range(parts)
        ]
        model_fee = raw_model_fee(price=price_value, quantity=total, configuration=config)
        lower = net_fee_lower_bound(model_fee)
        upper = net_fee_upper_bound(
            total_raw_model_fee=model_fee, quantity=total, precision=precision
        )
        results, _ = apply_fills(pieces, precision)
        assert lower <= total_net_fee(results) <= upper

    @given(
        price=price_units,
        quantity=st.integers(min_value=1, max_value=60),
        parts=st.integers(min_value=1, max_value=12),
        precision=precisions,
    )
    @SETTINGS
    def test_maximum_fragmentation_lies_within_the_bounds(self, price, quantity, parts, precision):
        """The worst case the granularity permits: every fill one increment."""
        del parts
        config = configuration()
        price_value = Price.from_units(price)
        total = Quantity.from_units(quantity)
        dust = [
            Fill(price=price_value, quantity=Quantity.from_units(1), configuration=config)
            for _ in range(quantity)
        ]
        model_fee = raw_model_fee(price=price_value, quantity=total, configuration=config)
        upper = net_fee_upper_bound(
            total_raw_model_fee=model_fee, quantity=total, precision=precision
        )
        results, _ = apply_fills(dust, precision)
        assert net_fee_lower_bound(model_fee) <= total_net_fee(results) <= upper

    @given(price=price_units, quantity=st.integers(min_value=1, max_value=80))
    @SETTINGS
    def test_the_conservative_bound_covers_the_other_member_class(self, price, quantity):
        """A bound built on the coarser grid holds for a direct member too."""
        config = configuration()
        price_value = Price.from_units(price)
        total = Quantity.from_units(quantity)
        model_fee = raw_model_fee(price=price_value, quantity=total, configuration=config)
        conservative = net_fee_upper_bound(
            total_raw_model_fee=model_fee,
            quantity=total,
            precision=BalancePrecision.unknown_member(),
        )
        for actual in (DIRECT, NON_DIRECT):
            dust = [
                Fill(price=price_value, quantity=Quantity.from_units(1), configuration=config)
                for _ in range(quantity)
            ]
            results, _ = apply_fills(dust, actual)
            assert total_net_fee(results) <= conservative

    @given(
        price=price_units,
        quantity=st.integers(min_value=2, max_value=200),
        parts=st.integers(2, 10),
    )
    @SETTINGS
    def test_total_trade_fee_overshoot_is_bounded_by_the_fill_count(self, price, quantity, parts):
        """ceil(F) <= sum(ceil(f_i)) <= ceil(F) + (k-1) micro-dollars."""
        assume(quantity % parts == 0)
        config = configuration()
        price_value = Price.from_units(price)
        whole = Fill(
            price=price_value, quantity=Quantity.from_units(quantity), configuration=config
        )
        pieces = [
            Fill(
                price=price_value,
                quantity=Quantity.from_units(quantity // parts),
                configuration=config,
            )
            for _ in range(parts)
        ]
        single, _ = apply_fills([whole], DIRECT)
        split, _ = apply_fills(pieces, DIRECT)
        overshoot = total_trade_fee(split).units - total_trade_fee(single).units
        assert 0 <= overshoot <= parts - 1

    @given(
        price=price_units,
        quantity=st.integers(min_value=2, max_value=200),
        parts=st.integers(2, 10),
    )
    @SETTINGS
    def test_the_raw_model_fee_is_wholly_unaffected(self, price, quantity, parts):
        assume(quantity % parts == 0)
        config = configuration()
        price_value = Price.from_units(price)
        single = raw_model_fee(
            price=price_value, quantity=Quantity.from_units(quantity), configuration=config
        )
        split = sum(
            raw_model_fee(
                price=price_value,
                quantity=Quantity.from_units(quantity // parts),
                configuration=config,
            )
            for _ in range(parts)
        )
        assert single == split


def _compositions(n: int) -> tuple[tuple[int, ...], ...]:
    """Every ordered way to split n increments into positive fills."""
    if n == 0:
        return ((),)
    return tuple((first, *rest) for first in range(1, n + 1) for rest in _compositions(n - first))


class TestExhaustiveSmallDomain:
    """Enumerate *every* legal segmentation, not a sample of them.

    Hypothesis searches; this proves-by-exhaustion over a small domain. For
    quantities up to 0.08 contracts there are at most 128 orderings, so every
    one can be checked.
    """

    @pytest.mark.parametrize("quantity_units", [1, 2, 3, 5, 8])
    @pytest.mark.parametrize("price_units_value", [1, 2, 37, 1234, 5000, 6789, 9999], ids=str)
    @pytest.mark.parametrize("precision", [DIRECT, NON_DIRECT], ids=["direct", "non-direct"])
    def test_every_partition_lands_inside_the_bounds(
        self, quantity_units, price_units_value, precision
    ):
        config = configuration()
        price_value = Price.from_units(price_units_value)
        total = Quantity.from_units(quantity_units)
        model_fee = raw_model_fee(price=price_value, quantity=total, configuration=config)
        lower = net_fee_lower_bound(model_fee)
        upper = net_fee_upper_bound(
            total_raw_model_fee=model_fee, quantity=total, precision=precision
        )
        seen = 0
        for parts in _compositions(quantity_units):
            fill_list = [
                Fill(price=price_value, quantity=Quantity.from_units(c), configuration=config)
                for c in parts
            ]
            results, _ = apply_fills(fill_list, precision)
            net = total_net_fee(results)
            assert lower <= net <= upper, f"{parts} gave {net}, outside [{lower}, {upper}]"
            seen += 1
        assert seen == 2 ** (quantity_units - 1)

    @pytest.mark.parametrize("quantity_units", [2, 3, 5, 8])
    @pytest.mark.parametrize("price_units_value", [1, 37, 1234, 6789, 9999], ids=str)
    @pytest.mark.parametrize("precision", [DIRECT, NON_DIRECT], ids=["direct", "non-direct"])
    def test_one_fill_is_never_beaten_at_a_single_price(
        self, quantity_units, price_units_value, precision
    ):
        """PROVEN for a single price; this checks the proof exhaustively.

        Proof sketch (``docs/fees.md`` §3.5): with ``U_i = gross_i + trade_i``,
        ``net = sum ceil_B(U_i) - gross - rebates``. The difference between a
        split and a single fill is ``D - R`` where ``D`` is a non-negative
        multiple of ``B`` and ``R``, also a multiple of ``B``, is strictly less
        than ``D + B``. Hence ``R <= D`` and the difference is non-negative.
        """
        config = configuration()
        price_value = Price.from_units(price_units_value)
        single, _ = apply_fills(
            [
                Fill(
                    price=price_value,
                    quantity=Quantity.from_units(quantity_units),
                    configuration=config,
                )
            ],
            precision,
        )
        baseline = total_net_fee(single)
        for parts in _compositions(quantity_units):
            fill_list = [
                Fill(price=price_value, quantity=Quantity.from_units(c), configuration=config)
                for c in parts
            ]
            results, _ = apply_fills(fill_list, precision)
            assert total_net_fee(results) >= baseline, f"{parts} beat the single fill"


class TestFragmentationIsNotInvariant:
    """Neither member class is immune. Withdrawn claim, kept as a counterexample."""

    def test_direct_member_net_fee_changes_with_fragmentation(self):
        """0.02 contracts at $0.0001: one fill costs 98 micro-dollars, two cost 198.

        This is why the direct-member result is reported as "equal in the tested
        examples" rather than as an invariance.
        """
        config = configuration()
        price_value = Price.from_units(1)
        single, _ = apply_fills(
            [Fill(price=price_value, quantity=Quantity.from_units(2), configuration=config)],
            DIRECT,
        )
        split, _ = apply_fills(
            [
                Fill(price=price_value, quantity=Quantity.from_units(1), configuration=config)
                for _ in range(2)
            ],
            DIRECT,
        )
        assert total_net_fee(single) == Money.from_units(98)
        assert total_net_fee(split) == Money.from_units(198)

    def test_non_direct_member_net_fee_changes_with_fragmentation(self):
        config = configuration()
        price_value = Price.from_units(6789)
        single, _ = apply_fills(
            [Fill(price=price_value, quantity=Quantity.from_units(300), configuration=config)],
            NON_DIRECT,
        )
        split, _ = apply_fills(
            [
                Fill(price=price_value, quantity=Quantity.from_units(3), configuration=config)
                for _ in range(100)
            ],
            NON_DIRECT,
        )
        assert total_net_fee(single) == Money.from_value("0.053300")
        assert total_net_fee(split) == Money.from_value("0.963300")


class TestMemberClassOrdering:
    @given(fill=fills(multiplier=Decimal(1)))
    @SETTINGS
    def test_a_direct_member_never_pays_more(self, fill):
        """A finer balance grid cannot round against you harder."""
        direct = apply_fill(fill, AccumulatorState.new_order(), DIRECT)
        non_direct = apply_fill(fill, AccumulatorState.new_order(), NON_DIRECT)
        assert direct.net_fee.units <= non_direct.net_fee.units

    @given(fill=fills(multiplier=Decimal(1)))
    @SETTINGS
    def test_the_trade_fee_itself_does_not_depend_on_member_class(self, fill):
        direct = apply_fill(fill, AccumulatorState.new_order(), DIRECT)
        non_direct = apply_fill(fill, AccumulatorState.new_order(), NON_DIRECT)
        assert direct.trade_fee == non_direct.trade_fee
        assert direct.raw_model_fee == non_direct.raw_model_fee


class TestNoMoneyIsFloat:
    @given(fill=fills(), precision=precisions)
    @SETTINGS
    def test_every_monetary_output_is_exact(self, fill, precision):
        """A float anywhere in this chain is the failure the project forbids."""
        result = apply_fill(fill, AccumulatorState.new_order(), precision)
        for value in (
            result.signed_revenue,
            result.trade_fee,
            result.aligned_change,
            result.rounding_fee,
            result.rebate,
            result.net_fee,
            result.balance_change,
        ):
            assert isinstance(value, Money)
            assert isinstance(value.units, int)
        assert isinstance(result.raw_model_fee, Decimal)
