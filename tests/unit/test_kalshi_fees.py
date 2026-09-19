"""Tests for the pre-execution fee estimator.

The point of this layer is that it must never overstate what it knows. Many of
these tests assert on the *classification* rather than on a number, because the
classification is what a later arbitrage detector has to obey.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from predarb.books.execution import ExecutionQuote, FillSlice
from predarb.books.liquidity import BookLiquidityId
from predarb.books.state import BookProvenance
from predarb.domain.average_price import AveragePrice
from predarb.domain.enums import FeeType, MarketSide
from predarb.domain.fees import FeeConfiguration, FeeScope, ResolvedFeeConfiguration
from predarb.domain.money import Money, Price, Quantity
from predarb.venues.kalshi import fees as module_under_test
from predarb.venues.kalshi.fee_engine import Fill
from predarb.venues.kalshi.fee_model import BalancePrecision
from predarb.venues.kalshi.fees import (
    Exactness,
    FeeQuote,
    MultiplierStatus,
    SegmentationAssumption,
    UnavailableFee,
    estimate_fees,
    estimate_multi_leg_fees,
)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
NON_DIRECT = BalancePrecision.non_direct_member()
DIRECT = BalancePrecision.direct_member()
UNKNOWN_PRECISION = BalancePrecision.unknown_member()


def config(multiplier: str = "1", fee_type: str = FeeType.QUADRATIC.value) -> FeeConfiguration:
    return FeeConfiguration(
        fee_type_raw=fee_type,
        multiplier=Decimal(multiplier),
        scope=FeeScope.SERIES,
        scope_ticker="KXTEST",
    )


def resolved(
    multiplier: str = "1", fee_type: str = FeeType.QUADRATIC.value
) -> ResolvedFeeConfiguration:
    return ResolvedFeeConfiguration(
        configuration=config(multiplier, fee_type),
        effective_from=None,
        provenance="series base configuration on KXTEST",
    )


RESOLVED = resolved()
UNKNOWN_TYPE = resolved(fee_type="margin_market_maker_program_fees")


def slice_at(price: str, quantity: str) -> FillSlice:
    p, q = Price.from_value(price), Quantity.from_value(quantity)
    return FillSlice(
        quantity=q,
        execution_price=p,
        gross_cost=p * q,
        liquidity=BookLiquidityId(
            connection_epoch=1,
            sid=1,
            market_ticker="M",
            source_outcome=MarketSide.NO,
            source_price=Price.from_value("0.5000"),
        ),
        source_outcome=MarketSide.NO,
        source_bid_price=Price.from_value("0.5000"),
    )


def quote_of(*slices: FillSlice, ticker: str = "M") -> ExecutionQuote:
    filled = Quantity.from_units(sum(s.quantity.units for s in slices))
    cost = Money.from_units(sum(s.gross_cost.units for s in slices))
    return ExecutionQuote(
        market_ticker=ticker,
        acquired_outcome=MarketSide.YES,
        requested_quantity=filled,
        filled_quantity=filled,
        unfilled_quantity=Quantity.zero(),
        fully_fillable=True,
        slices=slices,
        gross_cost=cost,
        vwap=AveragePrice(total_cost=cost, total_quantity=filled),
        best_execution_price=slices[0].execution_price,
        worst_execution_price=slices[-1].execution_price,
        connection_epoch=1,
        sid=1,
        book_seq=42,
        provenance=BookProvenance(connection_epoch=1, sid=1),
        quoted_at=T0,
    )


def as_quote(estimate: object) -> FeeQuote:
    """Narrow to a FeeQuote, asserting availability."""
    assert isinstance(estimate, FeeQuote), f"expected a FeeQuote, got {estimate!r}"
    return estimate


def fills_of(price: str, each: str, count: int) -> tuple[Fill, ...]:
    return tuple(
        Fill(
            price=Price.from_value(price),
            quantity=Quantity.from_value(each),
            configuration=config(),
        )
        for _ in range(count)
    )


class TestBoundedEstimate:
    """A quote built from L2 levels is inexact but bounded on both sides."""

    @pytest.fixture
    def estimate(self) -> FeeQuote:
        return as_quote(
            estimate_fees(
                quote_of(slice_at("0.1234", "3.00"), slice_at("0.4321", "2.00")),
                RESOLVED,
                NON_DIRECT,
            )
        )

    def test_is_classified_bounded_not_exact(self, estimate):
        assert estimate.exactness is Exactness.BOUNDED_ESTIMATE
        assert not estimate.is_exact

    def test_records_the_segmentation_it_assumed(self, estimate):
        assert estimate.segmentation is SegmentationAssumption.ONE_FILL_PER_PRICE_LEVEL

    def test_warns_that_a_level_is_not_a_fill(self, estimate):
        assert any("aggregate depth" in w for w in estimate.warnings)

    def test_bounds_are_ordered_and_contain_the_estimate(self, estimate):
        assert estimate.lower_bound_net_fee <= estimate.estimated_net_fee
        assert estimate.estimated_net_fee <= estimate.upper_bound_net_fee

    def test_lower_bound_is_the_ceiling_of_the_model_fee(self, estimate):
        assert estimate.lower_bound_net_fee == Money.ceil_from_decimal(estimate.raw_model_fee)

    def test_upper_bound_uses_the_documented_granularity_ceiling(self, estimate):
        """5 contracts at $0.01 granularity permits at most 500 fills."""
        assert estimate.max_fill_count == 500
        expected = Money.from_units(
            estimate.lower_bound_net_fee.units + 500 * NON_DIRECT.step.units - 1
        )
        assert estimate.upper_bound_net_fee == expected

    def test_raw_model_fee_is_stated_exactly(self, estimate):
        expected = Decimal("0.07") * (
            3 * Decimal("0.1234") * Decimal("0.8766") + 2 * Decimal("0.4321") * Decimal("0.5679")
        )
        assert estimate.raw_model_fee == expected

    def test_supports_an_arbitrage_claim_at_multiplier_one(self, estimate):
        assert estimate.supports_arbitrage_claim is True

    def test_the_components_reconcile(self, estimate):
        assert estimate.estimated_net_fee == (
            estimate.estimated_trade_fee
            + estimate.estimated_rounding_fee
            - estimate.estimated_rebate
        )


class TestBoundContainsEveryFragmentation:
    """The bound must hold for segmentations we did not assume."""

    @pytest.mark.parametrize("precision", [DIRECT, NON_DIRECT], ids=["direct", "non-direct"])
    @pytest.mark.parametrize(
        ("price", "total", "each", "count"),
        [
            ("0.6789", "3.00", "0.03", 100),
            ("0.0003", "1.00", "0.01", 100),
            ("0.1234", "0.50", "0.01", 50),
            ("0.9999", "2.00", "0.02", 100),
        ],
    )
    def test_heavy_fragmentation_stays_within_the_bound(self, precision, price, total, each, count):
        quote = quote_of(slice_at(price, total))
        estimated = as_quote(estimate_fees(quote, RESOLVED, precision))
        actual = as_quote(
            estimate_fees(quote, RESOLVED, precision, actual_fills=fills_of(price, each, count))
        )
        assert actual.estimated_net_fee <= estimated.upper_bound_net_fee
        assert actual.estimated_net_fee >= estimated.lower_bound_net_fee

    def test_one_fill_execution_stays_within_the_bound(self):
        quote = quote_of(slice_at("0.6789", "3.00"))
        estimated = as_quote(estimate_fees(quote, RESOLVED, NON_DIRECT))
        single = as_quote(
            estimate_fees(quote, RESOLVED, NON_DIRECT, actual_fills=fills_of("0.6789", "3.00", 1))
        )
        assert (
            estimated.lower_bound_net_fee
            <= single.estimated_net_fee
            <= estimated.upper_bound_net_fee
        )

    def test_a_zero_multiplier_is_bounded_too(self):
        """A free series still incurs balance rounding, so the bound is not zero."""
        free = resolved(multiplier="0")
        estimate = as_quote(estimate_fees(quote_of(slice_at("0.3333", "1.00")), free, NON_DIRECT))
        assert estimate.raw_model_fee == 0
        assert estimate.lower_bound_net_fee == Money.zero()
        assert estimate.upper_bound_net_fee > Money.zero()
        assert estimate.estimated_net_fee > Money.zero()

    def test_sub_cent_prices_are_bounded(self):
        estimate = as_quote(estimate_fees(quote_of(slice_at("0.0001", "0.01")), RESOLVED, DIRECT))
        assert estimate.max_fill_count == 1
        assert estimate.lower_bound_net_fee <= estimate.estimated_net_fee
        assert estimate.estimated_net_fee <= estimate.upper_bound_net_fee


class TestActualFillsAreExact:
    def test_known_segmentation_collapses_the_bounds(self):
        quote = quote_of(slice_at("0.1234", "3.00"), slice_at("0.4321", "2.00"))
        fills = tuple(
            Fill(price=s.execution_price, quantity=s.quantity, configuration=config())
            for s in quote.slices
        )
        estimate = as_quote(estimate_fees(quote, RESOLVED, NON_DIRECT, actual_fills=fills))
        assert estimate.exactness is Exactness.EXACT_ACTUAL_FILLS
        assert estimate.is_exact
        assert estimate.segmentation is SegmentationAssumption.ACTUAL_FILLS
        assert estimate.lower_bound_net_fee == estimate.upper_bound_net_fee
        assert estimate.bound_width == Money.zero()

    def test_exact_result_carries_no_segmentation_warning(self):
        quote = quote_of(slice_at("0.1234", "3.00"))
        estimate = as_quote(
            estimate_fees(quote, RESOLVED, NON_DIRECT, actual_fills=fills_of("0.1234", "3.00", 1))
        )
        assert estimate.warnings == ()

    def test_more_fragmentation_costs_more(self):
        quote = quote_of(slice_at("0.1234", "3.00"))
        coarse = as_quote(estimate_fees(quote, RESOLVED, NON_DIRECT))
        fine = as_quote(
            estimate_fees(quote, RESOLVED, NON_DIRECT, actual_fills=fills_of("0.1234", "0.10", 30))
        )
        assert fine.estimated_net_fee > coarse.estimated_net_fee
        assert fine.raw_model_fee == coarse.raw_model_fee


class TestMemberClassMatters:
    """Measured fragmentation exposure. These numbers are the Step 6 finding.

    3 contracts at $0.6789, as one fill versus a hundred fills of 0.03:

        ============  ==========  ==========
        member        1 fill      100 fills
        ============  ==========  ==========
        direct        $0.045800   $0.045800
        non-direct    $0.053300   $0.963300
        ============  ==========  ==========

    The non-direct member pays eighteen times more for the same contracts at
    the same prices, decided entirely by a segmentation that is invisible
    pre-trade. Equality in the direct row is an observation about these two
    points, not an invariance claim -- see the fragmentation tests in
    ``tests/property/test_fee_properties.py`` for the counterexamples.
    """

    QUOTE_PRICE = "0.6789"
    TOTAL = "3.00"

    def test_non_direct_member_explodes_with_fragmentation(self):
        quote = quote_of(slice_at(self.QUOTE_PRICE, self.TOTAL))
        one_fill = as_quote(estimate_fees(quote, RESOLVED, NON_DIRECT))
        many = as_quote(
            estimate_fees(
                quote,
                RESOLVED,
                NON_DIRECT,
                actual_fills=fills_of(self.QUOTE_PRICE, "0.03", 100),
            )
        )
        assert one_fill.estimated_net_fee == Money.from_value("0.053300")
        assert many.estimated_net_fee == Money.from_value("0.963300")
        assert many.estimated_net_fee <= one_fill.upper_bound_net_fee

    def test_direct_member_is_equal_in_these_examples(self):
        """Equal here; NOT invariant in general (see the property suite)."""
        quote = quote_of(slice_at(self.QUOTE_PRICE, self.TOTAL))
        one_fill = as_quote(estimate_fees(quote, RESOLVED, DIRECT))
        many = as_quote(
            estimate_fees(
                quote, RESOLVED, DIRECT, actual_fills=fills_of(self.QUOTE_PRICE, "0.03", 100)
            )
        )
        assert one_fill.estimated_net_fee == Money.from_value("0.045800")
        assert many.estimated_net_fee == one_fill.estimated_net_fee

    def test_the_raw_model_fee_is_identical_in_every_case(self):
        quote = quote_of(slice_at(self.QUOTE_PRICE, self.TOTAL))
        values = {
            as_quote(estimate_fees(quote, RESOLVED, precision, actual_fills=f)).raw_model_fee
            for precision in (DIRECT, NON_DIRECT)
            for f in (None, fills_of(self.QUOTE_PRICE, "0.03", 100))
        }
        assert len(values) == 1


class TestUnknownPrecision:
    """A bound valid for both documented member classes."""

    def test_unknown_member_uses_the_coarser_grid(self):
        assert UNKNOWN_PRECISION.step == NON_DIRECT.step
        assert UNKNOWN_PRECISION.assumed is True
        assert "assumed" in UNKNOWN_PRECISION.describe()

    def test_the_bound_covers_a_direct_member_too(self):
        quote = quote_of(slice_at("0.6789", "3.00"))
        conservative = as_quote(estimate_fees(quote, RESOLVED, UNKNOWN_PRECISION))
        for precision in (DIRECT, NON_DIRECT):
            actual = as_quote(
                estimate_fees(
                    quote, RESOLVED, precision, actual_fills=fills_of("0.6789", "0.03", 100)
                )
            )
            assert actual.estimated_net_fee <= conservative.upper_bound_net_fee

    def test_the_assumption_is_disclosed(self):
        estimate = as_quote(
            estimate_fees(quote_of(slice_at("0.6789", "3.00")), RESOLVED, UNKNOWN_PRECISION)
        )
        assert any("member class unknown" in w for w in estimate.warnings)


class TestMultiplierSemantics:
    """A-14: the API's single multiplier vs the schedule's two columns."""

    def test_multiplier_one_is_unambiguous(self):
        estimate = as_quote(estimate_fees(quote_of(slice_at("0.5000", "1.00")), RESOLVED, DIRECT))
        assert estimate.multiplier_status is MultiplierStatus.IDENTITY
        assert estimate.supports_arbitrage_claim is True

    @pytest.mark.parametrize("multiplier", ["0", "0.5"])
    def test_other_multipliers_are_computed_but_blocked(self, multiplier):
        """Still reported as a hypothetical; never load-bearing for arbitrage."""
        estimate = as_quote(
            estimate_fees(quote_of(slice_at("0.5000", "1.00")), resolved(multiplier), DIRECT)
        )
        assert estimate.multiplier_status is MultiplierStatus.UNRESOLVED_MAPPING
        assert estimate.supports_arbitrage_claim is False
        assert any("A-14" in w for w in estimate.warnings)

    def test_the_numbers_are_still_produced(self):
        estimate = as_quote(
            estimate_fees(quote_of(slice_at("0.5000", "2.00")), resolved("0.5"), DIRECT)
        )
        assert estimate.raw_model_fee == Decimal("0.5") * Decimal("0.07") * 2 * Decimal("0.25")


class TestUnknownIsNotZero:
    """An unestablished fee must not be able to masquerade as a free trade."""

    @pytest.fixture
    def unavailable(self) -> UnavailableFee:
        result = estimate_fees(quote_of(slice_at("0.5000", "1.00")), UNKNOWN_TYPE, NON_DIRECT)
        assert isinstance(result, UnavailableFee)
        return result

    def test_it_is_a_different_type_entirely(self, unavailable):
        assert not isinstance(unavailable, FeeQuote)

    @pytest.mark.parametrize(
        "attribute",
        [
            "estimated_net_fee",
            "estimated_trade_fee",
            "estimated_rounding_fee",
            "estimated_rebate",
            "lower_bound_net_fee",
            "upper_bound_net_fee",
            "raw_model_fee",
        ],
    )
    def test_no_monetary_field_exists_to_be_read_as_zero(self, unavailable, attribute):
        """The core guarantee: there is no number here to subtract by accident."""
        assert not hasattr(unavailable, attribute)
        with pytest.raises(AttributeError):
            getattr(unavailable, attribute)

    def test_it_cannot_be_summed(self, unavailable):
        """Aggregation must fail loudly rather than contribute zero."""
        legs: list[object] = [unavailable]
        with pytest.raises(AttributeError):
            Money.from_units(sum(leg.estimated_net_fee.units for leg in legs))  # type: ignore[attr-defined]

    def test_it_says_why(self, unavailable):
        assert "margin_market_maker_program_fees" in unavailable.reason
        assert any("it is not zero" in w for w in unavailable.warnings)

    def test_it_cannot_support_an_arbitrage_claim(self, unavailable):
        assert unavailable.supports_arbitrage_claim is False
        assert unavailable.is_exact is False

    def test_a_zero_fee_series_is_a_real_quote_not_an_unavailable_one(self):
        """The distinction that makes the separation necessary."""
        result = estimate_fees(quote_of(slice_at("0.5000", "1.00")), resolved("0"), NON_DIRECT)
        assert isinstance(result, FeeQuote)
        assert result.raw_model_fee == 0
        assert result.estimated_trade_fee == Money.zero()


class TestMultiLeg:
    def test_each_leg_gets_its_own_accumulator(self):
        """Two orders are two orders; sharing one would invent rebate timing.

        The shared-accumulator answer is a whole rebate step cheaper, so this is
        not a cosmetic distinction.
        """
        legs = [
            quote_of(slice_at("0.1234", "3.00"), ticker="A"),
            quote_of(slice_at("0.1234", "3.00"), ticker="B"),
        ]
        separate = estimate_multi_leg_fees(legs, [RESOLVED, RESOLVED], NON_DIRECT)
        combined = as_quote(
            estimate_fees(
                quote_of(slice_at("0.1234", "3.00"), slice_at("0.1234", "3.00")),
                RESOLVED,
                NON_DIRECT,
            )
        )
        assert separate.total_estimated_net_fee is not None
        assert separate.total_estimated_net_fee > combined.estimated_net_fee
        assert separate.total_estimated_net_fee - combined.estimated_net_fee == NON_DIRECT.step

    def test_totals_sum_the_available_legs(self):
        legs = [
            quote_of(slice_at("0.1234", "3.00"), ticker="A"),
            quote_of(slice_at("0.4321", "2.00"), ticker="B"),
        ]
        estimate = estimate_multi_leg_fees(legs, [RESOLVED, RESOLVED], NON_DIRECT)
        assert estimate.is_complete
        assert estimate.total_estimated_net_fee == Money.from_units(
            sum(leg.estimated_net_fee.units for leg in estimate.available_legs)
        )
        assert estimate.total_lower_bound == Money.from_units(
            sum(leg.lower_bound_net_fee.units for leg in estimate.available_legs)
        )
        assert estimate.total_upper_bound == Money.from_units(
            sum(leg.upper_bound_net_fee.units for leg in estimate.available_legs)
        )

    def test_aggregate_bounds_contain_the_aggregate_estimate(self):
        legs = [
            quote_of(slice_at("0.1234", "3.00"), ticker="A"),
            quote_of(slice_at("0.4321", "2.00"), ticker="B"),
        ]
        estimate = estimate_multi_leg_fees(legs, [RESOLVED, RESOLVED], NON_DIRECT)
        assert estimate.total_lower_bound is not None
        assert estimate.total_upper_bound is not None
        assert estimate.total_estimated_net_fee is not None
        assert estimate.total_lower_bound <= estimate.total_estimated_net_fee
        assert estimate.total_estimated_net_fee <= estimate.total_upper_bound

    def test_one_unavailable_leg_suppresses_every_total(self):
        """A partial total would silently omit a leg."""
        legs = [
            quote_of(slice_at("0.1234", "3.00"), ticker="A"),
            quote_of(slice_at("0.4321", "2.00"), ticker="B"),
        ]
        estimate = estimate_multi_leg_fees(legs, [RESOLVED, UNKNOWN_TYPE], NON_DIRECT)
        assert estimate.total_estimated_net_fee is None
        assert estimate.total_lower_bound is None
        assert estimate.total_upper_bound is None
        assert estimate.total_raw_model_fee is None
        assert not estimate.is_complete
        assert len(estimate.unavailable_legs) == 1
        assert estimate.supports_arbitrage_claim is False
        assert any("would omit a leg" in w for w in estimate.warnings)

    def test_an_unresolved_multiplier_leg_blocks_the_aggregate_claim(self):
        legs = [
            quote_of(slice_at("0.1234", "3.00"), ticker="A"),
            quote_of(slice_at("0.4321", "2.00"), ticker="B"),
        ]
        estimate = estimate_multi_leg_fees(legs, [RESOLVED, resolved("0.5")], NON_DIRECT)
        assert estimate.is_complete
        assert estimate.total_estimated_net_fee is not None
        assert estimate.supports_arbitrage_claim is False
        assert any("A-14" in w for w in estimate.warnings)

    def test_mismatched_lengths_are_rejected(self):
        legs = [quote_of(slice_at("0.1234", "3.00"))]
        with pytest.raises(ValueError, match="each leg needs its own"):
            estimate_multi_leg_fees(legs, [RESOLVED, RESOLVED], NON_DIRECT)

    def test_no_legs_supports_nothing(self):
        estimate = estimate_multi_leg_fees([], [], NON_DIRECT)
        assert estimate.supports_arbitrage_claim is False
        assert estimate.total_estimated_net_fee is None
        assert not estimate.is_complete


class TestProvenance:
    def test_the_quote_carries_the_config_it_used(self):
        estimate = as_quote(
            estimate_fees(quote_of(slice_at("0.5000", "1.00")), RESOLVED, NON_DIRECT)
        )
        assert estimate.effective_config is RESOLVED
        assert "series base" in estimate.effective_config.provenance
        assert estimate.balance_precision is NON_DIRECT

    def test_quantities_are_carried_through(self):
        quote = quote_of(slice_at("0.5000", "1.00"), slice_at("0.6000", "2.00"))
        estimate = as_quote(estimate_fees(quote, RESOLVED, NON_DIRECT))
        assert estimate.requested_quantity == Quantity.from_value("3.00")
        assert estimate.filled_quantity == Quantity.from_value("3.00")

    def test_per_fill_audit_trail_is_retained(self):
        quote = quote_of(slice_at("0.5000", "1.00"), slice_at("0.6000", "2.00"))
        estimate = as_quote(estimate_fees(quote, RESOLVED, NON_DIRECT))
        assert len(estimate.fill_results) == 2
        assert estimate.final_accumulator.fills_applied == 2

    def test_an_unavailable_leg_still_carries_its_provenance(self):
        result = estimate_fees(quote_of(slice_at("0.5000", "1.00")), UNKNOWN_TYPE, NON_DIRECT)
        assert isinstance(result, UnavailableFee)
        assert result.effective_config is UNKNOWN_TYPE
        assert result.filled_quantity == Quantity.from_value("1.00")


class TestExactnessEnum:
    def test_there_is_no_unbounded_state(self):
        """Fill count is provably finite, so every computable quote is bounded.

        Unverifiable configurations are a separate type, not a weaker value.
        """
        assert {member.value for member in Exactness} == {
            "EXACT_ACTUAL_FILLS",
            "BOUNDED_ESTIMATE",
        }


class TestNoProfitLogic:
    def test_module_exposes_no_profitability_surface(self):
        for forbidden in (
            "is_arbitrage",
            "profit",
            "edge",
            "roi",
            "guaranteed_payoff",
            "max_profitable_quantity",
        ):
            assert not hasattr(module_under_test, forbidden)
