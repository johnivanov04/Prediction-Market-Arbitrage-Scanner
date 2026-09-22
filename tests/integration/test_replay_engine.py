"""End-to-end replay through the real engine, coordinator and Kalshi resolver.

No custom test resolvers: every test drives the production
:class:`ReplayEngine` with :class:`KalshiReplayContext`, so what is verified is
the orchestration that live scanning uses, not a parallel one written for tests.
"""

from __future__ import annotations

import socket
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

import predarb.replay.coordinator as coordinator_module
import predarb.replay.engine as engine_module
import predarb.venues.kalshi.replay_context as resolver_module
from predarb.opportunities.models import Classification
from predarb.replay.bundle import load_bundle, write_bundle
from predarb.replay.completeness import (
    BASKET_DIMENSIONS,
    BINARY_COMPLEMENT_DIMENSIONS,
    CompletenessDimension,
    DimensionStatus,
)
from predarb.replay.decision import MISSING_KNOWLEDGE, DetectorKind, compare_decisions
from predarb.replay.knowledge import KnowledgeBase
from predarb.replay.observation import Observation, ObservationKind, ObservationStream
from predarb.replay.plan import ContextRefreshPolicy, DetectorPlan
from predarb.venues.kalshi.replay_context import KalshiReplayContext
from tests.integration.replay_fixtures import (
    BASKET_MEMBERS,
    BASKET_PLAN,
    BINARY_PLAN,
    BINARY_TICKER,
    basket_stream,
    binary_stream,
    build_engine,
    run,
    run_live,
)

pytestmark = pytest.mark.integration


def without(stream: ObservationStream, *kinds: ObservationKind) -> ObservationStream:
    """Drop observation kinds, keeping capture order and re-numbering."""
    kept = [o for o in stream if o.kind not in set(kinds)]
    return ObservationStream.from_iterable(
        Observation(
            ordinal=index,
            observed_at=o.observed_at,
            kind=o.kind,
            payload=o.payload,
            source=o.source,
            connection_epoch=o.connection_epoch,
        )
        for index, o in enumerate(kept)
    )


class TestBinaryFullPathThroughTheEngine:
    """no certificate -> certified -> proven -> fee change -> drift -> blocked."""

    @pytest.fixture
    def result(self):
        return run(BINARY_PLAN, binary_stream())

    def test_the_engine_drives_the_binary_detector(self, result):
        records = result.decisions_for(DetectorKind.BINARY_COMPLEMENT)
        assert records
        assert any(record.detector_did_run for record in records)

    def test_the_first_decision_is_determinate_without_the_detector(self, result):
        """An explicitly empty registry settles the question by itself."""
        first = result.decisions[0]
        assert first.detector_did_run is False
        assert first.classification == "BLOCKED_SETTLEMENT_SEMANTICS"
        assert first.classification != MISSING_KNOWLEDGE
        assert "known absent" in (first.blocking_reason or "")

    def test_a_proven_opportunity_appears_once_certified(self, result):
        assert "PROVEN_CONTRACTUAL_ARBITRAGE" in result.classification_counts

    def test_the_fee_change_changes_the_verdict(self, result):
        """Multiplier moves off 1, so the A-14 gate blocks the claim."""
        assert "BLOCKED_FEE_SEMANTICS" in result.classification_counts

    def test_evidence_drift_blocks_afterwards(self, result):
        assert result.decisions[-1].classification == "BLOCKED_SETTLEMENT_SEMANTICS"
        assert result.decisions[-1].detector_did_run is True

    def test_every_decision_is_fingerprinted(self, result):
        assert all(len(d.fingerprint.digest) == 64 for d in result.decisions)

    def test_blocked_decisions_are_fingerprinted_too(self, result):
        """Equivalence must be checkable even when nothing is profitable."""
        blocked = [d for d in result.decisions if not d.detector_did_run]
        assert blocked
        assert all(d.fingerprint.digest for d in blocked)

    def test_context_ids_are_retained_for_comparison(self, result):
        ran = next(d for d in result.decisions if d.detector_did_run)
        assert ran.context_ids["fee_multiplier"] is not None
        assert ran.context_ids["settlement_certificate"] is not None


