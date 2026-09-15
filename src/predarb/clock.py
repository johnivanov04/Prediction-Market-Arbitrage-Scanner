"""Time handling.

Three timestamps are kept distinct throughout the system, because collapsing
them destroys the ability to reason about staleness:

``exchange_timestamp``
    When the venue says the event happened. Optional -- Kalshi supplies
    ``ts_ms`` on some websocket messages and omits it on others.
``received_timestamp``
    When our process first saw the bytes. Always present.
``processed_timestamp``
    When our process finished applying the message to state. Always present.

Derived quantities (book age, receive latency, processing latency) are computed
from these and never stored as the only record.

All times are timezone-aware UTC. Naive datetimes are rejected rather than
coerced: a naive timestamp in a replay is a silent correctness bug, and Ruff's
``DTZ`` rules are enabled to keep them from being created in the first place.

Monotonic vs wall clock: durations that gate safety decisions (book age, staleness)
must be measured with a monotonic source, because the wall clock can step
backwards under NTP correction and would then under-report staleness.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, Self

__all__ = ["Clock", "FrozenClock", "SystemClock", "Timestamps", "ensure_utc", "from_epoch_ms"]


def ensure_utc(value: datetime) -> datetime:
    """Return ``value`` as timezone-aware UTC, rejecting naive datetimes."""
    if value.tzinfo is None:
        raise ValueError(
            f"naive datetime {value!r} is not allowed; all times must be timezone-aware UTC"
        )
    return value.astimezone(UTC)


def from_epoch_ms(epoch_ms: int) -> datetime:
    """Convert a venue millisecond epoch (e.g. Kalshi ``ts_ms``) to aware UTC."""
    if isinstance(epoch_ms, bool) or not isinstance(epoch_ms, int):
        raise TypeError(f"epoch_ms must be an int, got {type(epoch_ms).__name__}")
    return datetime.fromtimestamp(epoch_ms / 1000, tz=UTC)


class Clock(Protocol):
    """Time source. Injected everywhere so replay can supply historical time."""

    def now(self) -> datetime:
        """Current wall-clock time, timezone-aware UTC."""
        ...

    def monotonic_ns(self) -> int:
        """Monotonic counter in nanoseconds, for measuring durations."""
        ...


class SystemClock:
    """The real clock. Used in live capture and scanning."""

    def now(self) -> datetime:
        return datetime.now(tz=UTC)

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()


@dataclass(slots=True)
class FrozenClock:
    """A controllable clock for tests and replay.

    Replay drives this from the raw journal's timestamps so that detectors see
    exactly the time they would have seen live. Nothing in the detector path is
    allowed to call :class:`SystemClock` directly for this reason.
    """

    instant: datetime
    _monotonic_ns: int = 0

    def __post_init__(self) -> None:
        self.instant = ensure_utc(self.instant)

    def now(self) -> datetime:
        return self.instant

    def monotonic_ns(self) -> int:
        return self._monotonic_ns

    def advance_ms(self, milliseconds: int) -> Self:
        """Move both the wall and monotonic readings forward together."""
        if isinstance(milliseconds, bool) or not isinstance(milliseconds, int):
            raise TypeError(f"milliseconds must be an int, got {type(milliseconds).__name__}")
        self.instant = self.instant.fromtimestamp(
            self.instant.timestamp() + milliseconds / 1000, tz=UTC
        )
        self._monotonic_ns += milliseconds * 1_000_000
        return self

    def set_to(self, instant: datetime) -> Self:
        self.instant = ensure_utc(instant)
        return self


@dataclass(frozen=True, slots=True)
class Timestamps:
    """The provenance timestamps carried by every ingested message.

    ``exchange`` is nullable because the venue does not always supply one; that
    absence is itself information and is preserved rather than backfilled with
    a local time.
    """

    received: datetime
    processed: datetime
    exchange: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "received", ensure_utc(self.received))
        object.__setattr__(self, "processed", ensure_utc(self.processed))
        if self.exchange is not None:
            object.__setattr__(self, "exchange", ensure_utc(self.exchange))

    @property
    def processing_latency_ms(self) -> float:
        """Time we spent between receiving and applying the message."""
        return (self.processed - self.received).total_seconds() * 1000

    @property
    def receive_latency_ms(self) -> float | None:
        """Venue-to-us latency, or ``None`` when the venue supplied no timestamp.

        This is a clock *difference* between two machines and is only as good as
        their clock sync. It is reported for observability and must not be used
        as the staleness gate.
        """
        if self.exchange is None:
            return None
        return (self.received - self.exchange).total_seconds() * 1000
