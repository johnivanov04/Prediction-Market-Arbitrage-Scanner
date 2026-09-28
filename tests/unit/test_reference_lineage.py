"""Tracing a citation to the provision that governs it now.

The two cases in this corpus are opposites, and the whole point of the module
under test is that they come out differently:

* **BOND**, certified 2025-01-17 under Rulebook v1.14, where Rule 6.3(b) *was*
  the indeterminate-outcome payout provision. Exact when written, moved twice
  since by amendments we hold. Resolvable.
* **CRIMECHARGE**, certified 2025-07-24, nearly five months after the scalar
  amendment had already made 6.3(b) the Scalar Contract rule. Wrong on the day
  it was written. Not resolvable, and not repairable by borrowing BOND's
  pre-amendment lineage.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from predarb.semantics.dependency import ReferenceResolution
from predarb.semantics.lineage import (
    HopBasis,
    LineageHop,
    ReferenceLineage,
    SourceAmendment,
    SourceHistory,
    SourceVersion,
)
from predarb.semantics.reference_resolution import (
    ResolutionOutcome,
    heading_for_rule,
    headings_from_text,
    resolve_reference,
    rule_number_of,
    subsection_of,
)
from predarb.venues.kalshi.governing_sources import EXCHANGE_RULEBOOK
from predarb.venues.kalshi.rulebook_history import (
    BOND_LINEAGES,
    CRIMECHARGE_LINEAGES,
    DEATH_AMENDMENT,
    EXCHANGE_RULEBOOK_HISTORY,
    RULE_HEADINGS,
    SCALAR_AMENDMENT,
    headings_for_version,
    lineages_for_series,
)

pytestmark = pytest.mark.unit

HISTORY = EXCHANGE_RULEBOOK_HISTORY


def lineage_for(citation: str, lineages: tuple[ReferenceLineage, ...]) -> ReferenceLineage:
    return next(lin for lin in lineages if lin.citation_as_written == citation)


class TestRulebookRecord:
    def test_it_holds_the_versions_that_bracket_the_window(self):
        assert [v.version for v in HISTORY.versions] == ["1.14", "1.16", "1.18", "1.29"]

    def test_v114_was_in_force_when_bond_was_certified(self):
        version = HISTORY.version_at(date(2025, 1, 17))
        assert version is not None
        assert version.version == "1.14"

    def test_a_post_scalar_version_was_in_force_when_crimecharge_was_certified(self):
        version = HISTORY.version_at(date(2025, 7, 24))
        assert version is not None
        assert version.version == "1.18"

    def test_the_scalar_amendment_took_effect_between_the_two_certifications(self):
        assert date(2025, 1, 17) < SCALAR_AMENDMENT.effective_date < date(2025, 7, 24)

    def test_both_amendments_touch_rule_6_3(self):
        assert SCALAR_AMENDMENT.affects_rule("Rule 6.3(b)")
        assert DEATH_AMENDMENT.affects_rule("Rule 6.3(d)")

    def test_neither_amendment_touches_rule_7_1(self):
        assert not SCALAR_AMENDMENT.affects_rule("Rule 7.1")
        assert not DEATH_AMENDMENT.affects_rule("Rule 7.1")

    def test_a_subsection_citation_matches_its_rule(self):
        """An amendment to Rule 6.3 renumbers 6.3(b) without ever naming it."""
        assert SCALAR_AMENDMENT.affects_rule("Rule 6.3")
        assert SCALAR_AMENDMENT.affects_rule("RULE 6.3(d)")

    def test_amendments_are_found_in_a_window(self):
        found = HISTORY.amendments_affecting("Rule 6.3(b)", after=date(2025, 1, 17))
        assert [a.amendment_id for a in found] == [
            SCALAR_AMENDMENT.amendment_id,
            DEATH_AMENDMENT.amendment_id,
        ]

    def test_nothing_is_found_after_the_last_amendment(self):
        assert HISTORY.amendments_affecting("Rule 6.3(b)", after=date(2026, 4, 1)) == ()


class TestBondLineage:
    def test_the_payout_citation_was_exact_at_issuance(self):
        lineage = lineage_for("Rule 6.3(b)", BOND_LINEAGES)
        assert lineage.issuance_reference_valid
        assert lineage.issuance_version == "1.14"

    def test_the_payout_citation_resolves_to_6_3_c(self):
        lineage = lineage_for("Rule 6.3(b)", BOND_LINEAGES)
        resolution, authority = lineage.resolution(HISTORY)
        assert resolution is ReferenceResolution.HISTORICAL_REFERENCE_RESOLVED
        assert lineage.current_reference == "Rule 6.3(c)"
        assert authority is not None
        assert SCALAR_AMENDMENT.amendment_id in authority

    def test_the_review_citation_resolves_to_6_3_f(self):
        """Two hops: the scalar insert, then the death-of-person insert."""
        lineage = lineage_for("Rule 6.3(d)", BOND_LINEAGES)
        resolution, _ = lineage.resolution(HISTORY)
        assert resolution is ReferenceResolution.HISTORICAL_REFERENCE_RESOLVED
        assert lineage.current_reference == "Rule 6.3(f)"
        assert [h.to_reference for h in lineage.hops] == ["Rule 6.3(e)", "Rule 6.3(f)"]

    def test_it_is_never_called_ambiguous_merely_for_being_stale(self):
        for lineage in BOND_LINEAGES:
            resolution, _ = lineage.resolution(HISTORY)
            assert resolution is not ReferenceResolution.AMBIGUOUS_LEGACY_REFERENCE

    def test_the_written_citation_is_preserved(self):
        lineage = lineage_for("Rule 6.3(b)", BOND_LINEAGES)
        assert lineage.citation_as_written == "Rule 6.3(b)"

    def test_every_hop_names_an_amendment_and_its_hash(self):
        for lineage in BOND_LINEAGES:
            for hop in lineage.hops:
                assert hop.amendment_id
                assert hop.amendment_sha256
                assert hop.evidence

    def test_a_non_moving_hop_is_recorded_explicitly(self):
        """The death amendment left 6.3(c) alone, and that is evidence too."""
        lineage = lineage_for("Rule 6.3(b)", BOND_LINEAGES)
        unmoved = [hop for hop in lineage.hops if not hop.moved]
        assert len(unmoved) == 1
        assert unmoved[0].amendment_id == DEATH_AMENDMENT.amendment_id
        assert "unchanged" in (unmoved[0].evidence or "")

    def test_the_chain_is_continuous(self):
        for lineage in BOND_LINEAGES:
            assert lineage.chain_is_continuous() == (True, None)

    def test_there_are_no_unaccounted_amendments(self):
        for lineage in BOND_LINEAGES:
            assert lineage.missing_windows(HISTORY) == ()


class TestCrimechargeIsADifferentCase:
    def test_the_payout_citation_was_already_wrong_at_issuance(self):
        lineage = lineage_for("Rule 6.3(b)", CRIMECHARGE_LINEAGES)
        assert not lineage.issuance_reference_valid
        resolution, authority = lineage.resolution(HISTORY)
        assert resolution is ReferenceResolution.BROKEN_REFERENCE_AT_ISSUANCE
        assert authority is not None
        assert "Scalar Contract" in authority

    def test_the_review_citation_was_also_already_wrong(self):
        lineage = lineage_for("Rule 6.3(d)", CRIMECHARGE_LINEAGES)
        resolution, _ = lineage.resolution(HISTORY)
        assert resolution is ReferenceResolution.BROKEN_REFERENCE_AT_ISSUANCE

    def test_broken_at_issuance_is_not_a_resolved_state(self):
        assert not ReferenceResolution.BROKEN_REFERENCE_AT_ISSUANCE.is_resolved

    def test_it_cannot_borrow_bonds_lineage(self):
        """The decisive rule: a citation wrong on the day it was written does
        not acquire a valid historical target from another product's template."""
        borrowed = lineage_for("Rule 6.3(b)", BOND_LINEAGES)
        crimecharge = lineage_for("Rule 6.3(b)", CRIMECHARGE_LINEAGES)
        assert borrowed.issuance_date < SCALAR_AMENDMENT.effective_date
        assert crimecharge.issuance_date > SCALAR_AMENDMENT.effective_date
        assert crimecharge.resolution(HISTORY)[0] is (
            ReferenceResolution.BROKEN_REFERENCE_AT_ISSUANCE
        )

    def test_hops_cannot_rescue_an_invalid_issuance(self):
        """Even handed a perfect chain, an invalid issuance still fails."""
        rescued = ReferenceLineage(
            source_document="CRIMECHARGE contract terms",
            citation_as_written="Rule 6.3(b)",
            source_name=EXCHANGE_RULEBOOK,
            issuance_date=date(2025, 7, 24),
            issuance_version="1.18",
            issuance_reference_valid=False,
            hops=lineage_for("Rule 6.3(b)", BOND_LINEAGES).hops,
            current_reference="Rule 6.3(c)",
        )
        assert rescued.resolution(HISTORY)[0] is (ReferenceResolution.BROKEN_REFERENCE_AT_ISSUANCE)

    def test_lookup_is_per_product(self):
        assert lineages_for_series("KXBOND") == BOND_LINEAGES
        assert lineages_for_series("KXFEDERALCHARGE") == CRIMECHARGE_LINEAGES
        assert lineages_for_series("KXRAIN") == ()


