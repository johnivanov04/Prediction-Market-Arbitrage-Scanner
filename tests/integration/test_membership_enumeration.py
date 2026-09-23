"""Enumeration over recorded pages: order, dedup, anomalies, pagination proof.

No network. The client is replaced by a stub that serves recorded page shapes,
so the behaviour under test is the enumeration logic -- which tier wins a
duplicate, what counts as exhausted, which departures from the documented
partition get reported.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from predarb.semantics.membership import MembershipAnomaly, MembershipPathStatus, MembershipSource
from predarb.venues.kalshi.membership_capture import enumerate_event_membership
from predarb.venues.kalshi.membership_projection import (
    MembershipIdentityError,
    payload_digest,
    project_membership_member,
)
from predarb.venues.kalshi.models import KalshiEventEnvelope, KalshiMarket

pytestmark = pytest.mark.integration

EVENT = "E"
CUTOFF = datetime(2026, 7, 24, tzinfo=UTC)
BEFORE = CUTOFF - timedelta(days=5)
AFTER = CUTOFF + timedelta(days=5)


def market(ticker: str, **kwargs: Any) -> dict[str, Any]:
    """A raw market payload, exactly as the venue would send it."""
    return {
        "ticker": ticker,
        "event_ticker": EVENT,
        "market_type": "binary",
        "status": "finalized",
        **kwargs,
    }


class StubClient:
    """Serves recorded raw pages. Records every call so order can be asserted."""

    def __init__(
        self,
        *,
        live: list[list[dict[str, Any]]],
        historical: list[list[dict[str, Any]]],
        nested: list[str] | None = None,
        cutoffs: list[datetime | None] | None = None,
    ) -> None:
        self.live_pages = live
        self.historical_pages = historical
        self.nested = nested if nested is not None else []
        self.cutoffs = cutoffs or [CUTOFF, CUTOFF]
        self.calls: list[str] = []

    async def get_historical_cutoff(self) -> Any:
        self.calls.append("cutoff")
        index = min(len([c for c in self.calls if c == "cutoff"]) - 1, len(self.cutoffs) - 1)
        return type("Cutoff", (), {"market_settled_ts": self.cutoffs[index]})()

    async def get_market_payloads_page(
        self, *, historical: bool, cursor: str | None = None, **_: Any
    ) -> tuple[tuple[dict[str, Any], ...], str | None]:
        self.calls.append("historical" if historical else "live")
        pages = self.historical_pages if historical else self.live_pages
        index = int(cursor) if cursor else 0
        items = tuple(pages[index]) if index < len(pages) else ()
        return items, (str(index + 1) if index + 1 < len(pages) else None)

    async def get_event(self, event_ticker: str, **_: Any) -> Any:
        self.calls.append("event")
        return KalshiEventEnvelope.model_validate(
            {
                "event": {
                    "event_ticker": event_ticker,
                    "series_ticker": "S",
                    "mutually_exclusive": True,
                    "markets": [market(t) for t in self.nested],
                },
                "markets": [],
            }
        )


async def enumerate_with(client: Any) -> Any:
    return await enumerate_event_membership(client, EVENT, at=datetime.now(tz=UTC))


class TestEnumerationOrder:
    async def test_the_live_tier_is_queried_before_the_historical_one(self):
        """The order is a correctness property, not a style choice.

        Live-then-historical turns a market crossing the cutoff mid-walk into a
        duplicate. The reverse loses it from both, invisibly.
        """
        client = StubClient(live=[[market("A")]], historical=[[market("B")]])
        await enumerate_with(client)

        assert client.calls.index("live") < client.calls.index("historical")

    async def test_the_cutoff_is_read_before_and_after(self):
        client = StubClient(live=[[market("A")]], historical=[[]])
        await enumerate_with(client)

        assert client.calls.count("cutoff") == 2
        assert client.calls[0] == "cutoff"
        assert client.calls.index("historical") < len(client.calls) - client.calls[::-1].index(
            "cutoff"
        )


class TestPaginationExhaustion:
    async def test_every_page_is_walked_and_recorded(self):
        client = StubClient(
            live=[[market("A"), market("B")], [market("C")]], historical=[[market("D")]]
        )
        evidence = await enumerate_with(client)

        assert evidence.live_trace.pages == 2
        assert evidence.live_trace.returned == 3
        assert evidence.live_trace.status is MembershipPathStatus.EXHAUSTED
        assert len(evidence.live_trace.response_hashes) == 2

    async def test_a_walk_that_ends_leaves_no_trailing_cursor(self):
        """The only evidence of exhaustion the protocol offers."""
        client = StubClient(live=[[market("A")], [market("B")]], historical=[[]])
        evidence = await enumerate_with(client)

        assert evidence.live_trace.cursors == ("1",)
        assert evidence.both_paths_exhausted

    async def test_a_failing_page_is_not_an_empty_event(self):
        class Failing(StubClient):
            async def get_market_payloads_page(
                self, *, historical: bool, **kwargs: Any
            ) -> tuple[tuple[dict[str, Any], ...], str | None]:
                if historical:
                    raise RuntimeError("transport failure")
                return await StubClient.get_market_payloads_page(
                    self, historical=historical, **kwargs
                )

        evidence = await enumerate_with(Failing(live=[[market("A")]], historical=[[]]))
        assert evidence.historical_trace.status is MembershipPathStatus.FAILED
        assert not evidence.both_paths_exhausted
        assert "did not exhaust both paths" in evidence.statement()


class TestDeduplication:
    async def test_a_ticker_in_both_tiers_appears_once(self):
        client = StubClient(live=[[market("A")]], historical=[[market("A")]])
        evidence = await enumerate_with(client)

        assert evidence.member_tickers == ("A",)
        assert evidence.by_source(MembershipSource.BOTH) == ("A",)
        assert MembershipAnomaly.DUPLICATE_ACROSS_TIERS in evidence.anomalies

    async def test_the_live_payload_wins_but_the_conflict_is_recorded(self):
        """Neither view is merged; the disagreement is surfaced."""
        client = StubClient(
            live=[[market("A", status="active", result="")]],
            historical=[[market("A", status="finalized", result="no")]],
        )
        evidence = await enumerate_with(client)

        assert evidence.members[0].status == "active"
        assert MembershipAnomaly.CONFLICTING_PAYLOADS in evidence.anomalies
        assert any("tiers disagree on" in d for d in evidence.anomaly_detail)


class TestPartitionChecks:
    async def test_a_live_market_settled_before_the_cutoff_is_flagged(self):
        """Observed live on KXKNESSET-27: the documented partition is not strict."""
        client = StubClient(live=[[market("A", settlement_ts=BEFORE.isoformat())]], historical=[[]])
        evidence = await enumerate_with(client)

        assert MembershipAnomaly.LIVE_MEMBER_SETTLED_BEFORE_CUTOFF in evidence.anomalies

    async def test_a_duplicate_settled_before_the_cutoff_is_also_flagged(self):
        """BOTH still means the live tier returned it."""
        settled = market("A", settlement_ts=BEFORE.isoformat())
        evidence = await enumerate_with(StubClient(live=[[settled]], historical=[[settled]]))

        assert MembershipAnomaly.LIVE_MEMBER_SETTLED_BEFORE_CUTOFF in evidence.anomalies

    async def test_an_archived_market_settled_after_the_cutoff_is_flagged(self):
        client = StubClient(live=[[]], historical=[[market("A", settlement_ts=AFTER.isoformat())]])
        evidence = await enumerate_with(client)

        assert MembershipAnomaly.HISTORICAL_MEMBER_SETTLED_AFTER_CUTOFF in evidence.anomalies

    async def test_a_moving_cutoff_is_recorded(self):
        client = StubClient(
            live=[[market("A")]], historical=[[]], cutoffs=[CUTOFF, CUTOFF + timedelta(days=1)]
        )
        evidence = await enumerate_with(client)

        assert MembershipAnomaly.CUTOFF_MOVED_DURING_ENUMERATION in evidence.anomalies
        assert not evidence.cutoff_stable

    async def test_a_market_from_another_event_makes_membership_incomplete(self):
        """Not absorbed, and not silently dropped: nothing is guessed."""
        foreign = {"ticker": "X", "event_ticker": "OTHER", "market_type": "binary"}
        evidence = await enumerate_with(StubClient(live=[[foreign]], historical=[[]]))

        assert MembershipAnomaly.MEMBERSHIP_IDENTITY_UNUSABLE in evidence.anomalies
        assert not evidence.both_paths_exhausted
        assert evidence.member_tickers == ()


class TestNestedView:
    async def test_members_the_nested_view_omits_are_measured(self):
        client = StubClient(live=[[market("A")]], historical=[[market("B")]], nested=["A"])
        evidence = await enumerate_with(client)

        assert evidence.nested_omissions == ("B",)

    async def test_a_nested_only_member_is_flagged_not_absorbed(self):
        client = StubClient(live=[[market("A")]], historical=[[]], nested=["A", "Z"])
        evidence = await enumerate_with(client)

        assert MembershipAnomaly.NESTED_MEMBER_MISSING_FROM_TIERS in evidence.anomalies
        assert evidence.by_source(MembershipSource.NESTED_EVENT_ONLY) == ("Z",)

    async def test_the_event_flag_is_carried_but_proves_nothing(self):
        """Recorded as evidence toward AT_MOST_ONE only (A-52)."""
        evidence = await enumerate_with(StubClient(live=[[market("A")]], historical=[[]]))
        assert evidence.event_mutually_exclusive is True
        assert "outcome exhaustiveness" in evidence.statement()


class TestFinancialAnomaliesDoNotEraseMembership:
    """Correction 2: a bad price is not an argument about event membership.

    A-49: ``/historical/markets`` returns negative top-of-book sizes on
    finalized markets. Before the semantic projection existed, one such page
    failed an entire walk, so two of forty sampled events reported **zero
    members** -- indistinguishable from an event that genuinely has none. A
    data-quality problem had become a semantic conclusion.
    """

    async def test_a_negative_historical_size_keeps_the_member(self):
        archived = market(
            "A",
            yes_bid_size_fp="-19.00",
            yes_ask_size_fp="-389.00",
            settlement_ts=BEFORE.isoformat(),
        )
        evidence = await enumerate_with(StubClient(live=[[]], historical=[[archived]]))

        assert evidence.member_tickers == ("A",)
        assert MembershipAnomaly.DATA_QUALITY_ANOMALY in evidence.anomalies
        assert evidence.members[0].data_quality_anomalies == {
            "yes_bid_size_fp": "-19.00",
            "yes_ask_size_fp": "-389.00",
        }

    async def test_a_malformed_last_price_keeps_the_member(self):
        evidence = await enumerate_with(
            StubClient(live=[[market("A", last_price_dollars="not-a-price")]], historical=[[]])
        )
        assert evidence.member_tickers == ("A",)
        assert "last_price_dollars" in evidence.members[0].data_quality_anomalies

    async def test_a_malformed_volume_keeps_the_member(self):
        evidence = await enumerate_with(
            StubClient(live=[[market("A", volume_fp="-5.00")]], historical=[[]])
        )
        assert evidence.member_tickers == ("A",)
        assert "volume_fp" in evidence.members[0].data_quality_anomalies

    async def test_the_anomalous_field_is_excluded_from_semantic_reasoning(self):
        """It is recorded for audit and kept out of the fingerprint."""
        clean = await enumerate_with(StubClient(live=[[market("A")]], historical=[[]]))
        anomalous = await enumerate_with(
            StubClient(live=[[market("A", yes_bid_size_fp="-19.00")]], historical=[[]])
        )
        assert clean.fingerprint().digest == anomalous.fingerprint().digest
        assert anomalous.members[0].audit_values()["data_quality_anomalies"]

    async def test_the_walk_still_counts_as_exhausted(self):
        evidence = await enumerate_with(
            StubClient(live=[[market("A", yes_bid_size_fp="-19.00")]], historical=[[]])
        )
        assert evidence.both_paths_exhausted
        assert evidence.live_trace.status is MembershipPathStatus.EXHAUSTED


class TestIdentityFailuresMakeMembershipIncomplete:
    async def test_a_missing_ticker_makes_membership_incomplete(self):
        evidence = await enumerate_with(
            StubClient(live=[[{"event_ticker": EVENT, "status": "active"}]], historical=[[]])
        )
        assert MembershipAnomaly.MEMBERSHIP_IDENTITY_UNUSABLE in evidence.anomalies
        assert evidence.live_trace.status is MembershipPathStatus.IDENTITY_UNUSABLE
        assert not evidence.both_paths_exhausted

    async def test_a_missing_event_ticker_makes_membership_incomplete(self):
        evidence = await enumerate_with(
            StubClient(live=[[{"ticker": "A", "status": "active"}]], historical=[[]])
        )
        assert MembershipAnomaly.MEMBERSHIP_IDENTITY_UNUSABLE in evidence.anomalies
        assert not evidence.both_paths_exhausted

    async def test_a_good_record_beside_a_bad_one_still_counts(self):
        """The bad record is reported; the good one is not punished for it."""
        evidence = await enumerate_with(
            StubClient(live=[[market("A"), {"event_ticker": EVENT}]], historical=[[]])
        )
        assert evidence.member_tickers == ("A",)
        assert not evidence.both_paths_exhausted
        assert any("no ticker" in d for d in evidence.anomaly_detail)


class TestBothReadingsOfOnePayloadAreCorrect:
    def test_financial_normalisation_rejects_what_membership_accepts(self):
        """The same payload, two questions, two right answers.

        ``KalshiMarket`` keeps its invariants -- pricing code must never see a
        negative contract count. Membership reads the same bytes and answers a
        question none of that bears on.
        """
        payload = market("A", yes_bid_size_fp="-19.00")

        strict = KalshiMarket.model_validate(payload)
        assert strict.yes_bid_size_fp is None
        assert strict.quote_size_anomalies == {"yes_bid_size_fp": "-19.00"}

        semantic = project_membership_member(
            payload, expected_event_ticker=EVENT, source=MembershipSource.HISTORICAL
        )
        assert semantic.ticker == "A"
        assert semantic.event_ticker == EVENT
        assert semantic.data_quality_anomalies == {"yes_bid_size_fp": "-19.00"}

    def test_the_raw_record_stays_recoverable(self):
        payload = market("A", yes_bid_size_fp="-19.00")
        assert payload_digest(payload) == payload_digest(dict(reversed(list(payload.items()))))
        assert payload["yes_bid_size_fp"] == "-19.00"

    def test_identity_failure_is_raised_not_guessed(self):
        with pytest.raises(MembershipIdentityError, match="different question"):
            project_membership_member(
                {"ticker": "A", "event_ticker": "OTHER"},
                expected_event_ticker=EVENT,
                source=MembershipSource.LIVE,
            )
