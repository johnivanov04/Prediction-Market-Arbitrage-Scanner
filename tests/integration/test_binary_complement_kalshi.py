"""The detector driven by the real Kalshi fee engine.

The unit tests use a stub quoter so the detector's logic can be exercised
without a venue. This file checks the other half: that the Step 6 fee bounds
plug into the Step 7 detector and that the member-class assumption actually
decides the verdict.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from functools import partial

import pytest

from predarb.books.execution import (
    ExecutionContext,
    ExecutionQuote,
    build_execution_curve,
)
from predarb.books.levels import BookLevel
from predarb.books.orderbook import BookView
from predarb.books.state import BookIntegrity, BookProvenance
from predarb.detectors.binary_complement import (
    BinaryComplementResult,
    LegFeeQuoter,
    evaluate_quantity,
)
from predarb.domain.costs import FeeBounds, LegFees
from predarb.domain.enums import FeeType, MarketSide, SettlementKind, VenueId
from predarb.domain.fees import FeeConfiguration, FeeScope, ResolvedFeeConfiguration
from predarb.domain.models import VenueInstrument
from predarb.domain.money import Money, Price, Quantity
from predarb.opportunities.models import Classification, CostStatus
from predarb.semantics.certificate import standard_binary_complement
from predarb.semantics.fingerprint import synthetic_fingerprint
from predarb.venues.kalshi.fee_model import BalancePrecision
from predarb.venues.kalshi.fees import leg_fee_bounds

pytestmark = pytest.mark.integration

T0 = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
NOTIONAL = Price.from_value("1.0000")
HASH = "c" * 64
FINGERPRINT = synthetic_fingerprint(market_rules_hash=HASH, market_notional="1.0000")
CONTEXT = ExecutionContext(current_connection_epoch=1)
DIRECT = BalancePrecision.direct_member()
NON_DIRECT = BalancePrecision.non_direct_member()
UNKNOWN = BalancePrecision.unknown_member()


def resolved(
    multiplier: str = "1", fee_type: str = FeeType.QUADRATIC.value
) -> ResolvedFeeConfiguration:
    return ResolvedFeeConfiguration(
        configuration=FeeConfiguration(
            fee_type_raw=fee_type,
            multiplier=Decimal(multiplier),
            scope=FeeScope.SERIES,
            scope_ticker="KXTEST",
        ),
        effective_from=None,
        provenance="series base configuration on KXTEST",
    )


def quoter(
    precision: BalancePrecision, config: ResolvedFeeConfiguration | None = None
) -> LegFeeQuoter:
    """The Kalshi adapter, bound to a fee config and a member class."""
    return partial(leg_fee_bounds, resolved=config or resolved(), precision=precision)


# yes_bid 0.60 + no_bid 0.50 = 1.10, so a YES+NO pair costs $0.90 for a $1.00
# guaranteed payout: $0.10 of gross margin per contract.
CROSSED = BookView(
    market_ticker="MKT",
    integrity=BookIntegrity.VALID,
    yes_bids=(
        BookLevel(
            price=Price.from_value("0.6000"),
            quantity=Quantity.from_value("50.00"),
            side=MarketSide.YES,
        ),
    ),
    no_bids=(
        BookLevel(
            price=Price.from_value("0.5000"),
            quantity=Quantity.from_value("50.00"),
            side=MarketSide.NO,
        ),
    ),
    provenance=BookProvenance(connection_epoch=1, sid=1, latest_seq=10, latest_raw_id="raw-10"),
)

INSTRUMENT = VenueInstrument(
    venue=VenueId.KALSHI,
    ticker="MKT",
    event_ticker="EVT",
    title="",
    status_raw="active",
    settlement_kind=SettlementKind.BINARY,
    market_type_raw="binary",
    notional_value=NOTIONAL,
    price_grid=None,
    yes_sub_title=None,
    no_sub_title=None,
    rules_primary=None,
    rules_secondary=None,
    rules_hash=HASH,
    open_time=None,
    close_time=None,
    expected_expiration_time=None,
    latest_expiration_time=None,
    settlement_timer_seconds=None,
    result_raw=None,
    settlement_value=None,
    can_close_early=None,
    yes_bid=None,
    yes_ask=None,
    no_bid=None,
    no_ask=None,
    yes_bid_size=None,
    yes_ask_size=None,
    exclusion_reasons=(),
)

CERTIFICATE = standard_binary_complement(
    market_ticker="MKT",
    evidence_fingerprint=FINGERPRINT,
    rules_hash=HASH,
    notional=NOTIONAL,
    evidence="Synthetic fixture: two-state complement by construction.",
    verified_by="test",
    verification_method="fixture",
    verified_at=T0,
    valid_from=T0,
)


def evaluate(
    precision: BalancePrecision,
    *,
    config: ResolvedFeeConfiguration | None = None,
    quantity: str = "1.00",
) -> BinaryComplementResult:
    return evaluate_quantity(
        instrument=INSTRUMENT,
        view=CROSSED,
        certificate=CERTIFICATE,
        current_evidence_fingerprint=FINGERPRINT,
        context=CONTEXT,
        fee_quoter=quoter(precision, config),
        quantity=Quantity.from_value(quantity),
        at=T0,
    )


class TestMemberClassDecidesTheVerdict:
    """The same book and the same quantity, classified differently.

    Not a defect. The non-direct fee upper bound allows up to one cent of
    unrebated rounding on each of the 100 fills that 1.00 contracts permits, so
    it genuinely cannot rule out fees larger than the margin. That is the
    bound doing its job.
    """

    def test_a_direct_member_can_prove_it(self):
        result = evaluate(DIRECT)
        assert result.classification is Classification.PROVEN_CONTRACTUAL_ARBITRAGE
        assert result.profit is not None
        assert result.profit.profit_lower_bound == Money.from_value("0.045702")

    def test_a_non_direct_member_cannot(self):
        result = evaluate(NON_DIRECT)
        assert result.classification is Classification.INDETERMINATE_COST_BOUNDS
        assert result.profit is not None
        assert result.profit.profit_lower_bound.units < 0
        assert result.profit.profit_upper_bound.units > 0

    def test_the_conservative_unknown_bound_matches_the_non_direct_one(self):
        """Unknown membership must not accidentally be the optimistic case."""
        unknown = evaluate(UNKNOWN)
        non_direct = evaluate(NON_DIRECT)
        assert unknown.classification is non_direct.classification
        assert unknown.profit is not None
        assert non_direct.profit is not None
        assert unknown.profit.profit_lower_bound == non_direct.profit.profit_lower_bound

    def test_the_optimistic_bound_agrees_across_member_classes(self):
        """Fee lower bounds do not depend on balance precision."""
        direct = evaluate(DIRECT)
        non_direct = evaluate(NON_DIRECT)
        assert direct.profit is not None
        assert non_direct.profit is not None
        assert direct.profit.profit_upper_bound == non_direct.profit.profit_upper_bound

    @pytest.mark.parametrize("quantity", ["0.01", "0.10", "1.00", "5.00", "40.00"])
    def test_no_quantity_makes_the_non_direct_case_provable(self, quantity):
        """A structural result, not a property of this particular book.

        The fee upper bound admits up to one balance step of unrebated rounding
        per fill, and one contract permits 100 fills across two legs. For a
        non-direct member that is ``2 x 100 x $0.01 = $2.00`` per contract of
        admissible rounding, against a payout that cannot exceed the $1.00
        notional per contract. Shrinking the size does not help: the margin
        shrinks with it while the per-fill cent does not.

        So under the Step 6 bound, a same-market complement is **never**
        provable for a non-direct member at any size. The equivalent figure for
        a direct member is $0.02 per contract, which leaves real room.
        """
        result = evaluate(NON_DIRECT, quantity=quantity)
        assert result.classification is not Classification.PROVEN_CONTRACTUAL_ARBITRAGE

    def test_the_direct_member_headroom_is_a_hundred_times_smaller(self):
        """Which is exactly why the same trade is provable for a direct member."""
        direct = evaluate(DIRECT)
        non_direct = evaluate(NON_DIRECT)
        assert direct.profit is not None
        assert non_direct.profit is not None
        assert direct.profit.profit_lower_bound > non_direct.profit.profit_lower_bound


class TestFeeSemanticsBlock:
    def test_an_unresolved_multiplier_blocks(self):
        """A-14: the schedule's maker/taker column mapping is unproven."""
        result = evaluate(DIRECT, config=resolved(multiplier="0.5"))
        assert result.classification is Classification.BLOCKED_FEE_SEMANTICS
        assert result.blocking_reason is not None
        assert "cannot support a contractual claim" in result.blocking_reason

    def test_a_free_series_still_blocks_on_the_multiplier(self):
        """fee_multiplier = 0 is real, and still rests on the same mapping."""
        result = evaluate(DIRECT, config=resolved(multiplier="0"))
        assert result.classification is Classification.BLOCKED_FEE_SEMANTICS

    def test_an_unsupported_fee_type_blocks(self):
        result = evaluate(DIRECT, config=resolved(fee_type="margin_market_maker_program_fees"))
        assert result.classification is Classification.BLOCKED_FEE_SEMANTICS
        assert result.cost_status is CostStatus.UNAVAILABLE

    def test_an_unavailable_fee_is_never_counted_as_zero(self):
        """A zero fee would make this look more profitable, not less."""
        result = evaluate(DIRECT, config=resolved(fee_type="flat"))
        assert result.classification is Classification.BLOCKED_FEE_SEMANTICS
        assert result.profit is None


