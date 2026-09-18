"""Recovery from an integrity violation.

Policy: full connection reconnect
---------------------------------
When a sid's sequence breaks, the recovery is to drop the WebSocket and start a
new connection epoch. That is heavier than it needs to be in some cases, and it
is chosen anyway, for three reasons:

* **The hole belongs to a shared sid.** One subscription covers many markets
  under one counter, so a missing frame could have carried an update for any of
  them. There is no way to work out which from the outside.
* **A reconnect is a clean boundary.** New epoch, fresh sids, fresh snapshots
  for everything. The state after recovery is derived entirely from data the
  venue sent after the boundary, which is trivial to reason about and to audit.
* **Clever repair is where correctness goes to die.** Patching the gap from
  another source, or guessing which markets were affected, buys latency at the
  cost of a book whose provenance nobody can explain.

Why REST cannot repair a WebSocket gap
--------------------------------------
A REST order book is a snapshot at *some other instant*, with no sequence
coordinate shared with the WebSocket stream. Splicing it in produces a book that
is neither the REST state nor the WS state, and no subsequent delta can be known
to apply to it. REST is useful for advisory cross-checking; it is not a repair
mechanism.

``get_snapshot`` is documented (``update_subscription`` action) and would avoid
a full reconnect, since its snapshot arrives *in* the sid's sequence. It is
deliberately not the default yet: correctness first, and its behaviour under a
violated sid has not been established. See ``docs/api_assumptions.md`` A-40.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from predarb.books.events import EventKind, ReconstructionEvent
from predarb.books.state import InvalidationReason

__all__ = ["RecoveryAction", "RecoveryCoordinator", "RecoveryPolicy", "RecoveryRequest"]


class RecoveryAction(StrEnum):
    """What the collector should do next."""

    NONE = "NONE"
    RECONNECT = "RECONNECT"
    """Drop the socket and start a fresh epoch. The Phase 1 default."""

    STOP = "STOP"
    """Shut the collector down. Used when continuing could present a book whose
    inputs were not fully recorded -- a journal failure."""


@dataclass(frozen=True, slots=True)
class RecoveryPolicy:
    """Thresholds for reconnect behaviour.

    ``max_attempts`` bounds a reconnect loop against a persistently broken feed:
    repeatedly reconnecting into the same failure is not recovery, it is a busy
    wait that hides a real problem.
    """

    initial_backoff: timedelta = timedelta(seconds=1)
    max_backoff: timedelta = timedelta(seconds=30)
    max_attempts: int = 8
    stop_on_journal_failure: bool = True
    """A journal failure stops the collector rather than reconnecting.

    Reconnecting would produce fresh, correct-looking books whose history has a
    hole in it. Presenting a trustworthy book is worth less than being able to
    prove it later."""

    def backoff_for(self, attempt: int) -> timedelta:
        seconds = self.initial_backoff.total_seconds() * (2 ** max(0, attempt - 1))
        return timedelta(seconds=min(seconds, self.max_backoff.total_seconds()))


@dataclass(frozen=True, slots=True)
class RecoveryRequest:
    """A decision to recover, with the evidence that prompted it."""

    action: RecoveryAction
    at: datetime
    reason: InvalidationReason | None = None
    connection_epoch: int | None = None
    sid: int | None = None
    affected_markets: tuple[str, ...] = ()
    attempt: int = 0
    backoff: timedelta = timedelta(0)
    detail: str = ""


@dataclass(slots=True)
class RecoveryCoordinator:
    """Decides when a violation warrants a reconnect.

    Deliberately passive: it observes events and *returns* a decision rather
    than driving the socket itself. The collector owns the connection, so it
    owns the act of dropping it; keeping the decision separate makes the policy
    testable without a socket.
    """

    policy: RecoveryPolicy = field(default_factory=RecoveryPolicy)
    attempts: int = 0
    last_request: RecoveryRequest | None = None
    history: list[RecoveryRequest] = field(default_factory=list)

    def observe(self, event: ReconstructionEvent) -> RecoveryRequest | None:
        """Inspect one event and decide whether recovery is needed."""
        if event.kind is EventKind.JOURNAL_FAILURE:
            return self._request(
                RecoveryAction.STOP
                if self.policy.stop_on_journal_failure
                else RecoveryAction.RECONNECT,
                event,
                detail=(
                    "raw journal failed; continuing would present books whose "
                    "inputs were not recorded"
                ),
            )

        if event.kind in {EventKind.SEQUENCE_VIOLATION, EventKind.UNKNOWN_SEQUENCED_FRAME}:
            return self._request(
                RecoveryAction.RECONNECT,
                event,
                detail=(
                    "sequence integrity lost on a shared sid; reconnecting for a "
                    "clean boundary and fresh snapshots"
                ),
            )

        if event.kind is EventKind.RECONNECT_COMPLETED:
            # A completed reconnect resets the attempt counter: the next failure
            # is a new incident, not a continuation of the previous one.
            self.attempts = 0
            return None

        return None

    def _request(
        self, action: RecoveryAction, event: ReconstructionEvent, *, detail: str
    ) -> RecoveryRequest:
        if action is RecoveryAction.RECONNECT:
            self.attempts += 1
            if self.attempts > self.policy.max_attempts:
                action = RecoveryAction.STOP
                detail = (
                    f"exceeded {self.policy.max_attempts} reconnect attempts without "
                    "a clean run; the feed is persistently broken"
                )
        request = RecoveryRequest(
            action=action,
            at=event.at,
            reason=event.reason,
            connection_epoch=event.connection_epoch,
            sid=event.sid,
            affected_markets=event.affected_markets,
            attempt=self.attempts,
            backoff=(
                self.policy.backoff_for(self.attempts)
                if action is RecoveryAction.RECONNECT
                else timedelta(0)
            ),
            detail=detail,
        )
        self.last_request = request
        self.history.append(request)
        return request

    def note_recovered(self) -> None:
        """Called once a reconnect has completed and snapshots are flowing."""
        self.attempts = 0

    @property
    def should_stop(self) -> bool:
        return self.last_request is not None and self.last_request.action is RecoveryAction.STOP
