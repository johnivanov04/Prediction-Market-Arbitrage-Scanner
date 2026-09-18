"""Tests for per-(epoch, sid) sequence integrity."""

from __future__ import annotations

import pytest

from predarb.books.sequence import (
    SequenceOutcome,
    SequenceTracker,
    SequenceViolation,
)

pytestmark = pytest.mark.unit


def feed(tracker: SequenceTracker, seqs: list[int]) -> list[SequenceOutcome]:
    return [tracker.check(s).outcome for s in seqs]


class TestDenseStream:
    def test_accepts_1_2_3(self):
        tracker = SequenceTracker(1)
        assert feed(tracker, [1, 2, 3]) == [
            SequenceOutcome.ACCEPTED_FIRST,
            SequenceOutcome.ACCEPTED_NEXT,
            SequenceOutcome.ACCEPTED_NEXT,
        ]
        assert not tracker.is_violated
        assert tracker.last_seq == 3
        assert tracker.expected_seq == 4

    def test_first_frame_sets_the_baseline_wherever_it_starts(self):
        # Kalshi was observed starting at 1, but a resubscribe could begin
        # elsewhere and we can still follow it correctly.
        tracker = SequenceTracker(1)
        assert tracker.check(500).outcome is SequenceOutcome.ACCEPTED_FIRST
        assert tracker.check(501).outcome is SequenceOutcome.ACCEPTED_NEXT

    def test_expected_seq_is_none_before_the_first_frame(self):
        assert SequenceTracker(1).expected_seq is None
        assert not SequenceTracker(1).has_started


class TestViolations:
    def test_skip(self):
        tracker = SequenceTracker(1)
        tracker.check(1)
        tracker.check(2)
        check = tracker.check(4)
        assert check.outcome is SequenceOutcome.VIOLATION
        assert check.violation is SequenceViolation.SKIP
        assert check.expected == 3
        assert check.actual == 4

    def test_duplicate(self):
        tracker = SequenceTracker(1)
        tracker.check(1)
        tracker.check(2)
        check = tracker.check(2)
        assert check.violation is SequenceViolation.DUPLICATE

    def test_decrease(self):
        tracker = SequenceTracker(1)
        tracker.check(1)
        tracker.check(3 - 1)
        tracker.check(3)
        check = tracker.check(2)
        assert check.violation is SequenceViolation.DECREASE

    def test_violation_does_not_advance_the_counter(self):
        """The violating frame must not be able to mutate anything."""
        tracker = SequenceTracker(1)
        tracker.check(1)
        tracker.check(2)
        tracker.check(9)
        assert tracker.last_seq == 2  # unchanged

    def test_stream_stays_violated(self):
        """A stream does not heal by the next frame happening to line up.

        Recovery must be an explicit act, not a side effect of arrival.
        """
        tracker = SequenceTracker(1)
        tracker.check(1)
        tracker.check(5)
        assert tracker.is_violated
        for seq in (6, 7, 8):
            assert tracker.check(seq).outcome is SequenceOutcome.VIOLATION
        assert tracker.is_violated

    def test_reset_clears_the_violation(self):
        tracker = SequenceTracker(1)
        tracker.check(1)
        tracker.check(5)
        tracker.reset()
        assert not tracker.is_violated
        assert tracker.check(1).outcome is SequenceOutcome.ACCEPTED_FIRST


class TestIndependence:
    def test_two_sids_sequence_independently(self):
        a, b = SequenceTracker(1), SequenceTracker(2)
        assert feed(a, [1, 2, 3])[-1] is SequenceOutcome.ACCEPTED_NEXT
        # sid 2 starting at 1 while sid 1 is at 3 is normal, not a decrease.
        assert feed(b, [1, 2])[-1] is SequenceOutcome.ACCEPTED_NEXT
        assert not a.is_violated
        assert not b.is_violated

    def test_interleaved_sids_do_not_interfere(self):
        a, b = SequenceTracker(1), SequenceTracker(2)
        for seq_a, seq_b in ((1, 1), (2, 2), (3, 3)):
            assert a.check(seq_a).accepted
            assert b.check(seq_b).accepted
        assert a.last_seq == 3
        assert b.last_seq == 3

    def test_violation_on_one_sid_does_not_affect_another(self):
        a, b = SequenceTracker(1), SequenceTracker(2)
        a.check(1)
        a.check(9)
        b.check(1)
        b.check(2)
        assert a.is_violated
        assert not b.is_violated


class TestInputValidation:
    @pytest.mark.parametrize("bad", [1.5, "2", None, True])
    def test_non_int_seq_rejected(self, bad):
        with pytest.raises(TypeError):
            SequenceTracker(1).check(bad)


class TestCheckDescription:
    def test_accepted_description(self):
        assert "accepted" in SequenceTracker(1).check(1).describe()

    def test_violation_description_names_both_values(self):
        tracker = SequenceTracker(1)
        tracker.check(1)
        text = tracker.check(7).describe()
        assert "expected 2" in text
        assert "got 7" in text
