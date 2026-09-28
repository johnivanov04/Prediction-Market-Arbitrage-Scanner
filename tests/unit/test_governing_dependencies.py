"""Incorporation by reference: a governing document is a node, not a leaf.

The failure these tests exist to prevent, stated once: two markets were marked
EVIDENCE COMPLETE because their contract-terms PDF had been *fetched*, while the
terms handed payout determination for undeterminable outcomes to a Rulebook rule
nobody had fetched or read.

Each test below is an attempt to recreate some version of that.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime

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
    reference_slug,
)
from predarb.semantics.evidence import (
    DocumentRetrieval,
    EvidenceCompleteness,
    ExternalDocument,
    SettlementEvidenceBundle,
    TextExtraction,
    snapshot_id_for,
)
from predarb.semantics.incorporation import (
    DeclarationStore,
    DependencyDeclaration,
    classify_citation,
    dependency_set_for_document,
    scan_incorporations,
)
from predarb.semantics.policy import CertificateClaim, policy_for
from predarb.semantics.review import ReviewRequest, ReviewStatus
from predarb.venues.kalshi.governing_sources import (
    EXCHANGE_RULEBOOK,
    KALSHI_CITATION_GRAMMAR,
)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
CLAIM = CertificateClaim.STANDARD_BINARY_COMPLEMENT
POLICY = policy_for(CLAIM)

# Quoted verbatim from Kalshi's published contract terms. This exact sentence
# appears in every contract-terms document examined in the Phase-1 acceptance
# pass, and it is the whole reason this module exists.
INCORPORATION_CLAUSE = (
    "Contingencies: Before Settlement, Kalshi may, at its sole discretion, initiate "
    "the Market Outcome Review Process pursuant to Rule 6.3(d) of the Rulebook. If an "
    "Expiration Value cannot be determined on the Expiration Date, Kalshi has the right "
    "to determine payouts pursuant to Rule 6.3(b) in the Rulebook."
)

MARKET = {
    "ticker": "MKT",
    "market_type": "binary",
    "settlement_kind": "BINARY",
    "rules_primary": "Resolves YES if the team wins.",
    "notional_value": "1.0000",
    "yes_sub_title": "Team wins",
    "no_sub_title": "Team does not win",
}


def terms_document(text: str | None = "plain terms", payload: bytes = b"terms") -> ExternalDocument:
    return ExternalDocument.from_bytes(
        url="https://kalshi.test/terms",
        payload=payload,
        retrieved_at=T0,
        http_status=200,
        content_type="text/plain",
        extraction=TextExtraction.CLEAN if text else TextExtraction.FAILED,
        text=text,
    )


def rulebook(payload: bytes = b"%PDF-1.4 rulebook v1.29") -> ExternalDocument:
    return ExternalDocument.from_bytes(
        url="https://kalshi.test/rulebook.pdf",
        payload=payload,
        retrieved_at=T0,
        http_status=200,
        content_type="application/pdf",
        extraction=TextExtraction.FAILED,
        text=None,
    )


def scanned(text: str | None, sources: dict[str, ExternalDocument] | None = None) -> DependencySet:
    return dependency_set_for_document(
        "contract_terms",
        terms_document(text),
        grammar=KALSHI_CITATION_GRAMMAR,
        sources=sources,
    )


def resolve_all(dependency_set: DependencySet) -> DependencySet:
    """Mark every citation in a set as authoritatively resolved.

    Used by tests about *other* rules, so the citation-resolution gate does not
    mask what they are checking. Resolution has its own tests below.
    """
    return replace(
        dependency_set,
        dependencies=tuple(
            replace(
                dependency,
                reference_resolution=ReferenceResolution.EXACT_CURRENT_REFERENCE,
                resolution_authority="test fixture",
                dependencies=tuple(
                    replace(
                        nested,
                        reference_resolution=ReferenceResolution.EXACT_CURRENT_REFERENCE,
                        resolution_authority="test fixture",
                    )
                    for nested in dependency.dependencies
                ),
            )
            for dependency in dependency_set.dependencies
        ),
    )


def bundle(
    *,
    dependencies: dict[str, DependencySet] | None = None,
    documents: dict[str, ExternalDocument] | None = None,
) -> SettlementEvidenceBundle:
    docs = documents if documents is not None else {"contract_terms": terms_document()}
    closures = dict(dependencies or {})
    closures.setdefault(
        "market.rules_primary",
        DependencySet(parent="market.rules_primary", closure=DependencyClosure.ENUMERATED),
    )
    for name, document in docs.items():
        closures.setdefault(name, DependencySet.for_document(name, document))
    common = {
        "market_ticker": "MKT",
        "event_ticker": "EVT",
        "series_ticker": "SER",
        "captured_at": T0,
        "schema_version": "test-evidence/1",
        "market_fields": dict(MARKET),
        "event_fields": {"captured": True},
        "series_fields": {"captured": True},
        "documents": docs,
        "dependencies": closures,
    }
    provisional = SettlementEvidenceBundle(snapshot_id="pending", **common)  # type: ignore[arg-type]
    return SettlementEvidenceBundle(
        snapshot_id=snapshot_id_for(
            market_ticker="MKT", fingerprint=provisional.fingerprint(), captured_at=T0
        ),
        **common,  # type: ignore[arg-type]
    )


class TestScanningRealContractLanguage:
    """The scanner has to find what a human found by opening the PDF."""

    def _found(self) -> dict[str, GoverningDocumentDependency]:
        return {
            d.reference: d
            for d in scan_incorporations(
                INCORPORATION_CLAUSE, parent="contract_terms", grammar=KALSHI_CITATION_GRAMMAR
            )
        }

    def test_it_finds_the_payout_determination_rule(self):
        assert "Rule 6.3(b)" in self._found()

    def test_it_finds_the_outcome_review_rule(self):
        assert "Rule 6.3(d)" in self._found()

    def test_the_payout_rule_is_material_to_the_payout_amount(self):
        dependency = self._found()["Rule 6.3(b)"]
        assert dependency.materiality is DependencyMateriality.MATERIAL
        assert PayoutImpact.PAYOUT_AMOUNT in dependency.payout_impacts

    def test_it_records_the_sentence_that_incorporates(self):
        """So a reviewer can check the classification instead of trusting it."""
        context = self._found()["Rule 6.3(b)"].citation_context or ""
        assert "cannot be determined" in context

    def test_it_points_at_the_rulebook_as_the_source(self):
        assert self._found()["Rule 6.3(b)"].source_name == EXCHANGE_RULEBOOK

    def test_the_scanner_can_never_clear_a_reference(self):
        """It may raise concern and may never lower it.

        A scanner that could mark a rule procedural would be a scanner whose
        bugs silently approve certificates.
        """
        for text in ("See Rule 1.1.", INCORPORATION_CLAUSE, "Refer to Rule 9.9(z) generally."):
            for dependency in scan_incorporations(
                text, parent="contract_terms", grammar=KALSHI_CITATION_GRAMMAR
            ):
                assert dependency.materiality is not DependencyMateriality.PROCEDURAL

    def test_an_unremarkable_citation_is_unclassified_not_cleared(self):
        materiality, impacts = classify_citation("Members shall comply with Rule 1.1.")
        assert materiality is DependencyMateriality.UNCLASSIFIED
        assert not impacts
        assert materiality.blocks_until_read

    def test_one_citation_written_two_ways_is_one_dependency(self):
        assert reference_slug("the Rulebook") == reference_slug("Rulebook")
        assert reference_slug("Rule 6.3(b)") == reference_slug("rule 6.3 (b)")


class TestMaterialRuleMustBeRetrieved:
    """Task: an incorporated material rule that is missing => incomplete."""

    def test_an_unretrieved_payout_rule_blocks(self):
        report = POLICY.assess(
            bundle(dependencies={"contract_terms": scanned(INCORPORATION_CLAUSE)})
        )
        assert report.completeness is EvidenceCompleteness.EVIDENCE_INCOMPLETE
        assert any("rule-6-3-b" in key for key in report.unresolved_dependencies)

    def test_the_reason_names_the_rule_rather_than_just_failing(self):
        report = POLICY.assess(
            bundle(dependencies={"contract_terms": scanned(INCORPORATION_CLAUSE)})
        )
        assert "incorporated rule governs the payout" in report.describe()

    def test_retrieving_the_rulebook_holds_the_source_for_every_citation(self):
        """One fetch, not one per citation."""
        held = scanned(INCORPORATION_CLAUSE, {EXCHANGE_RULEBOOK: rulebook()})
        assert all(d.source_is_held for d in held.flatten())

    def test_holding_the_source_is_not_enough_while_the_citation_is_unresolved(self):
        """A rulebook in hand does not help if the citation points elsewhere.

        This is the BOND case exactly: we hold Rulebook v1.29 and the terms
        cite "Rule 6.3(b)", which in v1.29 is the Scalar Contract rule.
        """
        report = POLICY.assess(
            bundle(
                dependencies={
                    "contract_terms": scanned(INCORPORATION_CLAUSE, {EXCHANGE_RULEBOOK: rulebook()})
                }
            )
        )
        assert any("rule-6-3-b" in key for key in report.unresolved_dependencies)

    def test_holding_the_source_and_resolving_the_citation_discharges_it(self):
        report = POLICY.assess(
            bundle(
                dependencies={
                    "contract_terms": resolve_all(
                        scanned(INCORPORATION_CLAUSE, {EXCHANGE_RULEBOOK: rulebook()})
                    )
                }
            )
        )
        assert report.unresolved_dependencies == ()

    def test_a_review_request_cannot_be_approved_while_a_rule_is_missing(self):
        request = ReviewRequest.create(
            bundle=bundle(dependencies={"contract_terms": scanned(INCORPORATION_CLAUSE)}),
            claim=CLAIM,
            generated_at=T0,
        )
        assert request.status is ReviewStatus.EVIDENCE_INCOMPLETE
        assert not request.permits_approval

    def test_a_document_with_no_citations_is_complete(self):
        """The rule must not block markets whose terms incorporate nothing."""
        report = POLICY.assess(bundle(dependencies={"contract_terms": scanned("plain terms")}))
        assert report.completeness is EvidenceCompleteness.COMPLETE


class TestProceduralReferencesDoNotPoison:
    """Task: an irrelevant procedural reference must not block a claim."""

    def _procedural(self, *, resolved: bool) -> DependencySet:
        return DependencySet(
            parent="contract_terms",
            closure=DependencyClosure.ENUMERATED,
            dependencies=(
                GoverningDocumentDependency(
                    parent="contract_terms",
                    reference="Rule 40.2(a)",
                    source_name=EXCHANGE_RULEBOOK,
                    source=rulebook() if resolved else None,
                    materiality=DependencyMateriality.PROCEDURAL,
                    discovery=DependencyDiscovery.DECLARED_BY_REVIEWER,
                    rationale=(
                        "CFTC self-certification filing procedure; governs how the "
                        "contract was listed, not what it pays"
                    ),
                ),
            ),
        )

    def test_an_unretrieved_procedural_reference_does_not_block(self):
        report = POLICY.assess(
            bundle(dependencies={"contract_terms": self._procedural(resolved=False)})
        )
        assert report.completeness is EvidenceCompleteness.COMPLETE
        assert report.unresolved_dependencies == ()

    def test_it_is_still_recorded_so_the_downgrade_is_auditable(self):
        source = bundle(dependencies={"contract_terms": self._procedural(resolved=False)})
        assert any("rule-40-2-a" in key for key in source.component_values())

    def test_downgrading_without_a_reason_is_refused(self):
        """An unexplained downgrade is indistinguishable from an oversight."""
        with pytest.raises(ValueError, match="requires a rationale"):
            GoverningDocumentDependency(
                parent="contract_terms",
                reference="Rule 40.2(a)",
                source_name=EXCHANGE_RULEBOOK,
                materiality=DependencyMateriality.PROCEDURAL,
            )

    def test_a_payout_bearing_rule_cannot_be_called_procedural(self):
        with pytest.raises(ValueError, match="not procedural"):
            GoverningDocumentDependency(
                parent="contract_terms",
                reference="Rule 6.3(b)",
                source_name=EXCHANGE_RULEBOOK,
                payout_impacts=frozenset({PayoutImpact.PAYOUT_AMOUNT}),
                materiality=DependencyMateriality.PROCEDURAL,
                rationale="claimed harmless",
            )

    def test_unclassified_is_not_a_middle_ground(self):
        """Nobody looked is not the same as somebody cleared it."""
        unclassified = GoverningDocumentDependency(
            parent="contract_terms",
            reference="Rule 1.1",
            source_name=EXCHANGE_RULEBOOK,
        )
        assert unclassified.is_material_to(POLICY.material_payout_impacts)


class TestUnknownClosureFailsClosed:
    """The actual Phase-1 blocker: an unreadable PDF is not an empty one."""

    def test_an_unreadable_governing_document_is_not_complete(self):
        report = POLICY.assess(
            bundle(
                documents={"contract_terms": terms_document(None, b"%PDF-1.4 terms")},
                dependencies={},
            )
        )
        assert report.completeness is EvidenceCompleteness.EVIDENCE_INCOMPLETE
        assert "contract_terms" in report.unknown_dependency_closures

    def test_a_bundle_captured_before_incorporation_was_modelled_is_not_complete(self):
        """Silence in an old snapshot is unknown, never nothing.

        This is exactly the state the two acceptance-pass markets were in.
        """
        source = bundle(documents={"contract_terms": terms_document(None, b"%PDF-1.4 terms")})
        stripped = SettlementEvidenceBundle(
            snapshot_id=source.snapshot_id,
            market_ticker=source.market_ticker,
            event_ticker=source.event_ticker,
            series_ticker=source.series_ticker,
            captured_at=source.captured_at,
            schema_version=source.schema_version,
            market_fields=source.market_fields,
            event_fields=source.event_fields,
            series_fields=source.series_fields,
            documents=source.documents,
        )
        assert stripped.dependencies == {}
        report = POLICY.assess(stripped)
        assert report.completeness is EvidenceCompleteness.EVIDENCE_INCOMPLETE
        assert "contract_terms" in report.unknown_dependency_closures

    def test_an_unfetched_document_cannot_have_its_closure_enumerated(self):
        derived = DependencySet.for_document(
            "contract_terms",
            ExternalDocument.missing("https://kalshi.test/terms", DocumentRetrieval.HTTP_ERROR),
        )
        assert derived.closure is DependencyClosure.UNKNOWN

    def test_a_document_that_was_never_published_incorporates_nothing(self):
        derived = DependencySet.for_document("contract_terms", None)
        assert derived.closure is DependencyClosure.NOT_APPLICABLE
        assert derived.closure.is_established

    def test_a_readable_document_with_no_citations_is_enumerated_not_unknown(self):
        assert scanned("plain terms").closure is DependencyClosure.ENUMERATED


class TestReviewerDeclarationBindsToTheExactHash:
    """Task: a manual-view acknowledgement binds the exact dependency hash."""

    def _declaration(self, document: ExternalDocument) -> DependencyDeclaration:
        assert document.content_sha256 is not None
        return DependencyDeclaration(
            parent="contract_terms",
            document_sha256=document.content_sha256,
            declared_by="reviewer@example.test",
            declared_at=T0,
            references=(
                GoverningDocumentDependency(
                    parent="contract_terms",
                    reference="Rule 6.3(b)",
                    source_name=EXCHANGE_RULEBOOK,
                    payout_impacts=frozenset({PayoutImpact.PAYOUT_AMOUNT}),
                    materiality=DependencyMateriality.MATERIAL,
                    discovery=DependencyDiscovery.DECLARED_BY_REVIEWER,
                ),
            ),
        )

    def test_a_declaration_makes_an_unreadable_document_enumerable(self):
        document = terms_document(None, b"%PDF-1.4 terms")
        store = DeclarationStore((self._declaration(document),))
        derived = dependency_set_for_document(
            "contract_terms",
            document,
            grammar=KALSHI_CITATION_GRAMMAR,
            sources={EXCHANGE_RULEBOOK: rulebook()},
            declarations=store,
        )
        assert derived.closure is DependencyClosure.ENUMERATED
        assert [d.reference for d in derived.flatten()] == ["Rule 6.3(b)"]

    def test_it_does_not_carry_over_to_an_amended_document(self):
        """The whole point: nobody has read the new version."""
        store = DeclarationStore((self._declaration(terms_document(None, b"%PDF-1.4 rev1")),))
        derived = dependency_set_for_document(
            "contract_terms",
            terms_document(None, b"%PDF-1.4 rev2"),
            grammar=KALSHI_CITATION_GRAMMAR,
            declarations=store,
        )
        assert derived.closure is DependencyClosure.UNKNOWN

    def test_a_declaration_cannot_claim_to_be_a_scan(self):
        with pytest.raises(ValueError, match="human declaration must say so"):
            DependencyDeclaration(
                parent="contract_terms",
                document_sha256="f" * 64,
                declared_by="reviewer@example.test",
                declared_at=T0,
                references=(
                    GoverningDocumentDependency(
                        parent="contract_terms",
                        reference="Rule 6.3(b)",
                        source_name=EXCHANGE_RULEBOOK,
                        payout_impacts=frozenset({PayoutImpact.PAYOUT_AMOUNT}),
                        materiality=DependencyMateriality.MATERIAL,
                        discovery=DependencyDiscovery.EXTRACTED_FROM_TEXT,
                    ),
                ),
            )

    def test_a_declaration_must_name_a_full_hash(self):
        with pytest.raises(ValueError, match="full content hash"):
            DependencyDeclaration(
                parent="contract_terms",
                document_sha256="abc123",
                declared_by="reviewer@example.test",
                declared_at=T0,
                references=(),
            )

    def test_an_unreadable_rulebook_must_be_acknowledged_by_hash(self):
        """The rulebook is a PDF too, and cannot be read on the reviewer's behalf."""
        source = rulebook()
        report = POLICY.assess(
            bundle(
                dependencies={
                    "contract_terms": resolve_all(
                        scanned(INCORPORATION_CLAUSE, {EXCHANGE_RULEBOOK: source})
                    )
                }
            )
        )
        assert report.completeness is EvidenceCompleteness.COMPLETE
        keys = [k for k in report.manual_viewing_required if k.startswith("dependency:")]
        assert keys, "the incorporated rulebook must require acknowledgement"
        assert all(report.manual_viewing_required[k] == source.content_sha256 for k in keys)

    def test_the_rulebook_acknowledgement_is_keyed_apart_from_the_terms(self):
        """Acknowledging the terms must never stand in for the rulebook."""
        report = POLICY.assess(
            bundle(
                documents={"contract_terms": terms_document(None, b"%PDF-1.4 terms")},
                dependencies={
                    "contract_terms": DependencySet(
                        parent="contract_terms",
                        closure=DependencyClosure.ENUMERATED,
                        dependencies=(
                            GoverningDocumentDependency(
                                parent="contract_terms",
                                reference="Rule 6.3(b)",
                                source_name=EXCHANGE_RULEBOOK,
                                source=rulebook(),
                                payout_impacts=frozenset({PayoutImpact.PAYOUT_AMOUNT}),
                                materiality=DependencyMateriality.MATERIAL,
                                discovery=DependencyDiscovery.DECLARED_BY_REVIEWER,
                                reference_resolution=(ReferenceResolution.EXACT_CURRENT_REFERENCE),
                                resolution_authority="test fixture",
                            ),
                        ),
                    )
                },
            )
        )
        assert "contract_terms" in report.manual_viewing_required
        assert "dependency:contract_terms/exchange_rulebook/rule-6-3-b" in (
            report.manual_viewing_required
        )


