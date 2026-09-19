"""Live, read-only validation of the fee engine against production metadata.

Run with::

    uv run python tools/validate_fees.py --series 2000

Answers three questions the fixtures cannot:

1. **Coverage.** What fraction of live series carry a fee type this engine can
   actually price? Everything else must refuse, and a large refusal rate is a
   finding, not a bug to work around.
2. **Shape.** Which multipliers and fee types does production really use, and
   does anything arrive that the wire model would reject?
3. **Clearing.** Has the venue ever sent an event override with null fields --
   the documented "clear the override" record we implement but have not
   observed?

Reports diagnostics only. There is no profitability here: no payoff, no edge,
no arbitrage classification. Fee mechanics alone.

Safety: read-only public metadata. Touches no account, portfolio, order, fill
or balance endpoint, and places no order. Member balance precision is an
*account* property, so both documented classes are reported side by side rather
than any account being inspected to find out which applies.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from predarb.config import Settings
from predarb.domain.enums import FeeType
from predarb.domain.fees import FeeConfiguration, FeeScope
from predarb.domain.money import Price, Quantity
from predarb.venues.kalshi.client import KalshiReadOnlyClient
from predarb.venues.kalshi.fee_coverage import (
    ArbEligibility,
    CombinedCoverage,
    CoverageCensus,
    classify_fee_configuration,
)
from predarb.venues.kalshi.fee_engine import AccumulatorState, Fill, apply_fill
from predarb.venues.kalshi.fee_model import (
    SUPPORTED_TAKER_FEE_TYPES,
    BalancePrecision,
    UnsupportedFeeTypeError,
)

ROOT = Path(__file__).resolve().parent.parent
RESULTS_PATH = ROOT / "fee_validation.json"

SAMPLE_PRICE = Price.from_value("0.3300")
SAMPLE_QUANTITY = Quantity.from_value("10.00")


def _counter(values: Any) -> dict[str, int]:
    return dict(sorted(Counter(str(v) for v in values).items()))


async def survey_series(client: KalshiReadOnlyClient, limit: int) -> dict[str, Any]:
    """Fee configuration across live series."""
    fee_types: Counter[str] = Counter()
    multipliers: Counter[str] = Counter()
    missing_type = 0
    missing_multiplier = 0
    examples: dict[str, str] = {}
    listed: set[str] = set()
    rows: list[tuple[str | None, Any]] = []
    seen = 0

    async for series in client.iter_series():
        seen += 1
        listed.add(series.ticker)
        if series.fee_type is None:
            missing_type += 1
        else:
            fee_types[series.fee_type] += 1
            examples.setdefault(series.fee_type, series.ticker)
        if series.fee_multiplier is None:
            missing_multiplier += 1
        else:
            multipliers[str(series.fee_multiplier)] += 1
        rows.append((series.fee_type, series.fee_multiplier))
        if seen >= limit:
            break

    census = CoverageCensus.from_rows("listed series (/series listing)", rows)
    undocumented = {
        name: count for name, count in fee_types.items() if name not in {t.value for t in FeeType}
    }
    return {
        "series_sampled": seen,
        "fee_types": dict(fee_types.most_common()),
        "fee_type_examples": examples,
        "multipliers": dict(sorted(multipliers.items())),
        "series_without_fee_type": missing_type,
        "series_without_multiplier": missing_multiplier,
        "undocumented_fee_types": undocumented,
        "listed_tickers": listed,
        "census": census,
    }


def census_payload(census: CoverageCensus) -> dict[str, Any]:
    """A census as JSON, with its own denominators attached to every share."""
    return {
        "universe": census.universe,
        "total": census.total,
        "by_eligibility": {state.value: census.by_state[state] for state in ArbEligibility},
        "by_fee_type": census.by_fee_type,
        "calculable": census.calculable,
        "boundable_for_arb": census.boundable_for_arb,
        "blocked_for_arb": census.blocked,
        "fraction_boundable_for_arb": str(census.fraction(census.boundable_for_arb)),
        "fraction_calculable": str(census.fraction(census.calculable)),
    }


async def probe_unlisted_series(
    client: KalshiReadOnlyClient, tickers: set[str], listed: set[str]
) -> dict[str, Any]:
    """Check fee-change tickers that the /series listing never returned.

    The listing is not exhaustive: perpetual-futures series are reachable by
    ticker but absent from it (A-42). A census built only on the listing would
    conclude a live fee type no longer exists, so the gap is measured and
    counted as its own universe rather than folded into the listing's total.
    """
    missing = sorted(tickers - listed)
    resolved: list[dict[str, str]] = []
    for ticker in missing:
        try:
            series = await client.get_series(ticker)
        except Exception as exc:
            resolved.append({"ticker": ticker, "status": type(exc).__name__})
            continue
        resolved.append(
            {
                "ticker": ticker,
                "status": "REACHABLE_BUT_UNLISTED",
                "fee_type": series.fee_type or "",
                "fee_multiplier": str(series.fee_multiplier),
            }
        )
    rows: list[tuple[str | None, Any]] = [
        (row.get("fee_type") or None, Decimal(row["fee_multiplier"]))
        for row in resolved
        if row.get("status") == "REACHABLE_BUT_UNLISTED"
    ]
    return {
        "fee_change_tickers_not_in_listing": len(missing),
        "resolved": resolved,
        "fee_types_only_reachable_by_ticker": _counter(
            row["fee_type"] for row in resolved if row.get("fee_type")
        ),
        "census": CoverageCensus.from_rows("addressable but unlisted (A-42)", rows),
    }


async def survey_changes(client: KalshiReadOnlyClient) -> dict[str, Any]:
    """Scheduled changes and event overrides, including any clearing records."""
    series_response = await client.get_series_fee_changes(show_historical=True)
    event_response = await client.get_event_fee_changes()
    series_rows = series_response.series_fee_change_arr
    event_rows = event_response.event_fee_changes

    clearing = [
        row.id
        for row in event_rows
        if row.fee_type_override is None and row.fee_multiplier_override is None
    ]
    partial = [
        row.id
        for row in event_rows
        if (row.fee_type_override is None) != (row.fee_multiplier_override is None)
    ]
    return {
        "series_changes": len(series_rows),
        "series_change_fee_types": _counter(r.fee_type for r in series_rows),
        "series_change_multipliers": _counter(r.fee_multiplier for r in series_rows),
        "event_changes": len(event_rows),
        "event_change_fee_types": _counter(
            r.fee_type_override for r in event_rows if r.fee_type_override
        ),
        "event_change_multipliers": _counter(
            r.fee_multiplier_override for r in event_rows if r.fee_multiplier_override is not None
        ),
        "clearing_records_observed": len(clearing),
        "clearing_record_ids": clearing[:10],
        "partial_override_records": len(partial),
        "scheduled_in_future": sum(1 for r in series_rows if r.scheduled_ts > datetime.now(tz=UTC)),
        "change_tickers": sorted({r.series_ticker for r in series_rows}),
    }


def price_samples(fee_types: dict[str, int]) -> dict[str, Any]:
    """Run one sample fill through every live fee type and record the outcome.

    A refusal is as much a result as a number: the point is to show that the
    unsupported types genuinely fail closed against real values.
    """
    rows: list[dict[str, Any]] = []
    for fee_type in sorted(fee_types):
        for multiplier in (Decimal(0), Decimal("0.5"), Decimal(1)):
            config = FeeConfiguration(
                fee_type_raw=fee_type,
                multiplier=multiplier,
                scope=FeeScope.SERIES,
                scope_ticker="SAMPLE",
            )
            fill = Fill(price=SAMPLE_PRICE, quantity=SAMPLE_QUANTITY, configuration=config)
            eligibility = classify_fee_configuration(fee_type, multiplier)
            row: dict[str, Any] = {
                "fee_type": fee_type,
                "multiplier": str(multiplier),
                "eligibility": eligibility.value,
                "supports_arbitrage_claim": eligibility.supports_arbitrage_claim,
            }
            try:
                direct = apply_fill(
                    fill, AccumulatorState.new_order(), BalancePrecision.direct_member()
                )
                non_direct = apply_fill(
                    fill, AccumulatorState.new_order(), BalancePrecision.non_direct_member()
                )
            except UnsupportedFeeTypeError as exc:
                row["status"] = "REFUSED"
                row["reason"] = str(exc)
            else:
                row["status"] = "PRICED"
                row["raw_model_fee"] = str(direct.raw_model_fee)
                row["trade_fee"] = direct.trade_fee.to_str()
                row["net_fee_direct_member"] = direct.net_fee.to_str()
                row["net_fee_non_direct_member"] = non_direct.net_fee.to_str()
            rows.append(row)
    return {"sample_fill": f"{SAMPLE_QUANTITY} @ {SAMPLE_PRICE}", "results": rows}


async def main() -> None:
    parser = argparse.ArgumentParser(description="Live fee metadata validation (read-only)")
    parser.add_argument("--series", type=int, default=20_000, help="how many series to sample")
    args = parser.parse_args()

    settings = Settings()
    print(f"Surveying up to {args.series} live series ...")
    async with KalshiReadOnlyClient.public(env=settings.kalshi_env) as client:
        series = await survey_series(client, args.series)
        listed = series.pop("listed_tickers")
        changes = await survey_changes(client)
        gap = await probe_unlisted_series(client, set(changes.pop("change_tickers")), listed)

    listed_census: CoverageCensus = series.pop("census")
    unlisted_census: CoverageCensus = gap.pop("census")
    combined = CombinedCoverage(
        censuses=(listed_census, unlisted_census),
        assembly=(
            "Universe = every series returned by paginating /series, plus every series "
            "named in /series/fee_changes that the listing never returned (A-42). Both "
            "were reachable and classified. This is a lower bound on what exists: the "
            "listing is known to be non-exhaustive, and series appearing in neither "
            "source would not be counted here at all."
        ),
    )

    samples = price_samples(series["fee_types"])
    report = {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "environment": settings.kalshi_env.value,
        "series_survey": series,
        "coverage": {
            "listed": census_payload(listed_census),
            "addressable_outside_listing": census_payload(unlisted_census),
            "combined": {
                "total": combined.total,
                "calculable": combined.calculable,
                "boundable_for_arb": combined.boundable_for_arb,
                "blocked_for_arb": combined.blocked,
                "fraction_boundable_for_arb": str(combined.fraction_boundable()),
                "assembly": combined.assembly,
            },
        },
        "fee_changes": changes,
        "listing_coverage_gap": gap,
        "pricing_samples": samples,
    }
    RESULTS_PATH.write_text(json.dumps(report, indent=2) + "\n")

    def show(census: CoverageCensus) -> None:
        print(f"\n{census.universe}: {census.total} series")
        for state in ArbEligibility:
            count = census.by_state[state]
            print(f"  {count:>6}  {state.value:<26s} ({census.fraction(count)})")
        for name, count in census.by_fee_type.items():
            mark = "ok" if name in {t.value for t in SUPPORTED_TAKER_FEE_TYPES} else "unsupported"
            print(f"      {count:>6}  {name:<40s} {mark}")

    show(listed_census)
    show(unlisted_census)
    print(f"\nMultipliers across the listing: {series['multipliers']}")
    if series["undocumented_fee_types"]:
        print(f"Undocumented fee types live: {series['undocumented_fee_types']}")

    print("\nCOMBINED")
    print(f"  universe assembled: {combined.total} series")
    print(f"  calculable:         {combined.calculable}")
    print(f"  boundable for arb:  {combined.boundable_for_arb} ({combined.fraction_boundable()})")
    print(f"  blocked for arb:    {combined.blocked}")
    print(f"  {combined.assembly}")

    print(
        f"\nScheduled changes: {changes['series_changes']} series, {changes['event_changes']} event"
    )
    print(f"Clearing records observed: {changes['clearing_records_observed']}")
    print(f"Partial override records: {changes['partial_override_records']}")
    print(
        f"Series with fee changes but absent from /series listing: "
        f"{gap['fee_change_tickers_not_in_listing']}"
    )
    if gap["fee_types_only_reachable_by_ticker"]:
        print(f"  fee types reachable only by ticker: {gap['fee_types_only_reachable_by_ticker']}")
    print(f"\nWrote {RESULTS_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
