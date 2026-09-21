"""Read-only discovery of candidate AT_MOST_ONE events.

Run with::

    uv run python tools/discover_relations.py --events 400

Finds events whose metadata marks them mutually exclusive and which currently
return at least two ordinary member markets, captures the relation evidence, and
writes a **review queue**. It certifies nothing: every request comes out
``AWAITING_REVIEW`` and a person decides.

Two things this output must never be read as
--------------------------------------------
``mutually_exclusive = true`` is strong structured evidence and not a
certification. It pre-populates a review; a human still has to read the rules.

The member list is **current observed membership, not proven exhaustive
membership**: ``GET /events`` omits markets settled before the historical cutoff
(A-46). An AT_MOST_ONE claim over a selected subset tolerates that, which is
exactly why it is the claim we build. Exhaustiveness claims would not, and none
is implemented.

Safety: read-only public metadata. No order, balance, position or fill endpoint,
and no credentials are sent to the public contract-document hosts.
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
from predarb.domain.enums import SettlementKind
from predarb.semantics.policy import CertificateClaim
from predarb.semantics.registry import CertificateRegistry, RelationRegistry
from predarb.semantics.relation import RelationClaim, RelationReviewRequest
from predarb.venues.kalshi.client import KalshiReadOnlyClient
from predarb.venues.kalshi.evidence_capture import capture_settlement_evidence
from predarb.venues.kalshi.normalize import normalize_market, settlement_kind_for
from predarb.venues.kalshi.relation_capture import capture_relation_evidence

ROOT = Path(__file__).resolve().parent.parent
RESULTS_PATH = ROOT / "relation_discovery.json"

MIN_MEMBERS = 2


@dataclass
class DiscoveryMetrics:
    events_scanned: int = 0
    mutually_exclusive_events: int = 0
    events_with_enough_members: int = 0
    relation_requests_generated: int = 0
    requests_awaiting_review: int = 0
    requests_evidence_incomplete: int = 0
    events_with_verified_relation: int = 0
    candidate_member_markets: int = 0
    members_individually_certified: int = 0
    incompleteness_reasons: Counter[str] = field(default_factory=Counter)

    def as_dict(self) -> dict[str, Any]:
        return {
            "events_scanned": self.events_scanned,
            "mutually_exclusive_events": self.mutually_exclusive_events,
            "events_with_at_least_2_ordinary_members": self.events_with_enough_members,
            "relation_review_requests_generated": self.relation_requests_generated,
            "requests_awaiting_human_review": self.requests_awaiting_review,
            "requests_blocked_evidence_incomplete": self.requests_evidence_incomplete,
            "events_with_verified_relation_certificate": self.events_with_verified_relation,
            "candidate_member_markets": self.candidate_member_markets,
            "members_with_individual_settlement_certificate": (self.members_individually_certified),
            "incompleteness_reasons": dict(self.incompleteness_reasons.most_common()),
            "membership_caveat": (
                "member lists are CURRENT OBSERVED MEMBERSHIP, not proven exhaustive "
                "membership: GET /events omits markets settled before the historical "
                "cutoff (A-46)"
            ),
        }


async def discover(args: argparse.Namespace) -> dict[str, Any]:
    settings = Settings()
    metrics = DiscoveryMetrics()
    rows: list[dict[str, Any]] = []
    settlement_registry = CertificateRegistry(args.semantics_root)
    relation_registry = RelationRegistry(args.semantics_root)
    now = datetime.now(tz=UTC)

    async with KalshiReadOnlyClient.public(env=settings.kalshi_env) as client:
        # The listing does not carry nested markets, so membership needs a
        # per-event fetch. Scan cheaply for the flag first, then fetch only the
        # events actually being captured.
        candidates: list[str] = []
        async for event in client.iter_events(status="open"):
            metrics.events_scanned += 1
            if event.mutually_exclusive:
                metrics.mutually_exclusive_events += 1
                candidates.append(event.event_ticker)
            if metrics.events_scanned >= args.events:
                break

        for event_ticker in candidates[: args.capture]:
            envelope = await client.get_event(event_ticker, with_nested_markets=True)
            event = envelope.event
            ordinary = [
                market
                for market in envelope.member_markets
                if settlement_kind_for(market.market_type) is SettlementKind.BINARY
                and not normalize_market(market).exclusion_reasons
            ]
            if len(ordinary) < MIN_MEMBERS:
                continue
            metrics.events_with_enough_members += 1
            selected = sorted(m.ticker for m in ordinary)[: args.members]
            metrics.candidate_member_markets += len(selected)

            certified: dict[str, str] = {}
            for ticker in selected:
                try:
                    bundle = await capture_settlement_evidence(
                        client, ticker, at=now, fetch_documents=False
                    )
                except Exception:
                    continue
                record = settlement_registry.active_at(
                    market_ticker=ticker,
                    claim=CertificateClaim.STANDARD_BINARY_COMPLEMENT,
                    current_fingerprint=bundle.fingerprint(),
                    at=now,
                )
                if record is not None:
                    certified[ticker] = record.certificate_id
            metrics.members_individually_certified += len(certified)

            relation_bundle = await capture_relation_evidence(
                client,
                event_ticker=event.event_ticker,
                selected_members=selected,
                member_certificate_ids=certified,
                at=now,
                fetch_documents=not args.skip_documents,
            )
            relation_registry.store_evidence(relation_bundle)
            request = RelationReviewRequest.create(
                bundle=relation_bundle, claim=RelationClaim.AT_MOST_ONE, generated_at=now
            )
            relation_registry.store_request(request)
            metrics.relation_requests_generated += 1
            if request.permits_approval:
                metrics.requests_awaiting_review += 1
            else:
                metrics.requests_evidence_incomplete += 1
                for reason in request.incompleteness_reasons:
                    metrics.incompleteness_reasons[reason.split(":")[0]] += 1

            existing = relation_registry.active_at(
                event_ticker=event.event_ticker,
                claim=RelationClaim.AT_MOST_ONE,
                members=selected,
                current_fingerprint=relation_bundle.fingerprint(),
                member_settlement_fingerprints={
                    m.ticker: m.settlement_fingerprint
                    for m in relation_bundle.member_evidence
                    if m.settlement_fingerprint
                },
                at=now,
            )
            if existing is not None:
                metrics.events_with_verified_relation += 1

            rows.append(
                {
                    "event_ticker": event.event_ticker,
                    "request_id": request.request_id[:12],
                    "mutually_exclusive": event.mutually_exclusive,
                    "currently_observed_member_count": len(
                        relation_bundle.observed_event_membership
                    ),
                    "selected_members": selected,
                    "membership_caveat": (
                        "current observed membership, not proven exhaustive membership"
                    ),
                    "evidence_completeness": request.completeness.value,
                    "incompleteness_reasons": list(request.incompleteness_reasons),
                    "members_with_settlement_certificate": len(certified),
                    "proposed_claim": RelationClaim.AT_MOST_ONE.value,
                    "status": "AWAITING_REVIEW"
                    if request.permits_approval
                    else "EVIDENCE_INCOMPLETE",
                    "relation_certificate": (existing.certificate_id[:12] if existing else None),
                }
            )

    return {
        "generated_at": now.isoformat(),
        "environment": settings.kalshi_env.value,
        "metrics": metrics.as_dict(),
        "events": rows,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="Discover AT_MOST_ONE relation candidates")
    parser.add_argument("--events", type=int, default=400, help="events to scan")
    parser.add_argument("--capture", type=int, default=5, help="events to capture evidence for")
    parser.add_argument("--members", type=int, default=6, help="max members to select per event")
    parser.add_argument("--semantics-root", type=Path, default=Path("./data/semantics"))
    parser.add_argument("--skip-documents", action="store_true")
    args = parser.parse_args()

    report = await discover(args)
    RESULTS_PATH.write_text(json.dumps(report, indent=2, default=str) + "\n")

    print("\n=== RELATION DISCOVERY ===")
    for key, value in report["metrics"].items():
        if isinstance(value, dict):
            print(f"  {key}:")
            for inner, count in value.items():
                print(f"      {inner}: {count}")
        else:
            print(f"  {key}: {value}")

    print("\n=== REVIEW QUEUE ===")
    for row in report["events"]:
        print(
            f"  {row['request_id']}  {row['event_ticker'][:44]:<44s} "
            f"{row['status']:<20s} members={len(row['selected_members'])} "
            f"certified={row['members_with_settlement_certificate']}"
        )
        for reason in row["incompleteness_reasons"]:
            print(f"      - {reason}")
    print("\nNothing is certified by this tool. A human reviews each request.")
    print(f"Wrote {RESULTS_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
