"""Claim-scoped materiality: does source Y matter to claim C, cited *here*?

Separate from reference resolution, which asks whether citation X points at
source Y. A reference can be perfectly resolved and irrelevant, or material and
unidentifiable, and these tests exist largely to keep the two from collapsing
into one another.

The thing being prevented: the Rulebook cites twenty-nine federal regulations
about registration, capital, recordkeeping and filing procedure. Treating them
as part of the proof that YES + NO equals the notional blocked every market on
the exchange. The thing being prevented *by the other side* of the policy: a
blanket "CFTC regulations are procedural" rule that would wave through a future
Part 38 rule about settlement.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from predarb.semantics.dependency import (
    DependencyDiscovery,
    DependencyMateriality,
    GoverningDocumentDependency,
    PayoutImpact,
    ReferenceResolution,
)
from predarb.semantics.materiality import (
    CitationScope,
    ContextScope,
    DependencyMaterialityPolicy,
    MaterialityClass,
    MaterialityDecision,
    MaterialityDeclaration,
    ParentContext,
)
from predarb.venues.kalshi.governing_sources import CFTC_REGULATIONS, EXCHANGE_RULEBOOK
from predarb.venues.kalshi.materiality_declarations import (
    KALSHI_MATERIALITY_POLICY,
    MATERIALITY_POLICY_VERSION,
    PRODUCT_FILING_AUTHORITY,
)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 9, 27, tzinfo=UTC)
CLAIM = "STANDARD_BINARY_COMPLEMENT"
OTHER_CLAIM = "EVENT_EXHAUSTIVENESS"

FILING_CONTEXT = (
    "Pursuant to Section 5c(c) of the Commodity Exchange Act and Section 40.2(a) of "
    "the regulations of the Commodity Futures Trading Commission, KalshiEX LLC hereby "
    "notifies the Commission that it is self-certifying the Contract."
)
PAYOUT_CONTEXT = (
    "If an Expiration Value cannot be determined, payouts shall be determined pursuant "
    "to CFTC Regulation 40.2(a) and the Settlement Value allocated accordingly."
)


def citation(
    reference: str = "CFTC Regulation 40.2(a)",
    *,
    context: str = FILING_CONTEXT,
    source: str = CFTC_REGULATIONS,
    impacts: frozenset[PayoutImpact] = frozenset(),
) -> GoverningDocumentDependency:
    return GoverningDocumentDependency(
        parent="contract",
        reference=reference,
        source_name=source,
        payout_impacts=impacts,
        materiality=(
            DependencyMateriality.MATERIAL if impacts else DependencyMateriality.UNCLASSIFIED
        ),
        discovery=DependencyDiscovery.EXTRACTED_FROM_TEXT,
        citation_context=context,
    )


def declaration(
    *,
    claim: str = CLAIM,
    citations: tuple[str, ...] = ("CFTC Regulation 40.2(a)",),
    parents: tuple[ParentContext, ...] = (ParentContext.PRODUCT_CERTIFICATION_FILING,),
    source: str = CFTC_REGULATIONS,
) -> MaterialityDeclaration:
    return MaterialityDeclaration(
        claim=claim,
        citation_scope=CitationScope(source_name=source, citations=citations),
        context_scope=ContextScope(parents=parents),
        classification=MaterialityClass.PROCEDURAL_FOR_CLAIM,
        rationale="governs filing procedure, not settlement or payout",
        authority="17 CFR 40.2 'Listing products for trading by certification' (eCFR)",
        reviewer="test reviewer",
        reviewed_at=T0,
        policy_version="test-policy/1",
    )


def policy(*declarations: MaterialityDeclaration) -> DependencyMaterialityPolicy:
    return DependencyMaterialityPolicy(version="test-policy/1", declarations=declarations)


class TestClaimScopedMateriality:
    def test_a_matching_declaration_discharges_the_reference(self):
        decision = policy(declaration()).classify(
            citation(), claim=CLAIM, parent=ParentContext.PRODUCT_CERTIFICATION_FILING
        )
        assert decision.classification is MaterialityClass.PROCEDURAL_FOR_CLAIM
        assert not decision.blocks

    def test_the_decision_names_the_declaration_that_made_it(self):
        decision = policy(declaration()).classify(
            citation(), claim=CLAIM, parent=ParentContext.PRODUCT_CERTIFICATION_FILING
        )
        assert decision.declaration_id is not None
        assert decision.policy_version == "test-policy/1"
        assert decision.rationale

    def test_a_declaration_for_another_claim_does_not_apply(self):
        decision = policy(declaration(claim=OTHER_CLAIM)).classify(
            citation(), claim=CLAIM, parent=ParentContext.PRODUCT_CERTIFICATION_FILING
        )
        assert decision.classification is MaterialityClass.UNCLASSIFIED
        assert decision.blocks

    def test_unclassified_blocks_exactly_as_material_does(self):
        assert MaterialityClass.UNCLASSIFIED.blocks
        assert MaterialityClass.MATERIAL.blocks
        assert not MaterialityClass.PROCEDURAL_FOR_CLAIM.blocks


class TestContextScoping:
    def test_the_same_citation_in_a_payout_paragraph_is_not_discharged(self):
        """The declaration is for filing authority, not for a payout rule."""
        decision = policy(declaration()).classify(
            citation(context=PAYOUT_CONTEXT),
            claim=CLAIM,
            parent=ParentContext.PRODUCT_CERTIFICATION_FILING,
        )
        assert decision.classification is MaterialityClass.UNCLASSIFIED

    def test_the_same_citation_in_a_different_parent_is_not_discharged(self):
        decision = policy(declaration()).classify(
            citation(), claim=CLAIM, parent=ParentContext.CONTRACT_TERMS
        )
        assert decision.classification is MaterialityClass.UNCLASSIFIED

    def test_required_phrases_must_be_present(self):
        narrow = MaterialityDeclaration(
            claim=CLAIM,
            citation_scope=CitationScope(
                source_name=CFTC_REGULATIONS, citations=("CFTC Regulation 40.2(a)",)
            ),
            context_scope=ContextScope(
                parents=(ParentContext.PRODUCT_CERTIFICATION_FILING,),
                required_phrases=("self-certifying",),
            ),
            classification=MaterialityClass.PROCEDURAL_FOR_CLAIM,
            rationale="filing authority",
            authority="17 CFR 40.2",
            reviewer="test",
            reviewed_at=T0,
            policy_version="test-policy/1",
        )
        assert (
            policy(narrow)
            .classify(citation(), claim=CLAIM, parent=ParentContext.PRODUCT_CERTIFICATION_FILING)
            .classification
            is MaterialityClass.PROCEDURAL_FOR_CLAIM
        )
        assert (
            policy(narrow)
            .classify(
                citation(context="Filed under Section 40.2(a) of the regulations."),
                claim=CLAIM,
                parent=ParentContext.PRODUCT_CERTIFICATION_FILING,
            )
            .classification
            is MaterialityClass.UNCLASSIFIED
        )

    def test_payout_language_vetoes_any_declaration(self):
        """Even a declaration written for this exact parent cannot clear a
        citation whose sentence talks about the Settlement Value."""
        for phrase in ("the Settlement Value", "the payout", "an Expiration Value"):
            context = f"Filed pursuant to Section 40.2(a); {phrase} is determined thereby."
            decision = policy(declaration()).classify(
                citation(context=context),
                claim=CLAIM,
                parent=ParentContext.PRODUCT_CERTIFICATION_FILING,
            )
            assert decision.classification is MaterialityClass.UNCLASSIFIED, phrase


class TestMaterialityCannotBeDowngradedAway:
    def test_a_declared_payout_impact_is_never_discharged(self):
        decision = policy(declaration()).classify(
            citation(impacts=frozenset({PayoutImpact.PAYOUT_AMOUNT})),
            claim=CLAIM,
            parent=ParentContext.PRODUCT_CERTIFICATION_FILING,
        )
        assert decision.classification is MaterialityClass.MATERIAL

    def test_an_unknown_external_regulation_still_blocks(self):
        """A regulation nobody has reviewed is not cleared by its neighbours."""
        decision = KALSHI_MATERIALITY_POLICY.classify(
            citation("CFTC Regulation 99.9"),
            claim=CLAIM,
            parent=ParentContext.PRODUCT_CERTIFICATION_FILING,
        )
        assert decision.classification is MaterialityClass.UNCLASSIFIED
        assert decision.blocks

    def test_a_future_settlement_regulation_stays_material(self):
        """The reason there is no domain-level whitelist."""
        decision = KALSHI_MATERIALITY_POLICY.classify(
            citation(
                "CFTC Regulation 38.999",
                context="Settlement of contracts shall follow CFTC Regulation 38.999 payouts.",
                impacts=frozenset({PayoutImpact.PAYOUT_AMOUNT}),
            ),
            claim=CLAIM,
            parent=ParentContext.EXCHANGE_RULEBOOK,
        )
        assert decision.classification is MaterialityClass.MATERIAL

    def test_rulebook_references_are_not_covered_by_the_kalshi_policy(self):
        """The relaxation is for external legal citations, not Rule 6.3."""
        decision = KALSHI_MATERIALITY_POLICY.classify(
            citation("Rule 6.3(b)", source=EXCHANGE_RULEBOOK),
            claim=CLAIM,
            parent=ParentContext.CONTRACT_TERMS,
        )
        assert decision.blocks

    def test_a_scope_refuses_a_prefix(self):
        """Naming a part rather than its members would pre-approve a future rule."""
        with pytest.raises(ValueError, match="looks like a prefix"):
            CitationScope(source_name=CFTC_REGULATIONS, citations=("CFTC Regulation 40.",))

    def test_an_empty_scope_is_refused(self):
        with pytest.raises(ValueError, match="at least one citation"):
            CitationScope(source_name=CFTC_REGULATIONS, citations=())

    def test_a_procedural_declaration_must_record_its_authority(self):
        with pytest.raises(ValueError, match="must record its authority"):
            MaterialityDeclaration(
                claim=CLAIM,
                citation_scope=CitationScope(
                    source_name=CFTC_REGULATIONS, citations=("CFTC Regulation 40.2(a)",)
                ),
                context_scope=ContextScope(parents=(ParentContext.PRODUCT_CERTIFICATION_FILING,)),
                classification=MaterialityClass.PROCEDURAL_FOR_CLAIM,
                rationale="because",
                authority="",
                reviewer="test",
                reviewed_at=T0,
                policy_version="v1",
            )


class TestRevocation:
    def test_a_revoked_declaration_stops_applying(self):
        live = policy(declaration())
        revoked = live.without(declaration().declaration_id, at=T0, reason="superseded by review")
        assert (
            live.classify(
                citation(), claim=CLAIM, parent=ParentContext.PRODUCT_CERTIFICATION_FILING
            ).classification
            is MaterialityClass.PROCEDURAL_FOR_CLAIM
        )
        assert (
            revoked.classify(
                citation(), claim=CLAIM, parent=ParentContext.PRODUCT_CERTIFICATION_FILING
            ).classification
            is MaterialityClass.UNCLASSIFIED
        )

    def test_a_revoked_declaration_stays_on_the_record(self):
        revoked = policy(declaration()).without(
            declaration().declaration_id, at=T0, reason="superseded"
        )
        assert len(revoked.declarations) == 1
        assert revoked.declarations[0].revocation_reason == "superseded"
        assert not revoked.declarations[0].is_active

    def test_revocation_changes_the_dependency_fingerprint(self):
        """So evidence that relied on it becomes incomplete rather than
        silently meaning something different."""
        live = policy(declaration()).apply(
            citation(), claim=CLAIM, parent=ParentContext.PRODUCT_CERTIFICATION_FILING
        )
        revoked_policy = policy(declaration()).without(
            declaration().declaration_id, at=T0, reason="superseded"
        )
        revoked = revoked_policy.apply(
            citation(), claim=CLAIM, parent=ParentContext.PRODUCT_CERTIFICATION_FILING
        )
        assert live.fingerprint_values() != revoked.fingerprint_values()
        assert live.materiality is DependencyMateriality.PROCEDURAL
        assert revoked.materiality is DependencyMateriality.UNCLASSIFIED


class TestPolicyVersionFingerprinting:
    def test_the_policy_version_reaches_the_fingerprint(self):
        stamped = policy(declaration()).apply(
            citation(), claim=CLAIM, parent=ParentContext.PRODUCT_CERTIFICATION_FILING
        )
        values = stamped.fingerprint_values()
        assert any(v == "test-policy/1" for v in values.values())

    def test_a_different_policy_version_fingerprints_differently(self):
        first = policy(declaration()).apply(
            citation(), claim=CLAIM, parent=ParentContext.PRODUCT_CERTIFICATION_FILING
        )
        bumped = dataclasses.replace(declaration(), policy_version="test-policy/2")
        second = DependencyMaterialityPolicy(version="test-policy/2", declarations=(bumped,)).apply(
            citation(), claim=CLAIM, parent=ParentContext.PRODUCT_CERTIFICATION_FILING
        )
        assert first.fingerprint_values() != second.fingerprint_values()

    def test_the_declaration_id_reaches_the_fingerprint(self):
        stamped = policy(declaration()).apply(
            citation(), claim=CLAIM, parent=ParentContext.PRODUCT_CERTIFICATION_FILING
        )
        assert stamped.materiality_declaration_id is not None
        assert any(
            v == stamped.materiality_declaration_id for v in stamped.fingerprint_values().values()
        )


class TestOrderIndependence:
    def test_declaration_order_does_not_change_the_decision(self):
        other = declaration(citations=("CFTC Regulation 1.35",))
        forward = policy(declaration(), other)
        backward = policy(other, declaration())
        for built in (forward, backward):
            decision = built.classify(
                citation(), claim=CLAIM, parent=ParentContext.PRODUCT_CERTIFICATION_FILING
            )
            assert decision.declaration_id == declaration().declaration_id

    def test_declaration_order_does_not_change_the_fingerprint(self):
        other = declaration(citations=("CFTC Regulation 1.35",))
        forward = policy(declaration(), other).apply(
            citation(), claim=CLAIM, parent=ParentContext.PRODUCT_CERTIFICATION_FILING
        )
        backward = policy(other, declaration()).apply(
            citation(), claim=CLAIM, parent=ParentContext.PRODUCT_CERTIFICATION_FILING
        )
        assert forward.fingerprint_values() == backward.fingerprint_values()

    def test_a_scope_normalises_citation_order(self):
        first = CitationScope(source_name=CFTC_REGULATIONS, citations=("Rule 1.1", "Rule 2.2"))
        second = CitationScope(source_name=CFTC_REGULATIONS, citations=("Rule 2.2", "Rule 1.1"))
        assert first.citations == second.citations


class TestResolutionIsIndependentOfMateriality:
    def test_a_resolved_reference_can_be_procedural(self):
        resolved = GoverningDocumentDependency(
            parent="contract",
            reference="CFTC Regulation 40.2(a)",
            source_name=CFTC_REGULATIONS,
            citation_context=FILING_CONTEXT,
            reference_resolution=ReferenceResolution.EXACT_CURRENT_REFERENCE,
            resolution_authority="eCFR",
        )
        decision = policy(declaration()).classify(
            resolved, claim=CLAIM, parent=ParentContext.PRODUCT_CERTIFICATION_FILING
        )
        assert resolved.reference_resolution is ReferenceResolution.EXACT_CURRENT_REFERENCE
        assert decision.classification is MaterialityClass.PROCEDURAL_FOR_CLAIM

    def test_an_unresolved_reference_can_be_procedural(self):
        """Whether a citation resolves says nothing about whether it matters."""
        decision = policy(declaration()).classify(
            citation(), claim=CLAIM, parent=ParentContext.PRODUCT_CERTIFICATION_FILING
        )
        assert citation().reference_resolution.value == "UNKNOWN"
        assert decision.classification is MaterialityClass.PROCEDURAL_FOR_CLAIM

    def test_applying_the_policy_does_not_touch_resolution(self):
        stamped = policy(declaration()).apply(
            citation(), claim=CLAIM, parent=ParentContext.PRODUCT_CERTIFICATION_FILING
        )
        assert stamped.reference_resolution == citation().reference_resolution


class TestTheRecordedKalshiPolicy:
    def test_every_declaration_names_the_source_its_reviewer_read(self):
        """Federal citations cite the eCFR; the Rulebook one cites the Rulebook
        at an exact hash."""
        for declared in KALSHI_MATERIALITY_POLICY.declarations:
            if declared.citation_scope.source_name == CFTC_REGULATIONS:
                assert "eCFR" in declared.authority
                assert "17 CFR" in declared.authority
            else:
                assert "Rulebook" in declared.authority
                assert "sha256" in declared.authority

    def test_every_declaration_is_for_the_binary_complement_claim(self):
        assert all(d.claim == CLAIM for d in KALSHI_MATERIALITY_POLICY.declarations)

    def test_the_filing_declaration_is_scoped_to_certification_letters(self):
        assert PRODUCT_FILING_AUTHORITY.context_scope.parents == (
            ParentContext.PRODUCT_CERTIFICATION_FILING,
        )

    def test_the_policy_version_is_recorded(self):
        assert KALSHI_MATERIALITY_POLICY.version == MATERIALITY_POLICY_VERSION

    def test_only_the_fee_rule_touches_the_exchange_rulebook(self):
        """Rulebook rules stay strict; exactly one narrow exception exists."""
        rulebook = [
            d
            for d in KALSHI_MATERIALITY_POLICY.declarations
            if d.citation_scope.source_name == EXCHANGE_RULEBOOK
        ]
        assert [d.citation_scope.citations for d in rulebook] == [("rule-3-13",)]


class TestFeeRuleDeclaration:
    """Rule 3.13 is non-material to *terminal payoff*, and only when cited for fees."""

    def _citation(self, context: str) -> GoverningDocumentDependency:
        return GoverningDocumentDependency(
            parent="contract",
            reference="Rule 3.13",
            source_name=EXCHANGE_RULEBOOK,
            citation_context=context,
        )

    def _classify(
        self, context: str, parent: ParentContext = ParentContext.PRODUCT_CERTIFICATION_FILING
    ) -> MaterialityDecision:
        return KALSHI_MATERIALITY_POLICY.classify(
            self._citation(context), claim=CLAIM, parent=parent
        )

    def test_it_applies_when_cited_for_fees(self):
        decision = self._classify(
            "Members will be charged fees in accordance with Rule 3.13 of the Rulebook."
        )
        assert decision.classification is MaterialityClass.PROCEDURAL_FOR_CLAIM

    def test_it_applies_to_the_other_fee_phrasings(self):
        for phrase in (
            "The transaction fee is set by Rule 3.13.",
            "See the fee schedule in Rule 3.13.",
            "Trading charges follow Rule 3.13.",
        ):
            assert self._classify(phrase).classification is (
                MaterialityClass.PROCEDURAL_FOR_CLAIM
            ), phrase

    def test_it_does_not_apply_without_fee_language(self):
        decision = self._classify("Resolution follows Rule 3.13 of the Rulebook.")
        assert decision.classification is MaterialityClass.UNCLASSIFIED

    def test_the_payout_veto_still_wins(self):
        """Even with fee language present, payout language vetoes."""
        decision = self._classify(
            "Fees under Rule 3.13 are deducted from the Settlement Value payable."
        )
        assert decision.classification is MaterialityClass.UNCLASSIFIED

    def test_it_does_not_apply_in_the_rulebook_itself(self):
        decision = self._classify(
            "Members will be charged fees in accordance with Rule 3.13.",
            parent=ParentContext.EXCHANGE_RULEBOOK,
        )
        assert decision.classification is MaterialityClass.UNCLASSIFIED

    def test_it_does_not_apply_to_another_claim(self):
        decision = KALSHI_MATERIALITY_POLICY.classify(
            self._citation("Members will be charged fees in accordance with Rule 3.13."),
            claim=OTHER_CLAIM,
            parent=ParentContext.PRODUCT_CERTIFICATION_FILING,
        )
        assert decision.classification is MaterialityClass.UNCLASSIFIED

    def test_it_does_not_cover_a_neighbouring_rule_number(self):
        neighbour = GoverningDocumentDependency(
            parent="contract",
            reference="Rule 3.12",
            source_name=EXCHANGE_RULEBOOK,
            citation_context="Members will be charged fees in accordance with Rule 3.12.",
        )
        assert (
            KALSHI_MATERIALITY_POLICY.classify(
                neighbour, claim=CLAIM, parent=ParentContext.PRODUCT_CERTIFICATION_FILING
            ).classification
            is MaterialityClass.UNCLASSIFIED
        )

    def test_it_leaves_reference_resolution_untouched(self):
        """Non-material and unresolved are independent, and both stay visible."""
        stamped = KALSHI_MATERIALITY_POLICY.apply(
            self._citation("Members will be charged fees in accordance with Rule 3.13."),
            claim=CLAIM,
            parent=ParentContext.PRODUCT_CERTIFICATION_FILING,
        )
        assert stamped.materiality is DependencyMateriality.PROCEDURAL
        assert stamped.reference_resolution is ReferenceResolution.UNKNOWN
