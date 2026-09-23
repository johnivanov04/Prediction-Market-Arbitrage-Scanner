"""Structured strike coverage: what it can show, and what it refuses to assume.

The helper exists to support an AT_LEAST_ONE argument with arithmetic. Most of
these tests are about the places it declines to help -- an unbounded tail it was
not told is closed, a strike type with no documented grammar, a boundary whose
meaning depends on the contract's tick size.
"""

from __future__ import annotations

import inspect
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from predarb.semantics.partition import (
    CoverageStatus,
    DomainBound,
    DomainConstraintEvidence,
    IntervalCoverage,
    StrikeInterval,
    UnsupportedStrikeError,
    analyse_coverage,
    interval_from_strike,
)

pytestmark = pytest.mark.unit


def iv(
    ticker: str, strike_type: str, floor: str | None = None, cap: str | None = None
) -> StrikeInterval:
    return interval_from_strike(
        ticker=ticker,
        strike_type=strike_type,
        floor_strike=Decimal(floor) if floor is not None else None,
        cap_strike=Decimal(cap) if cap is not None else None,
    )


# The real shape of a Kalshi temperature event, observed live on
# KXHIGHTTTN-26SEP21: a catch-all low bucket, four two-degree bands, and a
# catch-all high bucket.
ATTESTED = DomainConstraintEvidence(
    variable="daily high temperature",
    units="degrees Fahrenheit",
    step=Decimal("1"),
    source_component_ids=("market.rules_primary",),
    source_hashes={"market.rules_primary": "a" * 64},
    quoted_language="the official high temperature in whole degrees Fahrenheit",
    rationale="the rules state the measurement granularity explicitly",
    acknowledged_by="reviewer",
    acknowledged_at=datetime(2026, 9, 22, tzinfo=UTC),
)

TEMPERATURE_BUCKETS = [
    iv("T66", "less", cap="66"),
    iv("B66", "between", floor="66", cap="67"),
    iv("B68", "between", floor="68", cap="69"),
    iv("B70", "between", floor="70", cap="71"),
    iv("B72", "between", floor="72", cap="73"),
    iv("T73", "greater", floor="73"),
]


class TestReadingDocumentedStrikes:
    def test_greater_is_exclusive_and_greater_or_equal_is_not(self):
        assert iv("A", "greater", floor="10").lower_inclusive is False
        assert iv("A", "greater_or_equal", floor="10").lower_inclusive is True

    def test_less_is_exclusive_and_less_or_equal_is_not(self):
        assert iv("A", "less", cap="10").upper_inclusive is False
        assert iv("A", "less_or_equal", cap="10").upper_inclusive is True

    def test_between_is_inclusive_at_both_ends(self):
        """Documented as the minimum and maximum values leading to YES."""
        interval = iv("A", "between", floor="66", cap="67")
        assert interval.contains(Decimal("66"))
        assert interval.contains(Decimal("67"))
        assert not interval.contains(Decimal("67.5"))

    @pytest.mark.parametrize("kind", ["functional", "custom", "structured"])
    def test_ungrammared_strike_types_are_refused(self, kind):
        """No documented grammar means nothing can be concluded mechanically."""
        with pytest.raises(UnsupportedStrikeError, match="no documented grammar"):
            iv("A", kind)

    def test_a_missing_strike_type_is_refused(self):
        with pytest.raises(UnsupportedStrikeError, match="nothing documented"):
            iv("A", "")

    def test_a_strike_type_without_its_bound_is_refused(self):
        with pytest.raises(UnsupportedStrikeError, match="without floor_strike"):
            iv("A", "greater")

    def test_a_floor_above_its_cap_is_malformed(self):
        with pytest.raises(UnsupportedStrikeError, match="exceeds cap"):
            iv("A", "between", floor="80", cap="70")


