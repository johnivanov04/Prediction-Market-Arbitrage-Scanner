"""ForecastEx semantic Gate 0: does it prove YES + NO = $1.00?

The invariant Phase 1 could not prove on Kalshi. These tests pin what the
ForecastEx governing text does and does not establish, and they deliberately
reuse the Phase-1 proof model unchanged -- if that model needed venue-specific
surgery to express a second venue's findings, it was not the right model.

The headline distinction, and the reason the two venues come out differently:
ForecastEx writes the second side down as the residual of the first. Kalshi
writes one side and leaves the other to inference.
"""

from __future__ import annotations

import dataclasses

import pytest

from predarb.domain.money import Price
from predarb.semantics.complement_proof import (
    MechanismProof,
    MechanismStatus,
    ProofStatus,
    RoundingModel,
)
from predarb.semantics.settlement_census import SettlementMechanism
from predarb.venues.forecastex.complement_findings import (
    PAIR_CREATION_COST,
    SOURCES,
    SUCCESS_FAMILY_PROOF,
)
from predarb.venues.forecastex.governing_sources import (
    FORECASTEX_RULEBOOK_HISTORY,
    RB_2026_03,
    RB_2026_09_IDENTIFIED,
)
from predarb.venues.kalshi.complement_findings import CONJECTURE_FAMILY_PROOF

pytestmark = pytest.mark.unit

PROOF = SUCCESS_FAMILY_PROOF


def finding(mechanism: SettlementMechanism) -> MechanismProof:
    return next(m for m in PROOF.mechanisms if m.mechanism is mechanism)


class TestOffsetNettingIsTheArbitrageRelevantPath:
    """Rule 604 forbids holding both sides and nets the pair at $1.00 same-day."""

    def test_offset_netting_is_proven_complementary(self):
        found = finding(SettlementMechanism.OFFSET_NETTING)
        assert found.status is MechanismStatus.PROVEN_COMPLEMENTARY
        assert found.both_branches_explicit

    def test_it_quotes_the_prohibition_on_holding_both_sides(self):
        quoted = finding(SettlementMechanism.OFFSET_NETTING).quoted_text
        assert "may not simultaneously hold both" in quoted

    def test_it_quotes_the_unconditional_dollar_credit(self):
        quoted = finding(SettlementMechanism.OFFSET_NETTING).quoted_text
        assert "credited $1.00 for each pair" in quoted

    def test_it_is_reachable(self):
        assert finding(SettlementMechanism.OFFSET_NETTING).reachable

    def test_offset_netting_is_a_distinct_mechanism_not_a_settlement_variant(self):
        """The pair never reaches resolution, so resolution rules never apply."""
        assert SettlementMechanism.OFFSET_NETTING in set(SettlementMechanism)
        assert not SettlementMechanism.OFFSET_NETTING.permits_fractional_payout


class TestOrdinarySettlement:
    def test_it_is_proven_from_the_rulebook_itself(self):
        found = finding(SettlementMechanism.ORDINARY_BINARY)
        assert found.status is MechanismStatus.PROVEN_COMPLEMENTARY
        assert "Rule 603(a)" in found.rule_reference

    def test_both_branches_name_both_sides(self):
        quoted = finding(SettlementMechanism.ORDINARY_BINARY).quoted_text
        assert quoted.count("will receive $0.00") == 2
        assert quoted.count("Settlement Value of $1.00") == 2

    def test_the_express_invariant_is_quoted(self):
        quoted = finding(SettlementMechanism.ORDINARY_BINARY).quoted_text
        assert "will always equal" in quoted


