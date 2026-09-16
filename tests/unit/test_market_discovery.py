"""Tests for active-market selection.

These pin down the behaviour that the original selector got wrong: it evaluated
zero candidates because the default ``/markets`` listing is ~100% combo markets,
which it skipped, and it never paginated deep enough to reach anything else.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from predarb.venues.kalshi.market_discovery import score_market
from predarb.venues.kalshi.models import KalshiMarket

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC)


def market(**overrides: object) -> KalshiMarket:
    payload: dict[str, object] = {
        "ticker": "TEST-1",
        "event_ticker": "TEST",
        "market_type": "binary",
        "status": "active",
        "volume_24h_fp": "1000.00",
        "open_interest_fp": "500.00",
        "yes_bid_size_fp": "100.00",
        "close_time": (NOW + timedelta(hours=6)).isoformat(),
    }
    payload.update(overrides)
    return KalshiMarket.model_validate(payload)


class TestRanking:
    def test_active_quoting_market_scores_positive(self):
        score, reason = score_market(market(), now=NOW)
        assert score > 0
        assert "volume" in reason

    def test_volume_dominates_the_score(self):
        busy, _ = score_market(market(volume_24h_fp="10000.00"), now=NOW)
        quiet, _ = score_market(market(volume_24h_fp="10.00"), now=NOW)
        assert busy > quiet

    def test_open_interest_contributes(self):
        high, _ = score_market(market(open_interest_fp="100000.00"), now=NOW)
        low, _ = score_market(market(open_interest_fp="1.00"), now=NOW)
        assert high > low

    def test_quoted_size_breaks_ties_when_there_is_no_volume(self):
        quoted, reason = score_market(
            market(volume_24h_fp="0.00", open_interest_fp="0.00", yes_bid_size_fp="50.00"),
            now=NOW,
        )
        assert quoted > 0
        assert reason


class TestExclusions:
    def test_combo_markets_excluded(self):
        score, reason = score_market(market(mve_collection_ticker="KXMVE-X"), now=NOW)
        assert score < 0
        assert "combo" in reason

    def test_provisional_markets_excluded(self):
        score, reason = score_market(market(is_provisional=True), now=NOW)
        assert score < 0
        assert "combo/provisional" in reason

    def test_inactive_markets_excluded(self):
        score, reason = score_market(market(status="initialized"), now=NOW)
        assert score < 0
        assert reason == "not active"

    def test_silent_market_excluded(self):
        score, reason = score_market(market(volume_24h_fp="0.00", yes_bid_size_fp="0.00"), now=NOW)
        assert score < 0
        assert "no quoted size" in reason

    def test_already_closed_market_excluded(self):
        score, reason = score_market(
            market(close_time=(NOW - timedelta(minutes=1)).isoformat()), now=NOW
        )
        assert score < 0
        assert reason == "already closed"


class TestOneSidedBooks:
    """A single quoted side must be enough.

    ``no_bid_size_fp`` is routinely absent or zero in the list response even for
    heavily traded markets -- every one of the 1,222 quoting markets found in
    the live survey quoted only the YES side. Requiring both sides selects
    nothing.
    """

    def test_yes_only_market_is_acceptable(self):
        score, _ = score_market(market(yes_bid_size_fp="100.00", no_bid_size_fp="0.00"), now=NOW)
        assert score > 0

    def test_absent_no_side_is_acceptable(self):
        payload = market(yes_bid_size_fp="100.00").model_dump()
        payload.pop("no_bid_size_fp", None)
        score, _ = score_market(KalshiMarket.model_validate(payload), now=NOW)
        assert score > 0

    def test_no_only_market_is_acceptable(self):
        score, _ = score_market(
            market(yes_bid_size_fp="0.00", no_bid_size_fp="80.00", volume_24h_fp="0.00"), now=NOW
        )
        assert score > 0


class TestClosingSoon:
    def test_quiet_market_closing_soon_is_penalised(self):
        soon = market(volume_24h_fp="10.00", close_time=(NOW + timedelta(minutes=5)).isoformat())
        later = market(volume_24h_fp="10.00")
        soon_score, reason = score_market(soon, now=NOW)
        later_score, _ = score_market(later, now=NOW)
        assert soon_score < later_score
        assert "closing soon" in reason

    def test_very_active_market_closing_soon_is_not_penalised(self):
        score, reason = score_market(
            market(volume_24h_fp="50000.00", close_time=(NOW + timedelta(minutes=5)).isoformat()),
            now=NOW,
        )
        assert score > 0
        assert "very active" in reason

    def test_close_time_is_optional(self):
        payload = market().model_dump()
        payload["close_time"] = None
        score, _ = score_market(KalshiMarket.model_validate(payload), now=NOW)
        assert score > 0

    def test_now_is_optional(self):
        score, _ = score_market(market(), now=None)
        assert score > 0
