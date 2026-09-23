"""AT_LEAST_ONE certificates must replay point-in-time, and must not be repriced.

Step 11 adds a second relation claim but no economics for it. Two things still
have to hold in replay:

1. the recorded claim survives serialisation, so a bundle carrying an
   AT_LEAST_ONE certificate is not silently replayed as AT_MOST_ONE;
2. a future issuance cannot leak backward into an earlier decision.

The basket detector's own refusal is unit-tested; what is checked here is that
the refusal survives the whole replay path, from a recorded observation through
the Kalshi resolver into a DecisionRecord.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from predarb.opportunities.models import Classification
from predarb.replay.bundle import load_bundle, write_bundle
from predarb.replay.decision import compare_decisions
from predarb.replay.knowledge import KnowledgeBase
from predarb.replay.observation import ObservationKind, ObservationStream
from predarb.semantics.relation import RelationClaim
from predarb.venues.kalshi.replay_context import KalshiReplayContext
from tests.integration.replay_fixtures import (
    BASKET_MEMBERS,
    BASKET_PLAN,
    EVENT,
    basket_stream,
    evidence_digest,
    run,
    run_live,
)

pytestmark = pytest.mark.integration


def with_claim(stream: ObservationStream, claim: str) -> ObservationStream:
    """Rewrite the recorded relation certificate's claim, changing nothing else."""
    return ObservationStream.from_iterable(
        replace(observation, payload={**observation.payload, "claim": claim})
        if observation.kind is ObservationKind.RELATION_CERTIFICATE
        else observation
        for observation in stream
    )


class TestTheRecordedClaimIsUsed:
    def test_an_unlabelled_certificate_still_replays_as_at_most_one(self):
        """Backwards compatibility: bundles captured before Step 11 carry no claim."""
        stream = basket_stream()
        knowledge = KnowledgeBase.from_stream(stream)
        provider = KalshiReplayContext(knowledge=knowledge, plan=BASKET_PLAN)
        horizon = stream.horizon_at(stream.observations[-1].ordinal)

        context = provider.basket_context(BASKET_PLAN.baskets[0], horizon)
        assert context.relation_certificate is not None
        assert context.relation_certificate.claim is RelationClaim.AT_MOST_ONE

    def test_an_at_least_one_certificate_resolves_as_at_least_one(self):
        stream = with_claim(basket_stream(), RelationClaim.AT_LEAST_ONE.value)
        knowledge = KnowledgeBase.from_stream(stream)
        provider = KalshiReplayContext(knowledge=knowledge, plan=BASKET_PLAN)
        horizon = stream.horizon_at(stream.observations[-1].ordinal)

        context = provider.basket_context(BASKET_PLAN.baskets[0], horizon)
        assert context.relation_certificate is not None
        assert context.relation_certificate.claim is RelationClaim.AT_LEAST_ONE

    def test_an_unknown_claim_fails_closed_rather_than_defaulting(self):
        """A claim we do not model must not quietly become one we do."""
        stream = with_claim(basket_stream(), "EXACTLY_ONE")
        knowledge = KnowledgeBase.from_stream(stream)
        provider = KalshiReplayContext(knowledge=knowledge, plan=BASKET_PLAN)
        horizon = stream.horizon_at(stream.observations[-1].ordinal)

        with pytest.raises(ValueError, match="EXACTLY_ONE"):
            provider.basket_context(BASKET_PLAN.baskets[0], horizon)


class TestTheBasketDetectorRefusesItEndToEnd:
    def test_every_basket_decision_blocks_on_the_wrong_claim(self):
        """The refusal survives the whole replay path, not just the unit call."""
        result = run(BASKET_PLAN, with_claim(basket_stream(), RelationClaim.AT_LEAST_ONE.value))

        ran = [d for d in result.decisions if d.detector_did_run]
        assert ran, "the detector should still run and then refuse"
        assert all(
            d.classification == Classification.BLOCKED_SETTLEMENT_SEMANTICS.value for d in ran
        )
        assert all("AT_LEAST_ONE" in (d.blocking_reason or "") for d in ran)
        assert all("not interchangeable" in (d.blocking_reason or "") for d in ran)

    def test_no_decision_claims_a_proven_arbitrage(self):
        result = run(BASKET_PLAN, with_claim(basket_stream(), RelationClaim.AT_LEAST_ONE.value))
        assert Classification.PROVEN_CONTRACTUAL_ARBITRAGE.value not in result.classification_counts

    def test_the_at_most_one_stream_still_proves(self):
        """The control: the refusal is about the claim, not about the fixture."""
        assert (
            Classification.PROVEN_CONTRACTUAL_ARBITRAGE.value
            in run(BASKET_PLAN, basket_stream()).classification_counts
        )


class TestNoBackwardLeak:
    def test_live_and_replay_agree_on_the_new_claim_too(self):
        """Same no-lookahead guarantee, with an AT_LEAST_ONE certificate present."""
        stream = with_claim(basket_stream(), RelationClaim.AT_LEAST_ONE.value)
        live = run_live(BASKET_PLAN, stream)
        replayed = run(BASKET_PLAN, stream)

        assert live.decisions
        assert compare_decisions(live.decisions, replayed.decisions) == ()

    def test_a_later_certificate_does_not_unblock_earlier_decisions(self):
        stream = with_claim(basket_stream(), RelationClaim.AT_LEAST_ONE.value)
        knowledge = KnowledgeBase.from_stream(stream)
        provider = KalshiReplayContext(knowledge=knowledge, plan=BASKET_PLAN)

        issued_at = next(
            o for o in stream if o.kind is ObservationKind.RELATION_CERTIFICATE
        ).ordinal
        before = stream.horizon_at(issued_at - 1)
        after = stream.horizon_at(stream.observations[-1].ordinal)

        assert provider.basket_context(BASKET_PLAN.baskets[0], before).relation_certificate is None
        assert (
            provider.basket_context(BASKET_PLAN.baskets[0], after).relation_certificate is not None
        )


class TestMembershipObservationsAreReplayable:
    def test_a_membership_snapshot_round_trips_through_a_bundle(self, tmp_path: Any) -> None:
        """Membership evidence is point-in-time like everything else."""
        stream = basket_stream()
        write_bundle(tmp_path, stream, detector_plan=BASKET_PLAN.to_payload())
        reloaded = load_bundle(tmp_path).stream

        original = KnowledgeBase.from_stream(stream)
        restored = KnowledgeBase.from_stream(reloaded)
        horizon = stream.horizon_at(stream.observations[-1].ordinal)

        assert original.relation_certificate_at(EVENT, horizon) == restored.relation_certificate_at(
            EVENT, horizon
        )
        assert original.relation_snapshot_at(EVENT, horizon) == restored.relation_snapshot_at(
            EVENT, horizon
        )

    def test_the_fixture_members_are_what_the_plan_monitors(self):
        assert set(BASKET_PLAN.baskets[0].members) == set(BASKET_MEMBERS)
        assert evidence_digest("v1")