class TestDependencyDriftIsStaleness:
    """Task: a change to a relied-upon rulebook participates in staleness."""

    def _with(self, payload: bytes) -> SettlementEvidenceBundle:
        return bundle(
            dependencies={
                "contract_terms": scanned(
                    INCORPORATION_CLAUSE, {EXCHANGE_RULEBOOK: rulebook(payload)}
                )
            }
        )

    def test_an_amended_rulebook_changes_the_evidence_fingerprint(self):
        before = self._with(b"%PDF-1.4 rulebook v1.29")
        after = self._with(b"%PDF-1.4 rulebook v1.30")
        assert not before.fingerprint().matches(after.fingerprint())

    def test_the_drift_names_the_rule_that_moved(self):
        diff = self._with(b"v1.29").fingerprint().diff(self._with(b"v1.30").fingerprint())
        assert any("rule-6-3-b" in name for name in diff.changed)

    def test_the_same_rulebook_is_not_drift(self):
        assert self._with(b"same").fingerprint().matches(self._with(b"same").fingerprint())

    def test_unknown_version_semantics_require_review_on_amendment(self):
        """We never established which Rulebook version the terms bind to.

        Under ``AS_AMENDED`` an amendment changed the contract; under
        ``AS_OF_ISSUANCE`` we must still be able to produce the superseded text
        we actually relied on. Both need a human, so UNKNOWN requires review.
        """
        assert VersionBinding.UNKNOWN.amendment_requires_review
        assert VersionBinding.AS_AMENDED_FROM_TIME_TO_TIME.amendment_requires_review
        assert not VersionBinding.AS_OF_ISSUANCE.amendment_requires_review

    def test_the_rulebook_binds_dynamically_per_the_member_agreement(self):
        """Settled by the Member Agreement's own words, not by inference."""
        rulebook_patterns = [
            p for p in KALSHI_CITATION_GRAMMAR.patterns if p.source_name == EXCHANGE_RULEBOOK
        ]
        assert rulebook_patterns
        assert all(
            p.version_binding is VersionBinding.AS_AMENDED_FROM_TIME_TO_TIME
            for p in rulebook_patterns
        )

    def test_dynamic_binding_does_not_resolve_a_citation(self):
        """The two axes are independent, and this is the whole point.

        Knowing that the current Rulebook governs says nothing about which
        provision "Rule 6.3(b)" was pointing at.
        """
        found = scan_incorporations(
            INCORPORATION_CLAUSE, parent="contract_terms", grammar=KALSHI_CITATION_GRAMMAR
        )
        assert all(d.reference_resolution is ReferenceResolution.UNKNOWN for d in found)


