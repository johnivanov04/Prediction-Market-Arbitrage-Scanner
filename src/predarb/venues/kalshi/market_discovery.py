"""Finding markets that are actually likely to emit order-book updates.

Why this is not a one-liner
---------------------------
The obvious approach -- page through ``GET /markets`` and keep the ones with a
quoted size -- returns **nothing usable**. Measured on production:

* **29,998 of 30,000** markets in the default listing are multivariate combo
  markets (``mve_collection_ticker`` set).
* The first non-combo market appears at **position 16,772**.
* Combo markets almost never quote: one market in the first 6,000 had any
  resting size at all.

So a scan that reads a few pages and skips combos evaluates *zero* real
candidates and reports "no active markets", which is a statement about the
listing order rather than about the exchange.

Enumerating series and querying markets per series instead surfaces ordinary
single-market series directly: the same scan found **1,222** quoting non-combo
markets out of 1,872 examined.

One-sided books are normal
--------------------------
``no_bid_size_fp`` is routinely absent or zero in the list response even for
heavily traded markets, so requiring both sides to be populated rejects almost
everything. A market quoting one side still emits deltas, which is what matters
here, so a single quoted side is sufficient.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final

from predarb.venues.kalshi.client import KalshiReadOnlyClient
from predarb.venues.kalshi.models import KalshiMarket

__all__ = ["MarketCandidate", "discover_active_markets", "score_market"]

# A market this close to closing may stop updating mid-observation.
_CLOSING_SOON_MINUTES: Final = 15
# ...unless it is busy enough that the risk is worth taking.
_BUSY_ENOUGH_VOLUME: Final = Decimal(1000)
# Volume is the strongest signal of imminent order-book traffic, so it dominates.
_VOLUME_WEIGHT: Final = 10


def _dec(value: object) -> Decimal:
    as_decimal = getattr(value, "as_decimal", None)
    return as_decimal() if callable(as_decimal) else Decimal(0)


@dataclass(frozen=True, slots=True)
class MarketCandidate:
    """A market ranked by how likely it is to produce order-book traffic."""

    ticker: str
    score: Decimal
    volume_24h: Decimal
    open_interest: Decimal
    quoted_size: Decimal
    close_time: datetime | None
    reason: str

    def describe(self) -> str:
        return (
            f"{self.ticker:46s} score={self.score:>14,.2f} "
            f"vol24h={self.volume_24h:>12,.2f} oi={self.open_interest:>12,.2f} "
            f"quoted={self.quoted_size:>10,.2f}  ({self.reason})"
        )


def score_market(market: KalshiMarket, *, now: datetime | None = None) -> tuple[Decimal, str]:
    """Rank a market by likely order-book activity.

    Signals, in order of weight: 24-hour volume, open interest, then currently
    quoted size as a tiebreaker for markets with no recent trade history.

    Decimal arithmetic is used because the inputs are already exact and there is
    no reason to drop to float; nothing here touches a payoff, so the choice is
    about consistency rather than correctness.
    """
    if market.status != "active":
        return Decimal(-1), "not active"
    if market.mve_collection_ticker or market.is_provisional:
        return Decimal(-1), "combo/provisional"

    volume = _dec(market.volume_24h_fp)
    interest = _dec(market.open_interest_fp)
    quoted = _dec(market.yes_bid_size_fp) + _dec(market.no_bid_size_fp)

    if quoted <= 0 and volume <= 0:
        return Decimal(-1), "no quoted size and no recent volume"

    score = volume * _VOLUME_WEIGHT + interest + quoted

    # A market minutes from closing may stop updating mid-observation. Penalise
    # it unless it is busy enough that the risk is worth taking.
    reason = "volume+interest+quoted"
    if now is not None and market.close_time is not None:
        minutes_left = (market.close_time - now).total_seconds() / 60
        if minutes_left < 0:
            return Decimal(-1), "already closed"
        if minutes_left < _CLOSING_SOON_MINUTES and volume < _BUSY_ENOUGH_VOLUME:
            score /= 10
            reason = "closing soon, low volume"
        elif minutes_left < _CLOSING_SOON_MINUTES:
            reason = "closing soon but very active"
    return score, reason


async def discover_active_markets(
    client: KalshiReadOnlyClient,
    *,
    count: int = 6,
    series_limit: int = 500,
    concurrency: int = 4,
    now: datetime | None = None,
) -> tuple[list[MarketCandidate], dict[str, int]]:
    """Rank currently active markets by likely order-book traffic.

    Returns the top candidates and a statistics dict describing what was
    examined, so a zero result can be explained rather than merely reported.

    Concurrency is kept modest: the read budget is shared with everything else,
    and the retry layer handles a 429 correctly but noisily.
    """
    stats = {
        "series_examined": 0,
        "markets_examined": 0,
        "combo_markets": 0,
        "not_active": 0,
        "yes_side_quoted": 0,
        "no_side_quoted": 0,
        "either_side_quoted": 0,
        "both_sides_quoted": 0,
        "scored_positive": 0,
    }

    series_tickers: list[str] = []
    async for series in client.iter_series(limit=200):
        series_tickers.append(series.ticker)
        if len(series_tickers) >= series_limit:
            break
    stats["series_examined"] = len(series_tickers)

    semaphore = asyncio.Semaphore(concurrency)

    async def markets_for(ticker: str) -> list[KalshiMarket]:
        async with semaphore:
            try:
                return [
                    market
                    async for market in client.iter_markets(
                        limit=100, series_ticker=ticker, status="open"
                    )
                ]
            except Exception:  # one bad series must not end discovery
                return []

    batches = await asyncio.gather(*(markets_for(t) for t in series_tickers))

    candidates: list[MarketCandidate] = []
    for batch in batches:
        for market in batch:
            stats["markets_examined"] += 1
            if market.mve_collection_ticker:
                stats["combo_markets"] += 1
            if market.status != "active":
                stats["not_active"] += 1
            yes_quoted = _dec(market.yes_bid_size_fp) > 0
            no_quoted = _dec(market.no_bid_size_fp) > 0
            if yes_quoted:
                stats["yes_side_quoted"] += 1
            if no_quoted:
                stats["no_side_quoted"] += 1
            if yes_quoted or no_quoted:
                stats["either_side_quoted"] += 1
            if yes_quoted and no_quoted:
                stats["both_sides_quoted"] += 1

            score, reason = score_market(market, now=now)
            if score <= 0:
                continue
            stats["scored_positive"] += 1
            candidates.append(
                MarketCandidate(
                    ticker=market.ticker,
                    score=score,
                    volume_24h=_dec(market.volume_24h_fp),
                    open_interest=_dec(market.open_interest_fp),
                    quoted_size=_dec(market.yes_bid_size_fp) + _dec(market.no_bid_size_fp),
                    close_time=market.close_time,
                    reason=reason,
                )
            )

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates[:count], stats
