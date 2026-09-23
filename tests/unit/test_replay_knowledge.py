"""Bitemporal resolution and lookahead-attack tests.

Every test here is an attempt to make the replay use something it did not have.
The distinction under test throughout: a fact's *effective* time says when it
applies; its *observation* time says whether we had it at all. Filtering on the
first alone is the classic backtest leak.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from predarb.replay.knowledge import KnowledgeBase, ReplayMode
from predarb.replay.observation import (
    KnowledgeHorizon,
    Observation,
    ObservationKind,
    ObservationStream,
    ObservedVersion,
    VersionHistory,
)

pytestmark = pytest.mark.unit

MON = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
TUE = MON + timedelta(days=1)
WED = MON + timedelta(days=2)
THU = MON + timedelta(days=3)
FRI = MON + timedelta(days=4)


def observation(
    ordinal: int,
    observed_at: datetime,
    kind: ObservationKind,
    **payload: object,
) -> Observation:
    return Observation(ordinal=ordinal, observed_at=observed_at, kind=kind, payload=dict(payload))


def horizon(ordinal: int, at: datetime) -> KnowledgeHorizon:
    return KnowledgeHorizon(ordinal=ordinal, at=at)


class TestVersionHistoryTwoPhaseResolution:
    def _history(self) -> VersionHistory[str]:
        return VersionHistory[str](
            key="fee",
            versions=(
                ObservedVersion(value="old", observed_at=MON, observed_ordinal=0),
                ObservedVersion(
                    value="new",
                    observed_at=MON,
                    observed_ordinal=1,
                    effective_from=WED,
                ),
            ),
        )

    def test_before_the_effective_time_the_old_version_applies(self):
        """Knowing about Wednesday's change on Monday does not price Tuesday."""
        resolved = self._history().resolve(horizon(5, TUE))
        assert resolved is not None
        assert resolved.value == "old"

    def test_after_the_effective_time_the_new_version_applies(self):
        resolved = self._history().resolve(horizon(5, THU))
        assert resolved is not None
        assert resolved.value == "new"

    def test_a_version_observed_later_is_invisible_even_if_effective_earlier(self):
        """The backfill attack, stated minimally."""
        history = VersionHistory[str](
            key="fee",
            versions=(
                ObservedVersion(value="old", observed_at=MON, observed_ordinal=0),
                ObservedVersion(
                    value="backfilled",
                    observed_at=FRI,
                    observed_ordinal=99,
                    effective_from=WED,
                ),
            ),
        )
        resolved = history.resolve(horizon(10, THU))
        assert resolved is not None
        assert resolved.value == "old"

    def test_nothing_known_resolves_to_none(self):
        """A real answer, not something to backfill."""
        history = VersionHistory[str](
            key="fee",
            versions=(ObservedVersion(value="later", observed_at=FRI, observed_ordinal=99),),
        )
        assert history.resolve(horizon(1, TUE)) is None

    def test_scheduled_but_not_yet_effective_is_reportable(self):
        """Knowable and correctly unapplied is different from unknown."""
        pending = self._history().scheduled_but_not_yet_effective(horizon(5, TUE))
        assert [v.value for v in pending] == ["new"]

    def test_it_stops_being_pending_once_effective(self):
        assert self._history().scheduled_but_not_yet_effective(horizon(5, THU)) == ()

    def test_ties_break_on_observation_order_not_dict_order(self):
        history = VersionHistory[str](
            key="x",
            versions=(
                ObservedVersion(
                    value="first", observed_at=MON, observed_ordinal=0, effective_from=WED
                ),
                ObservedVersion(
                    value="second", observed_at=MON, observed_ordinal=1, effective_from=WED
                ),
            ),
        )
        resolved = history.resolve(horizon(5, THU))
        assert resolved is not None
        assert resolved.value == "second"


