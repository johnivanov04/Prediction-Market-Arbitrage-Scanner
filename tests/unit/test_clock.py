"""Unit tests for the clock and timestamp model."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from predarb.clock import FrozenClock, SystemClock, Timestamps, ensure_utc, from_epoch_ms

pytestmark = pytest.mark.unit


class TestEnsureUtc:
    def test_naive_datetime_is_rejected(self):
        # A naive timestamp in a replay is a silent correctness bug.
        with pytest.raises(ValueError, match="naive datetime"):
            ensure_utc(datetime(2026, 1, 1, 12, 0, 0))  # noqa: DTZ001

    def test_aware_utc_passes_through(self):
        value = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        assert ensure_utc(value) == value

    def test_other_zone_is_converted_not_rejected(self):
        eastern = timezone(timedelta(hours=-5))
        value = datetime(2026, 1, 1, 7, 0, 0, tzinfo=eastern)
        assert ensure_utc(value) == datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


class TestFromEpochMs:
    def test_converts_kalshi_ts_ms(self):
        # The ts_ms from the documented orderbook_delta example.
        assert from_epoch_ms(1669149841000) == datetime(2022, 11, 22, 20, 44, 1, tzinfo=UTC)

    def test_result_is_timezone_aware(self):
        assert from_epoch_ms(0).tzinfo is UTC

    def test_float_rejected(self):
        with pytest.raises(TypeError):
            from_epoch_ms(1669149841000.0)  # type: ignore[arg-type]

    def test_bool_rejected(self):
        with pytest.raises(TypeError):
            from_epoch_ms(True)


class TestFrozenClock:
    def test_does_not_advance_on_its_own(self):
        clock = FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))
        assert clock.now() == clock.now()

    def test_advance_moves_wall_and_monotonic_together(self):
        clock = FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))
        before_mono = clock.monotonic_ns()
        clock.advance_ms(1500)
        assert clock.now() == datetime(2026, 1, 1, 0, 0, 1, 500_000, tzinfo=UTC)
        assert clock.monotonic_ns() - before_mono == 1_500_000_000

    def test_rejects_naive_instant(self):
        with pytest.raises(ValueError, match="naive datetime"):
            FrozenClock(datetime(2026, 1, 1))  # noqa: DTZ001

    def test_set_to(self):
        clock = FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))
        clock.set_to(datetime(2026, 6, 1, tzinfo=UTC))
        assert clock.now() == datetime(2026, 6, 1, tzinfo=UTC)

    def test_satisfies_clock_protocol(self):
        # Both clocks must be substitutable; replay depends on it.
        for clock in (FrozenClock(datetime(2026, 1, 1, tzinfo=UTC)), SystemClock()):
            assert isinstance(clock.now(), datetime)
            assert isinstance(clock.monotonic_ns(), int)


class TestTimestamps:
    def test_processing_latency(self):
        ts = Timestamps(
            received=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
            processed=datetime(2026, 1, 1, 0, 0, 0, 250_000, tzinfo=UTC),
        )
        assert ts.processing_latency_ms == pytest.approx(250.0)

    def test_receive_latency_is_none_without_exchange_timestamp(self):
        # Kalshi omits ts_ms on some messages; that absence is information.
        ts = Timestamps(
            received=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
            processed=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
        )
        assert ts.receive_latency_ms is None

    def test_receive_latency_when_exchange_timestamp_present(self):
        ts = Timestamps(
            received=datetime(2026, 1, 1, 0, 0, 0, 100_000, tzinfo=UTC),
            processed=datetime(2026, 1, 1, 0, 0, 0, 150_000, tzinfo=UTC),
            exchange=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
        )
        assert ts.receive_latency_ms == pytest.approx(100.0)

    def test_naive_timestamps_rejected(self):
        with pytest.raises(ValueError, match="naive datetime"):
            Timestamps(
                received=datetime(2026, 1, 1),  # noqa: DTZ001
                processed=datetime(2026, 1, 1, tzinfo=UTC),
            )

    def test_is_frozen(self):
        ts = Timestamps(
            received=datetime(2026, 1, 1, tzinfo=UTC),
            processed=datetime(2026, 1, 1, tzinfo=UTC),
        )
        with pytest.raises(AttributeError):
            ts.received = datetime(2027, 1, 1, tzinfo=UTC)  # type: ignore[misc]