class TestVersionsAreNotConflated:
    """Task: an old and a current Rulebook must never be treated as one.

    Real case: the contract terms cite "Rule 6.3(b)" for payout determination,
    but in Rulebook v1.29 that provision is Rule 6.3(c) -- 6.3(b) is the Scalar
    Contract rule. Silently mapping one onto the other would be asserting a
    legal interpretation.
    """

    def _cited(self, reference: str, payload: bytes, version: str) -> GoverningDocumentDependency:
        return GoverningDocumentDependency(
            parent="contract_terms",
            reference=reference,
            source_name=EXCHANGE_RULEBOOK,
            source=rulebook(payload),
            payout_impacts=frozenset({PayoutImpact.PAYOUT_AMOUNT}),
            materiality=DependencyMateriality.MATERIAL,
            discovery=DependencyDiscovery.DECLARED_BY_REVIEWER,
            source_version=version,
        )

    def test_the_citation_is_preserved_exactly_as_written(self):
        """The numbering is evidence about which edition the terms were drafted
        against, so it is never normalised away."""
        found = scan_incorporations(
            INCORPORATION_CLAUSE, parent="contract_terms", grammar=KALSHI_CITATION_GRAMMAR
        )
        assert "Rule 6.3(b)" in {d.reference for d in found}
        assert "Rule 6.3(c)" not in {d.reference for d in found}

    def test_two_rulebook_versions_fingerprint_differently(self):
        old = self._cited("Rule 6.3(b)", b"rulebook 1.28", "1.28")
        new = self._cited("Rule 6.3(b)", b"rulebook 1.29", "1.29")
        assert old.fingerprint_values() != new.fingerprint_values()

    def test_the_version_label_alone_is_enough_to_differ(self):
        """Same bytes relabelled is still a different claim about what governs."""
        a = self._cited("Rule 6.3(b)", b"same bytes", "1.28")
        b = self._cited("Rule 6.3(b)", b"same bytes", "1.29")
        assert a.fingerprint_values() != b.fingerprint_values()

    def test_different_rule_numbers_are_different_dependencies(self):
        a = self._cited("Rule 6.3(b)", b"rulebook", "1.29")
        b = self._cited("Rule 6.3(c)", b"rulebook", "1.29")
        assert a.key != b.key
        assert a.dependency_id != b.dependency_id


