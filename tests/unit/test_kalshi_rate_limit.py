"""Tests for discovered rate limiting."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from predarb.clock import FrozenClock
from predarb.venues.kalshi.rate_limit import (
    BucketLimit,
    ConservativePolicy,
    DiscoveredRateLimits,
    EndpointCost,
    EndpointCostRegistry,
    TokenBucket,
    TokenBucketPolicy,
    TrafficKind,
)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 9, 15, 12, 0, 0, tzinfo=UTC)


def clock() -> FrozenClock:
    return FrozenClock(T0)


class TestBucketLimit:
    def test_valid(self):
        limit = BucketLimit(refill_rate=200, bucket_capacity=400)
        assert limit.refill_rate == 200

    @pytest.mark.parametrize(("rate", "cap"), [(0, 100), (-1, 100), (100, 0), (100, -5)])
    def test_invalid_values_rejected(self, rate, cap):
        with pytest.raises(ValueError, match="must be positive"):
            BucketLimit(refill_rate=rate, bucket_capacity=cap)


class TestEndpointCostRegistry:
    def test_default_applies_when_no_override(self):
        registry = EndpointCostRegistry(default_cost=10)
        assert registry.cost_for("GET", "/markets") == 10

    def test_override_wins(self):
        registry = EndpointCostRegistry(default_cost=10, overrides={("GET", "/markets"): 3})
        assert registry.cost_for("GET", "/markets") == 3

    def test_method_matching_is_case_insensitive(self):
        registry = EndpointCostRegistry(default_cost=10, overrides={("GET", "/markets"): 3})
        assert registry.cost_for("get", "/markets") == 3

    def test_override_is_method_specific(self):
        registry = EndpointCostRegistry(default_cost=10, overrides={("GET", "/markets"): 3})
        assert registry.cost_for("HEAD", "/markets") == 10

    def test_from_response_marks_values_as_discovered(self):
        registry = EndpointCostRegistry.from_response(
            default_cost=7, costs=(EndpointCost(method="get", path="/markets", cost=2),)
        )
        assert registry.is_discovered
        assert registry.default_cost == 7
        assert registry.cost_for("GET", "/markets") == 2

    def test_fallback_registry_is_not_marked_discovered(self):
        # 10 is the current default but it is server configuration, not a
        # constant; an undiscovered registry must say so.
        assert not EndpointCostRegistry().is_discovered


class TestTokenBucketRefill:
    def test_starts_full(self):
        bucket = TokenBucket(BucketLimit(refill_rate=100, bucket_capacity=200), clock())
        assert bucket.tokens == Decimal(200)

    def test_consuming_reduces_tokens(self):
        bucket = TokenBucket(BucketLimit(refill_rate=100, bucket_capacity=200), clock())
        assert bucket.try_consume(50)
        assert bucket.tokens == Decimal(150)

    def test_refills_over_time(self):
        c = clock()
        bucket = TokenBucket(BucketLimit(refill_rate=100, bucket_capacity=200), c)
        bucket.try_consume(200)
        assert bucket.tokens == Decimal(0)
        c.advance_ms(500)  # half a second at 100/s
        assert bucket.tokens == Decimal(50)

    def test_refill_stops_at_capacity(self):
        c = clock()
        bucket = TokenBucket(BucketLimit(refill_rate=100, bucket_capacity=200), c)
        bucket.try_consume(10)
        c.advance_ms(60_000)
        assert bucket.tokens == Decimal(200)

    def test_cannot_consume_more_than_available(self):
        bucket = TokenBucket(BucketLimit(refill_rate=100, bucket_capacity=200), clock())
        assert bucket.try_consume(200)
        assert not bucket.try_consume(1)

    def test_uses_monotonic_not_wall_clock(self):
        """An NTP step backwards must not make the bucket believe it refilled."""
        c = clock()
        bucket = TokenBucket(BucketLimit(refill_rate=100, bucket_capacity=100), c)
        bucket.try_consume(100)
        c.set_to(T0.replace(year=2030))  # wall clock jumps forward
        assert bucket.tokens == Decimal(0)  # monotonic did not move


class TestWaitCalculation:
    def test_zero_when_tokens_available(self):
        bucket = TokenBucket(BucketLimit(refill_rate=100, bucket_capacity=200), clock())
        assert bucket.wait_time_s(50) == 0.0

    def test_computes_deficit_over_refill_rate(self):
        bucket = TokenBucket(BucketLimit(refill_rate=100, bucket_capacity=200), clock())
        bucket.try_consume(200)
        assert bucket.wait_time_s(50) == pytest.approx(0.5)

    def test_partial_refill_reduces_wait(self):
        c = clock()
        bucket = TokenBucket(BucketLimit(refill_rate=100, bucket_capacity=200), c)
        bucket.try_consume(200)
        c.advance_ms(250)
        assert bucket.wait_time_s(50) == pytest.approx(0.25)


class TestBucketValidation:
    def test_cost_exceeding_capacity_is_unsatisfiable(self):
        bucket = TokenBucket(BucketLimit(refill_rate=100, bucket_capacity=50), clock())
        with pytest.raises(ValueError, match="can never be satisfied"):
            bucket.try_consume(51)

    @pytest.mark.parametrize("bad", [0, -1])
    def test_non_positive_cost_rejected(self, bad):
        bucket = TokenBucket(BucketLimit(refill_rate=100, bucket_capacity=200), clock())
        with pytest.raises(ValueError, match="must be positive"):
            bucket.try_consume(bad)

    @pytest.mark.parametrize("bad", [1.5, True, "10"])
    def test_non_int_cost_rejected(self, bad):
        bucket = TokenBucket(BucketLimit(refill_rate=100, bucket_capacity=200), clock())
        with pytest.raises(TypeError):
            bucket.try_consume(bad)


class TestAsyncAcquire:
    async def test_acquires_immediately_when_tokens_available(self):
        bucket = TokenBucket(BucketLimit(refill_rate=100, bucket_capacity=200), clock())
        assert await bucket.acquire(10) == 0.0

    async def test_concurrent_consumers_do_not_overdraw(self):
        """Two callers must not both observe the same tokens."""
        bucket = TokenBucket(BucketLimit(refill_rate=1000, bucket_capacity=100), clock())
        results = await asyncio.gather(*(bucket.acquire(10) for _ in range(10)))
        assert len(results) == 10
        assert bucket.tokens == Decimal(0)


class TestPolicies:
    def test_token_bucket_policy_separates_read_and_write(self):
        limits = DiscoveredRateLimits(
            usage_tier="basic",
            read=BucketLimit(refill_rate=200, bucket_capacity=200),
            write=BucketLimit(refill_rate=100, bucket_capacity=100),
            costs=EndpointCostRegistry(default_cost=10, is_discovered=True),
        )
        policy = TokenBucketPolicy(limits, clock())
        assert policy.bucket(TrafficKind.READ).capacity == Decimal(200)
        assert policy.bucket(TrafficKind.WRITE).capacity == Decimal(100)

    async def test_read_spending_does_not_touch_the_write_budget(self):
        limits = DiscoveredRateLimits(
            usage_tier="basic",
            read=BucketLimit(refill_rate=200, bucket_capacity=200),
            write=BucketLimit(refill_rate=100, bucket_capacity=100),
            costs=EndpointCostRegistry(default_cost=10, is_discovered=True),
        )
        policy = TokenBucketPolicy(limits, clock())
        await policy.acquire("GET", "/markets", TrafficKind.READ)
        assert policy.bucket(TrafficKind.READ).tokens == Decimal(190)
        assert policy.bucket(TrafficKind.WRITE).tokens == Decimal(100)

    def test_policy_uses_discovered_endpoint_costs(self):
        limits = DiscoveredRateLimits(
            usage_tier="basic",
            read=BucketLimit(refill_rate=200, bucket_capacity=200),
            write=BucketLimit(refill_rate=100, bucket_capacity=100),
            costs=EndpointCostRegistry(
                default_cost=10, overrides={("GET", "/markets"): 2}, is_discovered=True
            ),
        )
        policy = TokenBucketPolicy(limits, clock())
        assert policy.cost_for("GET", "/markets") == 2
        assert policy.cost_for("GET", "/events") == 10

    async def test_conservative_policy_bounds_concurrency(self):
        policy = ConservativePolicy(clock(), max_concurrency=2, min_interval_s=0)
        await policy.acquire("GET", "/markets", TrafficKind.READ)
        await policy.acquire("GET", "/markets", TrafficKind.READ)
        third = asyncio.create_task(policy.acquire("GET", "/markets", TrafficKind.READ))
        await asyncio.sleep(0)
        assert not third.done()  # blocked until a permit is released
        policy.release()
        await asyncio.wait_for(third, timeout=1)
        policy.release()
        policy.release()

    def test_conservative_policy_reports_its_configured_concurrency(self):
        # Not the semaphore's remaining permits, which would under-report.
        policy = ConservativePolicy(clock(), max_concurrency=4, min_interval_s=0)
        assert policy.max_concurrency == 4

    @pytest.mark.parametrize(("concurrency", "interval"), [(0, 0.1), (-1, 0.1), (2, -1.0)])
    def test_conservative_policy_validates_arguments(self, concurrency, interval):
        with pytest.raises(ValueError, match="must"):
            ConservativePolicy(clock(), max_concurrency=concurrency, min_interval_s=interval)


class TestDiscoveredLimits:
    def test_bucket_for_selects_the_right_budget(self):
        limits = DiscoveredRateLimits(
            usage_tier="advanced",
            read=BucketLimit(refill_rate=300, bucket_capacity=300),
            write=BucketLimit(refill_rate=300, bucket_capacity=600),
            costs=EndpointCostRegistry(is_discovered=True),
        )
        assert limits.bucket_for(TrafficKind.READ).bucket_capacity == 300
        assert limits.bucket_for(TrafficKind.WRITE).bucket_capacity == 600
