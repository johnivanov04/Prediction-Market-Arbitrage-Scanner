"""Rate limiting driven by the venue's own configuration.

The budget is **discovered, not hardcoded**. Kalshi exposes both halves of it:

``GET /account/limits``
    ``usage_tier`` plus separate ``read`` and ``write`` buckets, each with a
    ``refill_rate`` (tokens/second) and a ``bucket_capacity``.
``GET /account/endpoint_costs``
    a ``default_cost`` plus the endpoints whose cost differs from it.

Both are authoritative and both are per account. Baking in "Basic tier, 10
tokens per request" would be wrong for any account on another tier, and would
silently go stale when an account is upgraded or when Kalshi changes a specific
endpoint's cost. ``10`` is merely the *current* default, and it is read from the
server rather than assumed.

Authenticated vs anonymous
--------------------------
The discovered budget describes the **authenticated account**. Nothing
documents that it also describes anonymous public requests, so for
unauthenticated access this module deliberately does **not** invent a token
budget. It falls back to bounded concurrency plus backoff on 429
(:class:`ConservativePolicy`), which is honest about what we know.

Read and write budgets are modelled separately because the venue separates
them. Phase 1 only ever spends from the read bucket -- it issues no write
request of any kind -- but representing both keeps the model faithful and
prevents a future write path from quietly drawing on the read budget.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Final, Protocol

from predarb.clock import Clock

__all__ = [
    "BucketLimit",
    "ConservativePolicy",
    "DiscoveredRateLimits",
    "EndpointCost",
    "EndpointCostRegistry",
    "RateLimitPolicy",
    "TokenBucket",
    "TokenBucketPolicy",
    "TrafficKind",
]

_NANOS_PER_SECOND: Final = Decimal(1_000_000_000)


class TrafficKind(StrEnum):
    """Which of the venue's two budgets a request draws on."""

    READ = "READ"
    WRITE = "WRITE"
    """Phase 1 never spends from this bucket. It exists so the model matches
    the venue, and so a future write path cannot silently use the read budget."""


@dataclass(frozen=True, slots=True)
class BucketLimit:
    """One token bucket's configuration, exactly as the venue reports it."""

    refill_rate: int
    """Tokens added per second."""

    bucket_capacity: int
    """Maximum tokens held. Also the largest single burst after idling."""

    def __post_init__(self) -> None:
        if self.refill_rate <= 0:
            raise ValueError(f"refill_rate must be positive, got {self.refill_rate}")
        if self.bucket_capacity <= 0:
            raise ValueError(f"bucket_capacity must be positive, got {self.bucket_capacity}")


@dataclass(frozen=True, slots=True)
class EndpointCost:
    """A single endpoint whose token cost differs from the default."""

    method: str
    path: str
    cost: int


@dataclass(frozen=True, slots=True)
class EndpointCostRegistry:
    """What each endpoint costs, per the venue.

    ``default_cost`` is whatever the server reported; there is no compiled-in
    fallback value in normal operation. The constructor default exists only so
    an unauthenticated client has something to reason with, and it is marked as
    unverified by :attr:`is_discovered`.
    """

    default_cost: int = 10
    overrides: dict[tuple[str, str], int] = field(default_factory=dict)
    is_discovered: bool = False
    """False when the registry holds fallback values rather than server-reported
    ones. Callers that care about accuracy should check this."""

    @classmethod
    def from_response(
        cls, *, default_cost: int, costs: tuple[EndpointCost, ...]
    ) -> EndpointCostRegistry:
        return cls(
            default_cost=default_cost,
            overrides={(c.method.upper(), c.path): c.cost for c in costs},
            is_discovered=True,
        )

    def cost_for(self, method: str, path_template: str) -> int:
        """Token cost for one call, falling back to the default."""
        return self.overrides.get((method.upper(), path_template), self.default_cost)


@dataclass(frozen=True, slots=True)
class DiscoveredRateLimits:
    """The account's full rate-limit picture as reported by the venue."""

    usage_tier: str | None
    read: BucketLimit
    write: BucketLimit
    costs: EndpointCostRegistry

    def bucket_for(self, kind: TrafficKind) -> BucketLimit:
        return self.read if kind is TrafficKind.READ else self.write