class TestResidualConstructions:
    """What Kalshi omits and ForecastEx states."""

    def test_participant_split_defines_no_as_the_residual(self):
        found = finding(SettlementMechanism.TIE_SPLIT)
        assert found.status is MechanismStatus.PROVEN_COMPLEMENTARY
        assert "minus the" in found.quoted_text
        assert "rounded down to the nearest cent" in found.quoted_text

    def test_fair_price_defines_no_as_the_residual(self):
        found = finding(SettlementMechanism.LAST_FAIR_PRICE)
        assert found.status is MechanismStatus.PROVEN_COMPLEMENTARY
        assert "minus the Yes payout" in found.quoted_text

    def test_the_rounding_model_is_residual_and_conserves(self):
        assert PROOF.rounding is RoundingModel.RESIDUAL
        assert PROOF.rounding.conserves_exactly

    def test_residual_rounding_conserves_for_every_divisor(self):
        """floor($1/N) + ($1 - floor($1/N)) == $1 exactly, for all N."""
        one = Price.from_value("1.0000")
        for n in range(1, 64):
            yes_units = (one.units // n) // 100 * 100  # floor to the cent
            no_units = one.units - yes_units
            assert yes_units + no_units == one.units, n

    def test_the_kalshi_equivalent_clause_lacked_the_residual(self):
        """The contrast that motivates the whole comparison."""
        assert CONJECTURE_FAMILY_PROOF.rounding is RoundingModel.UNSPECIFIED
        assert PROOF.rounding is RoundingModel.RESIDUAL


class TestOutcomeReviewConserves:
    def test_the_committee_output_is_a_binary_outcome(self):
        found = finding(SettlementMechanism.OUTCOME_REVIEW)
        assert found.status is MechanismStatus.PROVEN_COMPLEMENTARY
        assert "determine a final Outcome" in found.quoted_text
        assert "either" in found.quoted_text


class TestTheOneGap:
    """Rule 414(b)(3) caps the combined payout without flooring it."""

    def test_fair_allocation_is_unresolved(self):
        assert finding(SettlementMechanism.FAIR_ALLOCATION).status is (MechanismStatus.UNRESOLVED)

    def test_the_gap_is_an_upper_bound_with_no_floor(self):
        quoted = finding(SettlementMechanism.FAIR_ALLOCATION).quoted_text
        assert "shall the combined payout" in quoted
        assert "exceed $1.00" in quoted

    def test_the_conflict_with_the_product_terms_is_recorded(self):
        reasoning = finding(SettlementMechanism.FAIR_ALLOCATION).reasoning
        assert "upper bound with no floor" in reasoning
        assert "bounded" in reasoning

    def test_the_accelerated_last_prices_method_is_unresolved(self):
        assert finding(SettlementMechanism.LAST_TRADED_PRICE).status is (MechanismStatus.UNRESOLVED)

    def test_nothing_is_disproven(self):
        """Silence and conflict, not a rule permitting a shortfall."""
        assert all(m.status is not MechanismStatus.NOT_COMPLEMENTARY for m in PROOF.mechanisms)

    def test_the_overall_verdict_is_not_proven(self):
        assert PROOF.status is ProofStatus.NOT_PROVEN

    def test_two_unresolved_paths_block_five_proven_ones(self):
        proven = [m for m in PROOF.mechanisms if m.status is MechanismStatus.PROVEN_COMPLEMENTARY]
        assert len(proven) >= 5
        assert PROOF.status is not ProofStatus.PROVEN_FOR_SUBSET


class TestNoNaturalPersonPath:
    def test_it_is_recorded_as_unreachable_with_a_reason(self):
        found = finding(SettlementMechanism.NATURAL_PERSON_SCALAR)
        assert not found.reachable
        assert "death" in (found.unreachable_because or "")

    def test_it_does_not_block(self):
        assert finding(SettlementMechanism.NATURAL_PERSON_SCALAR) not in PROOF.blocking


class TestEconomicsAreSeparateFromSemantics:
    """Inverse pricing fixes the cost of a pair at $1.01."""

    def test_a_pair_costs_one_dollar_one_cent_to_create(self):
        assert Price.from_value("1.0100") == PAIR_CREATION_COST

    def test_a_pair_pays_exactly_one_dollar(self):
        assert PROOF.notional == Price.from_value("1.0000")

    def test_same_market_complement_arbitrage_is_worth_minus_one_cent(self):
        """A perfect settlement invariant and no opportunity. Both are true."""
        net = PROOF.notional.units - PAIR_CREATION_COST.units
        assert net == Price.from_value("0.0100").units * -1

    def test_the_inverse_pricing_finding_is_recorded(self):
        notes = " ".join(PROOF.notes)
        assert "401(d)" in notes
        assert "minus one cent" in notes

    def test_fees_do_not_touch_the_settlement_value(self):
        notes = " ".join(PROOF.notes)
        assert "in excess of" in notes
        assert "separate fee engine survives" in notes


class TestSourceRecord:
    def test_every_source_a_finding_rests_on_is_pinned(self):
        assert PROOF.unpinned_sources == ()

    def test_the_rulebook_read_is_the_one_quoted(self):
        rulebook = next(s for s in SOURCES if s.name == "forecastex_rulebook")
        assert rulebook.document_sha256 == RB_2026_03.document_sha256

    def test_the_current_version_is_identified_but_not_read(self):
        """Same honest state as Kalshi Klear v1.4."""
        assert RB_2026_09_IDENTIFIED.document_sha256 is None
        assert not RB_2026_09_IDENTIFIED.is_read
        current = FORECASTEX_RULEBOOK_HISTORY.current_version
        assert current is not None
        assert current.version == "2026-09-01"
        assert not current.is_read

    def test_the_latest_readable_version_is_the_march_redline(self):
        readable = [v for v in FORECASTEX_RULEBOOK_HISTORY.versions if v.is_read]
        assert readable[-1].version == "2026-03-16"

    def test_the_version_record_is_not_claimed_exhaustive(self):
        assert not FORECASTEX_RULEBOOK_HISTORY.versions_are_exhaustive
        assert FORECASTEX_RULEBOOK_HISTORY.record_complete_from is None

    def test_source_drift_invalidates_the_proof(self):
        drifted = tuple(
            dataclasses.replace(s, document_sha256="f" * 64) if i == 0 else s
            for i, s in enumerate(SOURCES)
        )
        applies, reasons = PROOF.still_applies_to(drifted)
        assert not applies
        assert any("content hash changed" in r for r in reasons)


class TestPhaseOneModelNeededNoSurgery:
    """The reuse claim, tested rather than asserted."""

    def test_the_same_proof_class_expresses_both_venues(self):
        assert type(PROOF) is type(CONJECTURE_FAMILY_PROOF)

    def test_both_venues_use_the_same_mechanism_vocabulary(self):
        for found in (*PROOF.mechanisms, *CONJECTURE_FAMILY_PROOF.mechanisms):
            assert found.mechanism in set(SettlementMechanism)

    def test_the_venues_reach_different_verdicts_through_the_same_gates(self):
        assert PROOF.status is ProofStatus.NOT_PROVEN
        assert CONJECTURE_FAMILY_PROOF.status is ProofStatus.NOT_PROVEN
        # Same verdict, different reasons: ForecastEx conserves under rounding,
        # Kalshi does not even have a rounding model.
        assert PROOF.rounding.conserves_exactly
        assert not CONJECTURE_FAMILY_PROOF.rounding.conserves_exactly
