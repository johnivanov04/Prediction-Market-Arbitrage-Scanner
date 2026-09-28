"""Find markets whose settlement semantics are *easiest to review*.

Ranked by evidence quality alone. Nothing here looks at price, spread, volume
or edge: the question is "which contract could a human actually finish reading
today", not "which one looks profitable". A market that is trivial to review and
worth nothing is a better candidate than a lucrative one whose governing rule
nobody can identify.

What makes a candidate easy
---------------------------
* its contract terms cite only rules whose citations resolve from objective
  evidence -- ideally the post-February-2026 template, which cites Rule 7.1 and
  drops the Rule 6.3 subsection references entirely;
* its governing PDFs extract cleanly, so the incorporation closure can be
  enumerated rather than declared by hand;
* its own rules text carries no fair-price, scalar, void/DNP or
  multi-winner clause;
* it is a plain two-sided binary with a live book.

Read-only and public throughout. Nothing here approves, scores or issues
anything, and no account endpoint is touched.
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
from predarb.semantics.dependency import ReferenceResolution
from predarb.semantics.policy import CertificateClaim, policy_for
from predarb.venues.kalshi.client import KalshiReadOnlyClient
from predarb.venues.kalshi.evidence_capture import (
    DocumentFetcher,
    GoverningSourceCache,
    capture_settlement_evidence,
)
from predarb.venues.kalshi.governing_sources import GOVERNING_SOURCE_URLS

CLAIM = CertificateClaim.STANDARD_BINARY_COMPLEMENT
POLICY = policy_for(CLAIM)

# Clauses that make a two-state complement doubtful. Screening on these is not
# a judgement that the market is bad -- only that reviewing it is harder.
HARD_CLAUSES: dict[str, re.Pattern[str]] = {
    "fair_price": re.compile(r"fair\s+(price|value|allocation)|pro\s*rata", re.I),
    "scalar": re.compile(r"\bscalar\b|proportion of the settlement", re.I),
    "void_refund": re.compile(r"\bvoid(ed)?\b|\bcancel(l?ed|lation)?\b|\brefund", re.I),
    "no_qualifying": re.compile(r"if (there is |there are )?no\b|if none\b|no qualifying", re.I),
    "multi_winner": re.compile(r"if multiple|more than one .{0,30}(win|announced|named)", re.I),
    "postponement": re.compile(r"postpon|suspend|not played|abandon|tie\b|draw\b", re.I),
    "discretion": re.compile(r"sole discretion|at its discretion|Kalshi may determine", re.I),
}

HARD_STRIKES = {"functional", "custom", "structured"}


@dataclass
class Candidate:
    ticker: str
    series_ticker: str | None
    event_ticker: str | None
    title: str
    completeness: str
    citations: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    clauses: dict[str, list[str]] = field(default_factory=dict)
    extraction: dict[str, str] = field(default_factory=dict)
    has_book: bool = False
    certified_note: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def score(self) -> tuple[int, int, int, int, str]:
        """Lower sorts better. Evidence quality only -- never economics."""
        return (
            len(self.unresolved),
            sum(len(v) for v in self.clauses.values()),
            0 if all(v == "CLEAN" for v in self.extraction.values()) else 1,
            0 if self.has_book else 1,
            self.ticker,
        )

    def payload(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "series_ticker": self.series_ticker,
            "event_ticker": self.event_ticker,
            "title": self.title,
            "completeness": self.completeness,
            "citations": self.citations,
            "unresolved": self.unresolved,
            "clauses": self.clauses,
            "extraction": self.extraction,
            "has_book": self.has_book,
            "notes": self.notes,
        }


def find_clauses(*texts: str | None) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for name, pattern in HARD_CLAUSES.items():
        hits: list[str] = []
        for text in texts:
            if not text:
                continue
            for match in pattern.finditer(text):
                start = max(0, match.start() - 60)
                hits.append(" ".join(text[start : match.end() + 90].split()))
        if hits:
            found[name] = hits[:2]
    return found


async def survey(
    limit: int, top: int, member_agreement: Path | None, max_series: int = 25
) -> list[Candidate]:
    now = datetime.now(tz=UTC)
    fetcher = DocumentFetcher()
    sources = dict(await GoverningSourceCache(fetcher).sources(at=now))
    if member_agreement is not None:
        sources["member_agreement"] = fetcher.from_local_file(
            url=GOVERNING_SOURCE_URLS["member_agreement"], path=member_agreement, at=now
        )

    candidates: list[Candidate] = []
    seen_series: set[str] = set()
    async with KalshiReadOnlyClient.public(env=Settings().kalshi_env) as client:
        # Enumerate by *event*, not by market. The venue returns markets
        # grouped, and the first two thousand open markets are all one series --
        # walking the market list is a very slow way to see two products.
        markets = []
        events = 0
        async for event in client.iter_events(status="open", with_nested_markets=True):
            events += 1
            if events > limit:
                break
            nested = list(getattr(event, "markets", ()) or ())
            if nested:
                markets.append((nested[0], event))
        for market, parent_event in markets:
            if market.market_type and market.market_type.lower() != "binary":
                continue
            if (market.strike_type or "").lower() in HARD_STRIKES:
                continue
            series = parent_event.series_ticker
            if len(seen_series) >= max_series:
                break
            if series is None or series in seen_series:
                # One market per series: the governing documents are the same
                # for every market in a series, so a second one adds nothing to
                # the review question.
                continue
            seen_series.add(series)

            clauses = find_clauses(market.rules_primary, market.rules_secondary)
            try:
                bundle = await capture_settlement_evidence(
                    client, market.ticker, at=now, governing_sources=sources
                )
            except Exception:
                continue
            report = POLICY.assess(bundle)
            terms = bundle.dependencies.get("contract_terms")
            citations = (
                [f"{d.reference}={d.reference_resolution.value}" for d in terms.flatten()]
                if terms
                else []
            )
            unresolved = (
                [
                    d.reference
                    for d in terms.flatten()
                    if d.is_material_to(POLICY.material_payout_impacts)
                    and d.reference_resolution
                    in {
                        ReferenceResolution.UNKNOWN,
                        ReferenceResolution.AMBIGUOUS_LEGACY_REFERENCE,
                        ReferenceResolution.BROKEN_REFERENCE,
                        ReferenceResolution.BROKEN_REFERENCE_AT_ISSUANCE,
                    }
                ]
                if terms
                else []
            )
            extraction = {
                name: (
                    document.extraction_detail.status
                    if document.extraction_detail
                    else document.extraction.value
                )
                for name, document in bundle.documents.items()
            }
            try:
                depth = await client.get_orderbook(market.ticker)
                book = bool(depth.yes_dollars or depth.no_dollars)
            except Exception:
                book = False

            candidates.append(
                Candidate(
                    ticker=market.ticker,
                    series_ticker=series,
                    event_ticker=market.event_ticker,
                    title=(market.title or market.yes_sub_title or "")[:90],
                    completeness=report.completeness.value,
                    citations=citations,
                    unresolved=unresolved,
                    clauses=clauses,
                    extraction=extraction,
                    has_book=book,
                    notes=[
                        f"unheld sources: {len(report.unheld_dependency_sources)}",
                        f"unknown closures: {list(report.unknown_dependency_closures)}",
                    ],
                )
            )
    return sorted(candidates, key=lambda c: c.score)[:top]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=120, help="Markets to screen.")
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument("--max-series", type=int, default=25, help="Distinct series to assess.")
    parser.add_argument("--member-agreement", type=Path, default=None)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    found = asyncio.run(survey(args.limit, args.top, args.member_agreement, args.max_series))
    print(f"=== {len(found)} REVIEW CANDIDATE(S), ranked by evidence quality only ===\n")
    for rank, candidate in enumerate(found, start=1):
        print(f"{rank}. {candidate.ticker}  [series {candidate.series_ticker}]")
        print(f"   {candidate.title}")
        print(f"   completeness       : {candidate.completeness}")
        print(f"   terms citations    : {', '.join(candidate.citations) or 'none'}")
        print(f"   material unresolved: {', '.join(candidate.unresolved) or 'none'}")
        print(f"   extraction         : {candidate.extraction}")
        print(f"   live book          : {candidate.has_book}")
        print(f"   hard clauses       : {list(candidate.clauses) or 'none'}")
        for name, hits in candidate.clauses.items():
            print(f"      {name}: {hits[0][:130]}")
        print()

    if args.out:
        args.out.write_text(
            json.dumps([c.payload() for c in found], indent=2, sort_keys=True) + "\n"
        )
        print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
