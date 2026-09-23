"""Measure whether live + historical enumeration behaves like the documented partition.

Run with::

    uv run python tools/research_membership.py --events 40
    uv run python tools/research_membership.py --event KXPRESPERSON-28

For each sampled event this walks all three sources -- live markets, historical
markets, nested event -- and reports what each returned, which members only one
of them had, and every place the observed behaviour departs from what the
documentation describes.

What this can and cannot answer
-------------------------------
It can measure how often the nested event view omits archived members, whether
duplicates appear across the tier boundary, and whether the tiers respect the
cutoff they claim to.

It cannot establish that ``live + historical`` is every market the event ever
had. ``is_provisional`` markets may be removed entirely, and a removed market is
in neither tier. "Zero omissions across this sample" is an observation about a
sample, and is reported as one.

Read-only public market data throughout. No account endpoint is touched.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from predarb.config import Settings
from predarb.semantics.membership import CombinedVenueMembershipEvidence, MembershipSource
from predarb.venues.kalshi.client import KalshiReadOnlyClient
from predarb.venues.kalshi.membership_capture import (
    enumerate_event_membership,
    membership_summary,
    sample_event_tickers,
)

ROOT = Path(__file__).resolve().parent.parent
RESULTS_PATH = ROOT / "membership_research.json"


def classify(evidence: CombinedVenueMembershipEvidence) -> str:
    """How this event's enumeration behaved, as a single comparable label."""
    if not evidence.both_paths_exhausted:
        return "PATH_NOT_EXHAUSTED"
    if evidence.by_source(MembershipSource.NESTED_EVENT_ONLY):
        return "NESTED_ONLY_MEMBER_PRESENT"
    if evidence.nested_omissions:
        return "NESTED_OMITS_ARCHIVED_MEMBERS"
    if evidence.by_source(MembershipSource.HISTORICAL):
        return "HISTORICAL_MEMBERS_PRESENT_NESTED_AGREED"
    return "LIVE_ONLY_EVENT"


async def main() -> None:
    parser = argparse.ArgumentParser(description="Membership enumeration research")
    parser.add_argument("--event", action="append", metavar="EVENT_TICKER")
    parser.add_argument("--events", type=int, default=40)
    parser.add_argument("--series", type=int, default=400)
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--out", type=Path, default=RESULTS_PATH)
    args = parser.parse_args()

    settings = Settings()
    rows: list[dict[str, Any]] = []
    details: list[dict[str, Any]] = []

    async with KalshiReadOnlyClient.public(env=settings.kalshi_env) as client:
        cutoff = await client.get_historical_cutoff()
        print(f"historical cutoff (market_settled_ts): {cutoff.market_settled_ts}")

        if args.event:
            tickers = list(args.event)
        else:
            print(f"Sampling up to {args.events} events ...")
            tickers = await sample_event_tickers(
                client, count=args.events, series_limit=args.series, on_progress=print
            )
        print(f"Enumerating {len(tickers)} event(s) ...\n")

        for index, event_ticker in enumerate(tickers, start=1):
            try:
                evidence = await enumerate_event_membership(
                    client, event_ticker, max_pages=args.max_pages
                )
            except Exception as exc:
                print(f"  [{index}/{len(tickers)}] {event_ticker}: {type(exc).__name__}: {exc}")
                rows.append({"event_ticker": event_ticker, "error": f"{type(exc).__name__}"})
                continue
            row = membership_summary(evidence)
            row["classification"] = classify(evidence)
            rows.append(row)
            details.append(evidence.audit_payload())
            print(f"  [{index}/{len(tickers)}] {evidence.describe()}")

    ok = [r for r in rows if "error" not in r]
    summary = {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "note": "derived research output; not source evidence",
        "cutoff_market_settled_ts": (
            cutoff.market_settled_ts.isoformat() if cutoff.market_settled_ts else None
        ),
        "events_sampled": len(rows),
        "events_enumerated": len(ok),
        "classification_counts": dict(Counter(r["classification"] for r in ok)),
        "anomaly_counts": dict(Counter(a for r in ok for a in r["anomalies"])),
        "totals": {
            key: sum(r[key] for r in ok)
            for key in (
                "live_only",
                "historical_only",
                "both",
                "nested_only",
                "union",
                "nested",
                "nested_omissions",
            )
        },
        "events_with_nested_omissions": sum(1 for r in ok if r["nested_omissions"]),
        "events_with_historical_members": sum(1 for r in ok if r["historical_only"] or r["both"]),
        "events_with_duplicates_across_tiers": sum(1 for r in ok if r["both"]),
        "mutually_exclusive_counts": dict(Counter(str(r["mutually_exclusive"]) for r in ok)),
        "paths_exhausted": sum(
            1
            for r in ok
            if r["live_status"] == "EXHAUSTED" and r["historical_status"] == "EXHAUSTED"
        ),
        "rows": rows,
        "details": details,
    }
    args.out.write_text(json.dumps(summary, indent=2, default=str) + "\n")

    print("\n=== MEMBERSHIP ENUMERATION ===")
    for key, value in summary.items():
        if key in {"rows", "details", "note"}:
            continue
        print(f"  {key}: {value}")
    print(f"\nWrote {args.out}")
    print(
        "\nThese are OBSERVED counts over one sample. They do not establish that "
        "live + historical is exhaustive: a removed provisional market is in "
        "neither tier, and nothing queried here would reveal it."
    )


if __name__ == "__main__":
    asyncio.run(main())
