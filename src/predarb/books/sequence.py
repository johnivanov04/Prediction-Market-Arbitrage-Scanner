"""Sequence integrity for one ``(connection epoch, sid)`` stream.

Scope, established experimentally
---------------------------------
``seq`` is keyed on ``(connection epoch, sid)``. Not on the market, and not on
the connection alone. The evidence is in ``docs/api_assumptions.md`` A-09:
across 2,764 production frames, the SID grouping produced 2,752 adjacent pairs
all advancing by exactly one, with zero skips, duplicates or decreases, while
every other candidate grouping showed violations on the same data.

Two consequences that shape this module:

* **A gap belongs to the sid, not to a market.** One subscription covers many
  markets under one counter, so a missing frame could have carried any of them.
  Invalidating only the market named in the *next* frame would leave the
  genuinely affected book silently stale.
* **Every seq-bearing frame advances the counter**, including control frames.
  The documented ``ok`` response to ``update_subscription`` carries ``sid`` and
  ``seq`` (A-37). A reconstructor that filters to order-book messages and only
  then checks ``seq`` sees a phantom gap every time a control frame passes.

Density is observed, not guaranteed
-----------------------------------
Nothing in Kalshi's documentation promises that ``seq`` is dense. We treat it as
dense anyway, because the cost of being wrong is asymmetric: a false violation
costs a resynchronisation, while a missed gap costs a book that looks tradeable
and is not. See :class:`SequenceTracker` for where that choice is enforced.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "SequenceCheck",
    "SequenceOutcome",
    "SequenceTracker",
    "SequenceViolation",
]


class SequenceOutcome(StrEnum):
    """What a sequence check concluded."""

    ACCEPTED_FIRST = "ACCEPTED_FIRST"
    """First frame seen for this stream; its value establishes the baseline."""

    ACCEPTED_NEXT = "ACCEPTED_NEXT"
    """Exactly ``previous + 1``."""

    VIOLATION = "VIOLATION"
    """Anything else. The frame must not mutate any book."""


class SequenceViolation(StrEnum):
    """How a sequence check failed.

    All three are treated identically -- the sid is invalidated -- but they are
    distinguished because they point at different underlying causes, and an
    audit of a real incident will want to know which occurred.
    """

    SKIP = "SKIP"
    """``actual > expected``: at least one frame was lost."""

    DUPLICATE = "DUPLICATE"
    """``actual == previous``: the same frame arrived twice."""

    DECREASE = "DECREASE"
    """``actual < previous``: out-of-order arrival, or a counter reset we did
    not expect. Kalshi resets ``seq`` on reconnect, but a reconnect creates a
    new epoch and therefore a new tracker, so a decrease within one epoch is
    never a legitimate reset."""


@dataclass(frozen=True, slots=True)
class SequenceCheck:
    """The result of checking one frame's ``seq``."""

    outcome: SequenceOutcome
    actual: int
    expected: int | None
    """What was required. ``None`` only for the first frame of a stream."""

    violation: SequenceViolation | None = None

    @property
    def accepted(self) -> bool:
        return self.outcome is not SequenceOutcome.VIOLATION

    def describe(self) -> str:
        """One line for logs and audit records."""
        if self.accepted:
            return f"seq={self.actual} accepted ({self.outcome.value})"
        return (
            f"seq violation {self.violation.value if self.violation else '?'}: "
            f"expected {self.expected}, got {self.actual}"
        )


class SequenceTracker:
    """Tracks the expected ``seq`` for one ``(connection epoch, sid)`` stream.

    Deliberately minimal and synchronous. It is called from the single
    authoritative receive path, before any routing, so it must not do anything
    that could reorder or defer.

    **A violation does not advance the counter.** Once a stream is violated it
    stays violated until explicitly reset by a recovery boundary. That is what
    stops a reconstructor from "catching up" to a corrupted stream and quietly
    declaring a book valid again: recovery has to be an explicit act, not a
    side effect of the next frame arriving.
    """

    __slots__ = ("_last_seq", "_violated", "sid")

    def __init__(self, sid: int) -> None:
        self.sid = sid
        self._last_seq: int | None = None
        self._violated = False

    @property
    def last_seq(self) -> int | None:
        return self._last_seq

    @property
    def expected_seq(self) -> int | None:
        """The only value the next frame may carry, or ``None`` before the first."""
        return None if self._last_seq is None else self._last_seq + 1

    @property
    def is_violated(self) -> bool:
        return self._violated

    @property
    def has_started(self) -> bool:
        return self._last_seq is not None

    def check(self, seq: int) -> SequenceCheck:
        """Validate ``seq`` and, if valid, advance.

        Once violated, every subsequent frame is reported as a violation too,
        without re-deriving a kind from a baseline that is no longer meaningful.
        """
        if isinstance(seq, bool) or not isinstance(seq, int):
            raise TypeError(f"seq must be an int, got {type(seq).__name__}")

        if self._violated:
            return SequenceCheck(
                outcome=SequenceOutcome.VIOLATION,
                actual=seq,
                expected=self.expected_seq,
                violation=SequenceViolation.SKIP,
            )

        if self._last_seq is None:
            # The first frame of a stream establishes the baseline. Kalshi was
            # observed starting every sid at 1, but that is not assumed: a
            # resubscribe mid-session could legitimately begin elsewhere, and
            # requiring 1 would reject a stream we can still follow correctly.
            self._last_seq = seq
            return SequenceCheck(outcome=SequenceOutcome.ACCEPTED_FIRST, actual=seq, expected=None)

        expected = self._last_seq + 1
        if seq == expected:
            self._last_seq = seq
            return SequenceCheck(
                outcome=SequenceOutcome.ACCEPTED_NEXT, actual=seq, expected=expected
            )

        if seq == self._last_seq:
            violation = SequenceViolation.DUPLICATE
        elif seq < self._last_seq:
            violation = SequenceViolation.DECREASE
        else:
            violation = SequenceViolation.SKIP

        self._violated = True
        return SequenceCheck(
            outcome=SequenceOutcome.VIOLATION,
            actual=seq,
            expected=expected,
            violation=violation,
        )

    def reset(self) -> None:
        """Clear the stream after a recovery boundary.

        Only a recovery coordinator calls this, and only after arranging for
        fresh authoritative snapshots. Calling it anywhere else would discard
        the memory of a violation without fixing what caused it.
        """
        self._last_seq = None
        self._violated = False

    def __repr__(self) -> str:
        return (
            f"SequenceTracker(sid={self.sid}, last_seq={self._last_seq}, violated={self._violated})"
        )
