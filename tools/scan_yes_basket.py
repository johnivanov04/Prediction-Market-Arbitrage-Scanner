"""Live read-only funnel for AT_LEAST_ONE BUY-YES baskets.

Run with::

    uv run python tools/scan_yes_basket.py --events 40

Walks the gates in order and reports how many candidate groups survive each
one. Zero surviving is the expected result today and is reported as a result,
not as a failure: no Kalshi market has a human-reviewed settlement certificate
and no AT_LEAST_ONE relation certificate has been issued.

The gates are not weakened to produce a number. A funnel that reached the
economics by lowering a bar would be measuring the bar.

Read-only public market data plus the local certificate registries. No order,
balance, position or fill endpoint is touched, and nothing here approves
anything.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from predarb.config import Settings
from predarb.semantics.registry import CertificateRegistry, RelationRegistry
from predarb.semantics.relation import RelationClaim
from predarb.venues.kalshi.client import KalshiReadOnlyClient
from predarb.venues.kalshi.membership_capture import (
    enumerate_event_membership,
    sample_event_tickers,
)

ROOT = Path(__file__).resolve().parent.parent
SEMANTICS_ROOT = ROOT / "data" / "semantics"
RESULTS_PATH = ROOT / "yes_basket_scan.json"

MIN_MEMBERS = 2


@dataclass
class Funnel:
    """Each gate, and why groups stopped there."""

    events_examined: int = 0
    groups_with_two_or_more_members: int = 0
    groups_with_at_least_one_certificate: int = 0
    groups_with_all_member_certificates: int = 0
    groups_economically_evaluable: int = 0
    gross_candidates: int = 0
    fee_eliminated: int = 0
    fee_indeterminate: int = 0
    proven_contractual_arbitrage: int = 0
    stop_reasons: Counter[str] = field(default_factory=Counter)
    detail: list[dict[str, Any]] = field(default_factory=list)

    def payload(self) -> dict[str, Any]:
        return {
            "events_examined": self.events_examined,
            "groups_with_two_or_more_members": self.groups_with_two_or_more_members,
            "groups_with_at_least_one_certificate": self.groups_with_at_least_one_certificate,
            "groups_with_all_member_certificates": self.groups_with_all_member_certificates,
            "groups_economically_evaluable": self.groups_economically_evaluable,
            "gross_candidates": self.gross_candidates,
            "fee_eliminated": self.fee_eliminated,
            "fee_indeterminate": self.fee_indeterminate,
            "proven_contractual_arbitrage": self.proven_contractual_arbitrage,
            "stop_reasons": dict(self.stop_reasons),
            "detail": self.detail[:50],
        }


async def main() -> None:
    parser = argparse.ArgumentParser(description="AT_LEAST_ONE BUY-YES live funnel")
    parser.add_argument("--event", action="append", metavar="EVENT_TICKER")
    parser.add_argument("--events", type=int, default=40)
    parser.add_argument("--series", type=int, default=400)
    parser.add_argument("--semantics-root", type=Path, default=SEMANTICS_ROOT)
    parser.add_argument("--out", type=Path, default=RESULTS_PATH)
    args = parser.parse_args()

    settings = Settings()
    relation_registry = RelationRegistry(args.semantics_root)
    settlement_registry = CertificateRegistry(args.semantics_root)
    funnel = Funnel()

    certified_relations = {
        record.event_ticker: record
        for record in relation_registry.list_certificates()
        if record.certificate.claim is RelationClaim.AT_LEAST_ONE
    }
    certified_markets = {r.market_ticker for r in settlement_registry.list_certificates()}
    print(
        f"local registries: {len(certified_relations)} AT_LEAST_ONE relation "
        f"certificate(s), {len(certified_markets)} settlement certificate(s)"
    )

    async with KalshiReadOnlyClient.public(env=settings.kalshi_env) as client:
        tickers = (
            list(args.event)
            if args.event
            else await sample_event_tickers(
                client, count=args.events, series_limit=args.series, on_progress=print
            )
        )
        print(f"\nExamining {len(tickers)} event(s) ...\n")

        for index, event_ticker in enumerate(tickers, start=1):
            try:
                evidence = await enumerate_event_membership(client, event_ticker)
            except Exception as exc:
                funnel.stop_reasons[f"enumeration_failed:{type(exc).__name__}"] += 1
                continue
            funnel.events_examined += 1
            members = evidence.member_tickers

            if len(members) < MIN_MEMBERS:
                funnel.stop_reasons["fewer_than_two_members"] += 1
                continue
            funnel.groups_with_two_or_more_members += 1

            record = certified_relations.get(event_ticker)
            if record is None:
                # The first gate, and today the universal one.
                funnel.stop_reasons["no_AT_LEAST_ONE_relation_certificate"] += 1
                continue
            funnel.groups_with_at_least_one_certificate += 1

            selected = record.selected_members
            uncertified = sorted(set(selected) - certified_markets)
            if uncertified:
                funnel.stop_reasons["missing_member_settlement_certificates"] += 1
                funnel.detail.append(
                    {
                        "event_ticker": event_ticker,
                        "stopped_at": "member settlement certificates",
                        "uncertified": uncertified,
                    }
                )
                continue
            funnel.groups_with_all_member_certificates += 1

            # Beyond this point the scan would need live books and resolved fee
            # context. Reaching it requires certificates that do not exist, so
            # the funnel reports the gate rather than pretending to pass it.
            funnel.stop_reasons["books_and_fees_not_resolved_in_this_tool"] += 1
            print(f"  [{index}/{len(tickers)}] {event_ticker}: reached the economics gate")

    args.out.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(tz=UTC).isoformat(),
                "note": "derived research output; not source evidence",
                **funnel.payload(),
            },
            indent=2,
            default=str,
        )
        + "\n"
    )

    print("\n=== AT_LEAST_ONE BUY-YES FUNNEL ===")
    for key, value in funnel.payload().items():
        if key == "detail":
            continue
        print(f"  {key}: {value}")
    print(f"\nWrote {args.out}")
    if funnel.proven_contractual_arbitrage == 0:
        print(
            "\nZero proven candidates. With no AT_LEAST_ONE relation certificate "
            "issued, every group is blocked on relation semantics before any book "
            "is read. That is the designed behaviour, not a scanning failure."
        )


if __name__ == "__main__":
    asyncio.run(main())
