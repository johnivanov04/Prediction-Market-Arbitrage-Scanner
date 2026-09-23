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
from predarb.replay.decision import MISSING_KNOWLEDGE, DetectorKind, compare_decisions
from predarb.replay.knowledge import KnowledgeBase
from predarb.replay.observation import ObservationKind, ObservationStream
from predarb.replay.plan import BasketPlan, DetectorPlan
from predarb.semantics.relation import RelationClaim
from predarb.venues.kalshi.replay_context import KalshiReplayContext
from tests.integration.replay_fixtures import (
    BASKET_MEMBERS,
    BASKET_PLAN,
    EVENT,
    YES_MEMBERS,
    YES_PLAN,
    basket_stream,
    build_engine,
    evidence_digest,
    run,
    run_live,
    yes_basket_stream,
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

    def test_an_at_least_one_certificate_resolves_only_for_that_claim(self):
        """The claim is part of the lookup key, not a filter applied after.

        An AT_LEAST_ONE certificate must be invisible to an AT_MOST_ONE lookup:
        the two forbid opposite terminal states, and a detector handed the wrong
        one would price against a guarantee nobody reviewed.
        """
        stream = with_claim(basket_stream(), RelationClaim.AT_LEAST_ONE.value)
        provider = KalshiReplayContext(
            knowledge=KnowledgeBase.from_stream(stream), plan=BASKET_PLAN
        )
        horizon = stream.horizon_at(stream.observations[-1].ordinal)
        basket = BASKET_PLAN.baskets[0]

        for_at_least_one = provider.basket_context(basket, horizon, RelationClaim.AT_LEAST_ONE)
        assert for_at_least_one.relation_certificate is not None
        assert for_at_least_one.relation_certificate.claim is RelationClaim.AT_LEAST_ONE

        for_at_most_one = provider.basket_context(basket, horizon, RelationClaim.AT_MOST_ONE)
        assert for_at_most_one.relation_certificate is None
        assert any("no AT_MOST_ONE certificate" in a for a in for_at_most_one.known_absent)

    def test_an_unknown_claim_is_simply_not_found(self):
        """A claim we do not model cannot be silently read as one we do."""
        stream = with_claim(basket_stream(), "EXACTLY_ONE")
        provider = KalshiReplayContext(
            knowledge=KnowledgeBase.from_stream(stream), plan=BASKET_PLAN
        )
        horizon = stream.horizon_at(stream.observations[-1].ordinal)

        for claim in (RelationClaim.AT_MOST_ONE, RelationClaim.AT_LEAST_ONE):
            context = provider.basket_context(BASKET_PLAN.baskets[0], horizon, claim)
            assert context.relation_certificate is None


class TestTheBasketDetectorRefusesItEndToEnd:
    def test_a_no_basket_plan_finds_no_certificate_at_all(self):
        """Defence in depth: the resolver never offers it, so the detector
        never has to refuse it."""
        result = run(BASKET_PLAN, with_claim(basket_stream(), RelationClaim.AT_LEAST_ONE.value))

        assert not any(d.detector_did_run for d in result.decisions)
        assert all(
            d.classification == Classification.BLOCKED_SETTLEMENT_SEMANTICS.value
            for d in result.decisions
        )
        assert all(
            "no AT_MOST_ONE certificate" in (d.blocking_reason or "") for d in result.decisions
        )

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
        provider = KalshiReplayContext(
            knowledge=KnowledgeBase.from_stream(stream), plan=BASKET_PLAN
        )
        basket = BASKET_PLAN.baskets[0]
        claim = RelationClaim.AT_LEAST_ONE

        issued_at = next(
            o for o in stream if o.kind is ObservationKind.RELATION_CERTIFICATE
        ).ordinal
        before = stream.horizon_at(issued_at - 1)
        after = stream.horizon_at(stream.observations[-1].ordinal)

        assert provider.basket_context(basket, before, claim).relation_certificate is None
        assert provider.basket_context(basket, after, claim).relation_certificate is not None


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


class TestYesBasketFullPathThroughTheEngine:
    """T0 .. T6, driven through the production engine and Kalshi resolver.

    No replay-specific detector: this is the same ``evaluate_yes_basket_quantity``
    a live session calls, reached through the same coordinator.
    """

    @pytest.fixture
    def result(self):
        return run(YES_PLAN, yes_basket_stream())

    def test_the_engine_drives_the_at_least_one_detector(self, result):
        decisions = result.decisions_for(DetectorKind.AT_LEAST_ONE_BASKET)
        assert len(decisions) == len(result.decisions)
        assert result.detector_run_count == 6

    def test_t0_no_relation_certificate_is_a_determinate_block(self, result):
        first = result.decisions[0]
        assert first.detector_did_run is False
        assert first.classification == Classification.BLOCKED_SETTLEMENT_SEMANTICS.value
        assert "no AT_LEAST_ONE certificate" in (first.blocking_reason or "")

    def test_t1_a_relation_alone_does_not_unblock_it(self, result):
        """Separate proof obligations: the relation says which joint outcomes
        are possible, the member certificates say what YES pays in them."""
        after_relation = result.decisions[3]
        assert after_relation.detector_did_run is False
        assert "no settlement certificate" in (after_relation.blocking_reason or "")
        assert "no AT_LEAST_ONE certificate" not in (after_relation.blocking_reason or "")

    def test_t2_and_t3_prove_a_candidate(self, result):
        proven = [
            d
            for d in result.decisions
            if d.classification == Classification.PROVEN_CONTRACTUAL_ARBITRAGE.value
        ]
        assert len(proven) == 2
        assert all(d.detector_did_run for d in proven)

    def test_t4_a_book_change_eliminates_it(self, result):
        assert result.decisions[8].classification == Classification.PROVEN_NOT_PROFITABLE.value

    def test_t5_a_fee_change_blocks_on_fee_semantics(self, result):
        blocked = result.decisions[9]
        assert blocked.classification == Classification.BLOCKED_FEE_SEMANTICS.value
        assert blocked.detector_did_run is True

    def test_t6_relation_evidence_drift_blocks(self, result):
        drifted = result.decisions[-1]
        assert drifted.classification == Classification.BLOCKED_SETTLEMENT_SEMANTICS.value
        assert "relation evidence has changed" in (drifted.blocking_reason or "")

    def test_every_decision_is_fingerprinted(self, result):
        digests = [d.fingerprint.digest for d in result.decisions]
        assert all(digests)
        assert len(set(digests)) == len(digests)

    def test_the_warnings_disclaim_mutual_exclusion(self, result):
        ran = [d for d in result.decisions if d.detector_did_run and d.warnings]
        assert ran
        assert any("does NOT assert mutual exclusion" in w for d in ran for w in d.warnings)


class TestYesBasketLiveAndReplayAgree:
    def test_every_decision_matches_field_for_field(self):
        """The no-lookahead proof, with the new detector in the plan.

        The live side holds only what it has been handed; the replay side has
        the whole stream indexed. Agreement means it declined to use the future.
        """
        stream = yes_basket_stream()
        live = run_live(YES_PLAN, stream)
        replayed = run(YES_PLAN, stream)

        assert live.decisions
        assert compare_decisions(live.decisions, replayed.decisions) == ()

    def test_a_bundle_round_trip_preserves_every_decision(self, tmp_path: Any) -> None:
        stream = yes_basket_stream()
        in_memory = run(YES_PLAN, stream)

        write_bundle(tmp_path, stream, detector_plan=YES_PLAN.to_payload())
        bundle = load_bundle(tmp_path)
        from_disk = build_engine(YES_PLAN, bundle.stream).run(bundle)

        assert compare_decisions(in_memory.decisions, from_disk.decisions) == ()
        assert in_memory.digest == from_disk.digest

    def test_the_plan_round_trips_with_its_yes_baskets(self):
        restored = DetectorPlan.from_payload(YES_PLAN.to_payload())
        assert restored.yes_baskets == YES_PLAN.yes_baskets
        assert restored.baskets == ()
        assert "1 YES basket(s)" in restored.describe()

    def test_the_two_basket_families_do_not_cross_contaminate(self):
        """An AT_MOST_ONE plan over the same members finds no certificate."""
        no_plan = replace(YES_PLAN, baskets=YES_PLAN.yes_baskets, yes_baskets=())
        result = run(no_plan, yes_basket_stream())

        assert result.decisions_for(DetectorKind.AT_MOST_ONE_BASKET)
        assert not any(d.detector_did_run for d in result.decisions)
        assert all(
            "no AT_MOST_ONE certificate" in (d.blocking_reason or "") for d in result.decisions
        )


class TestNoExponentialWorkInReplay:
    def test_a_large_yes_basket_replays_without_enumerating_states(self):
        """Thirty members: 2**30 - 1 permitted states, none of them built."""
        # The real members plus 27 the stream never carries, so triggers still
        # fire and the coordinator must resolve a 30-member group.
        members = (*YES_MEMBERS, *(f"M{i:02d}" for i in range(27)))
        plan = replace(YES_PLAN, yes_baskets=(BasketPlan(event_ticker=EVENT, members=members),))
        result = run(plan, yes_basket_stream())
        # The fixture only has books for P/Q/R, so the 27 absent members block
        # it. The point is that resolving a 30-member plan is instant and
        # allocates no state set -- 2**30 - 1 states are counted, never built.
        assert result.decisions
        assert all(not d.detector_did_run for d in result.decisions)
        assert all(d.classification == MISSING_KNOWLEDGE for d in result.decisions), {
            d.classification for d in result.decisions
        }