class TestBasketFullPathThroughTheEngine:
    """empty relation registry -> relation -> members -> proven -> book -> drift."""

    @pytest.fixture
    def result(self):
        return run(BASKET_PLAN, basket_stream())

    def test_the_engine_drives_the_basket_detector(self, result):
        records = result.decisions_for(DetectorKind.AT_MOST_ONE_BASKET)
        assert records
        assert any(record.detector_did_run for record in records)

    def test_the_basket_is_evaluated_once_per_trigger(self, result):
        """A trigger touching several members is still one basket."""
        by_trigger: dict[int, int] = {}
        for record in result.decisions_for(DetectorKind.AT_MOST_ONE_BASKET):
            by_trigger[record.trigger_ordinal] = by_trigger.get(record.trigger_ordinal, 0) + 1
        assert max(by_trigger.values()) == 1

    def test_it_blocks_before_the_relation_certificate(self, result):
        assert result.decisions[0].classification == "BLOCKED_SETTLEMENT_SEMANTICS"
        assert result.decisions[0].detector_did_run is False

    def test_a_relation_alone_does_not_unblock_it(self, result):
        """Member payout certificates are a separate obligation."""
        early = [d for d in result.decisions if d.trigger_ordinal <= 20]
        assert early
        assert all(not d.detector_did_run for d in early)

    def test_it_proves_once_members_are_certified(self, result):
        assert result.classification_counts.get("PROVEN_CONTRACTUAL_ARBITRAGE", 0) >= 1

    def test_a_member_book_change_flips_profitability(self, result):
        assert "PROVEN_NOT_PROFITABLE" in result.classification_counts

    def test_member_evidence_drift_blocks_the_relation(self, result):
        assert result.decisions[-1].classification == "BLOCKED_SETTLEMENT_SEMANTICS"
        assert result.decisions[-1].detector_did_run is True

    def test_all_members_are_the_subject(self, result):
        assert all(
            d.subjects == BASKET_MEMBERS
            for d in result.decisions_for(DetectorKind.AT_MOST_ONE_BASKET)
        )


