"""Review, issuance, registry and drift.

The property under test throughout: a certificate exists only when a human
approved *this* claim against *this* evidence, and stops applying the moment the
evidence moves.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from predarb.cli.semantics import _bundle_from_snapshot, _fingerprint
from predarb.domain.money import Price
from predarb.semantics import registry as registry_module
from predarb.semantics.certificate import CertificateStatus
from predarb.semantics.evidence import (
    DocumentRetrieval,
    ExternalDocument,
    SettlementEvidenceBundle,
    TextExtraction,
    snapshot_id_for,
)
from predarb.semantics.fingerprint import ABSENT
from predarb.semantics.policy import CertificateClaim
from predarb.semantics.registry import (
    CertificateApplicability,
    CertificateRecord,
    CertificateRegistry,
    IssuanceError,
    drift_report,
    issue_certificate,
)
from predarb.semantics.review import (
    STANDARD_BINARY_COMPLEMENT_CHECKLIST,
    ChecklistAnswer,
    Decision,
    ReviewDecision,
    ReviewRequest,
    ReviewStatus,
)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
CLAIM = CertificateClaim.STANDARD_BINARY_COMPLEMENT
DOLLAR = Price.from_value("1.0000")

MARKET = {
    "ticker": "MKT",
    "market_type": "binary",
    "settlement_kind": "BINARY",
    "rules_primary": "Resolves YES if the team wins.",
    "rules_hash": "a" * 64,
    "notional_value": "1.0000",
    "yes_sub_title": "Team wins",
    "no_sub_title": "Team does not win",
}


def bundle(
    *, market: dict[str, object] | None = None, readable: bool = True
) -> SettlementEvidenceBundle:
    fields = {**MARKET, **(market or {})}
    documents = {
        "contract_terms": ExternalDocument.from_bytes(
            url="https://kalshi.test/terms",
            payload=b"terms",
            retrieved_at=T0,
            http_status=200,
            content_type="text/plain",
            extraction=TextExtraction.CLEAN if readable else TextExtraction.FAILED,
            text="terms" if readable else None,
        )
    }
    provisional = SettlementEvidenceBundle(
        snapshot_id="pending",
        market_ticker="MKT",
        event_ticker="EVT",
        series_ticker="SER",
        captured_at=T0,
        schema_version="test-evidence/1",
        market_fields=fields,
        event_fields={"captured": True},
        series_fields={"captured": True},
        documents=documents,
    )
    return SettlementEvidenceBundle(
        snapshot_id=snapshot_id_for(
            market_ticker="MKT", fingerprint=provisional.fingerprint(), captured_at=T0
        ),
        market_ticker="MKT",
        event_ticker="EVT",
        series_ticker="SER",
        captured_at=T0,
        schema_version="test-evidence/1",
        market_fields=fields,
        event_fields={"captured": True},
        series_fields={"captured": True},
        documents=documents,
    )


def request_for(source: SettlementEvidenceBundle | None = None) -> ReviewRequest:
    return ReviewRequest.create(bundle=source or bundle(), claim=CLAIM, generated_at=T0)


def all_safe_answers() -> dict[str, ChecklistAnswer]:
    """Answer every question the way the claim requires."""
    return {q.key: q.safe_answers[0] for q in STANDARD_BINARY_COMPLEMENT_CHECKLIST}


def decision_for(
    request: ReviewRequest,
    *,
    decision: Decision = Decision.APPROVED,
    answers: dict[str, ChecklistAnswer] | None = None,
    acknowledged: dict[str, str] | None = None,
    reviewer: str = "researcher",
) -> ReviewDecision:
    return ReviewDecision(
        request_id=request.request_id,
        claim=request.claim,
        market_ticker=request.market_ticker,
        snapshot_id=request.snapshot_id,
        evidence_fingerprint=request.evidence_fingerprint,
        decision=decision,
        reviewer=reviewer,
        reviewed_at=T0,
        checklist_answers=all_safe_answers() if answers is None else answers,
        external_evidence_acknowledged=acknowledged or {},
    )


class TestReviewRequest:
    def test_a_complete_bundle_awaits_review(self):
        assert request_for().status is ReviewStatus.AWAITING_REVIEW

    def test_an_incomplete_bundle_is_flagged_and_unapprovable(self):
        incomplete = request_for(bundle(market={"rules_primary": ABSENT}))
        assert incomplete.status is ReviewStatus.EVIDENCE_INCOMPLETE
        assert not incomplete.permits_approval

    def test_the_request_carries_the_checklist(self):
        assert request_for().checklist == STANDARD_BINARY_COMPLEMENT_CHECKLIST

    def test_the_request_id_is_derived_from_claim_and_evidence(self):
        """The same evidence and claim always produce the same request."""
        assert request_for().request_id == request_for().request_id

    def test_different_evidence_gives_a_different_request(self):
        other = request_for(bundle(market={"rules_primary": "Different."}))
        assert other.request_id != request_for().request_id


class TestDecisionGating:
    def test_an_approval_with_full_safe_answers_permits_issuance(self):
        request = request_for()
        assert decision_for(request).blocking_reason(request) is None

    @pytest.mark.parametrize("decision", [Decision.REJECTED, Decision.NEEDS_MORE_EVIDENCE])
    def test_other_decisions_do_not(self, decision):
        request = request_for()
        reason = decision_for(request, decision=decision).blocking_reason(request)
        assert reason is not None
        assert decision.value in reason

    def test_an_unanswered_question_blocks(self):
        request = request_for()
        answers = all_safe_answers()
        del answers["void_or_refund"]
        reason = decision_for(request, answers=answers).blocking_reason(request)
        assert reason is not None
        assert "void_or_refund" in reason

    def test_an_unsafe_answer_blocks(self):
        """The claim is a conjunction; one "yes, it can void" defeats it."""
        request = request_for()
        answers = all_safe_answers()
        answers["void_or_refund"] = ChecklistAnswer.YES
        reason = decision_for(request, answers=answers).blocking_reason(request)
        assert reason is not None
        assert "block this claim" in reason

    def test_uncertainty_blocks_rather_than_passing(self):
        request = request_for()
        answers = all_safe_answers()
        answers["all_outcomes_enumerated"] = ChecklistAnswer.UNCERTAIN
        assert decision_for(request, answers=answers).blocking_reason(request) is not None

    def test_incomplete_evidence_blocks_even_a_willing_reviewer(self):
        request = request_for(bundle(market={"rules_primary": ABSENT}))
        reason = decision_for(request).blocking_reason(request)
        assert reason is not None
        assert "incomplete" in reason

    def test_an_unacknowledged_unreadable_document_blocks(self):
        """Approving on garbled extraction would be approving nothing."""
        request = request_for(bundle(readable=False))
        reason = decision_for(request).blocking_reason(request)
        assert reason is not None
        assert "acknowledged as viewed at source" in reason

    def test_acknowledging_it_unblocks(self):
        request = request_for(bundle(readable=False))
        digest = request.completeness.manual_viewing_required["contract_terms"]
        approved = decision_for(request, acknowledged={"contract_terms": digest})
        assert approved.blocking_reason(request) is None

    def test_a_decision_for_another_request_blocks(self):
        request = request_for()
        other = request_for(bundle(market={"rules_primary": "Different."}))
        reason = decision_for(other).blocking_reason(request)
        assert reason is not None
        assert "is for request" in reason

    def test_a_decision_must_name_its_reviewer(self):
        with pytest.raises(ValueError, match="who made it"):
            decision_for(request_for(), reviewer="  ")


class TestIssuance:
    def test_an_approved_review_issues_a_certificate(self):
        source = bundle()
        request = request_for(source)
        certificate = issue_certificate(
            bundle=source,
            request=request,
            decision=decision_for(request),
            notional=DOLLAR,
            issued_at=T0,
        )
        assert certificate.status is CertificateStatus.VERIFIED
        assert certificate.market_ticker == "MKT"
        assert certificate.evidence_fingerprint.matches(source.fingerprint())

    def test_the_certificate_records_who_reviewed_it_and_how(self):
        source = bundle()
        request = request_for(source)
        certificate = issue_certificate(
            bundle=source,
            request=request,
            decision=decision_for(request, reviewer="alex"),
            notional=DOLLAR,
            issued_at=T0,
        )
        assert certificate.verified_by == "alex"
        assert "human review" in certificate.verification_method
        assert source.snapshot_id[:16] in certificate.evidence

    @pytest.mark.parametrize("decision", [Decision.REJECTED, Decision.NEEDS_MORE_EVIDENCE])
    def test_a_non_approval_cannot_issue(self, decision):
        source = bundle()
        request = request_for(source)
        with pytest.raises(IssuanceError, match="not APPROVED"):
            issue_certificate(
                bundle=source,
                request=request,
                decision=decision_for(request, decision=decision),
                notional=DOLLAR,
                issued_at=T0,
            )

    def test_evidence_changed_after_review_cannot_issue(self):
        """The reviewer approved a version that no longer exists."""
        reviewed = bundle()
        request = request_for(reviewed)
        decision = decision_for(request)
        drifted = bundle(market={"rules_primary": "Amended."})
        with pytest.raises(IssuanceError, match="not the one reviewed"):
            issue_certificate(
                bundle=drifted,
                request=request,
                decision=decision,
                notional=DOLLAR,
                issued_at=T0,
            )

    def test_incomplete_evidence_cannot_issue(self):
        source = bundle(market={"rules_primary": ABSENT})
        request = request_for(source)
        with pytest.raises(IssuanceError, match="incomplete"):
            issue_certificate(
                bundle=source,
                request=request,
                decision=decision_for(request),
                notional=DOLLAR,
                issued_at=T0,
            )

    def test_there_is_no_constructor_that_bypasses_the_workflow(self):
        for forbidden in ("certify", "auto_issue", "force_issue", "trust_market"):
            assert not hasattr(registry_module, forbidden)


class TestRegistry:
    def test_records_round_trip(self, tmp_path: Path) -> None:
        registry = CertificateRegistry(tmp_path)
        source = bundle()
        request = request_for(source)
        decision = decision_for(request)
        registry.store_evidence(source)
        registry.store_request(request)
        registry.store_decision(decision)
        certificate = issue_certificate(
            bundle=source, request=request, decision=decision, notional=DOLLAR, issued_at=T0
        )
        record = registry.store_certificate(
            certificate=certificate,
            claim=CLAIM,
            snapshot_id=source.snapshot_id,
            request_id=request.request_id,
            issued_at=T0,
            policy_schema_version=request.policy_schema_version,
        )
        reloaded = registry.get_certificate(record.certificate_id)
        assert reloaded is not None
        assert reloaded.certificate.evidence_fingerprint.digest == (
            certificate.evidence_fingerprint.digest
        )
        assert reloaded.certificate.verified_by == "researcher"

    def test_evidence_is_never_silently_overwritten(self, tmp_path: Path) -> None:
        registry = CertificateRegistry(tmp_path)
        source = bundle()
        registry.store_evidence(source)
        registry.store_evidence(source)  # identical: fine
        mutated = SettlementEvidenceBundle(
            snapshot_id=source.snapshot_id,  # same id, different content
            market_ticker="MKT",
            event_ticker="EVT",
            series_ticker="SER",
            captured_at=T0,
            schema_version="test-evidence/1",
            market_fields={**MARKET, "rules_primary": "Tampered."},
            event_fields={"captured": True},
            series_fields={"captured": True},
            documents={},
        )
        with pytest.raises(ValueError, match="append-only"):
            registry.store_evidence(mutated)

    def test_documents_are_stored_by_content_hash(self, tmp_path: Path) -> None:
        registry = CertificateRegistry(tmp_path)
        path = registry.store_document("a" * 64, b"raw pdf bytes")
        assert path.read_bytes() == b"raw pdf bytes"
        assert path.name.startswith("a" * 8)

    def test_an_empty_registry_lists_nothing(self, tmp_path: Path) -> None:
        assert CertificateRegistry(tmp_path).list_certificates() == []


class TestDriftAndApplicability:
    def _issued(
        self, tmp_path: Path, *, issued_at: datetime = T0
    ) -> tuple[CertificateRegistry, CertificateRecord, SettlementEvidenceBundle]:
        registry = CertificateRegistry(tmp_path)
        source = bundle()
        request = request_for(source)
        decision = decision_for(request)
        certificate = issue_certificate(
            bundle=source,
            request=request,
            decision=decision,
            notional=DOLLAR,
            issued_at=issued_at,
        )
        record = registry.store_certificate(
            certificate=certificate,
            claim=CLAIM,
            snapshot_id=source.snapshot_id,
            request_id=request.request_id,
            issued_at=issued_at,
            policy_schema_version=request.policy_schema_version,
        )
        return registry, record, source

    def test_matching_evidence_is_active(self, tmp_path: Path) -> None:
        _, record, source = self._issued(tmp_path)
        assert (
            record.applicability(current_fingerprint=source.fingerprint(), at=T0)
            is CertificateApplicability.ACTIVE
        )

    def test_drifted_evidence_blocks_live_use(self, tmp_path: Path) -> None:
        _, record, _ = self._issued(tmp_path)
        drifted = bundle(market={"rules_primary": "Amended."}).fingerprint()
        applicability = record.applicability(current_fingerprint=drifted, at=T0)
        assert applicability is CertificateApplicability.STALE_EVIDENCE_DRIFT
        assert not applicability.permits_live_use

    def test_unavailable_evidence_fails_closed(self, tmp_path: Path) -> None:
        _, record, _ = self._issued(tmp_path)
        assert (
            record.applicability(current_fingerprint=None, at=T0)
            is CertificateApplicability.EVIDENCE_UNAVAILABLE
        )

    def test_the_historical_record_is_not_rewritten(self, tmp_path: Path) -> None:
        """Drift makes a certificate inapplicable, not retroactively unmade."""
        registry, record, _ = self._issued(tmp_path)
        drifted = bundle(market={"rules_primary": "Amended."}).fingerprint()
        assert not record.applicability(current_fingerprint=drifted, at=T0).permits_live_use
        stored = registry.get_certificate(record.certificate_id)
        assert stored is not None
        assert stored.certificate.status is CertificateStatus.VERIFIED
        assert stored.certificate.verified_by == "researcher"

    def test_history_retains_drifted_certificates(self, tmp_path: Path) -> None:
        registry, record, _ = self._issued(tmp_path)
        assert [r.certificate_id for r in registry.history_for("MKT")] == [record.certificate_id]

    def test_new_evidence_requires_a_new_certificate(self, tmp_path: Path) -> None:
        """The old record is never edited to cover the new evidence."""
        registry, old, _ = self._issued(tmp_path)
        amended = bundle(market={"rules_primary": "Amended."})
        request = request_for(amended)
        certificate = issue_certificate(
            bundle=amended,
            request=request,
            decision=decision_for(request),
            notional=DOLLAR,
            issued_at=T0 + timedelta(days=1),
        )
        new = registry.store_certificate(
            certificate=certificate,
            claim=CLAIM,
            snapshot_id=amended.snapshot_id,
            request_id=request.request_id,
            issued_at=T0 + timedelta(days=1),
            policy_schema_version=request.policy_schema_version,
        )
        assert new.certificate_id != old.certificate_id
        assert len(registry.history_for("MKT")) == 2
        assert old.certificate.evidence_fingerprint.digest != (
            new.certificate.evidence_fingerprint.digest
        )

    def test_the_drift_report_names_what_moved(self, tmp_path: Path) -> None:
        _, record, _ = self._issued(tmp_path)
        drifted = bundle(market={"rules_primary": "Amended."}).fingerprint()
        text = drift_report(record, drifted)
        assert "market.rules_primary" in text

    def test_the_drift_report_handles_unavailable_evidence(self, tmp_path: Path) -> None:
        _, record, _ = self._issued(tmp_path)
        assert "unavailable" in drift_report(record, None)


class TestPointInTime:
    def _registry_with(
        self, tmp_path: Path, issued_at: datetime
    ) -> tuple[CertificateRegistry, SettlementEvidenceBundle]:
        registry = CertificateRegistry(tmp_path)
        source = bundle()
        request = request_for(source)
        certificate = issue_certificate(
            bundle=source,
            request=request,
            decision=decision_for(request),
            notional=DOLLAR,
            issued_at=issued_at,
        )
        registry.store_certificate(
            certificate=certificate,
            claim=CLAIM,
            snapshot_id=source.snapshot_id,
            request_id=request.request_id,
            issued_at=issued_at,
            policy_schema_version=request.policy_schema_version,
        )
        return registry, source

    def test_a_certificate_applies_at_and_after_issuance(self, tmp_path: Path) -> None:
        registry, source = self._registry_with(tmp_path, T0)
        for moment in (T0, T0 + timedelta(days=5)):
            assert (
                registry.active_at(
                    market_ticker="MKT",
                    claim=CLAIM,
                    current_fingerprint=source.fingerprint(),
                    at=moment,
                )
                is not None
            )

    def test_a_future_certificate_is_invisible_to_a_past_replay(self, tmp_path: Path) -> None:
        """Certification tomorrow must not leak backward into yesterday."""
        registry, source = self._registry_with(tmp_path, T0 + timedelta(days=1))
        assert (
            registry.active_at(
                market_ticker="MKT",
                claim=CLAIM,
                current_fingerprint=source.fingerprint(),
                at=T0,
            )
            is None
        )

    def test_drifted_evidence_yields_no_active_certificate(self, tmp_path: Path) -> None:
        registry, _ = self._registry_with(tmp_path, T0)
        drifted = bundle(market={"rules_primary": "Amended."}).fingerprint()
        assert (
            registry.active_at(market_ticker="MKT", claim=CLAIM, current_fingerprint=drifted, at=T0)
            is None
        )

    def test_unavailable_evidence_yields_no_active_certificate(self, tmp_path: Path) -> None:
        registry, _ = self._registry_with(tmp_path, T0)
        assert (
            registry.active_at(market_ticker="MKT", claim=CLAIM, current_fingerprint=None, at=T0)
            is None
        )

    def test_the_most_recent_applicable_certificate_wins(self, tmp_path: Path) -> None:
        registry, source = self._registry_with(tmp_path, T0)
        request = request_for(source)
        later = issue_certificate(
            bundle=source,
            request=request,
            decision=decision_for(request),
            notional=DOLLAR,
            issued_at=T0 + timedelta(days=2),
        )
        newest = registry.store_certificate(
            certificate=later,
            claim=CLAIM,
            snapshot_id=source.snapshot_id,
            request_id=request.request_id,
            issued_at=T0 + timedelta(days=2),
            policy_schema_version=request.policy_schema_version,
        )
        active = registry.active_at(
            market_ticker="MKT",
            claim=CLAIM,
            current_fingerprint=source.fingerprint(),
            at=T0 + timedelta(days=3),
        )
        assert active is not None
        assert active.certificate_id == newest.certificate_id

    def test_an_unknown_market_has_no_certificate(self, tmp_path: Path) -> None:
        registry, source = self._registry_with(tmp_path, T0)
        assert (
            registry.active_at(
                market_ticker="OTHER",
                claim=CLAIM,
                current_fingerprint=source.fingerprint(),
                at=T0,
            )
            is None
        )


class TestSnapshotRoundTrip:
    """A stored snapshot must re-fingerprint to the digest it had when captured.

    Found the hard way: nested mappings were being stringified on the way to
    disk, so a reloaded snapshot produced a different digest and issuance failed
    against the very evidence that had been reviewed.
    """

    def _stored(self, tmp_path: Path, source: SettlementEvidenceBundle) -> dict[str, object]:
        registry = CertificateRegistry(tmp_path)
        registry.store_evidence(source)
        payload = registry.load_evidence(source.snapshot_id)
        assert payload is not None
        return payload

    def test_a_plain_bundle_round_trips(self, tmp_path: Path) -> None:
        source = bundle()
        payload = self._stored(tmp_path, source)
        assert _fingerprint(payload).matches(_bundle_from_snapshot(payload).fingerprint())

    def test_a_nested_mapping_round_trips(self, tmp_path: Path) -> None:
        """custom_strike is a dict; storing its repr changed the digest."""
        source = bundle(market={"custom_strike": {"team": "abc-123", "other": "x"}})
        payload = self._stored(tmp_path, source)
        assert isinstance(payload["market_fields"]["custom_strike"], dict)  # type: ignore[index]
        assert _fingerprint(payload).matches(_bundle_from_snapshot(payload).fingerprint())

    def test_nested_sequences_round_trip(self, tmp_path: Path) -> None:
        source = bundle(market={"sources": [["Name", "https://x.test"], ["Other", ""]]})
        payload = self._stored(tmp_path, source)
        assert _fingerprint(payload).matches(_bundle_from_snapshot(payload).fingerprint())

    def test_absent_survives_the_round_trip(self, tmp_path: Path) -> None:
        """Absent must not come back as the string 'ABSENT' or as null."""
        source = bundle(market={"early_close_condition": ABSENT})
        payload = self._stored(tmp_path, source)
        assert payload["market_fields"]["early_close_condition"] == {"__absent__": True}  # type: ignore[index]
        assert _fingerprint(payload).matches(_bundle_from_snapshot(payload).fingerprint())

    def test_null_and_empty_survive_distinctly(self, tmp_path: Path) -> None:
        as_null = self._stored(tmp_path, bundle(market={"early_close_condition": None}))
        as_empty = self._stored(tmp_path, bundle(market={"early_close_condition": ""}))
        assert _fingerprint(as_null).digest != _fingerprint(as_empty).digest
        for payload in (as_null, as_empty):
            assert _fingerprint(payload).matches(_bundle_from_snapshot(payload).fingerprint())

    def test_a_reloaded_snapshot_can_still_issue(self, tmp_path: Path) -> None:
        """The end-to-end consequence: review reloads, so issuance must match."""
        source = bundle(market={"custom_strike": {"team": "abc-123"}})
        payload = self._stored(tmp_path, source)
        revived = _bundle_from_snapshot(payload)
        request = ReviewRequest.create(bundle=revived, claim=CLAIM, generated_at=T0)
        certificate = issue_certificate(
            bundle=revived,
            request=request,
            decision=decision_for(request),
            notional=DOLLAR,
            issued_at=T0,
        )
        assert certificate.evidence_fingerprint.matches(source.fingerprint())


class TestAcknowledgementBindsToContentHash:
    """An acknowledgement of version A must not satisfy version B.

    A contract amended between review and issuance has not been read, however
    recently the reviewer looked at the old one.
    """

    def unreadable(self, payload: bytes = b"%PDF-1.4 terms") -> SettlementEvidenceBundle:
        documents = {
            "contract_terms": ExternalDocument.from_bytes(
                url="https://kalshi.test/terms.pdf",
                payload=payload,
                retrieved_at=T0,
                http_status=200,
                content_type="application/pdf",
                extraction=TextExtraction.FAILED,
                text=None,
            )
        }
        provisional = SettlementEvidenceBundle(
            snapshot_id="pending",
            market_ticker="MKT",
            event_ticker="EVT",
            series_ticker="SER",
            captured_at=T0,
            schema_version="test-evidence/1",
            market_fields=dict(MARKET),
            event_fields={"captured": True},
            series_fields={"captured": True},
            documents=documents,
        )
        return SettlementEvidenceBundle(
            snapshot_id=snapshot_id_for(
                market_ticker="MKT", fingerprint=provisional.fingerprint(), captured_at=T0
            ),
            market_ticker="MKT",
            event_ticker="EVT",
            series_ticker="SER",
            captured_at=T0,
            schema_version="test-evidence/1",
            market_fields=dict(MARKET),
            event_fields={"captured": True},
            series_fields={"captured": True},
            documents=documents,
        )

    def test_no_acknowledgement_blocks(self):
        request = request_for(self.unreadable())
        reason = decision_for(request).blocking_reason(request)
        assert reason is not None
        assert "not acknowledged as viewed at source" in reason

    def test_the_correct_hash_unblocks(self):
        source = self.unreadable()
        request = request_for(source)
        digest = request.completeness.manual_viewing_required["contract_terms"]
        approved = decision_for(request, acknowledged={"contract_terms": digest})
        assert approved.blocking_reason(request) is None

    def test_an_acknowledgement_of_another_version_does_not_carry_over(self):
        """The core of the correction: version A cannot satisfy version B."""
        old = self.unreadable(b"%PDF-1.4 terms rev1")
        new = self.unreadable(b"%PDF-1.4 terms rev2")
        old_digest = old.documents["contract_terms"].content_sha256
        new_request = request_for(new)
        assert old_digest != new.documents["contract_terms"].content_sha256

        stale = ReviewDecision(
            request_id=new_request.request_id,
            claim=new_request.claim,
            market_ticker=new_request.market_ticker,
            snapshot_id=new_request.snapshot_id,
            evidence_fingerprint=new_request.evidence_fingerprint,
            decision=Decision.APPROVED,
            reviewer="researcher",
            reviewed_at=T0,
            checklist_answers=all_safe_answers(),
            external_evidence_acknowledged={"contract_terms": old_digest or ""},
        )
        reason = stale.blocking_reason(new_request)
        assert reason is not None
        assert "not the one under review" in reason

    def test_issuance_is_blocked_without_acknowledgement(self, tmp_path: Path) -> None:
        del tmp_path
        source = self.unreadable()
        request = request_for(source)
        with pytest.raises(IssuanceError, match="not acknowledged"):
            issue_certificate(
                bundle=source,
                request=request,
                decision=decision_for(request),
                notional=DOLLAR,
                issued_at=T0,
            )

    def test_issuance_succeeds_with_a_hash_bound_acknowledgement(self):
        source = self.unreadable()
        request = request_for(source)
        digest = request.completeness.manual_viewing_required["contract_terms"]
        certificate = issue_certificate(
            bundle=source,
            request=request,
            decision=decision_for(request, acknowledged={"contract_terms": digest}),
            notional=DOLLAR,
            issued_at=T0,
        )
        assert certificate.status is CertificateStatus.VERIFIED

    def test_a_document_change_after_issuance_makes_the_certificate_stale(self):
        """The document hash is part of the evidence fingerprint."""
        source = self.unreadable(b"%PDF-1.4 terms rev1")
        request = request_for(source)
        digest = request.completeness.manual_viewing_required["contract_terms"]
        certificate = issue_certificate(
            bundle=source,
            request=request,
            decision=decision_for(request, acknowledged={"contract_terms": digest}),
            notional=DOLLAR,
            issued_at=T0,
        )
        amended = self.unreadable(b"%PDF-1.4 terms rev2")
        assert not certificate.permits_proof_at(current_fingerprint=amended.fingerprint(), at=T0)
        reason = certificate.blocking_reason(current_fingerprint=amended.fingerprint(), at=T0)
        assert reason is not None
        assert "document.contract_terms.sha256" in reason

    def test_a_failed_governing_document_fetch_cannot_issue(self):
        """URL present, fetch failed: incomplete, not absent."""
        failed_documents = {
            "contract_terms": ExternalDocument.missing(
                "https://kalshi.test/terms.pdf", DocumentRetrieval.HTTP_ERROR
            )
        }
        provisional = SettlementEvidenceBundle(
            snapshot_id="pending",
            market_ticker="MKT",
            event_ticker="EVT",
            series_ticker="SER",
            captured_at=T0,
            schema_version="test-evidence/1",
            market_fields=dict(MARKET),
            event_fields={"captured": True},
            series_fields={"captured": True},
            documents=failed_documents,
        )
        source = SettlementEvidenceBundle(
            snapshot_id=snapshot_id_for(
                market_ticker="MKT", fingerprint=provisional.fingerprint(), captured_at=T0
            ),
            market_ticker="MKT",
            event_ticker="EVT",
            series_ticker="SER",
            captured_at=T0,
            schema_version="test-evidence/1",
            market_fields=dict(MARKET),
            event_fields={"captured": True},
            series_fields={"captured": True},
            documents=failed_documents,
        )
        request = request_for(source)
        assert not request.permits_approval
        with pytest.raises(IssuanceError, match="incomplete"):
            issue_certificate(
                bundle=source,
                request=request,
                decision=decision_for(request),
                notional=DOLLAR,
                issued_at=T0,
            )
