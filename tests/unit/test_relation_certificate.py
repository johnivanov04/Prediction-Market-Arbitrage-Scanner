"""Relation certificates, review and registry.

The recurring property: a relation certificate proves only that at most one of
an explicit selected set may settle YES, it never certifies itself from a
metadata flag, and it stops applying when anything it was reviewed against
moves -- including any member's settlement evidence.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from predarb.semantics import relation as relation_module
from predarb.semantics.evidence import (
    DocumentRetrieval,
    ExternalDocument,
    TextExtraction,
)
from predarb.semantics.fingerprint import synthetic_fingerprint
from predarb.semantics.registry import RelationRecord, RelationRegistry
from predarb.semantics.relation import (
    AT_MOST_ONE_CHECKLIST,
    MIN_BASKET_MEMBERS,
    NO_SELECTED_MEMBER_WINS,
    MemberEvidence,
    RelationCertificate,
    RelationClaim,
    RelationCompleteness,
    RelationDecision,
    RelationEvidenceBundle,
    RelationReviewRequest,
    RelationStatus,
    at_most_one_states,
    canonical_members,
    issue_relation_certificate,
)
from predarb.semantics.review import ChecklistAnswer, Decision

pytestmark = pytest.mark.unit

T0 = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
CLAIM = RelationClaim.AT_MOST_ONE
EVENT = "EVT"


def member(
    ticker: str,
    *,
    salt: str = "v1",
    certificate_id: str | None = "cert-x",
    fingerprint: str | None = "derive",
    rules_hash: str | None = "h" * 64,
) -> MemberEvidence:
    """``fingerprint=None`` means semantic evidence was not captured."""
    digest = (
        synthetic_fingerprint(market=ticker, rules=salt).digest
        if fingerprint == "derive"
        else fingerprint
    )
    return MemberEvidence(
        ticker=ticker,
        event_ticker=EVENT,
        title=f"{ticker} wins",
        yes_sub_title=ticker,
        no_sub_title=f"not {ticker}",
        rules_hash=rules_hash,
        notional="1.0000",
        settlement_fingerprint=digest,
        settlement_certificate_id=certificate_id,
    )


def bundle(
    *,
    members: list[str] | None = None,
    member_objects: list[MemberEvidence] | None = None,
    observed: list[str] | None = None,
    mutually_exclusive: bool = True,
    documents: dict[str, ExternalDocument] | None = None,
) -> RelationEvidenceBundle:
    selected = members or ["A", "B", "C"]
    objects = member_objects or [member(t) for t in selected]
    provisional = RelationEvidenceBundle(
        snapshot_id="pending",
        event_ticker=EVENT,
        series_ticker="SER",
        selected_members=tuple(selected),
        captured_at=T0,
        schema_version="relation-evidence/1",
        event_fields={"mutually_exclusive": mutually_exclusive, "title": "The event"},
        member_evidence=tuple(objects),
        observed_event_membership=tuple(observed or [*selected, "D"]),
        documents=documents or {},
    )
    return RelationEvidenceBundle(
        snapshot_id="snap-" + provisional.fingerprint().short,
        event_ticker=EVENT,
        series_ticker="SER",
        selected_members=tuple(selected),
        captured_at=T0,
        schema_version="relation-evidence/1",
        event_fields=dict(provisional.event_fields),
        member_evidence=tuple(objects),
        observed_event_membership=provisional.observed_event_membership,
        documents=provisional.documents,
    )


def request_for(source: RelationEvidenceBundle | None = None) -> RelationReviewRequest:
    return RelationReviewRequest.create(bundle=source or bundle(), claim=CLAIM, generated_at=T0)


def all_safe() -> dict[str, ChecklistAnswer]:
    return {q.key: q.safe_answers[0] for q in AT_MOST_ONE_CHECKLIST}


def decision_for(
    request: RelationReviewRequest,
    *,
    decision: Decision = Decision.APPROVED,
    answers: dict[str, ChecklistAnswer] | None = None,
    reviewer: str = "researcher",
) -> RelationDecision:
    return RelationDecision(
        request_id=request.request_id,
        claim=request.claim,
        event_ticker=request.event_ticker,
        selected_members=request.selected_members,
        snapshot_id=request.snapshot_id,
        evidence_fingerprint=request.evidence_fingerprint,
        decision=decision,
        reviewer=reviewer,
        reviewed_at=T0,
        checklist_answers=all_safe() if answers is None else answers,
    )


class TestClaimNarrowness:
    def test_only_at_most_one_exists(self):
        """No EXACTLY_ONE shortcut, even as a convenience alias."""
        assert {c.value for c in RelationClaim} == {"AT_MOST_ONE"}

    def test_the_proposition_disclaims_exhaustiveness(self):
        text = CLAIM.proposition
        assert "at most one" in text
        assert "does not claim the selected set is exhaustive" in text
        assert "asserts nothing about whether any of them must settle YES" in text


class TestStateSpace:
    def test_n_plus_one_states(self):
        assert len(at_most_one_states(["A", "B", "C"])) == 4

    def test_the_none_state_is_first_and_named_for_the_subset(self):
        states = at_most_one_states(["A", "B"])
        assert states[0].name == NO_SELECTED_MEMBER_WINS
        assert "SELECTED" in states[0].name

    def test_ordering_is_canonical(self):
        assert at_most_one_states(["C", "A", "B"]) == at_most_one_states(["A", "B", "C"])

    def test_duplicate_members_are_refused(self):
        with pytest.raises(ValueError, match="duplicate members"):
            canonical_members(["A", "A", "B"])


class TestEvidenceBundle:
    def test_the_fingerprint_covers_selected_members(self):
        assert bundle().fingerprint().component("relation.selected_members") is not None

    def test_a_member_settlement_change_moves_the_fingerprint(self):
        """The relation was reviewed against those payoff tables."""
        drifted = bundle(member_objects=[member("A", salt="v2"), member("B"), member("C")])
        assert drifted.fingerprint().digest != bundle().fingerprint().digest

    def test_an_event_field_change_moves_the_fingerprint(self):
        assert bundle(mutually_exclusive=False).fingerprint().digest != (
            bundle().fingerprint().digest
        )

    def test_observed_membership_is_audit_only_not_material(self):
        """Outside membership is evidence about the event, not about the claim.

        "At most one of {A, B, C} settles YES" cannot be falsified by a market
        appearing or vanishing outside {A, B, C}. Fingerprinting it would expire
        certificates on unrelated venue activity, which trains reviewers to
        ignore drift.
        """
        source = bundle(observed=["A", "B", "C", "D", "E"])
        assert "D" in source.observed_event_membership
        assert "relation.observed_event_membership" not in source.component_values()
        assert "relation.observed_event_membership_NOT_PROVEN_EXHAUSTIVE" in source.audit_values()

    def test_an_outside_member_appearing_does_not_change_the_fingerprint(self):
        before = bundle(observed=["A", "B", "C"])
        after = bundle(observed=["A", "B", "C", "D"])
        assert before.fingerprint().digest == after.fingerprint().digest

    def test_an_outside_member_vanishing_does_not_change_the_fingerprint(self):
        before = bundle(observed=["A", "B", "C", "D"])
        after = bundle(observed=["A", "B", "C"])
        assert before.fingerprint().digest == after.fingerprint().digest

    def test_but_the_audit_view_still_shows_the_difference(self):
        """Retained and diffable, just not load-bearing."""
        before = bundle(observed=["A", "B", "C"])
        after = bundle(observed=["A", "B", "C", "D"])
        assert before.audit_values() != after.audit_values()

    def test_changing_the_selected_set_does_change_the_fingerprint(self):
        """The subset itself is material; membership outside it is not."""
        three = bundle(members=["A", "B", "C"])
        two = bundle(members=["A", "B"], member_objects=[member("A"), member("B")])
        assert three.fingerprint().digest != two.fingerprint().digest

    def test_a_governing_document_change_moves_the_fingerprint(self):
        first = bundle(
            documents={
                "contract_terms": ExternalDocument.from_bytes(
                    url="https://x.test/terms",
                    payload=b"rev1",
                    retrieved_at=T0,
                    http_status=200,
                    content_type="text/plain",
                    extraction=TextExtraction.CLEAN,
                    text="rev1",
                )
            }
        )
        second = bundle(
            documents={
                "contract_terms": ExternalDocument.from_bytes(
                    url="https://x.test/terms",
                    payload=b"rev2",
                    retrieved_at=T0,
                    http_status=200,
                    content_type="text/plain",
                    extraction=TextExtraction.CLEAN,
                    text="rev2",
                )
            }
        )
        assert first.fingerprint().digest != second.fingerprint().digest

    def test_selected_members_must_all_have_evidence(self):
        with pytest.raises(ValueError, match="without evidence"):
            RelationEvidenceBundle(
                snapshot_id="x",
                event_ticker=EVENT,
                series_ticker=None,
                selected_members=("A", "B"),
                captured_at=T0,
                schema_version="v1",
                event_fields={},
                member_evidence=(member("A"),),
                observed_event_membership=("A", "B"),
            )

    def test_unselected_members_do_not_enter_the_fingerprint(self):
        """Only the reviewed subset is part of the claim."""
        with_extra = bundle(
            members=["A", "B"],
            member_objects=[member("A"), member("B"), member("C")],
        )
        plain = bundle(members=["A", "B"], member_objects=[member("A"), member("B")])
        assert with_extra.fingerprint().digest == plain.fingerprint().digest


class TestReviewRequest:
    def test_a_complete_bundle_awaits_review(self):
        assert request_for().completeness is RelationCompleteness.COMPLETE
        assert request_for().permits_approval

    def test_an_uncertified_member_does_not_block_the_relation_review(self):
        """Payout certification is the detector's obligation, not the reviewer's.

        A human can judge "can two of these both settle YES?" from the rules
        alone. Requiring payout certificates first would impose an ordering the
        two proofs do not have.
        """
        source = bundle(
            member_objects=[
                member("A", certificate_id=None),
                member("B", certificate_id=None),
                member("C", certificate_id=None),
            ]
        )
        req = request_for(source)
        assert req.permits_approval
        assert req.incompleteness_reasons == ()

    def test_a_member_without_semantic_evidence_does_block(self):
        """What a reviewer actually needs is the rules, not a certificate."""
        source = bundle(member_objects=[member("A"), member("B", fingerprint=None), member("C")])
        req = request_for(source)
        assert not req.permits_approval
        assert any("without complete semantic evidence" in r for r in req.incompleteness_reasons)

    def test_a_member_without_a_rules_hash_blocks(self):
        source = bundle(member_objects=[member("A"), member("B", rules_hash=None), member("C")])
        assert not request_for(source).permits_approval

    def test_a_single_member_set_blocks_approval(self):
        source = bundle(members=["A"], member_objects=[member("A")])
        req = request_for(source)
        assert not req.permits_approval
        assert any("at least 2" in r for r in req.incompleteness_reasons)

    def test_a_failed_governing_document_blocks_approval(self):
        source = bundle(
            documents={
                "contract_terms": ExternalDocument.missing(
                    "https://x.test/terms", DocumentRetrieval.HTTP_ERROR
                )
            }
        )
        req = request_for(source)
        assert not req.permits_approval
        assert any("not retrieved" in r for r in req.incompleteness_reasons)

    def test_the_checklist_forces_the_exhaustiveness_question(self):
        keys = {q.key for q in request_for().checklist}
        assert "claim_is_only_at_most_one" in keys
        assert "ties_or_co_winners" in keys

    def test_the_checklist_asks_about_evidence_not_certification(self):
        """The reviewer is asked what they can actually judge."""
        keys = {q.key for q in request_for().checklist}
        assert "member_semantic_evidence_complete" in keys
        assert "members_describe_distinct_outcomes" in keys
        assert "members_individually_certified" not in keys
        assert "no_stale_member_certificates" not in keys

    def test_observed_membership_travels_with_the_request(self):
        req = request_for(bundle(observed=["A", "B", "C", "D"]))
        assert "D" in req.observed_event_membership


class TestNoAutoCertification:
    def test_mutually_exclusive_true_does_not_approve_anything(self):
        """A flag pre-populates a review; it cannot certify one."""
        req = request_for(bundle(mutually_exclusive=True))
        assert req.permits_approval  # eligible for review...
        pending = decision_for(req, decision=Decision.NEEDS_MORE_EVIDENCE)
        assert pending.blocking_reason(req) is not None  # ...but not approved

    def test_no_constructor_certifies_from_metadata(self):
        for forbidden in ("from_event", "auto_certify", "certify_if_exclusive", "infer"):
            assert not hasattr(relation_module, forbidden)

    def test_an_uncertain_answer_blocks(self):
        req = request_for()
        answers = all_safe()
        answers["ties_or_co_winners"] = ChecklistAnswer.UNCERTAIN
        reason = decision_for(req, answers=answers).blocking_reason(req)
        assert reason is not None
        assert "block this claim" in reason

    def test_an_unanswered_question_blocks(self):
        req = request_for()
        answers = all_safe()
        del answers["rules_forbid_two_yes"]
        reason = decision_for(req, answers=answers).blocking_reason(req)
        assert reason is not None
        assert "rules_forbid_two_yes" in reason

    def test_a_decision_over_a_different_member_set_blocks(self):
        req = request_for()
        other = decision_for(
            request_for(bundle(members=["A", "B"], member_objects=[member("A"), member("B")]))
        )
        assert other.blocking_reason(req) is not None


class TestIssuance:
    def test_an_approved_review_issues(self):
        source = bundle()
        req = request_for(source)
        certificate = issue_relation_certificate(
            bundle=source, request=req, decision=decision_for(req), issued_at=T0
        )
        assert certificate.status is RelationStatus.VERIFIED
        assert certificate.selected_members == ("A", "B", "C")
        assert set(certificate.member_settlement_fingerprints) == {"A", "B", "C"}

    def test_the_evidence_text_refuses_completeness_language(self):
        source = bundle()
        req = request_for(source)
        certificate = issue_relation_certificate(
            bundle=source, request=req, decision=decision_for(req), issued_at=T0
        )
        assert "selected certified mutually-exclusive subset" in certificate.evidence
        assert "complete event outcome set" not in certificate.evidence

    def test_a_rejection_cannot_issue(self):
        source = bundle()
        req = request_for(source)
        with pytest.raises(ValueError, match="not APPROVED"):
            issue_relation_certificate(
                bundle=source,
                request=req,
                decision=decision_for(req, decision=Decision.REJECTED),
                issued_at=T0,
            )

    def test_drifted_evidence_cannot_issue(self):
        reviewed = bundle()
        req = request_for(reviewed)
        drifted = bundle(mutually_exclusive=False)
        with pytest.raises(ValueError, match="not the one reviewed"):
            issue_relation_certificate(
                bundle=drifted, request=req, decision=decision_for(req), issued_at=T0
            )

    def test_a_single_member_certificate_is_refused(self):
        source = bundle(members=["A"], member_objects=[member("A")])
        req = request_for(source)
        with pytest.raises(ValueError, match="incomplete"):
            issue_relation_certificate(
                bundle=source, request=req, decision=decision_for(req), issued_at=T0
            )

    def test_the_minimum_is_two(self):
        assert MIN_BASKET_MEMBERS == 2


class TestApplicabilityAndDrift:
    def _issued(
        self, *, issued_at: datetime = T0
    ) -> tuple[RelationEvidenceBundle, RelationReviewRequest, RelationCertificate]:
        source = bundle()
        req = request_for(source)
        certificate = issue_relation_certificate(
            bundle=source, request=req, decision=decision_for(req), issued_at=issued_at
        )
        return source, req, certificate

    def _member_prints(self, *, drift: str | None = None) -> dict[str, str]:
        return {
            t: synthetic_fingerprint(market=t, rules="v2" if drift == t else "v1").digest
            for t in ("A", "B", "C")
        }

    def test_matching_evidence_permits_proof(self):
        source, _, certificate = self._issued()
        assert (
            certificate.blocking_reason(
                current_fingerprint=source.fingerprint(),
                member_settlement_fingerprints=self._member_prints(),
                at=T0,
            )
            is None
        )

    def test_relation_evidence_drift_blocks(self):
        _, _, certificate = self._issued()
        reason = certificate.blocking_reason(
            current_fingerprint=bundle(mutually_exclusive=False).fingerprint(),
            member_settlement_fingerprints=self._member_prints(),
            at=T0,
        )
        assert reason is not None
        assert "relation evidence has changed" in reason

    def test_a_member_settlement_drift_blocks(self):
        """Even when the relation's own evidence is unchanged."""
        source, _, certificate = self._issued()
        reason = certificate.blocking_reason(
            current_fingerprint=source.fingerprint(),
            member_settlement_fingerprints=self._member_prints(drift="B"),
            at=T0,
        )
        assert reason is not None
        assert "settlement evidence for B has changed" in reason

    def test_unavailable_relation_evidence_fails_closed(self):
        _, _, certificate = self._issued()
        reason = certificate.blocking_reason(
            current_fingerprint=None, member_settlement_fingerprints=None, at=T0
        )
        assert reason is not None
        assert "unavailable" in reason

    def test_missing_member_evidence_fails_closed(self):
        source, _, certificate = self._issued()
        reason = certificate.blocking_reason(
            current_fingerprint=source.fingerprint(),
            member_settlement_fingerprints={"A": self._member_prints()["A"]},
            at=T0,
        )
        assert reason is not None
        assert "no current settlement evidence for" in reason

    def test_covers_requires_the_exact_member_set(self):
        _, _, certificate = self._issued()
        assert certificate.covers(["C", "B", "A"])
        assert not certificate.covers(["A", "B"])
        assert not certificate.covers(["A", "B", "C", "D"])


