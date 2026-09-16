"""Tests for point-in-time fee configuration resolution."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from predarb.domain.enums import FeeType
from predarb.domain.fees import (
    FeeConfiguration,
    FeeScope,
    FeeTimeline,
    ResolvedFeeConfiguration,
    ScheduledFeeChange,
)
from predarb.venues.kalshi.models import (
    KalshiEvent,
    KalshiEventFeeChangesResponse,
    KalshiSeries,
    KalshiSeriesFeeChangesResponse,
)
from predarb.venues.kalshi.normalize import build_fee_timeline
from tests.conftest import load_payload

pytestmark = pytest.mark.unit

_ONE_MICRO = timedelta(microseconds=1)

T0 = datetime(2026, 1, 1, tzinfo=UTC)
T1 = datetime(2026, 6, 1, tzinfo=UTC)
T2 = datetime(2026, 9, 1, tzinfo=UTC)


def resolved(timeline: FeeTimeline, at: datetime) -> ResolvedFeeConfiguration:
    """Resolve and assert a configuration exists, so tests can read naturally."""
    result = timeline.resolve_at(at)
    assert result is not None, f"expected a fee configuration at {at}"
    return result


def series_base(multiplier: str = "1", fee_type: str = "quadratic") -> FeeConfiguration:
    return FeeConfiguration(
        fee_type_raw=fee_type,
        multiplier=Decimal(multiplier),
        scope=FeeScope.SERIES,
        scope_ticker="KXTEST",
    )


def change(
    change_id: str, when: datetime, multiplier: str, scope: FeeScope = FeeScope.SERIES
) -> ScheduledFeeChange:
    return ScheduledFeeChange(
        change_id=change_id,
        scope=scope,
        scope_ticker="KXTEST",
        fee_type_raw="quadratic",
        multiplier=Decimal(multiplier),
        scheduled_ts=when,
    )


class TestResolution:
    def test_series_base_used_when_nothing_else(self):
        timeline = FeeTimeline(series_ticker="KXTEST", series_base=series_base())
        resolved = timeline.resolve_at(T1)
        assert resolved is not None
        assert resolved.configuration.multiplier == Decimal(1)
        assert "series base" in resolved.provenance

    def test_no_configuration_returns_none(self):
        # There is no default fee configuration. The caller must fail closed.
        assert FeeTimeline(series_ticker="KXTEST").resolve_at(T1) is None

    def test_event_override_beats_series_base(self):
        timeline = FeeTimeline(
            series_ticker="KXTEST",
            event_ticker="KXTEST-26",
            series_base=series_base("1"),
            event_override=FeeConfiguration(
                fee_type_raw="quadratic",
                multiplier=Decimal("0.5"),
                scope=FeeScope.EVENT,
                scope_ticker="KXTEST-26",
            ),
        )
        resolved = timeline.resolve_at(T1)
        assert resolved is not None
        assert resolved.configuration.multiplier == Decimal("0.5")
        assert resolved.configuration.scope is FeeScope.EVENT

    def test_scheduled_change_beats_series_base_once_effective(self):
        timeline = FeeTimeline(
            series_ticker="KXTEST",
            series_base=series_base("1"),
            series_changes=(change("c1", T1, "0.5"),),
        )
        assert resolved(timeline, T2).configuration.multiplier == Decimal("0.5")

    def test_most_recent_effective_change_wins(self):
        timeline = FeeTimeline(
            series_ticker="KXTEST",
            series_base=series_base("1"),
            series_changes=(change("c1", T0, "0.5"), change("c2", T1, "0")),
        )
        assert resolved(timeline, T2).configuration.multiplier == Decimal(0)

    def test_tie_broken_deterministically(self):
        # Two changes at the same instant must resolve the same way every run;
        # replay determinism depends on it.
        timeline = FeeTimeline(
            series_ticker="KXTEST",
            series_changes=(change("b", T1, "0.5"), change("a", T1, "0.25")),
        )
        first = timeline.resolve_at(T2)
        assert first is not None
        assert all(timeline.resolve_at(T2) == first for _ in range(5))
        assert first.configuration.multiplier == Decimal("0.5")


class TestPointInTime:
    def test_future_change_is_invisible(self):
        # The core point-in-time guarantee: tomorrow's fee cannot apply today.
        timeline = FeeTimeline(
            series_ticker="KXTEST",
            series_base=series_base("1"),
            series_changes=(change("c1", T2, "0.5"),),
        )
        assert resolved(timeline, T1).configuration.multiplier == Decimal(1)

    def test_change_applies_exactly_at_its_scheduled_instant(self):
        timeline = FeeTimeline(
            series_ticker="KXTEST",
            series_base=series_base("1"),
            series_changes=(change("c1", T1, "0.5"),),
        )
        assert resolved(timeline, T1).configuration.multiplier == Decimal("0.5")

    def test_one_microsecond_before_is_still_the_old_value(self):
        timeline = FeeTimeline(
            series_ticker="KXTEST",
            series_base=series_base("1"),
            series_changes=(change("c1", T1, "0.5"),),
        )
        just_before = T1 - _ONE_MICRO
        assert resolved(timeline, just_before).configuration.multiplier == Decimal(1)

    def test_resolution_is_pure(self):
        timeline = FeeTimeline(
            series_ticker="KXTEST",
            series_base=series_base("1"),
            series_changes=(change("c1", T1, "0.5"),),
        )
        assert resolved(timeline, T0).configuration.multiplier == Decimal(1)
        assert resolved(timeline, T2).configuration.multiplier == Decimal("0.5")
        # Querying a later time must not mutate the answer for an earlier one.
        assert resolved(timeline, T0).configuration.multiplier == Decimal(1)

    def test_naive_datetime_rejected(self):
        timeline = FeeTimeline(series_ticker="KXTEST", series_base=series_base())
        with pytest.raises(ValueError, match="naive datetime"):
            timeline.resolve_at(datetime(2026, 1, 1))  # noqa: DTZ001


class TestUnrecognisedFeeTypes:
    def test_documented_types_are_recognised(self):
        config = series_base(fee_type="quadratic")
        assert config.fee_type is FeeType.QUADRATIC
        assert config.is_recognised

    def test_undocumented_type_is_carried_but_unresolvable(self):
        # Observed live on 24 series. Ingestion must not crash, but the fee
        # engine must not guess a formula for it either.
        config = series_base(fee_type="margin_market_maker_program_fees")
        assert config.fee_type is None
        assert not config.is_recognised
        timeline = FeeTimeline(series_ticker="KXTEST", series_base=config)
        resolved = timeline.resolve_at(T1)
        assert resolved is not None
        assert not resolved.is_resolvable

    def test_recognised_type_is_resolvable(self):
        timeline = FeeTimeline(series_ticker="KXTEST", series_base=series_base())
        assert resolved(timeline, T1).is_resolvable


class TestExactMultipliers:
    def test_float_multiplier_rejected(self):
        with pytest.raises(TypeError, match="must not be a float"):
            FeeConfiguration(
                fee_type_raw="quadratic",
                multiplier=0.5,  # type: ignore[arg-type]
                scope=FeeScope.SERIES,
                scope_ticker="KXTEST",
            )

    def test_int_multiplier_becomes_decimal(self):
        config = FeeConfiguration(
            fee_type_raw="quadratic",
            multiplier=1,  # type: ignore[arg-type]
            scope=FeeScope.SERIES,
            scope_ticker="KXTEST",
        )
        assert config.multiplier == Decimal(1)

    def test_zero_multiplier_is_valid(self):
        # fee_multiplier of 0 was observed live: fees genuinely waived.
        assert series_base("0").multiplier == Decimal(0)


class TestBuildFromRealFixtures:
    def test_builds_from_live_series_and_changes(self):
        series = KalshiSeries.model_validate(load_payload("rest/series_KXHIGHNY.json")["series"])
        changes = KalshiSeriesFeeChangesResponse.model_validate(
            load_payload("rest/series_fee_changes.json")
        ).series_fee_change_arr
        timeline = build_fee_timeline(series=series, series_changes=changes)
        assert timeline.series_ticker == "KXHIGHNY"
        assert timeline.series_base is not None
        assert timeline.series_base.fee_type is FeeType.QUADRATIC
        # Whole-exchange feed passed in; only this series' changes are kept.
        assert all(c.scope_ticker == "KXHIGHNY" for c in timeline.series_changes)

    def test_event_changes_are_filtered_to_the_event(self):
        series = KalshiSeries.model_validate(load_payload("rest/series_KXHIGHNY.json")["series"])
        event_changes = KalshiEventFeeChangesResponse.model_validate(
            load_payload("rest/events_fee_changes.json")
        ).event_fee_changes
        target = event_changes[0]
        event = KalshiEvent.model_validate(
            {"event_ticker": target.event_ticker, "series_ticker": "KXHIGHNY"}
        )
        timeline = build_fee_timeline(series=series, event=event, event_changes=event_changes)
        assert timeline.event_changes
        assert all(c.scope_ticker == target.event_ticker for c in timeline.event_changes)

    def test_real_event_change_resolves_after_its_scheduled_time(self):
        series = KalshiSeries.model_validate(load_payload("rest/series_KXHIGHNY.json")["series"])
        event_changes = KalshiEventFeeChangesResponse.model_validate(
            load_payload("rest/events_fee_changes.json")
        ).event_fee_changes
        target = event_changes[0]
        event = KalshiEvent.model_validate(
            {"event_ticker": target.event_ticker, "series_ticker": "KXHIGHNY"}
        )
        timeline = build_fee_timeline(series=series, event=event, event_changes=event_changes)
        after = target.scheduled_ts
        resolved = timeline.resolve_at(after)
        assert resolved is not None
        assert resolved.configuration.scope is FeeScope.EVENT
        assert resolved.effective_from == target.scheduled_ts
