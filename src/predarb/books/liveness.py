"""Connection liveness, kept separate from book freshness.

Three different clocks, three different questions
-------------------------------------------------
``book change time``
    When this market's book last changed. **Not a health signal.** A resting
    quote can sit unchanged for hours and still be exactly correct, so treating
    an old value here as staleness would exclude quiet markets for no reason.

``sid activity time``
    When the owning subscription last carried a sequenced frame. Useful context;
    still not proof the socket is alive, since a whole subscription can be quiet.

``connection liveness``
    Whether the socket itself is healthy. This is the one that gates
    eligibility.

Where the liveness signal comes from
------------------------------------
The WebSocket protocol has its own ping/pong control frames, and the ``websockets``
library answers pings and tracks whether a pong is overdue. That machinery knows
the connection is alive even when no application JSON has arrived, which is
precisely the case that a naive "no message in N seconds" check gets wrong.

The documentation states the library handles ping/pong automatically but does
**not** state how often the server pings (A-39). So any timeout configured here
is **our local safety policy**, not an exchange guarantee, and it is labelled as
such wherever it is surfaced.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

__all__ = ["ConnectionLiveness", "LivenessPolicy", "LivenessState"]


class LivenessState(StrEnum):
    """Health of the transport, independent of market activity."""

    HEALTHY = "HEALTHY"
    SUSPECT = "SUSPECT"
    """Past our local timeout without a positive liveness signal. Books sourced
    from this connection stop being scan-eligible, but are not invalidated --
    nothing has been proven wrong, we simply cannot prove it right."""

    CLOSED = "CLOSED"


@dataclass(frozen=True, slots=True)
class LivenessPolicy:
    """Local thresholds. **Not** exchange guarantees.

    ``max_silence`` is generous on purpose: it is a backstop for a connection
    that is wedged without the library noticing, not a substitute for the
    protocol's own keepalive.
    """

    max_silence: timedelta = timedelta(seconds=60)

    def __post_init__(self) -> None:
        if self.max_silence <= timedelta(0):
            raise ValueError("max_silence must be positive")


@dataclass(slots=True)
class ConnectionLiveness:
    """Tracks one connection's health.

    ``last_signal_at`` is updated by *any* positive evidence the socket works:
    an application frame, or a transport-level pong. Keeping both in one field
    is deliberate -- either one proves the same thing, and a quiet market must
    not look like a dead socket.
    """

    connection_epoch: int
    opened_at: datetime
    last_signal_at: datetime
    policy: LivenessPolicy = LivenessPolicy()
    closed_at: datetime | None = None

    def record_signal(self, at: datetime) -> None:
        """Note positive evidence the connection is alive."""
        self.last_signal_at = at

    def close(self, at: datetime) -> None:
        self.closed_at = at

    def age(self, now: datetime) -> timedelta:
        return now - self.last_signal_at

    def state(self, now: datetime) -> LivenessState:
        if self.closed_at is not None:
            return LivenessState.CLOSED
        if self.age(now) > self.policy.max_silence:
            return LivenessState.SUSPECT
        return LivenessState.HEALTHY

    def is_healthy(self, now: datetime) -> bool:
        return self.state(now) is LivenessState.HEALTHY