class TestRegistry:
    def _store(
        self, tmp_path: Path, *, issued_at: datetime = T0
    ) -> tuple[RelationRegistry, RelationEvidenceBundle, RelationRecord]:
        registry = RelationRegistry(tmp_path)
        source = bundle()
        req = request_for(source)
        decision = decision_for(req)
        registry.store_evidence(source)
        registry.store_request(req)
        registry.store_decision(decision)
        certificate = issue_relation_certificate(
            bundle=source, request=req, decision=decision, issued_at=issued_at
        )
        record = registry.store_certificate(certificate, request_id=req.request_id)
        return registry, source, record

    def _prints(self) -> dict[str, str]:
        return {t: synthetic_fingerprint(market=t, rules="v1").digest for t in ("A", "B", "C")}

    def test_records_round_trip(self, tmp_path: Path) -> None:
        registry, _, record = self._store(tmp_path)
        reloaded = registry.list_certificates(event_ticker=EVENT)
        assert len(reloaded) == 1
        assert reloaded[0].certificate_id == record.certificate_id
        assert reloaded[0].selected_members == ("A", "B", "C")

    def test_the_stored_membership_field_is_named_as_unproven(self, tmp_path: Path) -> None:
        """So nobody reading the record mistakes it for an outcome universe."""
        registry, source, _ = self._store(tmp_path)
        path = registry.evidence_dir / f"{source.snapshot_id}.json"
        payload = json.loads(path.read_text())
        assert "observed_event_membership_NOT_PROVEN_EXHAUSTIVE" in payload

    def test_active_at_finds_a_current_certificate(self, tmp_path: Path) -> None:
        registry, source, record = self._store(tmp_path)
        found = registry.active_at(
            event_ticker=EVENT,
            claim=CLAIM,
            members=["A", "B", "C"],
            current_fingerprint=source.fingerprint(),
            member_settlement_fingerprints=self._prints(),
            at=T0,
        )
        assert found is not None
        assert found.certificate_id == record.certificate_id

    def test_a_different_member_set_finds_nothing(self, tmp_path: Path) -> None:
        registry, source, _ = self._store(tmp_path)
        assert (
            registry.active_at(
                event_ticker=EVENT,
                claim=CLAIM,
                members=["A", "B"],
                current_fingerprint=source.fingerprint(),
                member_settlement_fingerprints=self._prints(),
                at=T0,
            )
            is None
        )

    def test_a_future_certificate_is_invisible_to_a_past_replay(self, tmp_path: Path) -> None:
        registry, source, _ = self._store(tmp_path, issued_at=T0 + timedelta(days=1))
        assert (
            registry.active_at(
                event_ticker=EVENT,
                claim=CLAIM,
                members=["A", "B", "C"],
                current_fingerprint=source.fingerprint(),
                member_settlement_fingerprints=self._prints(),
                at=T0,
            )
            is None
        )

    def test_drifted_evidence_yields_nothing_active(self, tmp_path: Path) -> None:
        registry, _, _ = self._store(tmp_path)
        assert (
            registry.active_at(
                event_ticker=EVENT,
                claim=CLAIM,
                members=["A", "B", "C"],
                current_fingerprint=bundle(mutually_exclusive=False).fingerprint(),
                member_settlement_fingerprints=self._prints(),
                at=T0,
            )
            is None
        )

    def test_history_retains_drifted_certificates(self, tmp_path: Path) -> None:
        registry, _, record = self._store(tmp_path)
        history = registry.history_for(EVENT)
        assert [r.certificate_id for r in history] == [record.certificate_id]
        assert history[0].certificate.status is RelationStatus.VERIFIED

    def test_records_are_append_only(self, tmp_path: Path) -> None:
        registry, source, _ = self._store(tmp_path)
        registry.store_evidence(source)  # identical is fine
        tampered = RelationEvidenceBundle(
            snapshot_id=source.snapshot_id,
            event_ticker=EVENT,
            series_ticker="SER",
            selected_members=("A", "B", "C"),
            captured_at=T0,
            schema_version="relation-evidence/1",
            event_fields={"mutually_exclusive": False, "title": "Tampered"},
            member_evidence=tuple(member(t) for t in ("A", "B", "C")),
            observed_event_membership=("A", "B", "C"),
        )
        with pytest.raises(ValueError, match="append-only"):
            registry.store_evidence(tampered)

    def test_relation_and_settlement_stores_are_separate(self, tmp_path: Path) -> None:
        """A settlement certificate must never satisfy a relation lookup."""
        registry = RelationRegistry(tmp_path)
        assert registry.certificates_dir.name == "relation_certificates"
        assert registry.evidence_dir.name == "relation_evidence"