class TestGapsAndBrokenChains:
    def _amendment(self, identifier: str, effective: date) -> SourceAmendment:
        return SourceAmendment(
            source_name=EXCHANGE_RULEBOOK,
            amendment_id=identifier,
            url="https://cftc.test/a.pdf",
            document_sha256="d" * 64,
            effective_date=effective,
            affects=("Rule 6.3",),
        )

    def test_an_unaccounted_amendment_makes_the_lineage_unresolved(self):
        history = SourceHistory(
            source_name=EXCHANGE_RULEBOOK,
            versions=(
                SourceVersion(
                    source_name=EXCHANGE_RULEBOOK,
                    version="1.14",
                    effective_date=date(2024, 11, 27),
                    document_sha256="a" * 64,
                    url="https://cftc.test/v114.pdf",
                ),
            ),
            amendments=(
                SCALAR_AMENDMENT,
                DEATH_AMENDMENT,
                self._amendment("rules-unknown-2026", date(2026, 6, 1)),
            ),
            record_complete_from=date(2024, 11, 27),
        )
        lineage = lineage_for("Rule 6.3(b)", BOND_LINEAGES)
        resolution, authority = lineage.resolution(history)
        assert resolution is ReferenceResolution.UNKNOWN
        assert authority is not None
        assert "rules-unknown-2026" in authority

    def test_a_record_that_starts_after_issuance_cannot_be_trusted(self):
        history = SourceHistory(
            source_name=EXCHANGE_RULEBOOK,
            amendments=(SCALAR_AMENDMENT, DEATH_AMENDMENT),
            record_complete_from=date(2025, 6, 1),
        )
        resolution, authority = lineage_for("Rule 6.3(b)", BOND_LINEAGES).resolution(history)
        assert resolution is ReferenceResolution.UNKNOWN
        assert authority is not None
        assert "only complete from" in authority

    def test_a_record_with_no_completeness_date_cannot_be_trusted(self):
        history = SourceHistory(
            source_name=EXCHANGE_RULEBOOK, amendments=(SCALAR_AMENDMENT, DEATH_AMENDMENT)
        )
        resolution, _ = lineage_for("Rule 6.3(b)", BOND_LINEAGES).resolution(history)
        assert resolution is ReferenceResolution.UNKNOWN

    def test_a_chain_that_does_not_start_at_the_citation_is_refused(self):
        broken = ReferenceLineage(
            source_document="x",
            citation_as_written="Rule 6.3(b)",
            source_name=EXCHANGE_RULEBOOK,
            issuance_date=date(2025, 1, 17),
            issuance_reference_valid=True,
            hops=(
                LineageHop(
                    from_reference="Rule 6.3(d)",
                    to_reference="Rule 6.3(e)",
                    basis=HopBasis.TRACKED_AMENDMENT,
                    effective_date=SCALAR_AMENDMENT.effective_date,
                    amendment_id=SCALAR_AMENDMENT.amendment_id,
                    amendment_sha256=SCALAR_AMENDMENT.document_sha256,
                    evidence="mismatched",
                ),
            ),
        )
        assert broken.resolution(HISTORY)[0] is ReferenceResolution.UNKNOWN

    def test_a_chain_that_does_not_reach_the_stated_target_is_refused(self):
        broken = ReferenceLineage(
            source_document="x",
            citation_as_written="Rule 6.3(b)",
            source_name=EXCHANGE_RULEBOOK,
            issuance_date=date(2025, 1, 17),
            issuance_reference_valid=True,
            hops=lineage_for("Rule 6.3(b)", BOND_LINEAGES).hops,
            current_reference="Rule 6.3(z)",
        )
        assert broken.resolution(HISTORY)[0] is ReferenceResolution.UNKNOWN

    def test_a_hop_without_evidence_is_refused(self):
        with pytest.raises(ValueError, match="must quote the language"):
            LineageHop(
                from_reference="Rule 6.3(b)",
                to_reference="Rule 6.3(c)",
                basis=HopBasis.TRACKED_AMENDMENT,
                effective_date=date(2025, 3, 3),
                amendment_id="x",
                amendment_sha256="y",
            )

    def test_an_amendment_hop_without_the_amendment_is_refused(self):
        with pytest.raises(ValueError, match="must name the amendment"):
            LineageHop(
                from_reference="Rule 6.3(b)",
                to_reference="Rule 6.3(c)",
                basis=HopBasis.AMENDMENT_STATEMENT,
                effective_date=date(2025, 3, 3),
                evidence="something",
            )

    def test_a_version_bracket_cannot_establish_a_movement(self):
        """Comparing two copies can show a provision stayed put. It can never
        be the authority for it having moved."""
        with pytest.raises(ValueError, match="cannot establish a movement"):
            LineageHop(
                from_reference="Rule 6.3(b)",
                to_reference="Rule 6.3(c)",
                basis=HopBasis.VERSION_BRACKET,
                effective_date=date(2025, 3, 3),
                evidence="the prose moved",
            )