class TestExplicitNegativeKnowledge:
    """Absence observed is not absence assumed."""

    def test_an_empty_registry_yields_a_determinate_block(self):
        result = run(BINARY_PLAN, binary_stream())
        first = result.decisions[0]
        assert first.classification == "BLOCKED_SETTLEMENT_SEMANTICS"
        assert (
            first.completeness.status(CompletenessDimension.SETTLEMENT_KNOWLEDGE)
            is DimensionStatus.COMPLETE
        )

    def test_a_missing_registry_snapshot_yields_missing_knowledge(self):
        """No snapshot means we never asked, which settles nothing."""
        stream = without(
            binary_stream(),
            ObservationKind.SETTLEMENT_REGISTRY_SNAPSHOT,
            ObservationKind.SETTLEMENT_CERTIFICATE,
        )
        result = run(BINARY_PLAN, stream)
        assert result.decisions
        assert result.decisions[0].classification == MISSING_KNOWLEDGE
        assert result.decisions[0].detector_did_run is False
        assert any(
            m.startswith("settlement_registry:") for m in result.decisions[0].missing_knowledge
        )

    def test_the_two_are_distinguishable_in_completeness(self):
        with_snapshot = run(BINARY_PLAN, binary_stream())
        without_snapshot = run(
            BINARY_PLAN,
            without(
                binary_stream(),
                ObservationKind.SETTLEMENT_REGISTRY_SNAPSHOT,
                ObservationKind.SETTLEMENT_CERTIFICATE,
            ),
        )
        assert (
            with_snapshot.completeness.status(CompletenessDimension.SETTLEMENT_KNOWLEDGE)
            is DimensionStatus.COMPLETE
        )
        assert (
            without_snapshot.completeness.status(CompletenessDimension.SETTLEMENT_KNOWLEDGE)
            is DimensionStatus.INCOMPLETE_MISSING_OBSERVATION
        )

    def test_a_missing_fee_snapshot_yields_missing_knowledge(self):
        stream = without(
            binary_stream(),
            ObservationKind.FEE_OBSERVATION,
            ObservationKind.FEE_KNOWLEDGE_SNAPSHOT,
        )
        result = run(BINARY_PLAN, stream)
        assert any(
            any(m.startswith("fee_knowledge:") for m in d.missing_knowledge)
            for d in result.decisions
        )

    def test_a_missing_relation_snapshot_blocks_the_basket_definitively(self):
        stream = without(
            basket_stream(),
            ObservationKind.RELATION_REGISTRY_SNAPSHOT,
            ObservationKind.RELATION_CERTIFICATE,
        )
        result = run(BASKET_PLAN, stream)
        assert result.decisions[0].classification == MISSING_KNOWLEDGE
        assert not result.completeness.permits_absence_claim_for(BASKET_DIMENSIONS)

    def test_stale_context_is_distinct_from_missing_context(self):
        """Nothing promises these fields cannot change mid-session."""
        strict = replace(
            BINARY_PLAN,
            refresh_policy=ContextRefreshPolicy(
                market_metadata=timedelta(seconds=1),
                fee_knowledge=timedelta(seconds=1),
                settlement_knowledge=timedelta(seconds=1),
                relation_knowledge=timedelta(seconds=1),
            ),
        )
        result = run(strict, binary_stream())
        assert (
            result.completeness.status(CompletenessDimension.MARKET_METADATA)
            is DimensionStatus.INCOMPLETE_STALE_CONTEXT
        )


class TestDetectorSpecificCompleteness:
    def test_missing_relation_knowledge_does_not_poison_binary_replay(self):
        """A complement never consults relation knowledge."""
        result = run(BINARY_PLAN, binary_stream())
        assert result.completeness.permits_absence_claim_for(BINARY_COMPLEMENT_DIMENSIONS)
        assert (
            result.completeness.status(CompletenessDimension.RELATION_KNOWLEDGE)
            is DimensionStatus.NOT_REQUIRED
        )

    def test_missing_settlement_knowledge_blocks_the_binary_absence_claim(self):
        stream = without(
            binary_stream(),
            ObservationKind.SETTLEMENT_REGISTRY_SNAPSHOT,
            ObservationKind.SETTLEMENT_CERTIFICATE,
        )
        result = run(BINARY_PLAN, stream)
        assert not result.completeness.permits_absence_claim_for(BINARY_COMPLEMENT_DIMENSIONS)
        statement = result.absence_statement("the window", DetectorKind.BINARY_COMPLEMENT)
        assert "incomplete" in statement

    def test_a_complete_basket_bundle_permits_the_claim(self):
        result = run(BASKET_PLAN, basket_stream())
        assert result.completeness.permits_absence_claim_for(BASKET_DIMENSIONS)


