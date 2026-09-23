"""A relation certificate is identified by event, claim **and exact member set**.

A reviewer answers questions about the set they were shown. A certificate over
``{A, B, C}`` is not a certificate about ``{A, B, C, D}``, and both may
legitimately exist for one event -- concurrently or historically.

The defect these tests exist to prevent: keying on ``(event, claim)`` alone,
which lets one certificate shadow another. The lookup then returns a different
set's certificate, ``covers()`` rejects it, and a valid basket blocks because it
was handed the wrong document. Fail-closed, but wrong, and invisible from the
outside.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from predarb.replay.knowledge import KnowledgeBase, relation_identity
from predarb.replay.observation import (
    KnowledgeHorizon,
    Observation,
    ObservationKind,
    ObservationStream,
)
from predarb.replay.plan import BasketPlan, DetectorPlan
from predarb.semantics.exactly_one import DerivationRefusal, derive_exactly_one
from predarb.semantics.fingerprint import synthetic_fingerprint
from predarb.semantics.relation import RelationCertificate, RelationClaim, RelationStatus
from predarb.venues.kalshi.replay_context import KalshiReplayContext

pytestmark = pytest.mark.unit

T0 = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
EVENT = "EVT"
S1 = ["A", "B", "C"]
S2 = ["A", "B", "C", "D"]


def certificate_observation(
    ordinal: int,
    members: list[str],
    *,
    claim: str = "AT_MOST_ONE",
    certificate_id: str | None = None,
    at: datetime | None = None,
) -> Observation:
    return Observation(
        ordinal=ordinal,
        observed_at=at or (T0 + timedelta(seconds=ordinal)),
        kind=ObservationKind.RELATION_CERTIFICATE,
        payload={
            "event_ticker": EVENT,
            "claim": claim,
            "certificate_id": certificate_id or f"{claim}-{'-'.join(members)}",
            "selected_members": list(members),
        },
    )


def base(*observations: Observation) -> tuple[KnowledgeBase, ObservationStream]:
    stream = ObservationStream.from_iterable(observations)
    return KnowledgeBase.from_stream(stream), stream


def final_horizon(stream: ObservationStream) -> KnowledgeHorizon:
    return stream.horizon_at(stream.observations[-1].ordinal)


class TestExactSetCoexistence:
    """1-3: two sets under one event and claim, each resolvable only as itself."""

    def _both(self) -> tuple[KnowledgeBase, KnowledgeHorizon]:
        knowledge, stream = base(certificate_observation(0, S1), certificate_observation(1, S2))
        return knowledge, final_horizon(stream)

    def test_two_sets_under_the_same_event_and_claim_coexist(self):
        knowledge, horizon = self._both()
        identities = knowledge.relation_certificates_for_event(EVENT, horizon)
        assert len(identities) == 2
        assert identities == (
            relation_identity(EVENT, "AT_MOST_ONE", S1),
            relation_identity(EVENT, "AT_MOST_ONE", S2),
        )

    def test_looking_up_s1_returns_only_s1(self):
        knowledge, horizon = self._both()
        record = knowledge.relation_certificate_at(EVENT, horizon, members=S1)
        assert record is not None
        assert record["selected_members"] == S1

    def test_looking_up_s2_returns_only_s2(self):
        knowledge, horizon = self._both()
        record = knowledge.relation_certificate_at(EVENT, horizon, members=S2)
        assert record is not None
        assert record["selected_members"] == S2

    def test_the_later_certificate_does_not_shadow_the_earlier(self):
        """The precise regression: S2 arriving second must not hide S1."""
        knowledge, horizon = self._both()
        s1 = knowledge.relation_certificate_at(EVENT, horizon, members=S1)
        assert s1 is not None
        assert s1["certificate_id"] == "AT_MOST_ONE-A-B-C"


class TestNoSupersetOrSubsetFallback:
    def test_a_superset_certificate_does_not_answer_for_a_subset(self):
        knowledge, stream = base(certificate_observation(0, S2))
        horizon = final_horizon(stream)
        assert knowledge.relation_certificate_at(EVENT, horizon, members=S2) is not None
        assert knowledge.relation_certificate_at(EVENT, horizon, members=S1) is None

    def test_a_subset_certificate_does_not_answer_for_a_superset(self):
        knowledge, stream = base(certificate_observation(0, S1))
        horizon = final_horizon(stream)
        assert knowledge.relation_certificate_at(EVENT, horizon, members=S1) is not None
        assert knowledge.relation_certificate_at(EVENT, horizon, members=S2) is None

    def test_an_unrelated_set_gets_nothing(self):
        knowledge, stream = base(certificate_observation(0, S1))
        horizon = final_horizon(stream)
        assert knowledge.relation_certificate_at(EVENT, horizon, members=["X", "Y"]) is None


class TestCanonicalOrdering:
    """4: the same set in any order is the same set."""

    def test_reversed_input_order_resolves_the_same_certificate(self):
        knowledge, stream = base(certificate_observation(0, S1))
        horizon = final_horizon(stream)
        forward = knowledge.relation_certificate_at(EVENT, horizon, members=["A", "B", "C"])
        reversed_ = knowledge.relation_certificate_at(EVENT, horizon, members=["C", "B", "A"])
        assert forward is not None
        assert forward == reversed_

    def test_a_certificate_recorded_out_of_order_still_resolves(self):
        knowledge, stream = base(certificate_observation(0, ["C", "A", "B"]))
        horizon = final_horizon(stream)
        assert knowledge.relation_certificate_at(EVENT, horizon, members=S1) is not None

    def test_the_identity_string_is_order_independent(self):
        assert relation_identity(EVENT, "AT_MOST_ONE", ["C", "A", "B"]) == relation_identity(
            EVENT, "AT_MOST_ONE", ["A", "B", "C"]
        )

    def test_duplicates_collapse_rather_than_raising(self):
        """An index must not lose a stream because one payload repeated a ticker."""
        assert relation_identity(EVENT, "AT_MOST_ONE", ["A", "A", "B"]) == relation_identity(
            EVENT, "AT_MOST_ONE", ["A", "B"]
        )


class TestHistoricalVersionsOfOneSet:
    """5: two versions of the same exact set resolve point-in-time."""

    def _versions(self) -> tuple[KnowledgeBase, ObservationStream]:
        return base(
            certificate_observation(0, S1, certificate_id="v1"),
            certificate_observation(1, S1, certificate_id="v2"),
        )

    def test_the_earlier_horizon_sees_the_earlier_version(self):
        knowledge, stream = self._versions()
        record = knowledge.relation_certificate_at(EVENT, stream.horizon_at(0), members=S1)
        assert record is not None
        assert record["certificate_id"] == "v1"

    def test_the_later_horizon_sees_the_later_version(self):
        knowledge, stream = self._versions()
        record = knowledge.relation_certificate_at(EVENT, stream.horizon_at(1), members=S1)
        assert record is not None
        assert record["certificate_id"] == "v2"

    def test_reissuing_one_set_does_not_disturb_another(self):
        knowledge, stream = base(
            certificate_observation(0, S1, certificate_id="s1-v1"),
            certificate_observation(1, S2, certificate_id="s2-v1"),
            certificate_observation(2, S1, certificate_id="s1-v2"),
        )
        horizon = final_horizon(stream)
        s1 = knowledge.relation_certificate_at(EVENT, horizon, members=S1)
        s2 = knowledge.relation_certificate_at(EVENT, horizon, members=S2)
        assert s1 is not None and s2 is not None
        assert s1["certificate_id"] == "s1-v2"
        assert s2["certificate_id"] == "s2-v1"


class TestClaimsStaySeparated:
    """6: the two claims forbid opposite states and never substitute."""

    def _both_claims(self) -> tuple[KnowledgeBase, KnowledgeHorizon]:
        knowledge, stream = base(
            certificate_observation(0, S1, claim="AT_MOST_ONE"),
            certificate_observation(1, S1, claim="AT_LEAST_ONE"),
        )
        return knowledge, final_horizon(stream)

    def test_at_most_one_never_resolves_an_at_least_one_certificate(self):
        knowledge, horizon = self._both_claims()
        record = knowledge.relation_certificate_at(EVENT, horizon, claim="AT_MOST_ONE", members=S1)
        assert record is not None
        assert record["claim"] == "AT_MOST_ONE"

    def test_at_least_one_never_resolves_an_at_most_one_certificate(self):
        knowledge, horizon = self._both_claims()
        record = knowledge.relation_certificate_at(EVENT, horizon, claim="AT_LEAST_ONE", members=S1)
        assert record is not None
        assert record["claim"] == "AT_LEAST_ONE"

    def test_a_missing_claim_is_not_satisfied_by_the_other(self):
        knowledge, stream = base(certificate_observation(0, S1, claim="AT_LEAST_ONE"))
        horizon = final_horizon(stream)
        assert (
            knowledge.relation_certificate_at(EVENT, horizon, claim="AT_MOST_ONE", members=S1)
            is None
        )

    def test_all_four_identities_coexist(self):
        """Two claims over two sets: four distinct certificates, one event."""
        knowledge, stream = base(
            certificate_observation(0, S1, claim="AT_MOST_ONE"),
            certificate_observation(1, S2, claim="AT_MOST_ONE"),
            certificate_observation(2, S1, claim="AT_LEAST_ONE"),
            certificate_observation(3, S2, claim="AT_LEAST_ONE"),
        )
        horizon = final_horizon(stream)
        assert len(knowledge.relation_certificates_for_event(EVENT, horizon)) == 4
        for claim in ("AT_MOST_ONE", "AT_LEAST_ONE"):
            for members in (S1, S2):
                record = knowledge.relation_certificate_at(
                    EVENT, horizon, claim=claim, members=members
                )
                assert record is not None
                assert record["claim"] == claim
                assert record["selected_members"] == members


class TestDerivedExactlyOneRequiresTheSameSet:
    """7: the conjunction is over one canonical set, from both parents."""

    def parent(self, claim: RelationClaim, members: list[str]) -> RelationCertificate:
        return RelationCertificate(
            certificate_id=f"{claim.value}-{'-'.join(members)}",
            claim=claim,
            event_ticker=EVENT,
            selected_members=tuple(members),
            snapshot_id="snap",
            evidence_fingerprint=synthetic_fingerprint(event=EVENT, claim=claim.value),
            member_settlement_fingerprints={
                t: synthetic_fingerprint(market=t).digest for t in members
            },
            status=RelationStatus.VERIFIED,
            reviewer="test",
            reviewed_at=T0,
            issued_at=T0,
            valid_from=T0,
            valid_to=None,
            policy_schema_version="relation-evidence/1",
            evidence="fixture",
        )

    def test_matching_sets_derive(self):
        proof, refusals = derive_exactly_one(
            at_most_one=self.parent(RelationClaim.AT_MOST_ONE, S1),
            at_least_one=self.parent(RelationClaim.AT_LEAST_ONE, S1),
            current_member_fingerprints={t: synthetic_fingerprint(market=t).digest for t in S1},
            at=T0 + timedelta(hours=1),
        )
        assert refusals == ()
        assert proof is not None
        assert proof.members == ("A", "B", "C")

    def test_a_superset_parent_refuses(self):
        proof, refusals = derive_exactly_one(
            at_most_one=self.parent(RelationClaim.AT_MOST_ONE, S2),
            at_least_one=self.parent(RelationClaim.AT_LEAST_ONE, S1),
            current_member_fingerprints={t: synthetic_fingerprint(market=t).digest for t in S2},
            at=T0 + timedelta(hours=1),
        )
        assert proof is None
        assert any(DerivationRefusal.MEMBER_SETS_DIFFER.value in r for r in refusals)

    def test_reversed_member_order_still_derives(self):
        """Canonicalisation happens at certificate construction, so order is moot."""
        proof, refusals = derive_exactly_one(
            at_most_one=self.parent(RelationClaim.AT_MOST_ONE, ["C", "B", "A"]),
            at_least_one=self.parent(RelationClaim.AT_LEAST_ONE, ["A", "B", "C"]),
            current_member_fingerprints={t: synthetic_fingerprint(market=t).digest for t in S1},
            at=T0 + timedelta(hours=1),
        )
        assert refusals == ()
        assert proof is not None


class TestReplayUsesTheSameIdentity:
    def test_the_resolver_passes_the_plan_member_set(self):
        """Replay resolution must use the same exact identity as the registry."""
        knowledge, stream = base(
            certificate_observation(0, S1, claim="AT_LEAST_ONE"),
            certificate_observation(1, S2, claim="AT_LEAST_ONE"),
        )
        horizon = final_horizon(stream)
        for members in (S1, S2):
            plan = DetectorPlan(
                yes_baskets=(BasketPlan(event_ticker=EVENT, members=tuple(members)),)
            )
            provider = KalshiReplayContext(knowledge=knowledge, plan=plan)
            context = provider.basket_context(
                plan.yes_baskets[0], horizon, RelationClaim.AT_LEAST_ONE
            )
            assert context.relation_certificate is not None
            assert list(context.relation_certificate.selected_members) == members

    def test_a_plan_over_an_uncertified_set_reports_the_others(self):
        """Saying "none at all" when one exists elsewhere would mislead.

        Reaching the *known-absent* verdict needs a registry snapshot: without
        one the honest answer is "we never checked", which is a different and
        weaker statement.
        """
        knowledge, stream = base(
            certificate_observation(0, S2, claim="AT_LEAST_ONE"),
            Observation(
                ordinal=1,
                observed_at=T0,
                kind=ObservationKind.RELATION_REGISTRY_SNAPSHOT,
                payload={"subjects": [EVENT], "certificates": [{"selected_members": S2}]},
            ),
        )
        plan = DetectorPlan(yes_baskets=(BasketPlan(event_ticker=EVENT, members=tuple(S1)),))
        provider = KalshiReplayContext(knowledge=knowledge, plan=plan)
        context = provider.basket_context(
            plan.yes_baskets[0], final_horizon(stream), RelationClaim.AT_LEAST_ONE
        )
        assert context.relation_certificate is None
        assert any("other identities" in a for a in context.known_absent)
