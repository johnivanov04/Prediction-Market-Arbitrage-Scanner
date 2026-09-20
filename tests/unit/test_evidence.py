"""Tests for evidence bundles, policies and completeness.

The recurring theme: a bundle that cannot support a claim must be *computed* as
incomplete, not left to the reviewer to notice.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from predarb.semantics.evidence import (
    DocumentRetrieval,
    EvidenceCompleteness,
    ExternalDocument,
    SettlementEvidenceBundle,
    TextExtraction,
    snapshot_id_for,
)
from predarb.semantics.fingerprint import ABSENT
from predarb.semantics.policy import (
    STANDARD_BINARY_COMPLEMENT_POLICY,
    CertificateClaim,
    DocumentRequirement,
    Requirement,
    policy_for,
)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)

MARKET = {
    "ticker": "MKT",
    "market_type": "binary",
    "settlement_kind": "BINARY",
    "rules_primary": "Resolves YES if the team wins.",
    "rules_secondary": "Void if the game is not played.",
    "rules_hash": "a" * 64,
    "notional_value": "1.0000",
    "yes_sub_title": "Team wins",
    "no_sub_title": "Team does not win",
    "can_close_early": True,
    "early_close_condition": ABSENT,
}
EVENT = {"captured": True, "mutually_exclusive": True, "collateral_return_type": "MECNET"}
SERIES = {"captured": True, "settlement_sources": [["Official league", "https://league.test"]]}


def document(**overrides: object) -> ExternalDocument:
    base: dict[str, object] = {
        "url": "https://kalshi.test/terms.pdf",
        "payload": b"%PDF-1.4 terms",
        "retrieved_at": T0,
        "http_status": 200,
        "content_type": "application/pdf",
        "extraction": TextExtraction.FAILED,
        "text": None,
    }
    base.update(overrides)
    return ExternalDocument.from_bytes(**base)  # type: ignore[arg-type]


def bundle(
    *,
    market: dict[str, object] | None = None,
    documents: dict[str, ExternalDocument] | None = None,
    captured_at: datetime = T0,
) -> SettlementEvidenceBundle:
    fields = {**MARKET, **(market or {})}
    docs = documents if documents is not None else {"contract_terms": document()}
    provisional = SettlementEvidenceBundle(
        snapshot_id="pending",
        market_ticker="MKT",
        event_ticker="EVT",
        series_ticker="SER",
        captured_at=captured_at,
        schema_version="test-evidence/1",
        market_fields=fields,
        event_fields=dict(EVENT),
        series_fields=dict(SERIES),
        documents=docs,
    )
    return SettlementEvidenceBundle(
        snapshot_id=snapshot_id_for(
            market_ticker="MKT",
            fingerprint=provisional.fingerprint(),
            captured_at=captured_at,
        ),
        market_ticker="MKT",
        event_ticker="EVT",
        series_ticker="SER",
        captured_at=captured_at,
        schema_version="test-evidence/1",
        market_fields=fields,
        event_fields=dict(EVENT),
        series_fields=dict(SERIES),
        documents=docs,
    )


class TestBundleFingerprinting:
    def test_the_same_evidence_fingerprints_identically(self):
        assert bundle().fingerprint().digest == bundle().fingerprint().digest

    def test_components_are_namespaced_by_layer(self):
        values = bundle().component_values()
        assert "market.rules_primary" in values
        assert "event.mutually_exclusive" in values
        assert "series.settlement_sources" in values

    def test_a_document_contributes_its_content_hash(self):
        values = bundle().component_values()
        assert values["document.contract_terms.sha256"] is not None
        assert values["document.contract_terms.retrieval"] == "RETRIEVED"

    def test_a_document_byte_change_changes_the_fingerprint(self):
        first = bundle()
        second = bundle(documents={"contract_terms": document(payload=b"%PDF-1.4 terms rev2")})
        assert first.fingerprint().digest != second.fingerprint().digest

    def test_an_unchanged_url_with_changed_bytes_still_drifts(self):
        """A URL is not evidence of its own content."""
        first = bundle()
        second = bundle(documents={"contract_terms": document(payload=b"different")})
        values_a, values_b = first.component_values(), second.component_values()
        assert values_a["document.contract_terms.url"] == values_b["document.contract_terms.url"]
        assert first.fingerprint().digest != second.fingerprint().digest

    def test_a_failed_fetch_is_itself_evidence(self):
        """ "We could not read the terms" must be visible, not silently absent."""
        missing = bundle(
            documents={
                "contract_terms": ExternalDocument.missing(
                    "https://kalshi.test/terms.pdf", DocumentRetrieval.HTTP_ERROR
                )
            }
        )
        values = missing.component_values()
        assert values["document.contract_terms.retrieval"] == "HTTP_ERROR"
        assert missing.fingerprint().digest != bundle().fingerprint().digest

    def test_a_rules_change_changes_the_fingerprint(self):
        drifted = bundle(market={"rules_primary": "Resolves YES if the team draws."})
        assert drifted.fingerprint().digest != bundle().fingerprint().digest

    def test_a_settlement_source_change_changes_the_fingerprint(self):
        original = bundle()
        drifted = SettlementEvidenceBundle(
            snapshot_id="x",
            market_ticker="MKT",
            event_ticker="EVT",
            series_ticker="SER",
            captured_at=T0,
            schema_version="test-evidence/1",
            market_fields=dict(MARKET),
            event_fields=dict(EVENT),
            series_fields={"captured": True, "settlement_sources": [["Other", "https://x.test"]]},
            documents={"contract_terms": document()},
        )
        assert drifted.fingerprint().digest != original.fingerprint().digest

    def test_snapshot_ids_differ_across_capture_times(self):
        """Two identical captures stay distinguishable records."""
        later = bundle(captured_at=datetime(2026, 9, 20, tzinfo=UTC))
        assert later.snapshot_id != bundle().snapshot_id
        assert later.fingerprint().digest == bundle().fingerprint().digest

    def test_the_rules_hash_is_exposed_for_audit(self):
        assert bundle().rules_hash == "a" * 64


class TestVolatileFieldsExcluded:
    def test_price_fields_are_not_in_the_bundle(self):
        values = bundle().component_values()
        for volatile in ("market.last_price", "market.yes_bid", "market.volume"):
            assert volatile not in values

    def test_the_policy_marks_them_ignored(self):
        policy = STANDARD_BINARY_COMPLEMENT_POLICY
        for volatile in (
            "market.last_price",
            "market.yes_bid",
            "market.volume",
            "market.open_interest",
        ):
            assert policy.requirement_for(volatile) is Requirement.IGNORED


class TestCompleteness:
    def test_a_full_bundle_is_complete(self):
        report = STANDARD_BINARY_COMPLEMENT_POLICY.assess(bundle())
        assert report.completeness is EvidenceCompleteness.COMPLETE
        assert report.permits_approval

    @pytest.mark.parametrize(
        "field",
        [
            "market.rules_primary",
            "market.notional_value",
            "market.market_type",
            "market.yes_sub_title",
        ],
    )
    def test_a_missing_required_field_blocks_approval(self, field):
        name = field.split(".", 1)[1]
        report = STANDARD_BINARY_COMPLEMENT_POLICY.assess(bundle(market={name: ABSENT}))
        assert report.completeness is EvidenceCompleteness.EVIDENCE_INCOMPLETE
        assert not report.permits_approval
        assert field in report.missing_required

    def test_a_null_required_field_also_blocks(self):
        """Present-but-null is not evidence of anything."""
        report = STANDARD_BINARY_COMPLEMENT_POLICY.assess(bundle(market={"rules_primary": None}))
        assert not report.permits_approval

    def test_the_report_says_what_is_missing(self):
        report = STANDARD_BINARY_COMPLEMENT_POLICY.assess(bundle(market={"rules_primary": ABSENT}))
        assert "market.rules_primary" in report.describe()

    def test_an_unreadable_document_is_flagged_for_manual_viewing(self):
        """A PDF we could not render must not be treated as read."""
        report = STANDARD_BINARY_COMPLEMENT_POLICY.assess(bundle())
        assert "contract_terms" in report.manual_viewing_required

    def test_a_readable_document_needs_no_acknowledgement(self):
        readable = bundle(
            documents={
                "contract_terms": document(
                    payload=b"plain terms",
                    content_type="text/plain",
                    extraction=TextExtraction.CLEAN,
                    text="plain terms",
                )
            }
        )
        report = STANDARD_BINARY_COMPLEMENT_POLICY.assess(readable)
        assert report.manual_viewing_required == {}

    def test_an_absent_governing_document_does_not_block(self):
        """Most Kalshi series publish no contract document at all."""
        report = STANDARD_BINARY_COMPLEMENT_POLICY.assess(bundle(documents={}))
        assert report.permits_approval

    def test_the_document_requirement_enum_has_three_states(self):
        assert {m.value for m in DocumentRequirement} == {
            "ABSENT",
            "PRESENT_BUT_OPTIONAL",
            "PRESENT_AND_REQUIRED",
        }


class TestPolicy:
    def test_categories_do_not_overlap(self):
        policy = STANDARD_BINARY_COMPLEMENT_POLICY
        assert not set(policy.required) & set(policy.optional)
        assert not set(policy.required) & set(policy.ignored)

    def test_every_required_component_has_a_rationale(self):
        """An inclusion nobody can justify is an inclusion nobody can argue with."""
        policy = STANDARD_BINARY_COMPLEMENT_POLICY
        for name in policy.required:
            assert name in policy.rationale, f"{name} is required but unjustified"

    def test_the_claim_states_its_own_proposition(self):
        text = CertificateClaim.STANDARD_BINARY_COMPLEMENT.proposition
        assert "YES payout + NO payout" in text
        assert "notional" in text

    def test_every_claim_has_a_policy(self):
        for claim in CertificateClaim:
            assert policy_for(claim).claim is claim

    def test_an_unknown_claim_has_no_policy(self):
        with pytest.raises(ValueError, match="no evidence policy"):
            policy_for("NOT_A_CLAIM")  # type: ignore[arg-type]


class TestExternalDocument:
    def test_a_retrieved_document_must_carry_a_content_hash(self):
        with pytest.raises(ValueError, match="must carry a content hash"):
            ExternalDocument(
                url="https://x.test",
                retrieval=DocumentRetrieval.RETRIEVED,
                retrieved_at=T0,
            )

    def test_a_missing_document_needs_no_hash(self):
        missing = ExternalDocument.missing("https://x.test", DocumentRetrieval.NETWORK_ERROR)
        assert missing.content_sha256 is None
        assert not missing.retrieval.is_usable

    def test_unreadable_extraction_requires_manual_viewing(self):
        assert document(extraction=TextExtraction.FAILED).requires_manual_viewing
        assert document(extraction=TextExtraction.PARTIAL).requires_manual_viewing

    def test_clean_extraction_does_not(self):
        assert not document(extraction=TextExtraction.CLEAN).requires_manual_viewing

    def test_an_unfetched_document_does_not_require_viewing(self):
        """There is nothing to view; the policy decides whether that blocks."""
        missing = ExternalDocument.missing(None, DocumentRetrieval.NO_URL_PUBLISHED)
        assert not missing.requires_manual_viewing

    def test_the_hash_is_over_the_bytes(self):
        payload = b"%PDF-1.4 terms"
        assert document(payload=payload).content_sha256 == hashlib.sha256(payload).hexdigest()


class TestDecimalEvidence:
    def test_trailing_zeros_are_preserved(self):
        """How the venue reported a number is itself evidence."""
        coarse = bundle(market={"notional_value": Decimal("1.00")})
        fine = bundle(market={"notional_value": Decimal("1.0000")})
        assert coarse.fingerprint().digest != fine.fingerprint().digest


class TestConditionalDocumentRequirement:
    """A governing document is conditional, not optional.

    Most Kalshi series publish none, so absence cannot block. But once the venue
    references a governing document, that document governs, and a review
    conducted without it is not a review of the contract. The failure this
    closes: "the field is globally OPTIONAL" quietly becoming "the document may
    fail to load and the certificate can still be issued".
    """

    POLICY = STANDARD_BINARY_COMPLEMENT_POLICY

    def test_no_url_published_is_still_complete(self):
        """Absence of a governing document is not missing evidence."""
        none_published = bundle(
            documents={
                "contract_terms": ExternalDocument.missing(
                    None, DocumentRetrieval.NO_URL_PUBLISHED
                ),
                "contract": ExternalDocument.missing(None, DocumentRetrieval.NO_URL_PUBLISHED),
            }
        )
        report = self.POLICY.assess(none_published)
        assert report.permits_approval
        assert report.unretrievable_documents == ()

    def test_no_documents_at_all_is_still_complete(self):
        assert self.POLICY.assess(bundle(documents={})).permits_approval

    def test_a_published_url_successfully_captured_is_complete(self):
        readable = bundle(
            documents={
                "contract_terms": document(
                    payload=b"plain terms",
                    content_type="text/plain",
                    extraction=TextExtraction.CLEAN,
                    text="plain terms",
                )
            }
        )
        report = self.POLICY.assess(readable)
        assert report.permits_approval
        assert report.manual_viewing_required == {}

    @pytest.mark.parametrize(
        "failure",
        [
            DocumentRetrieval.HTTP_ERROR,
            DocumentRetrieval.NETWORK_ERROR,
            DocumentRetrieval.NOT_ATTEMPTED,
        ],
    )
    def test_a_published_url_that_failed_to_fetch_is_incomplete(self, failure):
        """A governing document we could not read is missing evidence."""
        failed = bundle(
            documents={
                "contract_terms": ExternalDocument.missing("https://kalshi.test/terms.pdf", failure)
            }
        )
        report = self.POLICY.assess(failed)
        assert not report.permits_approval
        assert "contract_terms" in report.unretrievable_documents
        assert "governing document referenced but not retrieved" in report.describe()

    def test_a_retrieved_pdf_is_complete_but_needs_manual_viewing(self):
        report = self.POLICY.assess(bundle())
        assert report.permits_approval
        assert "contract_terms" in report.manual_viewing_required

    def test_the_manual_viewing_entry_carries_the_content_hash(self):
        """So an acknowledgement can be bound to the exact version reviewed."""
        source = bundle()
        report = self.POLICY.assess(source)
        expected = source.documents["contract_terms"].content_sha256
        assert report.manual_viewing_required["contract_terms"] == expected

    def test_the_requirement_verdict_is_recorded_per_document(self):
        with_url = self.POLICY.assess(bundle())
        assert with_url.document_requirements["contract_terms"] == "PRESENT_AND_REQUIRED"
        without = self.POLICY.assess(
            bundle(
                documents={
                    "contract_terms": ExternalDocument.missing(
                        None, DocumentRetrieval.NO_URL_PUBLISHED
                    )
                }
            )
        )
        assert without.document_requirements["contract_terms"] == "ABSENT"

    def test_the_policy_lists_documents_as_conditional_not_optional(self):
        assert self.POLICY.conditionally_required_documents == ("contract_terms", "contract")
        assert self.POLICY.optional_documents == ()