class TestDeterminism:
    def test_the_same_stream_twice_gives_identical_decisions(self):
        first, second = run(BINARY_PLAN, binary_stream()), run(BINARY_PLAN, binary_stream())
        assert first.digest == second.digest
        assert compare_decisions(first.decisions, second.decisions) == ()

    def test_the_basket_path_is_deterministic_too(self):
        first, second = run(BASKET_PLAN, basket_stream()), run(BASKET_PLAN, basket_stream())
        assert compare_decisions(first.decisions, second.decisions) == ()

    @pytest.mark.parametrize(
        ("plan", "builder"), [(BINARY_PLAN, binary_stream), (BASKET_PLAN, basket_stream)]
    )
    def test_a_bundle_round_trip_preserves_every_decision(
        self, plan: DetectorPlan, builder: Callable[[], ObservationStream], tmp_path: Path
    ) -> None:
        """Path A: in memory. Path B: serialise, reload, replay."""
        stream = builder()
        in_memory = run(plan, stream)

        write_bundle(tmp_path, stream, detector_plan=plan.to_payload())
        bundle = load_bundle(tmp_path)
        from_disk = build_engine(plan, bundle.stream).run(bundle)

        assert compare_decisions(in_memory.decisions, from_disk.decisions) == ()
        assert in_memory.digest == from_disk.digest
        assert in_memory.completeness.as_dict() == from_disk.completeness.as_dict()

    def test_the_manifest_advertises_economic_replay_support(self, tmp_path: Path) -> None:
        manifest = write_bundle(tmp_path, binary_stream(), detector_plan=BINARY_PLAN.to_payload())
        assert manifest.supports_economic_replay
        assert manifest.context_snapshot_counts["SETTLEMENT_REGISTRY_SNAPSHOT"] == 1
        assert "economic-replay-ready" in manifest.describe()

    def test_a_bundle_without_context_says_so(self, tmp_path: Path) -> None:
        stream = without(
            binary_stream(),
            ObservationKind.METADATA_SNAPSHOT,
            ObservationKind.FEE_KNOWLEDGE_SNAPSHOT,
            ObservationKind.SETTLEMENT_REGISTRY_SNAPSHOT,
        )
        manifest = write_bundle(tmp_path, stream, detector_plan=BINARY_PLAN.to_payload())
        assert not manifest.supports_economic_replay
        assert "book-replay only" in manifest.describe()


class TestNoLookahead:
    def test_truncating_history_hides_later_observations(self):
        full = binary_stream()
        early = ObservationStream.from_iterable(o for o in full if o.ordinal <= 8)
        assert len(run(BINARY_PLAN, early).decisions) < len(run(BINARY_PLAN, full).decisions)

    def test_a_later_certificate_does_not_unblock_earlier_decisions(self):
        result = run(BINARY_PLAN, binary_stream())
        assert result.decisions[0].detector_did_run is False

    def test_later_drift_does_not_block_earlier_decisions(self):
        result = run(BINARY_PLAN, binary_stream())
        assert any(d.classification == "PROVEN_CONTRACTUAL_ARBITRAGE" for d in result.decisions)


