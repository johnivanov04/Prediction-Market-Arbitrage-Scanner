"""Live, read-only validation of the execution-depth engine.

Run with::

    uv run python tools/validate_execution.py --seconds 120

Collects live books, then for each VALID book reports the direct bids, the
derived executable asks, available depth, and sample quotes. The point is to
confirm the complement arithmetic and depth accounting hold against the real
venue, not just against fixtures.

Reports diagnostics only. There is no profitability here: fees, payoff and
settlement semantics all belong to later layers, and a number that looked like
an edge but was computed without them would be worse than no number.

Safety: read-only. Subscribes to public market-data channels, places no order,
and touches no account, portfolio, order or fill endpoint.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from predarb.books.execution import (
    BookNotExecutableError,
    ExecutionContext,
    UnsupportedExecutionSemanticsError,
    build_execution_curve,
)
from predarb.books.orderbook import BookView
from predarb.config import Settings
from predarb.domain.enums import MarketSide
from predarb.domain.models import VenueInstrument
from predarb.domain.money import Price, Quantity
from predarb.ingest.book_collector import BookCollector, default_journal_path
from predarb.venues.kalshi.client import KalshiReadOnlyClient
from predarb.venues.kalshi.market_discovery import discover_active_markets
from predarb.venues.kalshi.normalize import normalize_market

ROOT = Path(__file__).resolve().parent.parent
JOURNAL_DIR = ROOT / "data" / "raw"
RESULTS_PATH = ROOT / "execution_validation.json"

SAMPLE_QUANTITIES = ("1.00", "10.00", "100.00")


async def resolve_markets(settings: Settings, args: argparse.Namespace) -> list[str]:
    if args.market:
        return list(args.market)
    print("Discovering active markets ...")
    async with KalshiReadOnlyClient.public(env=settings.kalshi_env) as client:
        candidates, _ = await discover_active_markets(
            client, count=args.markets, series_limit=args.series, now=datetime.now(tz=UTC)
        )
    for candidate in candidates:
        print(f"    {candidate.describe()}")
    return [c.ticker for c in candidates]


async def fetch_instruments(settings: Settings, tickers: list[str]) -> dict[str, VenueInstrument]:
    """Instrument metadata, so the notional comes from the venue, not a guess."""
    instruments: dict[str, VenueInstrument] = {}
    async with KalshiReadOnlyClient.public(env=settings.kalshi_env) as client:
        for ticker in tickers:
            try:
                instruments[ticker] = normalize_market(await client.get_market(ticker))
            except Exception as exc:
                print(f"  !! could not fetch {ticker}: {type(exc).__name__}: {exc}")
    return instruments


def describe_book(
    view: BookView, instrument: VenueInstrument, context: ExecutionContext
) -> dict[str, Any]:
    """Depth metrics and sample quotes for one market."""
    report: dict[str, Any] = {
        "ticker": view.market_ticker,
        "integrity": view.integrity.value,
        "notional": instrument.notional_value.to_str() if instrument.notional_value else None,
        "best_yes_bid": view.best_yes_bid.price.to_str() if view.best_yes_bid else None,
        "best_no_bid": view.best_no_bid.price.to_str() if view.best_no_bid else None,
        "total_yes_bid_quantity": view.total_quantity(MarketSide.YES).to_str(),
        "total_no_bid_quantity": view.total_quantity(MarketSide.NO).to_str(),
    }

    for outcome in (MarketSide.YES, MarketSide.NO):
        key = outcome.value.lower()
        try:
            curve = build_execution_curve(view, instrument, context, outcome)
        except (BookNotExecutableError, UnsupportedExecutionSemanticsError) as exc:
            report[f"buy_{key}"] = {"unavailable": f"{type(exc).__name__}: {exc}"}
            continue

        quotes = []
        for raw in SAMPLE_QUANTITIES:
            quote = curve.quote_up_to(Quantity.from_value(raw))
            quotes.append(
                {
                    "requested": raw,
                    "filled": quote.filled_quantity.to_str(),
                    "gross_cost": quote.gross_cost.to_str(),
                    "vwap": str(quote.vwap) if quote.vwap else None,
                    "levels_consumed": len(quote.slices),
                    "fully_fillable": quote.fully_fillable,
                    "fees_included": quote.fees_included,
                }
            )
        report[f"buy_{key}"] = {
            "best_executable_ask": (
                curve.best_execution_price.to_str() if curve.best_execution_price else None
            ),
            "worst_executable_ask": (
                curve.worst_execution_price.to_str() if curve.worst_execution_price else None
            ),
            "max_depth": curve.max_fillable_quantity.to_str(),
            "levels": len(curve.levels),
            "breakpoints": [
                {
                    "quantity": point.cumulative_quantity.to_str(),
                    "cumulative_cost": point.cumulative_cost.to_str(),
                    "vwap": str(point.vwap),
                    "marginal_price": point.marginal_price.to_str(),
                }
                for point in curve.breakpoints[:6]
            ],
            "quotes": quotes,
        }

    # Diagnostic only. A crossed or locked complementary book is economically
    # interesting and is exactly the state later detectors will want to inspect,
    # so it is surfaced and never "repaired".
    if view.best_yes_bid and view.best_no_bid and instrument.notional_value:
        combined = view.best_yes_bid.price.units + view.best_no_bid.price.units
        report["complementary_spread"] = Price.from_units(
            abs(instrument.notional_value.units - combined)
        ).to_str()
        report["complementary_sum_exceeds_notional"] = combined > instrument.notional_value.units
    return report


def print_report(books: list[dict[str, Any]]) -> None:
    print("\n================ EXECUTION VALIDATION ================")
    for entry in books:
        print(f"\n  {entry['ticker']}  ({entry['integrity']}, notional {entry['notional']})")
        print(
            f"    best YES bid {entry['best_yes_bid']}   "
            f"best NO bid {entry['best_no_bid']}   "
            f"spread {entry.get('complementary_spread')}"
        )
        for outcome in ("yes", "no"):
            block = entry.get(f"buy_{outcome}", {})
            if "unavailable" in block:
                print(f"    BUY {outcome.upper():3s}: {block['unavailable']}")
                continue
            print(
                f"    BUY {outcome.upper():3s}: best ask {block['best_executable_ask']} "
                f"depth {block['max_depth']} over {block['levels']} levels"
            )
            for quote in block["quotes"]:
                print(
                    f"        q={quote['requested']:>8}: filled={quote['filled']:>10} "
                    f"gross={quote['gross_cost']:>14} vwap={quote['vwap']} "
                    f"levels={quote['levels_consumed']} full={quote['fully_fillable']}"
                )


async def main() -> None:
    parser = argparse.ArgumentParser(description="Live execution-depth validation")
    parser.add_argument("--market", action="append", metavar="TICKER")
    parser.add_argument("--markets", type=int, default=4)
    parser.add_argument("--series", type=int, default=250)
    parser.add_argument("--seconds", type=float, default=90.0)
    args = parser.parse_args()

    settings = Settings()
    if not settings.has_credentials:
        print("No Kalshi credentials configured; the WebSocket requires them.")
        raise SystemExit(2)

    tickers = await resolve_markets(settings, args)
    if not tickers:
        print("No active markets found.")
        raise SystemExit(1)
    print(f"\nMarkets: {tickers}")

    instruments = await fetch_instruments(settings, tickers)

    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    journal_path = default_journal_path(JOURNAL_DIR)
    collector = BookCollector(settings=settings, markets=tickers, journal_path=journal_path)
    print(f"\nCollecting for {args.seconds:.0f}s ...")
    await collector.run(duration_s=args.seconds)

    reconstructor = collector.reconstructor
    if reconstructor is None:
        print("No reconstructor; nothing collected.")
        raise SystemExit(1)

    # The collector closes its connection on teardown, which correctly
    # invalidates every book. Quote the views captured while they were still
    # authoritative, under the epoch that applied at that moment -- honest
    # reconstruction of the authority, not a bypass of the check.
    epoch = collector.stats.final_epoch
    context = ExecutionContext(
        current_connection_epoch=epoch if epoch is not None else 1,
        journal_healthy=reconstructor.journal_healthy,
    )

    reports: list[dict[str, Any]] = []
    for ticker, view in collector.stats.final_views.items():
        instrument = instruments.get(ticker)
        if instrument is None:
            continue
        reports.append(describe_book(view, instrument, context))

    print_report(reports)
    RESULTS_PATH.write_text(
        json.dumps(
            {
                "generated_at_utc": datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
                "collector": collector.summary(),
                "books": reports,
            },
            indent=2,
            default=str,
        )
        + "\n"
    )
    print(f"\nWrote {RESULTS_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