class TestNestedDependencies:
    """Task: nested dependencies cannot silently disappear."""

    def _nested(self, *, inner_resolved: bool) -> DependencySet:
        inner = GoverningDocumentDependency(
            parent="contract_terms",
            reference="Rule 7.2",
            source_name="contract_modification_notice",
            source=rulebook(b"notice") if inner_resolved else None,
            payout_impacts=frozenset({PayoutImpact.CONTRACT_MODIFICATION}),
            materiality=DependencyMateriality.MATERIAL,
            discovery=DependencyDiscovery.DECLARED_BY_REVIEWER,
        )
        outer = GoverningDocumentDependency(
            parent="contract_terms",
            reference="Rule 6.3(b)",
            source_name=EXCHANGE_RULEBOOK,
            source=rulebook(),
            payout_impacts=frozenset({PayoutImpact.PAYOUT_AMOUNT}),
            materiality=DependencyMateriality.MATERIAL,
            discovery=DependencyDiscovery.DECLARED_BY_REVIEWER,
            dependencies=(inner,),
        )
        return DependencySet(
            parent="contract_terms",
            closure=DependencyClosure.ENUMERATED,
            dependencies=(outer,),
        )

    def test_a_missing_nested_source_blocks_just_as_a_top_level_one_does(self):
        report = POLICY.assess(
            bundle(dependencies={"contract_terms": self._nested(inner_resolved=False)})
        )
        assert report.completeness is EvidenceCompleteness.EVIDENCE_INCOMPLETE
        assert any("rule-7-2" in key for key in report.unresolved_dependencies)

    def test_resolving_the_nested_source_unblocks(self):
        report = POLICY.assess(
            bundle(dependencies={"contract_terms": resolve_all(self._nested(inner_resolved=True))})
        )
        assert report.completeness is EvidenceCompleteness.COMPLETE

    def test_a_nested_source_is_fingerprinted(self):
        values = bundle(
            dependencies={"contract_terms": self._nested(inner_resolved=True)}
        ).component_values()
        assert any("rule-7-2" in name for name in values)

    def test_flatten_reaches_every_depth(self):
        assert len(self._nested(inner_resolved=True).flatten()) == 2

    def test_a_deeper_chain_is_not_truncated(self):
        depth = 4
        node = GoverningDocumentDependency(
            parent="p", reference="Rule 0.0", source_name="s0", source=rulebook()
        )
        for level in range(1, depth):
            node = GoverningDocumentDependency(
                parent="p",
                reference=f"Rule {level}.0",
                source_name=f"s{level}",
                source=rulebook(),
                dependencies=(node,),
            )
        assert len(tuple(node.flatten())) == depth


