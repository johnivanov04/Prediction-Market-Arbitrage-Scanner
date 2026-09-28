"""Assemble a human-review packet for each pending settlement-certificate request.

Read-only, offline, and deliberately non-judgemental. It gathers everything a
reviewer needs in one place -- the claim, the payoff table it asserts, the rules
text, the governing documents and their hashes, the settlement sources, and the
checklist they must answer -- and it answers nothing.

The clause scan is a **reading aid, not a verdict**. It highlights language that
historically breaks a two-state payoff model so a reviewer looks there first. A
market with no highlights is not thereby safe: the most dangerous contract is
one whose exception lives in a document nobody opened, and this tool cannot read
documents. Highlight counts are review *prioritisation* and nothing else.

Nothing here approves, scores or ranks by suitability.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from predarb.config import Settings
from predarb.venues.kalshi.client import KalshiReadOnlyClient

ROOT = Path(__file__).resolve().parent.parent
SEMANTICS_ROOT = ROOT / "data" / "semantics"

# Language that historically breaks the standard two-state payoff. Surfaced
# verbatim with context; never used to pass or fail a market.
CLAUSE_PATTERNS: dict[str, re.Pattern[str]] = {
    "scalar / fair-price settlement": re.compile(
        r"fair\s+(market\s+)?(price|value)|\bscalar\b|pro\s*-?\s*rata|proportional", re.I
    ),
    "refund / void": re.compile(r"\bvoid(ed)?\b|\brefund(ed)?\b", re.I),
    "cancellation": re.compile(r"\bcancell?(ed|ation)?\b|\babandon(ed|ment)?\b", re.I),
    "postponement / rescheduling": re.compile(r"postpon|suspend|reschedul|delay(ed)?\b", re.I),
    "did-not-play / non-participation": re.compile(
        r"did not (play|start|participate|occur)|\bDNP\b|fails? to (play|occur)", re.I
    ),
    # Multiple markets in one event settling YES breaks mutual exclusion, and a
    # market that pays on a *group* containing the named subject breaks the
    # simple reading of its own YES condition.
    "multi-winner / co-winner / group payout": re.compile(
        r"multiple (persons?|people|winners?|markets?)|co-?winners?|\btie(s|d)?\b|jointly"
        r"|all (persons?|entities|names) that are listed|encompassed within",
        re.I,
    ),
    # The ALL-NO route: the underlying award, contest or measurement simply not
    # happening. Easy to miss because it is phrased as an ordinary condition.
    "no qualifying outcome": re.compile(
        r"if there is no\b|is not (awarded|named|declared|selected|held)"
        r"|no (winner|person of the year|award|qualifying)"
        r"|does not (occur|happen|take place)",
        re.I,
    ),
    # Anything that makes settlement a judgement rather than a lookup.
    "exchange discretion / interpretation": re.compile(
        r"sole discretion|at its discretion|may determine|Exchange may"
        r"|will resolve[^.]{0,60}reasonable|most simple meaning"
        r"|in the (judg|judge)ment of|as determined by",
        re.I,
    ),
    "early close": re.compile(r"early clos|close early|expire early", re.I),
    "source unavailability": re.compile(
        r"not (published|available|reported)|unavailab|no longer (published|reported)", re.I
    ),
    "amendment / revision": re.compile(r"revis(ed|ion)|amend(ed|ment)|correct(ed|ion)", re.I),
}


@dataclass
class Packet:
    request_id: str
    ticker: str
    claim: str
    proposition: str
    notional: str | None
    evidence_fingerprint: str
    rules_hash: str | None
    completeness: str
    missing_required: list[str] = field(default_factory=list)
    unretrievable: list[str] = field(default_factory=list)
    manual_viewing: dict[str, str] = field(default_factory=dict)
    rules_primary: str | None = None
    rules_secondary: str | None = None
    contract_url: str | None = None
    contract_terms_url: str | None = None
    settlement_sources: Any = None
    market_fields: dict[str, Any] = field(default_factory=dict)
    documents: dict[str, dict[str, Any]] = field(default_factory=dict)
    clauses: dict[str, list[str]] = field(default_factory=dict)
    checklist: list[dict[str, str]] = field(default_factory=list)
    captured_at: str | None = None
    live_status: str | None = None
    """Current venue status, when ``--check-live`` was used.

    Worth knowing before spending a review on it: a market that has already
    settled has no book, so certifying it cannot exercise a detector. The
    evidence bundle records status *as captured*, which may be hours old."""

    live_result: str | None = None

    @property
    def flag_count(self) -> int:
        return sum(len(v) for v in self.clauses.values())

    def payload(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "ticker": self.ticker,
            "claim": self.claim,
            "notional": self.notional,
            "evidence_fingerprint": self.evidence_fingerprint,
            "rules_hash": self.rules_hash,
            "completeness": self.completeness,
            "missing_required": self.missing_required,
            "unretrievable_documents": self.unretrievable,
            "manual_viewing_required": self.manual_viewing,
            "contract_url": self.contract_url,
            "contract_terms_url": self.contract_terms_url,
            "settlement_sources": self.settlement_sources,
            "documents": self.documents,
            "flagged_clauses": self.clauses,
            "flag_count": self.flag_count,
            "captured_at": self.captured_at,
            "live_status": self.live_status,
            "live_result": self.live_result,
            "checklist": self.checklist,
        }


def find_clauses(*texts: str | None) -> dict[str, list[str]]:
    body = "\n".join(t for t in texts if t)
    found: dict[str, list[str]] = {}
    for name, pattern in CLAUSE_PATTERNS.items():
        excerpts: list[str] = []
        for match in pattern.finditer(body):
            start = max(0, match.start() - 110)
            excerpt = body[start : match.end() + 110].strip().replace("\n", " ")
            excerpt = re.sub(r"\s+", " ", excerpt)
            if not any(excerpt[:60] in existing for existing in excerpts):
                excerpts.append(f"...{excerpt}...")
        if excerpts:
            found[name] = excerpts[:3]
    return found


def build(request: dict[str, Any], evidence: dict[str, Any]) -> Packet:
    market = evidence.get("market_fields", {})
    series = evidence.get("series_fields", {})
    completeness = request["completeness"]
    return Packet(
        request_id=request["request_id"],
        ticker=request["market_ticker"],
        claim=request["claim"],
        proposition=request["proposition"],
        notional=market.get("notional_value"),
        evidence_fingerprint=request["evidence_fingerprint"]["digest"],
        rules_hash=market.get("rules_hash"),
        completeness=completeness["completeness"],
        missing_required=list(completeness.get("missing_required", [])),
        unretrievable=list(completeness.get("unretrievable_documents", [])),
        manual_viewing=dict(completeness.get("manual_viewing_required", {})),
        rules_primary=market.get("rules_primary"),
        rules_secondary=market.get("rules_secondary"),
        contract_url=series.get("contract_url"),
        contract_terms_url=series.get("contract_terms_url"),
        settlement_sources=series.get("settlement_sources"),
        market_fields=market,
        documents=evidence.get("documents", {}),
        clauses=find_clauses(market.get("rules_primary"), market.get("rules_secondary")),
        checklist=list(request.get("checklist", [])),
        captured_at=evidence.get("captured_at"),
    )


def wrap(text: object) -> str:
    return textwrap.fill(str(text), width=94, initial_indent="    ", subsequent_indent="    ")


def render(packet: Packet) -> str:
    lines = [
        "=" * 96,
        f"  {packet.ticker}",
        f"  request {packet.request_id}",
        "=" * 96,
        "",
        f"  claim                : {packet.claim}",
        f"  notional             : {packet.notional}",
        f"  evidence fingerprint : {packet.evidence_fingerprint}",
        f"  rules hash           : {packet.rules_hash}",
        f"  evidence completeness: {packet.completeness}",
        f"  evidence captured at : {packet.captured_at}",
        f"  live status          : {packet.live_status or '(not checked)'}"
        + (f"  result={packet.live_result!r}" if packet.live_result else ""),
        f"  market_type          : {packet.market_fields.get('market_type')}",
        f"  settlement_kind      : {packet.market_fields.get('settlement_kind')}",
        f"  strike_type          : {packet.market_fields.get('strike_type')}",
        f"  can_close_early      : {packet.market_fields.get('can_close_early')}",
        f"  early_close_condition: {packet.market_fields.get('early_close_condition')}",
        "",
        "  PROPOSITION UNDER REVIEW",
        wrap(packet.proposition),
        "",
        "  RULES (primary)",
        wrap(packet.rules_primary or "(none published)"),
        "",
        "  RULES (secondary)",
        wrap(packet.rules_secondary or "(none published)"),
        "",
        "  GOVERNING DOCUMENTS",
        f"    contract_url       : {packet.contract_url}",
        f"    contract_terms_url : {packet.contract_terms_url}",
    ]
    for name, document in sorted(packet.documents.items()):
        lines.append(
            f"    {name:<18} : {document.get('retrieval')} "
            f"{document.get('content_bytes')}B  sha256 {str(document.get('content_sha256'))[:16]}"
        )
    lines += ["", "  SETTLEMENT SOURCES", wrap(json.dumps(packet.settlement_sources)), ""]

    if packet.manual_viewing:
        lines.append("  DOCUMENTS REQUIRING MANUAL SOURCE VIEWING")
        lines.append(
            wrap(
                "These were retrieved but could not be rendered readably. Approval "
                "requires acknowledging each by content hash -- acknowledging one "
                "version does not satisfy another."
            )
        )
        for name, digest in sorted(packet.manual_viewing.items()):
            lines.append(f"    {name:<18} : hash {digest[:32]}")
        lines.append("")

    lines.append(f"  CLAUSES TO READ CLOSELY ({packet.flag_count} match(es))")
    if not packet.clauses:
        lines.append(
            wrap(
                "No flagged language matched in the published rules. This is NOT a "
                "clearance and not evidence of simplicity. The scan matches known "
                "phrasings; a contract can create an ALL-NO or multi-YES path in words "
                "it has never seen. The exception that matters most often lives in a "
                "governing document, which this scan does not read at all -- read the "
                "rules above and the documents yourself."
            )
        )
    for name, excerpts in sorted(packet.clauses.items()):
        lines.append(f"    [{name}]")
        for excerpt in excerpts:
            lines.append(wrap(excerpt))
    lines += ["", "  CHECKLIST -- for you to answer, not me", ""]
    for index, question in enumerate(packet.checklist, start=1):
        lines.append(f"    {index:>2}. {question['prompt']}")
        lines.append(wrap(f"(why: {question['why_it_matters']})"))
    lines += [
        "",
        f"  review with : predarb certificates review {packet.request_id[:12]} --reviewer <NAME>",
        "",
    ]
    return "\n".join(lines)


def _annotate_live_status(packets: list[Packet]) -> None:
    """Fetch current venue status. The only networked part of this tool."""

    async def fetch() -> None:
        async with KalshiReadOnlyClient.public(env=Settings().kalshi_env) as client:
            for packet in packets:
                try:
                    market = await client.get_market(packet.ticker)
                except Exception as exc:
                    packet.live_status = f"unavailable ({type(exc).__name__})"
                    continue
                packet.live_status = market.status
                packet.live_result = market.result or None

    asyncio.run(fetch())


def main() -> None:
    parser = argparse.ArgumentParser(description="Human review packets")
    parser.add_argument("--request", action="append", metavar="REQUEST_ID")
    parser.add_argument("--root", type=Path, default=SEMANTICS_ROOT)
    parser.add_argument("--out", type=Path, default=ROOT / "review_packets.json")
    parser.add_argument(
        "--check-live",
        action="store_true",
        help="Fetch each market's current status. A settled market cannot "
        "exercise a detector, however good its evidence looks.",
    )
    args = parser.parse_args()

    packets: list[Packet] = []
    for path in sorted((args.root / "requests").glob("*.json")):
        request = json.loads(path.read_text())
        if args.request and not any(
            request["request_id"].startswith(prefix) for prefix in args.request
        ):
            continue
        snapshot = args.root / "evidence" / f"{request['snapshot_id']}.json"
        if not snapshot.exists():
            continue
        packets.append(build(request, json.loads(snapshot.read_text())))

    if args.check_live:
        _annotate_live_status(packets)

    for packet in packets:
        print(render(packet))

    print("=" * 96)
    print("  REVIEW PRIORITISATION (fewest flagged clauses first)")
    print("  This orders where to start reading. It is NOT a safety ranking, and")
    print("  none of these is 'safe to approve' -- that is your judgement alone.")
    print("=" * 96)
    for packet in sorted(packets, key=lambda p: (p.flag_count, p.ticker)):
        flags = ", ".join(sorted(packet.clauses)) or "none in published rules"
        if packet.live_status and packet.live_status not in {"active", "initialized"}:
            flags = f"[{packet.live_status.upper()} -- cannot exercise a detector] {flags}"
        print(
            f"  {packet.flag_count:>2} flag(s)  {packet.request_id[:12]}  "
            f"{packet.ticker:<32s} {flags}"
        )

    args.out.write_text(json.dumps([p.payload() for p in packets], indent=2, default=str) + "\n")
    print(f"\nWrote {args.out}")
    print(
        "\nNothing here is approved, and no checklist answer has been supplied. "
        "Issuance requires an APPROVED review recorded against the exact evidence "
        "fingerprint above."
    )


if __name__ == "__main__":
    main()
