"""Resolving -- or refusing to resolve -- a citation written against an older text.

Two questions that look like one and are not:

* **Which edition governs?** Settled for Kalshi: the Member Agreement binds a
  Member to the Rulebook "as supplemented or amended from time to time".
* **What does this citation point at?** Open. BOND's terms cite "Rule 6.3(b)"
  for payout determination, and Rule 6.3(b) is the Scalar Contract rule -- in
  the current Rulebook *and* in v1.18, the edition in force when the
  neighbouring CRIMECHARGE product was certified.

Dynamic binding does not repair a citation. It only means the target moves.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from predarb.semantics.dependency import (
    DependencyClosure,
    DependencyDiscovery,
    DependencyMateriality,
    DependencySet,
    GoverningDocumentDependency,
    PayoutImpact,
    ReferenceResolution,
    VersionBinding,
)
from predarb.semantics.evidence import (
    EvidenceCompleteness,
    ExternalDocument,
    SettlementEvidenceBundle,
    TextExtraction,
    snapshot_id_for,
)
from predarb.semantics.policy import CertificateClaim, policy_for
from predarb.semantics.product_terms import (
    AmendmentSearch,
    ProductTermsVersion,
    ProductTermsVersionHistory,
)
from predarb.venues.kalshi.governing_sources import EXCHANGE_RULEBOOK
from predarb.venues.kalshi.product_filings import (
    BOND_HISTORY,
    CRIMECHARGE_HISTORY,
    history_for_series,
)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
POLICY = policy_for(CertificateClaim.STANDARD_BINARY_COMPLEMENT)

MARKET = {
    "ticker": "MKT",
    "market_type": "binary",
    "settlement_kind": "BINARY",
    "rules_primary": "Resolves YES if the team wins.",
    "notional_value": "1.0000",
    "yes_sub_title": "Team wins",
    "no_sub_title": "Team does not win",
}

# The Member Agreement's operative words, quoted verbatim.
MEMBER_AGREEMENT_BINDING = (
    "You will be bound by, and comply with, the rules and regulations established by "
    "Kalshi applicable to the Services contained in the Kalshi rules (as supplemented "
    "or amended from time to time, the “Kalshi Rulebook”). In the event of any "
    "conflict between this Agreement and the Kalshi Rulebook, the Kalshi Rulebook will "
    "govern."
)


def source(payload: bytes = b"%PDF-1.4 rulebook") -> ExternalDocument:
    return ExternalDocument.from_bytes(
        url="https://kalshi.test/rulebook.pdf",
        payload=payload,
        retrieved_at=T0,
        http_status=200,
        content_type="application/pdf",
        extraction=TextExtraction.CLEAN,
        text="rulebook text",
    )


def citation(
    reference: str = "Rule 6.3(b)",
    *,
    resolution: ReferenceResolution = ReferenceResolution.UNKNOWN,
    authority: str | None = None,
    resolved_to: str | None = None,
    payload: bytes = b"%PDF-1.4 rulebook",
) -> GoverningDocumentDependency:
    return GoverningDocumentDependency(
        parent="contract_terms",
        reference=reference,
        source_name=EXCHANGE_RULEBOOK,
        source=source(payload),
        payout_impacts=frozenset({PayoutImpact.PAYOUT_AMOUNT}),
        materiality=DependencyMateriality.MATERIAL,
        discovery=DependencyDiscovery.EXTRACTED_FROM_TEXT,
        version_binding=VersionBinding.AS_AMENDED_FROM_TIME_TO_TIME,
        reference_resolution=resolution,
        resolution_authority=authority,
        resolved_reference=resolved_to,
    )


def bundle_with(*dependencies: GoverningDocumentDependency) -> SettlementEvidenceBundle:
    closures = {
        "contract_terms": DependencySet(
            parent="contract_terms",
            closure=DependencyClosure.ENUMERATED,
            dependencies=dependencies,
        ),
        "market.rules_primary": DependencySet(
            parent="market.rules_primary", closure=DependencyClosure.ENUMERATED
        ),
    }
    common = {
        "market_ticker": "MKT",
        "event_ticker": "EVT",
        "series_ticker": "SER",
        "captured_at": T0,
        "schema_version": "test-evidence/1",
        "market_fields": dict(MARKET),
        "event_fields": {"captured": True},
        "series_fields": {"captured": True},
        "documents": {"contract_terms": source(b"terms")},
        "dependencies": closures,
    }
    provisional = SettlementEvidenceBundle(snapshot_id="pending", **common)  # type: ignore[arg-type]
    return SettlementEvidenceBundle(
        snapshot_id=snapshot_id_for(
            market_ticker="MKT", fingerprint=provisional.fingerprint(), captured_at=T0
        ),
        **common,  # type: ignore[arg-type]
    )


class TestACitationIsNeverSilentlyRepointed:
    def test_the_written_citation_survives_resolution(self):
        resolved = citation(
            resolution=ReferenceResolution.HISTORICAL_REFERENCE_RESOLVED,
            authority="Rulebook v1.19 redline, CFTC filing rules0101261234",
            resolved_to="Rule 6.3(c)",
        )
        assert resolved.reference == "Rule 6.3(b)"
        assert resolved.resolved_reference == "Rule 6.3(c)"

    def test_naming_a_target_while_unresolved_is_refused(self):
        """No path exists that rewrites 6.3(b) to 6.3(c) without authority."""
        with pytest.raises(ValueError, match="never silently"):
            citation(resolved_to="Rule 6.3(c)")

    def test_resolving_without_naming_the_authority_is_refused(self):
        with pytest.raises(ValueError, match="prose similarity is research evidence"):
            GoverningDocumentDependency(
                parent="contract_terms",
                reference="Rule 6.3(b)",
                source_name=EXCHANGE_RULEBOOK,
                source=source(),
                reference_resolution=ReferenceResolution.HISTORICAL_REFERENCE_RESOLVED,
            )

    def test_an_unresolved_citation_defaults_to_unknown(self):
        assert citation().reference_resolution is ReferenceResolution.UNKNOWN

    def test_unknown_is_not_a_resolved_state(self):
        assert not ReferenceResolution.UNKNOWN.is_resolved
        assert not ReferenceResolution.AMBIGUOUS_LEGACY_REFERENCE.is_resolved
        assert not ReferenceResolution.BROKEN_REFERENCE.is_resolved

    def test_the_three_resolved_states_are_resolved(self):
        assert ReferenceResolution.EXACT_CURRENT_REFERENCE.is_resolved
        assert ReferenceResolution.HISTORICAL_REFERENCE_RESOLVED.is_resolved
        assert ReferenceResolution.REFERENCE_AMENDED_BY_PRODUCT_FILING.is_resolved


class TestUnresolvedLegacyReferenceBlocks:
    def test_an_unresolved_material_citation_is_incomplete(self):
        report = POLICY.assess(bundle_with(citation()))
        assert report.completeness is EvidenceCompleteness.EVIDENCE_INCOMPLETE
        assert any("rule-6-3-b" in key for key in report.unresolved_citations)

    def test_holding_the_rulebook_does_not_discharge_it(self):
        """The source is held; it is the citation that is unclear."""
        report = POLICY.assess(bundle_with(citation()))
        assert report.unheld_dependency_sources == ()
        assert report.unresolved_citations != ()

    def test_an_ambiguous_legacy_reference_still_blocks(self):
        report = POLICY.assess(
            bundle_with(citation(resolution=ReferenceResolution.AMBIGUOUS_LEGACY_REFERENCE))
        )
        assert report.completeness is EvidenceCompleteness.EVIDENCE_INCOMPLETE

    def test_a_broken_reference_blocks(self):
        report = POLICY.assess(
            bundle_with(citation(resolution=ReferenceResolution.BROKEN_REFERENCE))
        )
        assert report.completeness is EvidenceCompleteness.EVIDENCE_INCOMPLETE

    def test_an_authoritatively_resolved_citation_does_not_block(self):
        report = POLICY.assess(
            bundle_with(
                citation(
                    resolution=ReferenceResolution.EXACT_CURRENT_REFERENCE,
                    authority="Rulebook v1.29 Rule 6.3(c), read at source",
                )
            )
        )
        assert report.completeness is EvidenceCompleteness.COMPLETE

    def test_the_reason_distinguishes_a_bad_citation_from_a_missing_document(self):
        report = POLICY.assess(bundle_with(citation()))
        assert "does not resolve to a known provision" in report.describe()

    def test_the_resolution_is_fingerprinted(self):
        """Re-pointing a citation changes what was approved, even with no new bytes."""
        unresolved = bundle_with(citation()).fingerprint()
        resolved = bundle_with(
            citation(
                resolution=ReferenceResolution.EXACT_CURRENT_REFERENCE,
                authority="read at source",
            )
        ).fingerprint()
        assert not unresolved.matches(resolved)


class TestProductAmendmentsResolve:
    def _amended_history(self, product_key: str) -> ProductTermsVersionHistory:
        return ProductTermsVersionHistory(
            product_key=product_key,
            versions=(
                ProductTermsVersion(
                    product_key=product_key,
                    product_name=f"{product_key} contract",
                    filing_date=date(2025, 1, 17),
                    regulation="40.2(a)",
                    source_url="https://kalshi.test/initial.pdf",
                    document_sha256="a" * 64,
                    contingency_citations=("Rule 6.3(d)", "Rule 6.3(b)"),
                ),
                ProductTermsVersion(
                    product_key=product_key,
                    product_name=f"{product_key} contract",
                    filing_date=date(2026, 3, 1),
                    regulation="40.6",
                    source_url="https://kalshi.test/amended.pdf",
                    document_sha256="b" * 64,
                    changed_sections=("Contingencies",),
                    contingency_citations=("Rule 7.1",),
                ),
            ),
        )

    def test_a_product_specific_amendment_resolves_its_own_citation(self):
        history = self._amended_history("KXBOND")
        resolution, authority, target = history.resolution_for(citation(), product_key="KXBOND")
        assert resolution is ReferenceResolution.REFERENCE_AMENDED_BY_PRODUCT_FILING
        assert authority is not None
        assert "40.6" in authority
        assert target == "Rule 7.1"

    def test_an_unrelated_products_amendment_resolves_nothing(self):
        """A rainfall amendment says nothing about a James Bond contract."""
        history = self._amended_history("KXRAIN")
        resolution, authority, target = history.resolution_for(citation(), product_key="KXBOND")
        assert resolution is ReferenceResolution.UNKNOWN
        assert authority is None
        assert target is None

    def test_a_history_refuses_a_version_from_another_product(self):
        with pytest.raises(ValueError, match="is not history for another"):
            ProductTermsVersionHistory(
                product_key="KXBOND",
                versions=(
                    ProductTermsVersion(
                        product_key="KXRAIN",
                        product_name="rain",
                        filing_date=date(2026, 3, 1),
                        regulation="40.6",
                        source_url="https://kalshi.test/rain.pdf",
                        document_sha256="c" * 64,
                    ),
                ),
            )

    def test_an_initial_listing_alone_resolves_nothing(self):
        history = ProductTermsVersionHistory(
            product_key="KXBOND",
            versions=(
                ProductTermsVersion(
                    product_key="KXBOND",
                    product_name="bond",
                    filing_date=date(2025, 1, 17),
                    regulation="40.2(a)",
                    source_url="https://kalshi.test/initial.pdf",
                    document_sha256="a" * 64,
                    contingency_citations=("Rule 6.3(b)",),
                ),
            ),
        )
        resolution, _, _ = history.resolution_for(citation(), product_key="KXBOND")
        assert resolution is ReferenceResolution.UNKNOWN

    def test_a_no_amendment_search_must_name_what_was_searched(self):
        """Otherwise it is indistinguishable from never having looked."""
        with pytest.raises(ValueError, match="must name the sources searched"):
            AmendmentSearch(product_key="KXBOND", outcome=AmendmentSearch.NO_AMENDMENT_FOUND)

    def test_never_having_looked_is_distinguishable_from_having_found_nothing(self):
        assert history_for_series("KXNEVERRESEARCHED") is None
        assert BOND_HISTORY.search is not None
        assert BOND_HISTORY.search.outcome == AmendmentSearch.NO_AMENDMENT_FOUND


class TestRecordedKalshiHistories:
    def test_bond_records_its_initial_listing(self):
        version = BOND_HISTORY.latest_filed
        assert version is not None
        assert version.filing_date == date(2025, 1, 17)
        assert version.regulation == "40.2(a)"
        assert version.cites("Rule 6.3(b)")

    def test_crimecharge_records_its_initial_listing(self):
        version = CRIMECHARGE_HISTORY.latest_filed
        assert version is not None
        assert version.filing_date == date(2025, 7, 24)
        assert version.cites("Rule 6.3(d)")

    def test_neither_product_has_a_recorded_amendment(self):
        assert BOND_HISTORY.amendments() == ()
        assert CRIMECHARGE_HISTORY.amendments() == ()

    def test_neither_history_resolves_the_legacy_citation(self):
        for history, key in ((BOND_HISTORY, "KXBOND"), (CRIMECHARGE_HISTORY, "KXFEDERALCHARGE")):
            resolution, _, _ = history.resolution_for(citation(), product_key=key)
            assert resolution is ReferenceResolution.UNKNOWN

    def test_lookup_is_by_series_ticker(self):
        assert history_for_series("KXBOND") is BOND_HISTORY
        assert history_for_series(None) is None


class TestMemberAgreementBinding:
    def _member_agreement(self, payload: bytes) -> DependencySet:
        return DependencySet(
            parent="member_agreement",
            closure=DependencyClosure.ENUMERATED,
            dependencies=(
                GoverningDocumentDependency(
                    parent="member_agreement",
                    reference="the Kalshi Rulebook",
                    source_name=EXCHANGE_RULEBOOK,
                    source=source(payload),
                    payout_impacts=frozenset({PayoutImpact.PAYOUT_AMOUNT}),
                    materiality=DependencyMateriality.MATERIAL,
                    discovery=DependencyDiscovery.EXTRACTED_FROM_TEXT,
                    version_binding=VersionBinding.AS_AMENDED_FROM_TIME_TO_TIME,
                    citation_context=MEMBER_AGREEMENT_BINDING,
                ),
            ),
        )

    def test_the_binding_language_is_carried_as_evidence(self):
        dependency = self._member_agreement(b"v1").dependencies[0]
        assert dependency.citation_context is not None
        assert "as supplemented or amended from time to time" in dependency.citation_context

    def test_it_records_dynamic_binding(self):
        dependency = self._member_agreement(b"v1").dependencies[0]
        assert dependency.version_binding is VersionBinding.AS_AMENDED_FROM_TIME_TO_TIME

    def test_dynamic_binding_means_an_amendment_requires_review(self):
        assert VersionBinding.AS_AMENDED_FROM_TIME_TO_TIME.amendment_requires_review

    def test_a_changed_member_agreement_is_evidence_drift(self):
        """A change to the amendment language itself must be visible.

        This is why the Member Agreement is a node in the graph rather than a
        conclusion written into a comment.
        """
        before = self._member_agreement(b"rulebook v1.29").fingerprint_values()
        after = self._member_agreement(b"rulebook v1.30").fingerprint_values()
        assert before != after

    def test_the_binding_is_part_of_the_fingerprint(self):
        values = self._member_agreement(b"v1").fingerprint_values()
        assert any(
            value == VersionBinding.AS_AMENDED_FROM_TIME_TO_TIME.value for value in values.values()
        )

    def test_dynamic_binding_does_not_make_the_citation_resolved(self):
        dependency = self._member_agreement(b"v1").dependencies[0]
        assert dependency.version_binding is VersionBinding.AS_AMENDED_FROM_TIME_TO_TIME
        assert dependency.reference_resolution is ReferenceResolution.UNKNOWN
        assert not dependency.is_resolved