class TestAdapterContract:
    def test_the_adapter_returns_venue_neutral_bounds(self):
        curve = build_execution_curve(CROSSED, INSTRUMENT, CONTEXT, MarketSide.YES, at=T0)
        quote: ExecutionQuote = curve.quote_up_to(Quantity.from_value("1.00"), at=T0)
        fees: LegFees = leg_fee_bounds(quote, resolved(), DIRECT)
        assert isinstance(fees, FeeBounds)
        assert fees.lower <= fees.upper
        assert fees.supports_arbitrage_claim

    def test_the_provenance_records_how_the_bound_was_derived(self):
        curve = build_execution_curve(CROSSED, INSTRUMENT, CONTEXT, MarketSide.YES, at=T0)
        fees = leg_fee_bounds(
            curve.quote_up_to(Quantity.from_value("1.00"), at=T0), resolved(), DIRECT
        )
        assert isinstance(fees, FeeBounds)
        for expected in ("series base", "BOUNDED_ESTIMATE", "IDENTITY", "k_max=100"):
            assert expected in fees.provenance

    def test_each_leg_is_quoted_as_its_own_order(self):
        """Independent accumulators: the legs must not share rebate timing."""
        result = evaluate(DIRECT)
        assert isinstance(result.yes_fees, FeeBounds)
        assert isinstance(result.no_fees, FeeBounds)
        # Different prices, so different fees; identical values would hint at
        # one shared computation rather than two independent ones.
        assert result.yes_fees.lower != result.no_fees.lower
