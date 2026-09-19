"""The fee engine against real captured Kalshi fee metadata.

These are production rows, not constructed examples. They are here to answer a
question the unit tests cannot: *how much of the real venue can this engine
actually price?* The answer is "a minority of series with scheduled changes",
and that is recorded rather than worked around.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import pytest

from predarb.domain.enums import FeeType
from predarb.domain.fees import FeeConfiguration, FeeScope
from predarb.domain.money import Money, Price, Quantity
from predarb.venues.kalshi.fee_coverage import ArbEligibility, CoverageCensus
from predarb.venues.kalshi.fee_engine import AccumulatorState, Fill, apply_fill
from predarb.venues.kalshi.fee_model import (
    SUPPORTED_TAKER_FEE_TYPES,
    BalancePrecision,
    UnsupportedFeeTypeError,
    raw_model_fee,
)
from predarb.venues.kalshi.models import (
    KalshiEventFeeChangesResponse,
    KalshiSeriesFeeChangesResponse,
)
from tests.conftest import load_payload

pytestmark = pytest.mark.integration

NON_DIRECT = BalancePrecision.non_direct_member()


@pytest.fixture(scope="module")
def series_changes():
    payload = load_payload("rest/series_fee_changes.json")
    return KalshiSeriesFeeChangesResponse.model_validate(payload).series_fee_change_arr


@pytest.fixture(scope="module")
def event_changes():
    payload = load_payload("rest/events_fee_changes.json")
    return KalshiEventFeeChangesResponse.model_validate(payload).event_fee_changes


class TestObservedMetadataShape:
    """What production actually contains, recorded as OBSERVED."""

    def test_every_documented_multiplier_appears(self, series_changes):
        """0, 0.5 and 1 are all live. A zero multiplier means a fee-free series."""
        multipliers = {c.fee_multiplier for c in series_changes}
        assert multipliers == {Decimal(0), Decimal("0.5"), Decimal(1)}

    def test_multipliers_are_exact_decimals_not_floats(self, series_changes):
        """0.5 through a float would be the silent corruption this repo forbids."""
        assert all(isinstance(c.fee_multiplier, Decimal) for c in series_changes)

    def test_four_distinct_fee_types_are_live(self, series_changes):
        observed = Counter(c.fee_type for c in series_changes)
        assert set(observed) == {
            "quadratic",
            "quadratic_with_maker_fees",
            "quadratic_with_combo_maker_fees",
            "margin_market_maker_program_fees",
        }

    def test_an_undocumented_fee_type_is_live(self, series_changes):
        """margin_market_maker_program_fees is in no documentation we can reach.

        It is the reason fee_type is carried as a raw string rather than a
        closed enum: ingestion must not crash on a value the venue invents.
        """
        undocumented = [
            c.fee_type for c in series_changes if c.fee_type not in {t.value for t in FeeType}
        ]
        assert undocumented
        assert set(undocumented) == {"margin_market_maker_program_fees"}

    def test_event_overrides_carry_their_series(self, event_changes):
        assert all(c.event_ticker for c in event_changes)
        assert any(c.series_ticker for c in event_changes)

    def test_no_clearing_record_has_been_observed(self, event_changes):
        """The model permits null overrides; production has not yet sent one.

        Recorded so the claim stays honest: the clearing path is implemented
        from documentation, not from an observation.
        """
        assert all(c.fee_type_override is not None for c in event_changes)


class TestCensusOverRealRows:
    """Coverage counted over a named universe, with denominators that reconcile.

    The universe here is *scheduled series fee changes*, which is deliberately
    NOT the series population: series that receive fee changes skew heavily
    toward maker-fee and margin types. Quoting this 37% as though it described
    the venue would badly understate coverage, which is exactly why the census
    carries its universe in the data.
    """

    @pytest.fixture
    def census(self, series_changes: Sequence[Any]) -> CoverageCensus:
        return CoverageCensus.from_rows(
            "series with scheduled fee changes",
            [(row.fee_type, row.fee_multiplier) for row in series_changes],
        )

    def test_the_counts_reconcile(self, census):
        assert census.total == 147
        assert sum(census.by_state.values()) == 147
        assert sum(census.by_fee_type.values()) == 147

    def test_calculable_and_boundable_are_different_numbers(self, census):
        """55 rows are quadratic, but only 7 of those carry multiplier 1.

        Conflating "we can compute this" with "this can support a riskless
        claim" is the error the census exists to prevent, and here the two
        differ by a factor of eight. Scheduled fee changes are largely
        promotional: of the 55 quadratic rows, 30 set the multiplier to 0 and
        18 set it to 0.5, both of which depend on the unresolved A-14 mapping.
        """
        assert census.calculable == 55
        assert census.boundable_for_arb == 7
        assert census.by_state[ArbEligibility.CALCULABLE_ONLY] == 48
        assert census.by_state[ArbEligibility.BLOCKED_UNSUPPORTED_TYPE] == 92

    def test_blocked_includes_the_unresolved_multipliers(self, census):
        """Blocked is 140, not 92: the 48 unresolved multipliers count too."""
        assert census.blocked == 140
        assert census.blocked > census.by_state[ArbEligibility.BLOCKED_UNSUPPORTED_TYPE]

    def test_the_universe_is_carried_with_the_numbers(self, census):
        assert "scheduled fee changes" in census.universe


class TestPricingCoverage:
    """How much of this metadata the engine can actually price."""

    def test_most_series_changes_cannot_be_priced(self, series_changes):
        """92 of 147 rows carry a fee type with no established taker formula.

        The engine refuses them rather than applying the quadratic rate on the
        strength of the name. Note this is the fee-change universe, not the
        series population -- see TestCensusOverRealRows.
        """
        supported = {t.value for t in SUPPORTED_TAKER_FEE_TYPES}
        priceable = [c for c in series_changes if c.fee_type in supported]
        assert len(series_changes) == 147
        assert len(priceable) == 55
        assert len(series_changes) - len(priceable) == 92

    def test_unpriceable_types_raise_rather_than_defaulting(self, series_changes):
        """Every refusal is a real refusal, checked one row at a time."""
        supported = {t.value for t in SUPPORTED_TAKER_FEE_TYPES}
        refused = 0
        for row in series_changes:
            if row.fee_type in supported:
                continue
            config = FeeConfiguration(
                fee_type_raw=row.fee_type,
                multiplier=row.fee_multiplier,
                scope=FeeScope.SERIES,
                scope_ticker=row.series_ticker,
            )
            with pytest.raises(UnsupportedFeeTypeError):
                raw_model_fee(
                    price=Price.from_value("0.5000"),
                    quantity=Quantity.from_value("1.00"),
                    configuration=config,
                )
            refused += 1
        assert refused == 92

    def test_every_priceable_row_computes_a_sane_fee(self, series_changes):
        """For the rows we do support, the whole chain runs without exception."""
        supported = {t.value for t in SUPPORTED_TAKER_FEE_TYPES}
        priced = 0
        for row in series_changes:
            if row.fee_type not in supported:
                continue
            fill = Fill(
                price=Price.from_value("0.5000"),
                quantity=Quantity.from_value("10.00"),
                configuration=FeeConfiguration(
                    fee_type_raw=row.fee_type,
                    multiplier=row.fee_multiplier,
                    scope=FeeScope.SERIES,
                    scope_ticker=row.series_ticker,
                ),
            )
            result = apply_fill(fill, AccumulatorState.new_order(), NON_DIRECT)
            assert result.net_fee.units >= 0
            # 10 contracts at $0.50 can never cost more than the $5 notional.
            assert result.net_fee < Money.from_value("5.000000")
            priced += 1
        assert priced == 55

    def test_a_zero_multiplier_series_is_genuinely_free(self, series_changes):
        """Observed live, and it must compute as zero rather than as a rounding."""
        supported = {t.value for t in SUPPORTED_TAKER_FEE_TYPES}
        free = [c for c in series_changes if c.fee_multiplier == 0 and c.fee_type in supported]
        assert free, "expected at least one fee-free quadratic series in the capture"
        config = FeeConfiguration(
            fee_type_raw=free[0].fee_type,
            multiplier=free[0].fee_multiplier,
            scope=FeeScope.SERIES,
            scope_ticker=free[0].series_ticker,
        )
        assert (
            raw_model_fee(
                price=Price.from_value("0.5000"),
                quantity=Quantity.from_value("100.00"),
                configuration=config,
            )
            == 0
        )

    def test_a_free_series_still_incurs_balance_rounding(self, series_changes):
        """A zero fee does not mean a zero cost: the balance still realigns.

        Worth pinning, because "free series" is exactly the case where a
        careless detector would assume there is nothing to subtract.
        """
        supported = {t.value for t in SUPPORTED_TAKER_FEE_TYPES}
        free = next(c for c in series_changes if c.fee_multiplier == 0 and c.fee_type in supported)
        fill = Fill(
            price=Price.from_value("0.3333"),
            quantity=Quantity.from_value("1.00"),
            configuration=FeeConfiguration(
                fee_type_raw=free.fee_type,
                multiplier=free.fee_multiplier,
                scope=FeeScope.SERIES,
                scope_ticker=free.series_ticker,
            ),
        )
        result = apply_fill(fill, AccumulatorState.new_order(), NON_DIRECT)
        assert result.trade_fee == Money.zero()
        assert result.rounding_fee > Money.zero()
        assert result.net_fee > Money.zero()