class TestDependencySetIntegrity:
    def test_a_set_refuses_a_dependency_belonging_to_another_parent(self):
        with pytest.raises(ValueError, match="names parent"):
            DependencySet(
                parent="contract_terms",
                closure=DependencyClosure.ENUMERATED,
                dependencies=(
                    GoverningDocumentDependency(
                        parent="contract", reference="Rule 6.3(b)", source_name=EXCHANGE_RULEBOOK
                    ),
                ),
            )

    def test_a_set_refuses_the_same_citation_twice(self):
        duplicate = GoverningDocumentDependency(
            parent="contract_terms", reference="Rule 6.3(b)", source_name=EXCHANGE_RULEBOOK
        )
        with pytest.raises(ValueError, match="duplicate citation"):
            DependencySet(
                parent="contract_terms",
                closure=DependencyClosure.ENUMERATED,
                dependencies=(duplicate, duplicate),
            )

    def test_an_absent_document_cannot_carry_dependencies(self):
        with pytest.raises(ValueError, match="incorporates nothing"):
            DependencySet(
                parent="contract_terms",
                closure=DependencyClosure.NOT_APPLICABLE,
                dependencies=(
                    GoverningDocumentDependency(
                        parent="contract_terms",
                        reference="Rule 6.3(b)",
                        source_name=EXCHANGE_RULEBOOK,
                    ),
                ),
            )

    def test_a_human_may_list_references_in_a_document_still_unreadable(self):
        """Partial knowledge must not upgrade the closure."""
        partial = DependencySet(
            parent="contract_terms",
            closure=DependencyClosure.UNKNOWN,
            dependencies=(
                GoverningDocumentDependency(
                    parent="contract_terms",
                    reference="Rule 6.3(b)",
                    source_name=EXCHANGE_RULEBOOK,
                    source=rulebook(),
                ),
            ),
        )
        assert not partial.closure.is_established
        assert POLICY.assess(bundle(dependencies={"contract_terms": partial})).completeness is (
            EvidenceCompleteness.EVIDENCE_INCOMPLETE
        )