class TestCoverage:
    def test_a_complete_partition_is_gap_free(self):
        result = analyse_coverage(
            [
                iv("A", "less", cap="70"),
                iv("B", "between", floor="70", cap="80"),
                iv("C", "greater", floor="80"),
            ]
        )
        assert result.status is CoverageStatus.GAP_FREE
        assert result.supports_exhaustiveness_argument

    def test_a_gap_is_reported_as_an_all_no_path(self):
        result = analyse_coverage([iv("A", "less", cap="70"), iv("C", "greater", floor="80")])
        assert result.status is CoverageStatus.GAP
        assert not result.supports_exhaustiveness_argument
        assert "ALL-NO" in result.detail

    def test_an_unbounded_upper_tail_is_not_silently_closed(self):
        result = analyse_coverage(
            [iv("A", "less", cap="70"), iv("B", "between", floor="70", cap="80")]
        )
        assert result.status is CoverageStatus.UNBOUNDED_UPPER_TAIL_UNCOVERED
        assert "no contract bound was supplied" in result.detail

    def test_an_unbounded_lower_tail_is_not_silently_closed(self):
        """The commonest real shape: a ladder of `greater` thresholds.

        265 of 810 sampled members were `greater` strikes. A ladder of them
        leaves every value below the lowest threshold uncovered, which is a
        direct ALL-NO path.
        """
        result = analyse_coverage([iv("A", "greater", floor="10"), iv("B", "greater", floor="20")])
        assert result.status is CoverageStatus.UNBOUNDED_LOWER_TAIL_UNCOVERED

    def test_a_reviewer_may_close_a_tail_and_it_is_recorded(self):
        result = analyse_coverage(
            [iv("A", "greater_or_equal", floor="0")],
            domain=DomainBound(lower=Decimal("0"), justification="rate cannot be negative"),
        )
        assert result.status is CoverageStatus.GAP_FREE
        assert any("asserted by reviewer" in a for a in result.assumptions)

    def test_overlap_is_recorded_but_never_disqualifying(self):
        """Overlap is an AT_MOST_ONE concern; AT_LEAST_ONE permits two winners."""
        result = analyse_coverage(
            [
                iv("A", "less", cap="75"),
                iv("B", "greater", floor="70"),
            ]
        )
        assert result.status is CoverageStatus.GAP_FREE
        assert result.overlaps

    def test_no_intervals_is_not_applicable_rather_than_covered(self):
        result = analyse_coverage([])
        assert result.status is CoverageStatus.NOT_APPLICABLE
        assert not result.supports_exhaustiveness_argument

    def test_an_unreadable_member_withholds_support(self):
        """A partition over a subset describes a smaller claim than the one asked."""
        result = analyse_coverage(
            [iv("A", "less", cap="70"), iv("B", "greater_or_equal", floor="70")],
            unreadable=["C: custom strike"],
        )
        assert result.status is CoverageStatus.GAP_FREE
        assert result.supports_exhaustiveness_argument is False


class TestDiscreteDomainIsConditionalUntilAttested:
    """Correction 3: a CLI flag cannot discharge a contract question.

    The temperature-bucket family tiles whole degrees and has real holes over
    the reals. Which is true is a fact about the contract, so a supplied step
    *names* the condition and only attested evidence discharges it.
    """

    def test_buckets_have_real_gaps_over_a_continuous_domain(self):
        result = analyse_coverage(TEMPERATURE_BUCKETS)
        assert result.status is CoverageStatus.GAP
        assert any("(67, 68)" in gap for gap in result.gaps)

    def test_a_bare_step_yields_a_conditional_result_not_a_proof(self):
        result = analyse_coverage(
            TEMPERATURE_BUCKETS,
            domain=DomainBound(step=Decimal("1"), justification="typed at a prompt"),
        )
        assert result.status is CoverageStatus.GAP_FREE_IF_DOMAIN_DISCRETE
        assert result.supports_exhaustiveness_argument is False
        assert result.required_domain_step == Decimal("1")

    def test_the_conditional_result_preserves_the_real_domain_gaps(self):
        """A conditional answer must keep the thing it is conditional on."""
        result = analyse_coverage(
            TEMPERATURE_BUCKETS,
            domain=DomainBound(step=Decimal("1"), justification="typed at a prompt"),
        )
        assert len(result.unconditional_gaps) == 3
        assert any("(67, 68)" in gap for gap in result.unconditional_gaps)
        assert "not discharged" in result.detail

    def test_attested_evidence_discharges_the_condition(self):
        result = analyse_coverage(TEMPERATURE_BUCKETS, domain=DomainBound(evidence=ATTESTED))
        assert result.status is CoverageStatus.GAP_FREE
        assert result.supports_exhaustiveness_argument is True

    @pytest.mark.parametrize(
        "weakened",
        [
            replace(ATTESTED, source_component_ids=()),
            replace(ATTESTED, source_hashes={}),
            replace(ATTESTED, quoted_language=""),
            replace(ATTESTED, acknowledged_by=""),
        ],
        ids=["no source", "no hash", "no quote", "no reviewer"],
    )
    def test_evidence_shaped_assertions_without_a_source_do_not_discharge(
        self, weakened: DomainConstraintEvidence
    ) -> None:
        assert weakened.is_attested is False
        assert weakened.blocking_reason() is not None

        result = analyse_coverage(TEMPERATURE_BUCKETS, domain=DomainBound(evidence=weakened))
        assert result.status is CoverageStatus.GAP_FREE_IF_DOMAIN_DISCRETE

    def test_a_title_saying_whole_degrees_is_not_evidence(self):
        """Titles are marketing copy, and there is no path from one to a domain.

        Checked behaviourally: the interval reader takes no title, a domain
        assertion whose only support is a title is not attested, and no title
        appears among the fingerprinted components.
        """
        assert "title" not in inspect.signature(interval_from_strike).parameters
        assert "title" not in set(ATTESTED.component_values())

        from_title = DomainConstraintEvidence(
            variable="daily high temperature",
            units="degrees Fahrenheit",
            step=Decimal("1"),
            rationale='the market title says "66° to 67°", so it must be whole degrees',
        )
        assert from_title.is_attested is False
        assert (
            analyse_coverage(TEMPERATURE_BUCKETS, domain=DomainBound(evidence=from_title)).status
            is CoverageStatus.GAP_FREE_IF_DOMAIN_DISCRETE
        )

    def test_historical_integer_settlements_are_not_evidence(self):
        """A sample of past values is not a statement about admissible values."""
        observed_integers = DomainConstraintEvidence(
            variable="daily high temperature",
            units="degrees Fahrenheit",
            step=Decimal("1"),
            rationale="every settlement we have seen was a whole number",
        )
        assert observed_integers.is_attested is False
        result = analyse_coverage(
            TEMPERATURE_BUCKETS, domain=DomainBound(evidence=observed_integers)
        )
        assert result.status is CoverageStatus.GAP_FREE_IF_DOMAIN_DISCRETE

    def test_a_finer_step_reopens_the_gaps_even_when_attested(self):
        half = replace(ATTESTED, step=Decimal("0.5"))
        result = analyse_coverage(TEMPERATURE_BUCKETS, domain=DomainBound(evidence=half))
        assert result.status is CoverageStatus.GAP

    def test_the_step_assumption_is_never_defaulted(self):
        """Silence means continuous, which is the conservative reading."""
        assert DomainBound().step is None
        assert DomainBound().effective_step is None
        assert DomainBound().step_is_attested is False


