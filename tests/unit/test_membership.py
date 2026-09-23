"""Venue membership evidence: what enumeration shows, and what it never shows.

Two distinct claims run through these tests, and the point is that they stay
distinct: knowing every market Kalshi lists for an event is not knowing that one
of them must win.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from predarb.semantics.membership import (
    MEMBERSHIP_SCHEMA_VERSION,
    CombinedVenueMembershipEvidence,
    MembershipAnomaly,
    MembershipMember,
    MembershipPathStatus,
    MembershipSource,
    PageTrace,
    membership_snapshot_id,
)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
CUTOFF = datetime(2026, 7, 24, tzinfo=UTC)


def trace(
    path: str = "/markets",
    *,
    status: MembershipPathStatus = MembershipPathStatus.EXHAUSTED,
    pages: int = 1,
    returned: int = 2,
    cursors: tuple[str, ...] = (),
) -> PageTrace:
    return PageTrace(
        path=path,
        query={"event_ticker": "E", "limit": "1000"},
        pages=pages,
        returned=returned,
        cursors=cursors,
        response_hashes=("h" * 64,) * pages,
        status=status,
    )


def member(
    ticker: str,
    source: MembershipSource = MembershipSource.LIVE,
    **kwargs: object,
) -> MembershipMember:
    return MembershipMember(ticker=ticker, source=source, event_ticker="E", **kwargs)  # type: ignore[arg-type]


def evidence(**kwargs: object) -> CombinedVenueMembershipEvidence:
    defaults: dict[str, object] = {
        "snapshot_id": "snap",
        "event_ticker": "E",
        "observed_at": T0,
        "knowledge_at": T0,
        "schema_version": MEMBERSHIP_SCHEMA_VERSION,
        "cutoff_before": CUTOFF,
        "cutoff_after": CUTOFF,
        "live_trace": trace(),
        "historical_trace": trace("/historical/markets", returned=0),
        "nested_members": ("A", "B"),
        "members": (member("A"), member("B")),
    }
    return CombinedVenueMembershipEvidence(**{**defaults, **kwargs})  # type: ignore[arg-type]


class TestItNeverClaimsCompleteness:
    def test_the_statement_refuses_the_word_complete(self):
        text = evidence().statement()
        assert "not a proof" in text
        assert "outcome exhaustiveness" in text

    def test_the_structural_caveat_is_always_present(self):
        """No enumeration result removes it, however clean."""
        notes = evidence().caveats()
        assert any("removed provisional market appears in neither tier" in n for n in notes)

    def test_a_provisional_member_is_called_out(self):
        notes = evidence(members=(member("A", is_provisional=True), member("B"))).caveats()
        assert any("may remove after determination" in n or "provisional" in n for n in notes)

    def test_an_open_event_is_called_out_as_not_final(self):
        notes = evidence(members=(member("A", status="active"), member("B"))).caveats()
        assert any("may create more" in n for n in notes)

    def test_a_truncated_path_prevents_any_size_conclusion(self):
        text = evidence(
            live_trace=trace(status=MembershipPathStatus.TRUNCATED_BY_LIMIT)
        ).statement()
        assert "did not exhaust both paths" in text
        assert "nothing may be concluded from its size" in text


class TestNestedOmission:
    def test_archived_members_missing_from_the_nested_view_are_measured(self):
        """46% of sampled events omitted at least one member this way (A-50)."""
        record = evidence(
            nested_members=("A",),
            members=(member("A"), member("B", MembershipSource.HISTORICAL)),
        )
        assert record.nested_omissions == ("B",)

    def test_a_nested_only_member_is_a_recorded_anomaly_not_a_quiet_union(self):
        record = evidence(
            nested_members=("A", "B", "Z"),
            members=(member("A"), member("B"), member("Z", MembershipSource.NESTED_EVENT_ONLY)),
            anomalies=(MembershipAnomaly.NESTED_MEMBER_MISSING_FROM_TIERS,),
        )
        assert record.by_source(MembershipSource.NESTED_EVENT_ONLY) == ("Z",)
        assert MembershipAnomaly.NESTED_MEMBER_MISSING_FROM_TIERS in record.anomalies


class TestCutoffStability:
    def test_a_moving_cutoff_is_a_caveat(self):
        record = evidence(cutoff_after=CUTOFF + timedelta(days=1))
        assert record.cutoff_stable is False
        assert any("moved during enumeration" in n for n in record.caveats())

    def test_an_unreadable_cutoff_leaves_the_partition_unverified(self):
        record = evidence(cutoff_before=None, cutoff_after=None)
        assert any("partition is unverified" in n for n in record.caveats())


class TestFingerprint:
    def test_which_tier_answered_is_not_material(self):
        """The cutoff sweeping past a member is not a membership change.

        Fingerprinting the source would expire a certificate every time an
        unchanged market crossed the boundary.
        """
        live = evidence(members=(member("A"), member("B")))
        archived = evidence(
            members=(
                member("A", MembershipSource.HISTORICAL),
                member("B", MembershipSource.BOTH),
            )
        )
        assert live.fingerprint().digest == archived.fingerprint().digest

    def test_a_new_member_changes_the_fingerprint(self):
        grown = evidence(members=(member("A"), member("B"), member("C")))
        assert grown.fingerprint().digest != evidence().fingerprint().digest

    def test_a_changed_strike_changes_the_fingerprint(self):
        moved = evidence(
            members=(member("A", strike_type="greater", floor_strike="10"), member("B"))
        )
        assert moved.fingerprint().digest != evidence().fingerprint().digest

    def test_the_snapshot_id_binds_event_fingerprint_and_time(self):
        record = evidence()
        first = membership_snapshot_id(
            event_ticker="E", fingerprint=record.fingerprint(), observed_at=T0
        )
        later = membership_snapshot_id(
            event_ticker="E",
            fingerprint=record.fingerprint(),
            observed_at=T0 + timedelta(seconds=1),
        )
        assert first != later


class TestPointInTime:
    def test_knowledge_time_is_recorded_separately_from_observation(self):
        record = evidence(knowledge_at=T0 + timedelta(days=30))
        assert record.observed_at == T0
        assert record.knowledge_at != record.observed_at

    def test_the_latest_member_creation_time_is_exposed(self):
        record = evidence(
            members=(
                member("A", created_time=T0 - timedelta(days=2)),
                member("B", created_time=T0 - timedelta(hours=1)),
            )
        )
        assert record.latest_member_created_time == T0 - timedelta(hours=1)