class TestSerialisationRoundTrip:
    def test_a_dependency_set_survives_storage(self):
        original = scanned(INCORPORATION_CLAUSE, {EXCHANGE_RULEBOOK: rulebook()})
        revived = DependencySet.from_payload(original.payload())
        assert revived == original

    def test_a_reloaded_bundle_fingerprints_identically(self):
        """Otherwise a stored snapshot could not be issued against its own review."""
        source = bundle(
            dependencies={
                "contract_terms": scanned(INCORPORATION_CLAUSE, {EXCHANGE_RULEBOOK: rulebook()})
            }
        )
        revived = {
            name: DependencySet.from_payload(dependency_set.payload())
            for name, dependency_set in source.dependencies.items()
        }
        reloaded = SettlementEvidenceBundle(
            snapshot_id=source.snapshot_id,
            market_ticker=source.market_ticker,
            event_ticker=source.event_ticker,
            series_ticker=source.series_ticker,
            captured_at=source.captured_at,
            schema_version=source.schema_version,
            market_fields=source.market_fields,
            event_fields=source.event_fields,
            series_fields=source.series_fields,
            documents=source.documents,
            dependencies=revived,
        )
        assert reloaded.fingerprint().matches(source.fingerprint())

    def test_a_declaration_survives_storage(self, tmp_path):
        document = terms_document(None, b"%PDF-1.4 terms")
        assert document.content_sha256 is not None
        store = DeclarationStore(
            (
                DependencyDeclaration(
                    parent="contract_terms",
                    document_sha256=document.content_sha256,
                    declared_by="reviewer@example.test",
                    declared_at=T0,
                    references=(
                        GoverningDocumentDependency(
                            parent="contract_terms",
                            reference="Rule 6.3(b)",
                            source_name=EXCHANGE_RULEBOOK,
                            payout_impacts=frozenset({PayoutImpact.PAYOUT_AMOUNT}),
                            materiality=DependencyMateriality.MATERIAL,
                            discovery=DependencyDiscovery.DECLARED_BY_REVIEWER,
                        ),
                    ),
                ),
            )
        )
        path = tmp_path / "declarations.json"
        store.write(path)
        assert DeclarationStore.load(path).lookup("contract_terms", document) is not None

    def test_an_absent_declaration_file_is_an_empty_store(self, tmp_path):
        assert DeclarationStore.load(tmp_path / "nope.json").declarations == ()