class TestOfflineGuarantee:
    def test_replay_makes_no_socket_connection(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Aggressive: any connection attempt fails the test."""
        calls: list[Any] = []

        def refuse(*args: Any, **_kwargs: Any) -> Any:
            calls.append(args)
            raise AssertionError("replay attempted a network connection")

        monkeypatch.setattr(socket.socket, "connect", refuse)
        monkeypatch.setattr(socket.socket, "connect_ex", refuse)
        monkeypatch.setattr(socket, "create_connection", refuse)
        monkeypatch.setattr(socket, "getaddrinfo", refuse)

        binary = run(BINARY_PLAN, binary_stream())
        basket = run(BASKET_PLAN, basket_stream())
        assert calls == []
        assert binary.network_calls == 0
        assert basket.network_calls == 0

    def test_the_engine_imports_no_venue_client(self) -> None:
        assert engine_module.__file__ is not None
        source = Path(engine_module.__file__).read_text()
        for forbidden in ("httpx", "websockets", "KalshiReadOnlyClient", "requests"):
            assert forbidden not in source

    def test_the_engine_uses_the_production_detectors(self) -> None:
        assert coordinator_module.__file__ is not None
        source = Path(coordinator_module.__file__).read_text()
        assert "from predarb.detectors.binary_complement import evaluate_quantity" in source
        assert "from predarb.detectors.no_basket import" in source
        for forbidden in ("def detect_binary_replay", "def detect_no_basket_replay"):
            assert forbidden not in source

    def test_the_resolver_imports_no_client(self) -> None:
        assert resolver_module.__file__ is not None
        source = Path(resolver_module.__file__).read_text()
        for forbidden in ("httpx", "KalshiReadOnlyClient", "websockets"):
            assert forbidden not in source


class TestLiveAndReplayAgree:
    """The no-lookahead proof, run as an equivalence rather than asserted.

    The live side is fed one observation at a time and holds only what it has
    been handed. The replay side indexes the whole stream first, so every later
    observation is sitting in its knowledge base, available. Identical decisions
    mean the replay declined to use any of them.
    """

    @pytest.mark.parametrize(
        ("plan", "builder"), [(BINARY_PLAN, binary_stream), (BASKET_PLAN, basket_stream)]
    )
    def test_every_decision_matches_field_for_field(
        self, plan: DetectorPlan, builder: Callable[[], ObservationStream]
    ) -> None:
        stream = builder()
        live = run_live(plan, stream)
        replayed = run(plan, stream)

        assert live.decisions, "the live control produced no decisions to compare against"
        assert compare_decisions(live.decisions, replayed.decisions) == ()

    def test_the_replay_really_did_hold_the_future(self) -> None:
        """Without this the equivalence above would be vacuous.

        If the replay's knowledge base did not in fact contain later facts, it
        could not have cheated and matching would prove nothing. So the *same*
        provider over the *same* full-stream index is asked twice: the
        certificate is invisible at the early horizon and present at the late
        one. Only the horizon changed.
        """
        stream = binary_stream()
        knowledge = KnowledgeBase.from_stream(stream)
        provider = KalshiReplayContext(knowledge=knowledge, plan=BINARY_PLAN)

        live = run_live(BINARY_PLAN, stream)
        first = live.decisions[0]
        assert first.detector_did_run is False
        assert "no settlement certificate" in (first.blocking_reason or "")

        early = stream.horizon_at(first.trigger_ordinal)
        late = stream.horizon_at(stream.observations[-1].ordinal)
        assert provider.binary_context(BINARY_TICKER, early).certificate is None
        assert provider.binary_context(BINARY_TICKER, late).certificate is not None

    def test_a_live_session_and_a_bundle_round_trip_agree(self, tmp_path: Path) -> None:
        """Live -> bundle on disk -> reload -> replay. The capture tool's path."""
        stream = basket_stream()
        live = run_live(BASKET_PLAN, stream)

        write_bundle(tmp_path, stream, detector_plan=BASKET_PLAN.to_payload())
        bundle = load_bundle(tmp_path)
        replayed = build_engine(BASKET_PLAN, bundle.stream).run(bundle)

        assert compare_decisions(live.decisions, replayed.decisions) == ()
        assert live.triggers == replayed.triggers


class TestCertificateWithoutObservedEvidence:
    """A certificate may not vouch for itself."""

    def test_a_certificate_with_no_current_evidence_blocks(self) -> None:
        """The resolver has no fallback, and that is deliberate.

        Substituting the certificate's own fingerprint for unobserved current
        evidence would compare it against itself -- always a match -- turning
        "nobody checked whether the rules moved" into "the rules have not
        moved". Instead the detector runs and reports evidence unavailable.
        """
        stream = without(binary_stream(), ObservationKind.SETTLEMENT_EVIDENCE)
        result = run(BINARY_PLAN, stream)

        after_certificate = [d for d in result.decisions if d.detector_did_run]
        assert after_certificate, "the certificate should still let the detector run"
        assert all(
            d.classification == Classification.BLOCKED_SETTLEMENT_SEMANTICS.value
            for d in after_certificate
        )
        assert all(
            "evidence is unavailable" in (d.blocking_reason or "") for d in after_certificate
        )

    def test_a_relation_without_current_evidence_blocks_too(self) -> None:
        stream = without(basket_stream(), ObservationKind.RELATION_EVIDENCE)
        result = run(BASKET_PLAN, stream)

        ran = [d for d in result.decisions if d.detector_did_run]
        assert ran
        assert all("relation evidence is unavailable" in (d.blocking_reason or "") for d in ran)