class TestDomainEvidenceBindsToSources:
    def test_changing_the_source_hash_changes_the_assertion(self):
        moved = replace(ATTESTED, source_hashes={"market.rules_primary": "b" * 64})
        assert ATTESTED.fingerprint().digest != moved.fingerprint().digest

    def test_changing_the_source_hash_changes_the_proof_fingerprint(self):
        """A certificate relying on the domain must go stale when its source moves."""
        first = analyse_coverage(TEMPERATURE_BUCKETS, domain=DomainBound(evidence=ATTESTED))
        moved = analyse_coverage(
            TEMPERATURE_BUCKETS,
            domain=DomainBound(
                evidence=replace(ATTESTED, source_hashes={"market.rules_primary": "b" * 64})
            ),
        )
        assert first.status is moved.status is CoverageStatus.GAP_FREE
        assert first.fingerprint().digest != moved.fingerprint().digest

    def test_changing_the_step_changes_the_proof_fingerprint(self):
        first = analyse_coverage(TEMPERATURE_BUCKETS, domain=DomainBound(evidence=ATTESTED))
        coarser = analyse_coverage(
            [
                iv("T66", "less", cap="66"),
                iv("B66", "between", floor="66", cap="69"),
                iv("T69", "greater", floor="69"),
            ],
            domain=DomainBound(evidence=replace(ATTESTED, step=Decimal("2"))),
        )
        assert first.fingerprint().digest != coarser.fingerprint().digest

    def test_the_quoted_language_is_carried_for_a_later_reader(self):
        assert "whole degrees" in ATTESTED.quoted_language
        assert ATTESTED.describe().startswith("daily high temperature")


class TestFingerprint:
    def test_the_digest_covers_the_intervals_not_the_verdict(self):
        """A certificate relying on a partition must go stale when a strike moves."""
        first = analyse_coverage(
            [iv("A", "less", cap="70"), iv("B", "greater_or_equal", floor="70")]
        )
        moved = analyse_coverage(
            [iv("A", "less", cap="71"), iv("B", "greater_or_equal", floor="71")]
        )
        assert first.status is moved.status is CoverageStatus.GAP_FREE
        assert first.fingerprint().digest != moved.fingerprint().digest

    def test_the_same_intervals_fingerprint_identically(self):
        def build() -> IntervalCoverage:
            return analyse_coverage(
                [iv("A", "less", cap="70"), iv("B", "greater_or_equal", floor="70")]
            )

        assert build().fingerprint().digest == build().fingerprint().digest