class TestHeadingStability:
    """Whole-rule citations resolve from objective evidence, with no human."""

    V114 = RULE_HEADINGS["1.14"]
    V129 = RULE_HEADINGS["1.29"]

    def _resolve(self, reference: str, issued: date = date(2025, 1, 17)) -> ResolutionOutcome:
        return resolve_reference(
            reference,
            source_name=EXCHANGE_RULEBOOK,
            issued_on=issued,
            history=HISTORY,
            issuance_headings=self.V114,
            current_headings=self.V129,
            source_held=True,
        )

    def test_rule_7_1_resolves_without_a_human(self):
        outcome = self._resolve("Rule 7.1")
        assert outcome.resolution is ReferenceResolution.EXACT_CURRENT_REFERENCE
        assert "MARKET OUTCOME REVIEW" in (outcome.authority or "")

    def test_rule_7_2_resolves_without_a_human(self):
        assert self._resolve("Rule 7.2").resolution is (ReferenceResolution.EXACT_CURRENT_REFERENCE)

    def _exhaustive(self) -> SourceHistory:
        return replace(HISTORY, versions_are_exhaustive=True)

    def _resolve_exhaustive(self, reference: str) -> ResolutionOutcome:
        return resolve_reference(
            reference,
            source_name=EXCHANGE_RULEBOOK,
            issued_on=date(2025, 1, 17),
            history=self._exhaustive(),
            issuance_headings=self.V114,
            current_headings=self.V129,
            source_held=True,
        )

    def test_a_reused_number_is_not_asserted_from_a_sampled_record(self):
        """Rule 3.6 reads differently now, but v1.15 is not held.

        Saying "the number was reused" would be claiming to know what the
        edition in force on the issuance date said. We do not hold it.
        """
        outcome = self._resolve("Rule 3.6")
        assert outcome.resolution is ReferenceResolution.UNKNOWN
        assert "sampled rather than exhaustive" in (outcome.authority or "")

    def test_a_reused_number_is_flagged_once_the_record_is_exhaustive(self):
        outcome = self._resolve_exhaustive("Rule 3.6")
        assert outcome.resolution is ReferenceResolution.AMBIGUOUS_LEGACY_REFERENCE
        assert "reused" in (outcome.authority or "")

    def test_another_reused_number_is_flagged(self):
        assert self._resolve_exhaustive("Rule 5.12").resolution is (
            ReferenceResolution.AMBIGUOUS_LEGACY_REFERENCE
        )

    def test_a_sampled_record_never_produces_a_negative_verdict(self):
        """Both negative verdicts are withheld, not just one."""
        for reference in ("Rule 3.6", "Rule 5.12", "Rule 5.16"):
            assert self._resolve(reference).resolution is ReferenceResolution.UNKNOWN

    def test_a_subsection_citation_is_never_resolved_by_heading(self):
        """Rule 6.3 is headed SETTLEMENT in every version ever filed."""
        assert self.V114["6.3"] == self.V129["6.3"] == "SETTLEMENT"
        assert self._resolve("Rule 6.3(b)").resolution is ReferenceResolution.UNKNOWN

    def test_a_rule_absent_from_the_current_book_is_broken(self):
        outcome = resolve_reference(
            "Rule 99.9",
            source_name=EXCHANGE_RULEBOOK,
            current_headings=self.V129,
            source_held=True,
        )
        assert outcome.resolution is ReferenceResolution.BROKEN_REFERENCE

    def test_a_rule_that_did_not_exist_at_issuance_is_broken_at_issuance(self):
        """Rule 5.16 is in v1.29 and was not in v1.14 -- stated only when the
        version record is exhaustive enough to support the claim."""
        assert "5.16" in self.V129
        assert "5.16" not in self.V114
        assert self._resolve_exhaustive("Rule 5.16").resolution is (
            ReferenceResolution.BROKEN_REFERENCE_AT_ISSUANCE
        )

    def test_a_positive_verdict_survives_a_sampled_record(self):
        """Heading stability across every held version is evidence even with gaps."""
        assert self._resolve("Rule 7.1").resolution is (ReferenceResolution.EXACT_CURRENT_REFERENCE)
        assert not HISTORY.versions_are_exhaustive

    def test_a_whole_document_citation_resolves_when_the_document_is_held(self):
        outcome = resolve_reference("the Rulebook", source_name=EXCHANGE_RULEBOOK, source_held=True)
        assert outcome.resolution is ReferenceResolution.EXACT_CURRENT_REFERENCE

    def test_a_whole_document_citation_is_unresolved_when_not_held(self):
        outcome = resolve_reference(
            "the Rulebook", source_name=EXCHANGE_RULEBOOK, source_held=False
        )
        assert outcome.resolution is ReferenceResolution.UNKNOWN

    def test_an_amendment_since_issuance_forces_a_lineage(self):
        """Even a whole-rule citation must yield if the rule was amended."""
        outcome = self._resolve("Rule 6.3")
        assert outcome.resolution is ReferenceResolution.UNKNOWN

    def test_resolution_falls_back_to_unknown_without_the_issuance_book(self):
        outcome = resolve_reference(
            "Rule 7.1",
            source_name=EXCHANGE_RULEBOOK,
            issued_on=date(2025, 1, 17),
            history=HISTORY,
            issuance_headings=None,
            current_headings=self.V129,
            source_held=True,
        )
        assert outcome.resolution is ReferenceResolution.UNKNOWN


