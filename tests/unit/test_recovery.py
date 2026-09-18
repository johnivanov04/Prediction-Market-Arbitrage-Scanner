"""Tests for the recovery policy."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from predarb.books.events import EventKind, ReconstructionEvent
from predarb.books.recovery import RecoveryAction, RecoveryCoordinator, RecoveryPolicy
from predarb.books.sequence import SequenceViolation
from predarb.books.state import InvalidationReason

pytestmark = pytest.mark.unit

T0 = datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC)


def event(kind: EventKind, **kwargs: object) -> ReconstructionEvent:
    return ReconstructionEvent(kind=kind, at=T0, **kwargs)  # type: ignore[arg-type]


class TestSequenceViolationTriggersReconnect:
    def test_reconnect_requested(self) -> None:
        coordinator = RecoveryCoordinator()
        request = coordinator.observe(
            event(
                EventKind.SEQUENCE_VIOLATION,
                sid=1,
                violation=SequenceViolation.SKIP,
                reason=InvalidationReason.SEQUENCE_VIOLATION,
            )
        )
        assert request is not None
        assert request.action is RecoveryAction.RECONNECT

    def test_reason_explains_the_shared_sid(self) -> None:
        coordinator = RecoveryCoordinator()
        request = coordinator.observe(event(EventKind.SEQUENCE_VIOLATION, sid=1))
        assert request is not None
        assert "shared sid" in request.detail

    def test_unknown_sequenced_frame_also_reconnects(self) -> None:
        coordinator = RecoveryCoordinator()
        request = coordinator.observe(event(EventKind.UNKNOWN_SEQUENCED_FRAME, sid=1))
        assert request is not None
        assert request.action is RecoveryAction.RECONNECT

    def test_benign_events_request_nothing(self) -> None:
        coordinator = RecoveryCoordinator()
        for kind in (
            EventKind.SNAPSHOT_APPLIED,
            EventKind.DELTA_APPLIED,
            EventKind.CONTROL_SEQ_CONSUMED,
            EventKind.SUBSCRIBED,
        ):
            assert coordinator.observe(event(kind)) is None


class TestJournalFailureStops:
    def test_journal_failure_stops_by_default(self) -> None:
        """Reconnecting would produce correct-looking books with a hole behind them."""
        coordinator = RecoveryCoordinator()
        request = coordinator.observe(
            event(EventKind.JOURNAL_FAILURE, reason=InvalidationReason.JOURNAL_FAILURE)
        )
        assert request is not None
        assert request.action is RecoveryAction.STOP
        assert coordinator.should_stop

    def test_can_be_configured_to_reconnect_instead(self) -> None:
        coordinator = RecoveryCoordinator(policy=RecoveryPolicy(stop_on_journal_failure=False))
        request = coordinator.observe(event(EventKind.JOURNAL_FAILURE))
        assert request is not None
        assert request.action is RecoveryAction.RECONNECT


class TestBackoff:
    def test_backoff_grows(self) -> None:
        policy = RecoveryPolicy(initial_backoff=timedelta(seconds=1))
        assert policy.backoff_for(1) == timedelta(seconds=1)
        assert policy.backoff_for(2) == timedelta(seconds=2)
        assert policy.backoff_for(3) == timedelta(seconds=4)

    def test_backoff_is_capped(self) -> None:
        policy = RecoveryPolicy(
            initial_backoff=timedelta(seconds=1), max_backoff=timedelta(seconds=5)
        )
        assert policy.backoff_for(20) == timedelta(seconds=5)

    def test_successive_violations_back_off_further(self) -> None:
        coordinator = RecoveryCoordinator()
        first = coordinator.observe(event(EventKind.SEQUENCE_VIOLATION, sid=1))
        second = coordinator.observe(event(EventKind.SEQUENCE_VIOLATION, sid=1))
        assert first is not None and second is not None
        assert second.backoff > first.backoff


class TestAttemptLimit:
    def test_persistent_failure_eventually_stops(self) -> None:
        """Reconnecting into the same failure forever is not recovery."""
        coordinator = RecoveryCoordinator(policy=RecoveryPolicy(max_attempts=3))
        actions = [
            coordinator.observe(event(EventKind.SEQUENCE_VIOLATION, sid=1)) for _ in range(5)
        ]
        assert actions[0] is not None and actions[0].action is RecoveryAction.RECONNECT
        assert actions[-1] is not None and actions[-1].action is RecoveryAction.STOP
        assert "persistently broken" in actions[-1].detail

    def test_a_completed_reconnect_resets_the_counter(self) -> None:
        coordinator = RecoveryCoordinator(policy=RecoveryPolicy(max_attempts=2))
        coordinator.observe(event(EventKind.SEQUENCE_VIOLATION, sid=1))
        coordinator.observe(event(EventKind.RECONNECT_COMPLETED))
        assert coordinator.attempts == 0
        request = coordinator.observe(event(EventKind.SEQUENCE_VIOLATION, sid=1))
        assert request is not None
        assert request.action is RecoveryAction.RECONNECT

    def test_note_recovered_resets(self) -> None:
        coordinator = RecoveryCoordinator()
        coordinator.observe(event(EventKind.SEQUENCE_VIOLATION, sid=1))
        coordinator.note_recovered()
        assert coordinator.attempts == 0


class TestHistory:
    def test_requests_are_recorded_for_audit(self) -> None:
        coordinator = RecoveryCoordinator()
        coordinator.observe(event(EventKind.SEQUENCE_VIOLATION, sid=1, market_ticker="A"))
        coordinator.observe(event(EventKind.SEQUENCE_VIOLATION, sid=2))
        assert len(coordinator.history) == 2
        assert coordinator.history[0].sid == 1
        assert coordinator.history[1].sid == 2

    def test_affected_markets_are_carried_through(self) -> None:
        coordinator = RecoveryCoordinator()
        request = coordinator.observe(
            event(EventKind.SEQUENCE_VIOLATION, sid=1, affected_markets=("A", "B"))
        )
        assert request is not None
        assert request.affected_markets == ("A", "B")


class TestNoRestRepair:
    def test_no_rest_based_recovery_action_exists(self) -> None:
        """REST has no sequence coordinate shared with the WS stream.

        Splicing one in yields a book that is neither state, and no later delta
        can be known to apply to it.
        """
        values = {a.value for a in RecoveryAction}
        assert values == {"NONE", "RECONNECT", "STOP"}
        for forbidden in ("REST_SNAPSHOT", "REST_REPAIR", "PATCH"):
            assert forbidden not in values