class TestNoCredentialMaterialLeaks:
    """Dependency records carry public document metadata and nothing else."""

    def test_a_dependency_payload_has_no_unexpected_keys(self):
        payload = GoverningDocumentDependency(
            parent="contract_terms",
            reference="Rule 6.3(b)",
            source_name=EXCHANGE_RULEBOOK,
            source=rulebook(),
        ).payload()
        assert set(payload) == {
            "parent",
            "reference",
            "source_name",
            "source",
            "payout_impacts",
            "materiality",
            "discovery",
            "version_binding",
            "source_version",
            "reference_resolution",
            "resolution_authority",
            "resolved_reference",
            "current_text_excerpt",
            "materiality_declaration_id",
            "materiality_policy_version",
            "citation_context",
            "rationale",
            "dependencies",
        }

    def test_document_bytes_are_never_inlined(self):
        payload = rulebook(b"secret-looking bytes").payload()
        assert "secret-looking" not in repr(payload)
        assert payload["content_sha256"] == hashlib.sha256(b"secret-looking bytes").hexdigest()


class TestStaleAcknowledgementAcrossTime:
    def test_a_rulebook_amended_after_issuance_is_visible_as_drift(self):
        issued = bundle(
            dependencies={
                "contract_terms": scanned(
                    INCORPORATION_CLAUSE, {EXCHANGE_RULEBOOK: rulebook(b"v1.29")}
                )
            }
        )
        later = bundle(
            dependencies={
                "contract_terms": scanned(
                    INCORPORATION_CLAUSE, {EXCHANGE_RULEBOOK: rulebook(b"v1.30")}
                )
            }
        )
        diff = issued.fingerprint().diff(later.fingerprint())
        assert diff.has_drift
        assert all("rule-6-3" in name or "rulebook" in name for name in diff.changed)