class TestCitationParsing:
    def test_it_reads_a_rule_number(self):
        assert rule_number_of("Rule 6.3(b)") == "6.3"
        assert rule_number_of("RULE 7.1") == "7.1"

    def test_it_reads_a_subsection(self):
        assert subsection_of("Rule 6.3(b)") == "b"
        assert subsection_of("Rule 7.1") is None

    def test_a_bare_document_name_has_no_rule_number(self):
        assert rule_number_of("the Rulebook") is None

    def test_headings_come_from_the_body_not_the_contents_page(self):
        text = "RULE 6.3 SETTLEMENT 51\nsome body\nRULE 6.3 SETTLEMENT\n(a) ..."
        assert heading_for_rule(text, "6.3") == "SETTLEMENT"

    def test_headings_extract_in_bulk(self):
        text = (
            "RULE 7.1 THE MARKET OUTCOME REVIEW PROCESS\n(a) x\nRULE 7.2 CONTRACT MODIFICATIONS\n"
        )
        assert headings_from_text(text) == {
            "7.1": "THE MARKET OUTCOME REVIEW PROCESS",
            "7.2": "CONTRACT MODIFICATIONS",
        }

    def test_recorded_headings_are_available_per_version(self):
        assert headings_for_version("1.14") is RULE_HEADINGS["1.14"]
        assert headings_for_version("9.99") is None