class TestOrdinalOrdering:
    def test_same_timestamp_observations_are_still_ordered(self):
        """Ordinal decides; the clock cannot.

        A detector triggered by #100 must not see #101 merely because both
        round to the same microsecond.
        """
        stream = ObservationStream.from_iterable(
            [
                observation(100, MON, ObservationKind.FRAME_RECEIVED, raw="{}"),
                observation(101, MON, ObservationKind.FEE_OBSERVATION, scope_ticker="S"),
            ]
        )
        visible = stream.up_to(horizon(100, MON))
        assert [o.ordinal for o in visible] == [100]

    def test_a_stream_refuses_out_of_order_construction(self):
        """Re-sorting by timestamp cannot recreate live ordering."""
        with pytest.raises(ValueError, match="capture order"):
            ObservationStream.from_iterable(
                [
                    observation(2, MON, ObservationKind.FRAME_RECEIVED, raw="{}"),
                    observation(1, MON, ObservationKind.FRAME_RECEIVED, raw="{}"),
                ]
            )

    def test_duplicate_ordinals_are_refused(self):
        with pytest.raises(ValueError, match="duplicate capture ordinals"):
            ObservationStream.from_iterable(
                [
                    observation(1, MON, ObservationKind.FRAME_RECEIVED, raw="{}"),
                    observation(1, TUE, ObservationKind.FRAME_RECEIVED, raw="{}"),
                ]
            )

    def test_the_horizon_admits_exactly_up_to_its_ordinal(self):
        first = observation(5, MON, ObservationKind.FRAME_RECEIVED, raw="{}")
        second = observation(6, MON, ObservationKind.FRAME_RECEIVED, raw="{}")
        cut = horizon(5, MON)
        assert cut.admits(first)
        assert not cut.admits(second)


class TestFeeLookahead:
    """The worked scenario from the Step 10 brief, both directions."""

    def _base(self, backfilled: bool) -> KnowledgeBase:
        observed_at = FRI if backfilled else MON
        ordinal = 50 if backfilled else 1
        return KnowledgeBase.from_stream(
            ObservationStream.from_iterable(
                [
                    observation(
                        0,
                        MON,
                        ObservationKind.FEE_OBSERVATION,
                        scope_ticker="SER",
                        multiplier="1",
                    ),
                    observation(
                        ordinal,
                        observed_at,
                        ObservationKind.FEE_OBSERVATION,
                        scope_ticker="SER",
                        multiplier="0.5",
                        effective_from=WED.isoformat(),
                    ),
                ]
            )
        )

    def test_tuesday_uses_the_old_config(self):
        record = self._base(backfilled=False).fee_record_at("SER", horizon(10, TUE))
        assert record is not None
        assert record["multiplier"] == "1"

    def test_thursday_uses_the_new_config_when_it_was_observed_monday(self):
        record = self._base(backfilled=False).fee_record_at("SER", horizon(10, THU))
        assert record is not None
        assert record["multiplier"] == "0.5"

    def test_thursday_must_not_see_a_change_first_observed_friday(self):
        """Effective Wednesday, learned Friday: invisible on Thursday."""
        record = self._base(backfilled=True).fee_record_at("SER", horizon(10, THU))
        assert record is not None
        assert record["multiplier"] == "1"

    def test_the_pending_change_is_reported_on_tuesday(self):
        pending = self._base(backfilled=False).scheduled_not_yet_effective(horizon(10, TUE))
        assert any("fee:SER" in entry for entry in pending)

    def test_no_fee_knowledge_at_all_resolves_to_none(self):
        """The only record was captured later, so Tuesday knew nothing.

        Note the ordinal must be consistent with capture order: an observation
        made on Friday necessarily has a higher ordinal than a Tuesday horizon,
        because ordinals are assigned as records are written.
        """
        base = KnowledgeBase.from_stream(
            ObservationStream.from_iterable(
                [observation(50, FRI, ObservationKind.FEE_OBSERVATION, scope_ticker="SER")]
            )
        )
        assert base.fee_record_at("SER", horizon(10, TUE)) is None