class TokenBucket:
    """A continuously refilling token bucket.

    Mirrors the venue's described mechanism: tokens accrue at ``refill_rate``
    per second up to ``bucket_capacity``, with no fixed windows. Timing uses the
    injected clock's **monotonic** reading, never the wall clock, so an NTP step
    cannot make the bucket believe it has refilled.

    Accounting is in ``Decimal`` rather than ``float`` -- not because a
    fractional token matters economically, but because this codebase keeps
    accumulating quantities exact by default, and a drifting accumulator in a
    long-lived process is an avoidable class of bug.
    """

    __slots__ = ("_capacity", "_clock", "_last_refill_ns", "_lock", "_refill_rate", "_tokens")

    def __init__(self, limit: BucketLimit, clock: Clock) -> None:
        self._clock = clock
        self._refill_rate = Decimal(limit.refill_rate)
        self._capacity = Decimal(limit.bucket_capacity)
        self._tokens = Decimal(limit.bucket_capacity)
        self._last_refill_ns = clock.monotonic_ns()
        self._lock = asyncio.Lock()

    @property
    def tokens(self) -> Decimal:
        """Currently modelled tokens, after refilling to now."""
        self._refill()
        return self._tokens

    @property
    def capacity(self) -> Decimal:
        return self._capacity

    def _refill(self) -> None:
        now_ns = self._clock.monotonic_ns()
        elapsed_ns = now_ns - self._last_refill_ns
        if elapsed_ns <= 0:
            return
        gained = (Decimal(elapsed_ns) / _NANOS_PER_SECOND) * self._refill_rate
        self._tokens = min(self._capacity, self._tokens + gained)
        self._last_refill_ns = now_ns

    def try_consume(self, cost: int) -> bool:
        """Spend ``cost`` tokens if available. Returns whether it succeeded."""
        self._validate(cost)
        self._refill()
        if self._tokens >= cost:
            self._tokens -= cost
            return True
        return False

    def wait_time_s(self, cost: int) -> float:
        """Seconds until ``cost`` tokens would be available. 0 if available now."""
        self._validate(cost)
        self._refill()
        if self._tokens >= cost:
            return 0.0
        deficit = Decimal(cost) - self._tokens
        return float(deficit / self._refill_rate)

    async def acquire(self, cost: int) -> float:
        """Wait until ``cost`` tokens are available, then spend them.

        Returns the seconds spent waiting, for observability. The lock
        serialises concurrent consumers so two callers cannot both observe the
        same tokens and overdraw the bucket.
        """
        self._validate(cost)
        waited = 0.0
        async with self._lock:
            while True:
                if self.try_consume(cost):
                    return waited
                delay = self.wait_time_s(cost)
                if delay <= 0:
                    # The clock did not advance (a frozen clock in tests);
                    # looping would spin forever, so report the need to wait.
                    return waited
                await asyncio.sleep(delay)
                waited += delay

    def _validate(self, cost: int) -> None:
        if isinstance(cost, bool) or not isinstance(cost, int):
            raise TypeError(f"cost must be an int, got {type(cost).__name__}")
        if cost <= 0:
            raise ValueError(f"cost must be positive, got {cost}")
        if cost > self._capacity:
            raise ValueError(
                f"cost {cost} exceeds bucket capacity {self._capacity}; "
                "this request can never be satisfied"
            )


class RateLimitPolicy(Protocol):
    """How a client paces its requests."""

    async def acquire(self, method: str, path_template: str, kind: TrafficKind) -> float:
        """Block until the request may proceed. Returns seconds waited."""
        ...

    def release(self) -> None:
        """Signal that a request has completed."""
        ...


class TokenBucketPolicy:
    """Pacing from the venue's discovered budget. Authenticated use only."""

    __slots__ = ("_buckets", "_limits")

    def __init__(self, limits: DiscoveredRateLimits, clock: Clock) -> None:
        self._limits = limits
        self._buckets = {
            TrafficKind.READ: TokenBucket(limits.read, clock),
            TrafficKind.WRITE: TokenBucket(limits.write, clock),
        }

    @property
    def limits(self) -> DiscoveredRateLimits:
        return self._limits

    def bucket(self, kind: TrafficKind) -> TokenBucket:
        return self._buckets[kind]

    def cost_for(self, method: str, path_template: str) -> int:
        return self._limits.costs.cost_for(method, path_template)

    async def acquire(self, method: str, path_template: str, kind: TrafficKind) -> float:
        return await self._buckets[kind].acquire(self.cost_for(method, path_template))

    def release(self) -> None:
        return None


class ConservativePolicy:
    """Pacing for unauthenticated access: bounded concurrency, no invented budget.

    We know the *authenticated* budget because the venue reports it. We do not
    know the anonymous one, and nothing documents that they are the same. So
    rather than model a token bucket whose numbers would be fiction, this caps
    in-flight requests and enforces a minimum spacing, and lets the retry layer
    handle a 429 if the real limit is tighter than we guessed.

    ``min_interval_s`` is a politeness floor, not a claimed limit. The defaults
    were tightened after a wide metadata scan produced sustained 429s: at 10
    tokens per request, the observed Basic read budget of 200 tokens/second
    allows ~20 requests/second, and pacing near that ceiling leaves no headroom
    for anything else sharing the key. ~8/second does.
    """

    __slots__ = (
        "_clock",
        "_last_start_ns",
        "_lock",
        "_max_concurrency",
        "_min_interval_s",
        "_semaphore",
    )

    def __init__(
        self, clock: Clock, *, max_concurrency: int = 3, min_interval_s: float = 0.12
    ) -> None:
        if max_concurrency <= 0:
            raise ValueError(f"max_concurrency must be positive, got {max_concurrency}")
        if min_interval_s < 0:
            raise ValueError(f"min_interval_s must not be negative, got {min_interval_s}")
        self._clock = clock
        self._max_concurrency = max_concurrency
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._min_interval_s = min_interval_s
        self._last_start_ns = 0
        self._lock = asyncio.Lock()

    @property
    def max_concurrency(self) -> int:
        # Stored rather than read from the semaphore: Semaphore._value is the
        # number of *remaining* permits, so it would under-report while
        # requests are in flight.
        return self._max_concurrency

    async def acquire(self, method: str, path_template: str, kind: TrafficKind) -> float:  # noqa: ARG002
        await self._semaphore.acquire()
        waited = 0.0
        async with self._lock:
            if self._min_interval_s > 0:
                elapsed_s = (self._clock.monotonic_ns() - self._last_start_ns) / 1e9
                remaining = self._min_interval_s - elapsed_s
                if remaining > 0 and self._last_start_ns:
                    await asyncio.sleep(remaining)
                    waited = remaining
            self._last_start_ns = self._clock.monotonic_ns()
        return waited

    def release(self) -> None:
        self._semaphore.release()
