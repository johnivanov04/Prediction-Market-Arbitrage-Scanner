"""Prepare a curated human-review queue of straightforward current markets.

Phase 1's standing empirical limitation is that no real Kalshi settlement
certificate has been human-issued, so the detector economics have never run
through the semantic gate on live data. This tool builds the shortlist that
would let a human close that gap.

It does **not** approve anything, and it cannot: issuance requires an APPROVED
review recorded against an exact evidence fingerprint, and there is one code
path to that (`predarb certificates review`). What this produces is a queue of
`AWAITING_REVIEW` requests plus the reasons a reader should look closely.

Screening is conservative and structural
-----------------------------------------
Candidates are *screened out* on signals that a market's payoff is not the
simple two-state one -- scalar type, a non-binary result, unreadable strike
semantics, rules text advertising fair-price, void, refund, postponement or
discretionary settlement. Screening in is not a judgement that a market is
certifiable: it means nothing obvious disqualified it, and a human still has to
read the contract.

The flagged clauses are surfaced verbatim rather than scored. "This market
mentions cancellation" is something a reviewer should see; a number claiming
how much it matters would be invented.

Read-only public market data. Nothing here submits or approves.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from predarb.config import Settings
from predarb.semantics.policy import CertificateClaim
from predarb.semantics.registry import CertificateRegistry
from predarb.semantics.review import ReviewRequest
from predarb.venues.kalshi.client import KalshiReadOnlyClient
from predarb.venues.kalshi.evidence_capture import capture_settlement_evidence
from predarb.venues.kalshi.market_discovery import discover_active_markets

ROOT = Path(__file__).resolve().parent.parent
SEMANTICS_ROOT = ROOT / "data" / "semantics"
RESULTS_PATH = ROOT / "review_queue.json"

# Phrases that mean "the payoff may not be the simple two-state one". Matched
# case-insensitively against the rules text and surfaced verbatim; never used
# to auto-reject a market a human has actually read.
DISQUALIFYING_PATTERNS: dict[str, re.Pattern[str]] = {
    "fair_price_settlement": re.compile(r"fair\s+(market\s+)?(price|value)", re.I),
    "scalar_settlement": re.compile(r"\bscalar\b|pro\s*-?\s*rata|proportional(ly)?\s+settle", re.I),
    "void_or_refund": re.compile(r"\bvoid(ed)?\b|\brefund(ed)?\b|\bcancell?ed\b", re.I),
    "postponement": re.compile(r"postpon|suspend|rescheduled|abandon", re.I),
    "did_not_play": re.compile(r"did not (play|start|participate)|\bDNP\b", re.I),
    "exchange_discretion": re.compile(r"sole discretion|at its discretion|may determine", re.I),
    "tie_or_co_winner": re.compile(r"\btie\b|\bties\b|co-?winner|jointly", re.I),
}


@dataclass
class Candidate:
    """One screened market, with everything a reviewer needs to decide."""

    ticker: str
    title: str | None
    market_type: str | None
    status: str | None
    strike_type: str | None
    result: str | None
    screened_out: list[str] = field(default_factory=list)
    flagged_clauses: dict[str, str] = field(default_factory=dict)
    evidence_completeness: str | None = None
    incompleteness_reasons: list[str] = field(default_factory=list)
    documents: dict[str, str] = field(default_factory=dict)
    request_id: str | None = None
    rules_primary: str | None = None

    @property
    def eligible(self) -> bool:
        return not self.screened_out

    def payload(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "title": self.title,
            "market_type": self.market_type,
            "status": self.status,
            "strike_type": self.strike_type,
            "result": self.result,
            "eligible_for_review": self.eligible,
            "screened_out": self.screened_out,
            "flagged_clauses": self.flagged_clauses,
            "evidence_completeness": self.evidence_completeness,
            "incompleteness_reasons": self.incompleteness_reasons,
            "documents": self.documents,
            "request_id": self.request_id,
            "rules_primary": self.rules_primary,
        }


def screen(market: Any) -> Candidate:
    """Structural screening. Conservative in one direction only.

    A market that survives has no *obvious* disqualifier. That is not the same
    as being certifiable, and nothing downstream treats it as such.
    """
    rules = " ".join(filter(None, [market.rules_primary or "", market.rules_secondary or ""]))
    candidate = Candidate(
        ticker=market.ticker,
        title=market.title,
        market_type=market.market_type,
        status=market.status,
        strike_type=market.strike_type,
        result=market.result,
        rules_primary=market.rules_primary,
    )

    if (market.market_type or "").lower() != "binary":
        candidate.screened_out.append(
            f"market_type is {market.market_type!r}, not binary; the standard "
            "two-state payoff table does not apply"
        )
    if (market.result or "") not in ("", None, "yes", "no"):
        candidate.screened_out.append(f"result {market.result!r} is not a binary outcome")
    if (market.strike_type or "").lower() in {"functional", "structured"}:
        candidate.screened_out.append(
            f"strike_type {market.strike_type!r} has no documented grammar"
        )
    if not rules.strip():
        candidate.screened_out.append("no rules text published, so nothing can be reviewed")

    for name, pattern in DISQUALIFYING_PATTERNS.items():
        match = pattern.search(rules)
        if match is None:
            continue
        start = max(0, match.start() - 90)
        excerpt = rules[start : match.end() + 90].strip().replace("\n", " ")
        candidate.flagged_clauses[name] = f"...{excerpt}..."
        # Structural payoff hazards disqualify; the rest are surfaced for a
        # reader without blocking the request.
        if name in {"fair_price_settlement", "scalar_settlement"}:
            candidate.screened_out.append(f"rules mention {name.replace('_', ' ')}")
    return candidate


async def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare a human review queue")
    parser.add_argument("--market", action="append", metavar="TICKER")
    parser.add_argument("--markets", type=int, default=25)
    parser.add_argument("--series", type=int, default=250)
    parser.add_argument("--queue", type=int, default=5, help="How many requests to open.")
    parser.add_argument("--semantics-root", type=Path, default=SEMANTICS_ROOT)
    parser.add_argument("--out", type=Path, default=RESULTS_PATH)
    parser.add_argument("--dry-run", action="store_true", help="Screen only; open no requests.")
    args = parser.parse_args()

    settings = Settings()
    registry = CertificateRegistry(args.semantics_root)
    candidates: list[Candidate] = []

    async with KalshiReadOnlyClient.public(env=settings.kalshi_env) as client:
        if args.market:
            tickers = list(args.market)
        else:
            found, _ = await discover_active_markets(
                client,
                count=args.markets,
                series_limit=args.series,
                now=datetime.now(tz=UTC),
            )
            tickers = [c.ticker for c in found]
        print(f"Screening {len(tickers)} market(s) ...\n")

        for ticker in tickers:
            try:
                market = await client.get_market(ticker)
            except Exception as exc:
                print(f"  !! {ticker}: {type(exc).__name__}")
                continue
            candidate = screen(market)
            candidates.append(candidate)
            state = "eligible" if candidate.eligible else "screened out"
            print(f"  {ticker:<34s} {state}")
            for reason in candidate.screened_out:
                print(f"      - {reason}")

        eligible = [c for c in candidates if c.eligible][: args.queue]
        if eligible and not args.dry_run:
            print(f"\nCapturing evidence and opening {len(eligible)} review request(s) ...")
        for candidate in eligible:
            if args.dry_run:
                continue
            try:
                bundle = await capture_settlement_evidence(client, candidate.ticker)
            except Exception as exc:
                candidate.incompleteness_reasons.append(
                    f"evidence capture failed: {type(exc).__name__}: {exc}"
                )
                continue
            registry.store_evidence(bundle)
            request = ReviewRequest.create(
                bundle=bundle,
                claim=CertificateClaim.STANDARD_BINARY_COMPLEMENT,
                generated_at=datetime.now(tz=UTC),
            )
            registry.store_request(request)
            candidate.request_id = request.request_id
            candidate.evidence_completeness = request.completeness.completeness.value
            candidate.incompleteness_reasons = [
                *request.completeness.missing_required,
                *(
                    f"{name}: not retrievable"
                    for name in request.completeness.unretrievable_documents
                ),
                *(
                    f"{name}: must be read at source (hash {digest[:12]})"
                    for name, digest in request.completeness.manual_viewing_required.items()
                ),
            ]
            candidate.documents = {
                name: document.retrieval.value for name, document in bundle.documents.items()
            }
            print(f"  {candidate.ticker}: {request.describe()}")

    payload = {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "note": "derived research output; nothing here is approved",
        "screened": len(candidates),
        "eligible": sum(1 for c in candidates if c.eligible),
        "requests_opened": sum(1 for c in candidates if c.request_id),
        "candidates": [c.payload() for c in candidates],
    }
    args.out.write_text(json.dumps(payload, indent=2, default=str) + "\n")

    print("\n=== HUMAN REVIEW QUEUE ===")
    for candidate in candidates:
        if not candidate.request_id:
            continue
        print(f"\n  {candidate.ticker}  [{candidate.request_id[:12]}]")
        print(f"    claim        : {CertificateClaim.STANDARD_BINARY_COMPLEMENT.value}")
        print(f"    completeness : {candidate.evidence_completeness}")
        print(f"    documents    : {candidate.documents}")
        if candidate.incompleteness_reasons:
            print(f"    missing      : {candidate.incompleteness_reasons}")
        for name, excerpt in candidate.flagged_clauses.items():
            print(f"    !! {name}: {excerpt[:160]}")
        print(
            f"    review with  : predarb certificates review {candidate.request_id[:12]} "
            "--reviewer <NAME>"
        )
    print(f"\nWrote {args.out}")
    print(
        "\nNothing here is approved. Issuance requires an APPROVED review recorded "
        "against this exact evidence fingerprint, and only a human can record one."
    )


if __name__ == "__main__":
    asyncio.run(main())
