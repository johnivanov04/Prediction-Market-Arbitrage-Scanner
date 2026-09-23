"""EXACTLY_ONE composed from two certificates, never reviewed as a third.

The conjunction is the design. These tests pin what it requires, what it
refuses, and the state semantics that follow -- n states, not n + 1 and not
2**n - 1.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import combinations
from typing import Any

import pytest

from predarb.semantics.exactly_one import (
    DerivationRefusal,
    DerivedExactlyOneProof,
    derive_exactly_one,
    exactly_one_states,
)
from predarb.semantics.fingerprint import synthetic_fingerprint
from predarb.semantics.relation import (
    JointStateRule,
    RelationCertificate,
    RelationClaim,
    RelationStatus,
)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
EVENT = "EVT"
MEMBERS = ["A", "B", "C"]


def member_prints(members: list[str], salt: str = "v1") -> dict[str, str]:
    return {t: synthetic_fingerprint(market=t, rules=salt).digest for t in members}


def certificate(
    claim: RelationClaim,
    members: list[str] = MEMBERS,
    *,
    status: RelationStatus = RelationStatus.VERIFIED,
    valid_from: datetime = T0,
    valid_to: datetime | None = None,
    prints: dict[str, str] | None = None,
) -> RelationCertificate:
    return RelationCertificate(
        certificate_id=f"{claim.value.lower()}-" + "-".join(members),
        claim=claim,
        event_ticker=EVENT,
        selected_members=tuple(members),
        snapshot_id=f"snap-{claim.value}",
        # Deliberately different per claim: the two evidence policies differ, so
        # their aggregate fingerprints are expected never to match.
        evidence_fingerprint=synthetic_fingerprint(event=EVENT, claim=claim.value),
        member_settlement_fingerprints=prints or member_prints(members),
        status=status,
        reviewer="test",
        reviewed_at=T0,
        issued_at=valid_from,
        valid_from=valid_from,
        valid_to=valid_to,
        policy_schema_version="relation-evidence/1",
        evidence=f"Synthetic {claim.value} fixture.",
    )


def derive(
    **kwargs: Any,
) -> tuple[DerivedExactlyOneProof | None, tuple[str, ...]]:
    params: dict[str, Any] = {
        "at_most_one": certificate(RelationClaim.AT_MOST_ONE),
        "at_least_one": certificate(RelationClaim.AT_LEAST_ONE),
        "current_member_fingerprints": member_prints(MEMBERS),
        "at": T0 + timedelta(hours=1),
    }
    return derive_exactly_one(**{**params, **kwargs})


class TestComposition:
    def test_matching_parents_derive_a_proof(self):
        proof, refusals = derive()
        assert refusals == ()
        assert proof is not None
        assert proof.members == ("A", "B", "C")

    def test_both_parent_identities_are_preserved(self):
        proof, _ = derive()
        assert proof is not None
        assert proof.at_most_one_certificate_id.startswith("at_most_one")
        assert proof.at_least_one_certificate_id.startswith("at_least_one")
        assert proof.at_most_one_evidence_digest != proof.at_least_one_evidence_digest

    def test_differing_aggregate_fingerprints_are_allowed(self):
        """Their evidence policies differ, so requiring a match would forbid all derivation."""
        most = certificate(RelationClaim.AT_MOST_ONE)
        least = certificate(RelationClaim.AT_LEAST_ONE)
        assert most.evidence_fingerprint.digest != least.evidence_fingerprint.digest
        proof, refusals = derive(at_most_one=most, at_least_one=least)
        assert refusals == ()
        assert proof is not None

    def test_the_derivation_id_is_deterministic(self):
        first, _ = derive()
        second, _ = derive(at=T0 + timedelta(days=1))
        assert first is not None and second is not None
        assert first.derivation_id == second.derivation_id

    def test_it_is_not_a_certificate_and_has_no_review_workflow(self):
        """No issuance, no reviewer, no status. It is computed and discarded."""
        proof, _ = derive()
        assert proof is not None
        for absent in ("status", "reviewer", "reviewed_at", "certificate_id"):
            assert not hasattr(proof, absent)


class TestRefusals:
    def test_a_missing_at_most_one_refuses(self):
        proof, refusals = derive(at_most_one=None)
        assert proof is None
        assert any(DerivationRefusal.MISSING_AT_MOST_ONE.value in r for r in refusals)

    def test_a_missing_at_least_one_refuses(self):
        proof, refusals = derive(at_least_one=None)
        assert proof is None
        assert any(DerivationRefusal.MISSING_AT_LEAST_ONE.value in r for r in refusals)

    def test_a_swapped_claim_refuses(self):
        proof, refusals = derive(at_most_one=certificate(RelationClaim.AT_LEAST_ONE))
        assert proof is None
        assert any(DerivationRefusal.WRONG_CLAIM.value in r for r in refusals)

    def test_different_member_sets_refuse(self):
        proof, refusals = derive(at_least_one=certificate(RelationClaim.AT_LEAST_ONE, ["A", "B"]))
        assert proof is None
        assert any(DerivationRefusal.MEMBER_SETS_DIFFER.value in r for r in refusals)

    def test_a_rejected_parent_refuses(self):
        proof, refusals = derive(
            at_most_one=certificate(RelationClaim.AT_MOST_ONE, status=RelationStatus.REJECTED)
        )
        assert proof is None
        assert any(DerivationRefusal.PARENT_STALE.value in r for r in refusals)

    def test_a_parent_not_yet_valid_refuses(self):
        """A future-issued certificate cannot reach backward."""
        future = certificate(RelationClaim.AT_LEAST_ONE, valid_from=T0 + timedelta(days=7))
        proof, refusals = derive(at_least_one=future)
        assert proof is None
        assert any(DerivationRefusal.PARENT_NOT_YET_VALID.value in r for r in refusals)

    def test_an_expired_parent_refuses(self):
        expired = certificate(RelationClaim.AT_MOST_ONE, valid_to=T0 + timedelta(minutes=1))
        proof, refusals = derive(at_most_one=expired)
        assert proof is None
        assert any(DerivationRefusal.PARENT_STALE.value in r for r in refusals)

    def test_parents_reviewed_against_different_member_evidence_refuse(self):
        """They are not proofs about the same contracts."""
        divergent = certificate(
            RelationClaim.AT_LEAST_ONE, prints=member_prints(MEMBERS, salt="other")
        )
        proof, refusals = derive(at_least_one=divergent)
        assert proof is None
        assert any(DerivationRefusal.MEMBER_EVIDENCE_DIVERGED.value in r for r in refusals)

    def test_member_settlement_drift_refuses(self):
        proof, refusals = derive(current_member_fingerprints=member_prints(MEMBERS, "drifted"))
        assert proof is None
        assert any(DerivationRefusal.PARENT_STALE.value in r for r in refusals)

    def test_unavailable_current_evidence_refuses(self):
        proof, refusals = derive(current_member_fingerprints=None)
        assert proof is None
        assert any(DerivationRefusal.MEMBER_EVIDENCE_DIVERGED.value in r for r in refusals)

    def test_there_is_never_a_partial_derivation(self):
        """Half of EXACTLY_ONE is a claim we already have; calling it the
        conjunction would overstate it."""
        for kwargs in ({"at_most_one": None}, {"at_least_one": None}):
            proof, refusals = derive(**kwargs)
            assert proof is None
            assert refusals


class TestValidityIntersection:
    def test_the_window_is_the_intersection(self):
        proof, _ = derive(
            at_most_one=certificate(
                RelationClaim.AT_MOST_ONE,
                valid_from=T0,
                valid_to=T0 + timedelta(days=10),
            ),
            at_least_one=certificate(
                RelationClaim.AT_LEAST_ONE,
                valid_from=T0 - timedelta(days=1),
                valid_to=T0 + timedelta(days=3),
            ),
        )
        assert proof is not None
        assert proof.valid_from == T0
        assert proof.valid_to == T0 + timedelta(days=3)

    def test_an_open_ended_parent_leaves_the_window_open(self):
        proof, _ = derive()
        assert proof is not None
        assert proof.valid_to is None


class TestStateSemantics:
    def test_there_are_exactly_n_permitted_states(self):
        """Not n + 1 (AT_MOST_ONE) and not 2**n - 1 (AT_LEAST_ONE)."""
        assert exactly_one_states(MEMBERS) == ("A", "B", "C")
        proof, _ = derive()
        assert proof is not None
        assert len(proof.permitted_states) == len(MEMBERS)

    def test_all_no_is_invalid(self):
        proof, _ = derive()
        assert proof is not None
        assert proof.permits(()) is False

    def test_a_singleton_is_valid(self):
        proof, _ = derive()
        assert proof is not None
        for ticker in MEMBERS:
            assert proof.permits([ticker]) is True

    @pytest.mark.parametrize("size", [2, 3])
    def test_multi_winner_states_are_invalid(self, size: int) -> None:
        proof, _ = derive()
        assert proof is not None
        for winners in combinations(MEMBERS, size):
            assert proof.permits(winners) is False

    def test_it_is_exactly_the_intersection_of_the_parents(self):
        """The derived permitted set equals what both parents allow."""
        most = JointStateRule(claim=RelationClaim.AT_MOST_ONE, members=tuple(MEMBERS))
        least = JointStateRule(claim=RelationClaim.AT_LEAST_ONE, members=tuple(MEMBERS))
        proof, _ = derive()
        assert proof is not None

        for size in range(len(MEMBERS) + 1):
            for winners in combinations(MEMBERS, size):
                both_parents = most.permits(winners) and least.permits(winners)
                assert proof.permits(winners) is both_parents

    def test_a_foreign_member_is_refused(self):
        proof, _ = derive()
        assert proof is not None
        with pytest.raises(ValueError, match="not members of this relation"):
            proof.permits(["A", "Z"])


class TestNoEconomicDetector:
    def test_no_exactly_one_detector_module_exists(self):
        with pytest.raises(ImportError):
            __import__("predarb.detectors.exactly_one")

    def test_the_claim_enum_still_has_no_exactly_one(self):
        assert not any("EXACTLY" in c.value for c in RelationClaim)

    def test_the_proof_prices_nothing(self):
        proof, _ = derive()
        assert proof is not None
        for economic in ("profit", "quantity", "gross_cost", "classification"):
            assert not hasattr(proof, economic)