class TestCertificateLookahead:
    def _base(self) -> KnowledgeBase:
        return KnowledgeBase.from_stream(
            ObservationStream.from_iterable(
                [
                    observation(
                        0,
                        MON,
                        ObservationKind.SETTLEMENT_EVIDENCE,
                        market_ticker="MKT",
                        digest="v1",
                    ),
                    observation(
                        1,
                        TUE,
                        ObservationKind.SETTLEMENT_CERTIFICATE,
                        market_ticker="MKT",
                        certificate_id="cert-1",
                    ),
                    observation(
                        2,
                        THU,
                        ObservationKind.SETTLEMENT_EVIDENCE,
                        market_ticker="MKT",
                        digest="v2-drifted",
                    ),
                ]
            )
        )

    def test_a_certificate_issued_later_does_not_exist_yet(self):
        assert self._base().settlement_certificate_at("MKT", horizon(0, MON)) is None

    def test_it_exists_once_issued(self):
        record = self._base().settlement_certificate_at("MKT", horizon(1, TUE))
        assert record is not None
        assert record["certificate_id"] == "cert-1"

    def test_drift_observed_later_does_not_reach_backward(self):
        """10:30 sees the 10:00 evidence, not the 11:00 drift.

        Using today's evidence to invalidate yesterday's decision would answer
        a different question than the one replay asks.
        """
        evidence = self._base().settlement_evidence_at("MKT", horizon(1, TUE))
        assert evidence is not None
        assert evidence["digest"] == "v1"

    def test_after_the_drift_is_observed_it_applies(self):
        evidence = self._base().settlement_evidence_at("MKT", horizon(2, THU))
        assert evidence is not None
        assert evidence["digest"] == "v2-drifted"

    def test_a_relation_certificate_issued_later_is_invisible(self):
        base = KnowledgeBase.from_stream(
            ObservationStream.from_iterable(
                [
                    observation(
                        0,
                        THU,
                        ObservationKind.RELATION_CERTIFICATE,
                        event_ticker="EVT",
                        certificate_id="rel-1",
                        selected_members=["A", "B", "C"],
                    )
                ]
            )
        )
        members = ["A", "B", "C"]
        assert base.relation_certificate_at("EVT", horizon(0, THU), members=members) is not None
        assert base.relation_certificate_at("EVT", horizon(-1, MON), members=members) is None


class TestMetadataLookahead:
    def test_metadata_observed_later_is_unavailable(self):
        base = KnowledgeBase.from_stream(
            ObservationStream.from_iterable(
                [
                    observation(
                        0, MON, ObservationKind.MARKET_METADATA, ticker="MKT", notional="1.0000"
                    ),
                    observation(
                        1, THU, ObservationKind.MARKET_METADATA, ticker="MKT", notional="2.0000"
                    ),
                ]
            )
        )
        early = base.market_at("MKT", horizon(0, MON))
        assert early is not None
        assert early["notional"] == "1.0000"
        late = base.market_at("MKT", horizon(1, THU))
        assert late is not None
        assert late["notional"] == "2.0000"

    def test_an_unknown_market_resolves_to_none(self):
        base = KnowledgeBase.from_stream(ObservationStream())
        assert base.market_at("MKT", horizon(0, MON)) is None


class TestReplayMode:
    def test_the_mode_is_named_as_known_at_time(self):
        """Not "objective historical truth" -- a different question entirely."""
        assert ReplayMode.AS_KNOWN_AT_TIME.value == "AS_KNOWN_AT_TIME"
        assert {m.value for m in ReplayMode} == {"AS_KNOWN_AT_TIME"}

    def test_the_default_knowledge_base_uses_it(self):
        assert KnowledgeBase().mode is ReplayMode.AS_KNOWN_AT_TIME
