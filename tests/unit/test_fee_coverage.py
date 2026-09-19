"""Tests for the fee-coverage census.

The census exists because an earlier revision of this work printed
``14,004 / 14,167 priceable`` alongside a list of unsupported series that summed
to a different number, and conflated "we can compute a fee" with "this fee can
support a riskless claim". Both mistakes are now structurally impossible: the
counts are validated on construction, and the two questions are separate states.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from predarb.venues.kalshi.fee_coverage import (
    ArbEligibility,
    CombinedCoverage,
    CoverageCensus,
    classify_fee_configuration,
)

pytestmark = pytest.mark.unit

QUADRATIC = "quadratic"
MAKER = "quadratic_with_maker_fees"
MARGIN = "margin_market_maker_program_fees"


class TestClassification:
    def test_quadratic_at_multiplier_one_is_boundable(self):
        assert classify_fee_configuration(QUADRATIC, Decimal(1)) is ArbEligibility.BOUNDABLE_FOR_ARB

    @pytest.mark.parametrize("multiplier", ["0", "0.5", "2"])
    def test_other_multipliers_are_calculable_but_not_boundable(self, multiplier):
        """A number can be produced; the A-14 mapping is still unproven."""
        state = classify_fee_configuration(QUADRATIC, Decimal(multiplier))
        assert state is ArbEligibility.CALCULABLE_ONLY
        assert state.is_calculable
        assert not state.supports_arbitrage_claim

    @pytest.mark.parametrize("fee_type", [MAKER, "quadratic_with_combo_maker_fees", "flat"])
    def test_documented_but_unpriceable_types_are_blocked(self, fee_type):
        state = classify_fee_configuration(fee_type, Decimal(1))
        assert state is ArbEligibility.BLOCKED_UNSUPPORTED_TYPE
        assert not state.is_calculable

    def test_an_undocumented_type_is_blocked(self):
        assert (
            classify_fee_configuration(MARGIN, Decimal(0))
            is ArbEligibility.BLOCKED_UNSUPPORTED_TYPE
        )

    @pytest.mark.parametrize(
        ("fee_type", "multiplier"),
        [(None, Decimal(1)), (QUADRATIC, None), (None, None)],
    )
    def test_missing_metadata_fails_closed(self, fee_type, multiplier):
        assert (
            classify_fee_configuration(fee_type, multiplier)
            is ArbEligibility.BLOCKED_UNSUPPORTED_TYPE
        )

    def test_calculable_is_weaker_than_boundable(self):
        """The distinction the whole module exists to preserve."""
        assert ArbEligibility.CALCULABLE_ONLY.is_calculable
        assert not ArbEligibility.CALCULABLE_ONLY.supports_arbitrage_claim
        assert ArbEligibility.BOUNDABLE_FOR_ARB.supports_arbitrage_claim


class TestCensusArithmetic:
    """A census whose parts do not add up cannot be constructed at all."""

    def test_eligibility_counts_must_sum_to_the_total(self):
        with pytest.raises(ValueError, match="sum to 5 but total is 6"):
            CoverageCensus(
                universe="listed",
                total=6,
                by_state={
                    ArbEligibility.BOUNDABLE_FOR_ARB: 3,
                    ArbEligibility.CALCULABLE_ONLY: 1,
                    ArbEligibility.BLOCKED_UNSUPPORTED_TYPE: 1,
                },
            )

    def test_fee_type_counts_must_sum_to_the_total(self):
        with pytest.raises(ValueError, match="fee-type counts sum to 1 but total is 2"):
            CoverageCensus(
                universe="listed",
                total=2,
                by_state={
                    ArbEligibility.BOUNDABLE_FOR_ARB: 2,
                    ArbEligibility.CALCULABLE_ONLY: 0,
                    ArbEligibility.BLOCKED_UNSUPPORTED_TYPE: 0,
                },
                by_fee_type={QUADRATIC: 1},
            )

    def test_the_exact_inconsistency_that_was_shipped_is_now_rejected(self):
        """14,004 supported + 160 + 3 + 23 unsupported != 14,167.

        The 23 unlisted series belong to a different universe; adding them to
        the listing's numerator while keeping the listing's denominator is what
        made the old summary irreconcilable.
        """
        with pytest.raises(ValueError, match="sum to 14190 but total is 14167"):
            CoverageCensus(
                universe="listed",
                total=14_167,
                by_state={
                    ArbEligibility.BOUNDABLE_FOR_ARB: 14_004,
                    ArbEligibility.CALCULABLE_ONLY: 0,
                    ArbEligibility.BLOCKED_UNSUPPORTED_TYPE: 186,
                },
            )

    def test_negative_total_is_rejected(self):
        with pytest.raises(ValueError, match="must not be negative"):
            CoverageCensus(universe="x", total=-1, by_state={})

    def test_from_rows_always_balances(self):
        rows: list[tuple[str | None, Decimal | None]] = [
            (QUADRATIC, Decimal(1)),
            (QUADRATIC, Decimal(1)),
            (QUADRATIC, Decimal("0.5")),
            (MAKER, Decimal(1)),
            (None, None),
        ]
        census = CoverageCensus.from_rows("sample", rows)
        assert census.total == 5
        assert sum(census.by_state.values()) == 5
        assert sum(census.by_fee_type.values()) == 5
        assert census.boundable_for_arb == 2
        assert census.calculable == 3
        assert census.blocked == 3

    def test_blocked_counts_everything_not_boundable(self):
        """Including calculable-but-unresolved, which is not a safe state."""
        census = CoverageCensus.from_rows(
            "sample", [(QUADRATIC, Decimal(1)), (QUADRATIC, Decimal("0.5"))]
        )
        assert census.boundable_for_arb == 1
        assert census.blocked == 1
        assert census.calculable == 2

    def test_fractions_use_this_censuss_own_total(self):
        census = CoverageCensus.from_rows(
            "sample", [(QUADRATIC, Decimal(1))] * 3 + [(MAKER, Decimal(1))]
        )
        assert census.fraction(census.boundable_for_arb) == Decimal("0.7500")

    def test_an_empty_census_has_no_fraction(self):
        """0/0 is not 100% coverage, and must not be printed as a number."""
        census = CoverageCensus.from_rows("empty", [])
        assert census.total == 0
        assert census.fraction(0) is None


class TestCombinedCoverage:
    def test_totals_are_the_sum_of_the_parts(self):
        listed = CoverageCensus.from_rows("listed", [(QUADRATIC, Decimal(1))] * 10)
        unlisted = CoverageCensus.from_rows("unlisted", [(MARGIN, Decimal(0))] * 2)
        combined = CombinedCoverage(
            censuses=(listed, unlisted), assembly="listing plus fee-change tickers"
        )
        assert combined.total == 12
        assert combined.boundable_for_arb == 10
        assert combined.blocked == 2
        assert combined.fraction_boundable() == Decimal("0.8333")

    def test_a_combined_figure_requires_an_assembly_description(self):
        """The listing is non-exhaustive, so a bare percentage would mislead."""
        listed = CoverageCensus.from_rows("listed", [(QUADRATIC, Decimal(1))])
        for assembly in ("", "   "):
            with pytest.raises(ValueError, match="how the universe was assembled"):
                CombinedCoverage(censuses=(listed,), assembly=assembly)

    def test_duplicate_universes_are_rejected(self):
        census = CoverageCensus.from_rows("listed", [(QUADRATIC, Decimal(1))])
        with pytest.raises(ValueError, match="must be distinct"):
            CombinedCoverage(censuses=(census, census), assembly="x")

    def test_describe_names_every_denominator(self):
        listed = CoverageCensus.from_rows("listed", [(QUADRATIC, Decimal(1))] * 10)
        unlisted = CoverageCensus.from_rows("unlisted", [(MARGIN, Decimal(0))] * 2)
        text = CombinedCoverage(
            censuses=(listed, unlisted), assembly="assembled from A and B"
        ).describe()
        assert "listed: 10/10" in text
        assert "unlisted: 0/2" in text
        assert "combined 10/12" in text
        assert "assembled from A and B" in text

    def test_an_empty_combination_has_no_fraction(self):
        combined = CombinedCoverage(censuses=(), assembly="nothing was reachable")
        assert combined.total == 0
        assert combined.fraction_boundable() is None
