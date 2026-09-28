"""Census: does a strict two-state Kalshi contract exist under current rules?

Not a detector feature and not a candidate hunt. After several markets were
chased down only to reveal structural fallback settlement clauses, the question
worth answering first is whether a strictly two-state contract is available on
this exchange at all.

Each family is read, not keyword-matched: every mechanism detected is reported
with the sentence that established it, so a human can check the reading. A
family is only called disproven when authoritative language actually permits a
non-two-state settlement -- never because something is unknown.

Read-only and public throughout. Nothing is approved, scored or issued.
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
from predarb.semantics.policy import CertificateClaim, policy_for
from predarb.semantics.product_terms import certification_date_from
from predarb.semantics.settlement_census import (
    FamilyClassification,
    SettlementMechanism,
    classify_family,
)
from predarb.venues.kalshi.client import KalshiReadOnlyClient
from predarb.venues.kalshi.evidence_capture import (
    DocumentFetcher,
    GoverningSourceCache,
    capture_settlement_evidence,
)
from predarb.venues.kalshi.governing_sources import GOVERNING_SOURCE_URLS

POLICY = policy_for(CertificateClaim.STANDARD_BINARY_COMPLEMENT)

# Each pattern must match language that *permits* the mechanism, and the match
# is reported with its sentence so the reading can be checked.
MECHANISM_PATTERNS: dict[SettlementMechanism, re.Pattern[str]] = {
    SettlementMechanism.LAST_FAIR_PRICE: re.compile(r"last fair price", re.I),
    SettlementMechanism.LAST_TRADED_PRICE: re.compile(r"last traded price", re.I),
    SettlementMechanism.FAIR_ALLOCATION: re.compile(
        r"fair allocation|outcome review committee", re.I
    ),
    SettlementMechanism.FRACTIONAL_SHARE: re.compile(
        r"settlement value equal to \$\s*1\s*/|\$1/N|a settlement value of \$0\.", re.I
    ),
    SettlementMechanism.TIE_SPLIT: re.compile(
        r"sharing the same (stage|placement)|split (evenly|equally)|divided (evenly|equally)"
        r"|if the compared entities finish at the same",
        re.I,
    ),
    SettlementMechanism.CANCELLATION_LAST_RESULTS: re.compile(
        r"last official standings|last official results", re.I
    ),
    SettlementMechanism.VOID_REFUND: re.compile(
        r"\bvoid(ed)?\b|\brefund(ed)?\b|return of collateral", re.I
    ),
    SettlementMechanism.OUTCOME_REVIEW: re.compile(r"market outcome review", re.I),
    SettlementMechanism.INDETERMINATE_FALLBACK: re.compile(
        r"expiration value cannot be determined|cannot otherwise be unconditionally determined",
        re.I,
    ),
}

# A product whose primary subject is a natural person is exposed to Rule 6.3(e),
# which settles at a price rather than to a side. Detected from the terms'
# own description of the underlying, and reported for human confirmation.
NATURAL_PERSON = re.compile(
    r"\bperson\b|\bindividual\b|\bathlete\b|\bcompetitor\b|\bplayer\b|\bcandidate\b"
    r"|\bnominee\b|\bactor\b|\bceo\b|\bpresident\b|\bofficial\b",
    re.I,
)

EXCLUSION = re.compile(
    r"notwithstanding (rule|any).{0,80}(6\.3|7\.1)|shall not be settled at a (fair|last traded) "
    r"price|no fractional settlement|in no event shall.{0,60}(fraction|proportion)",
    re.I,
)


@dataclass
class FamilyRecord:
    series: str
    example_ticker: str
    title: str
    product_name: str = ""
    certified: str = ""
    completeness: str = ""
    extraction: dict[str, str] = field(default_factory=dict)
    hashes: dict[str, str] = field(default_factory=dict)
    unresolved_material: list[str] = field(default_factory=list)
    mechanisms: list[str] = field(default_factory=list)
    evidence: dict[str, str] = field(default_factory=dict)
    natural_person: bool = False
    excludes_fallback: bool = False
    classification: str = ""
    has_book: bool = False

    def payload(self) -> dict[str, Any]:
        return dict(self.__dict__)


def sentence_around(text: str, start: int, end: int, width: int = 150) -> str:
    return " ".join(text[max(0, start - width) : end + width].split())


# Mechanisms the Exchange Rulebook applies to every event contract, which no
# product term examined attempts to exclude. See the census report: Rule 6.3(c)
# is triggered by an epistemic condition rather than by a gap in the terms
# ("This includes, but is not limited to, circumstances where the Underlying
# cannot be measured and this contingency is not explicitly addressed in the
# Contract terms"), and Rule 7.1 is initiated at Kalshi's sole discretion with
# no carve-out for product terms at all.
SYSTEMIC_MECHANISMS: tuple[SettlementMechanism, ...] = (
    SettlementMechanism.INDETERMINATE_FALLBACK,
    SettlementMechanism.LAST_TRADED_PRICE,
    SettlementMechanism.FAIR_ALLOCATION,
    SettlementMechanism.OUTCOME_REVIEW,
)


def detect(text: str) -> tuple[list[SettlementMechanism], dict[str, str]]:
    """Mechanisms the product's own text permits, with the sentence for each.

    Whitespace is normalised first. The extracted text of several contract
    terms separates words with tabs rather than spaces, and matching on literal
    spaces silently reported five families as strictly two-state when their
    terms carried the Market Outcome Review clause in plain sight.
    """
    normalised = " ".join(text.split())
    found: list[SettlementMechanism] = [SettlementMechanism.ORDINARY_BINARY]
    evidence: dict[str, str] = {}
    for mechanism, pattern in MECHANISM_PATTERNS.items():
        match = pattern.search(normalised)
        if match:
            found.append(mechanism)
            evidence[mechanism.value] = sentence_around(normalised, match.start(), match.end())
    return found, evidence


async def census(
    families: int, member_agreement: Path | None, out: Path | None
) -> list[FamilyRecord]:
    now = datetime.now(tz=UTC)
    fetcher = DocumentFetcher()
    sources = dict(await GoverningSourceCache(fetcher).sources(at=now))
    if member_agreement is not None:
        sources["member_agreement"] = fetcher.from_local_file(
            url=GOVERNING_SOURCE_URLS["member_agreement"], path=member_agreement, at=now
        )

    records: list[FamilyRecord] = []
    seen: set[str] = set()
    async with KalshiReadOnlyClient.public(env=Settings().kalshi_env) as client:
        async for event in client.iter_events(status="open", with_nested_markets=True):
            if len(seen) >= families:
                break
            series = event.series_ticker
            nested = list(getattr(event, "markets", ()) or ())
            if not series or series in seen or not nested:
                continue
            market = nested[0]
            if market.market_type and market.market_type.lower() != "binary":
                continue
            seen.add(series)
            try:
                bundle = await capture_settlement_evidence(
                    client, market.ticker, at=now, governing_sources=sources
                )
            except Exception as exc:
                records.append(
                    FamilyRecord(
                        series=series,
                        example_ticker=market.ticker,
                        title=(market.title or "")[:70],
                        classification=f"CAPTURE_FAILED: {type(exc).__name__}",
                    )
                )
                continue

            report = POLICY.assess(bundle)
            terms = bundle.documents.get("contract_terms")
            text = (terms.text if terms else "") or ""
            rules = " ".join(
                str(bundle.market_fields.get(k) or "") for k in ("rules_primary", "rules_secondary")
            )
            corpus = " ".join(f"{text}\n{rules}".split())
            mechanisms, evidence = detect(corpus)
            excludes = bool(EXCLUSION.search(corpus))
            if not excludes:
                # The Rulebook's own fallbacks reach every contract. A product
                # whose terms are silent is not thereby exempt from them.
                for systemic in SYSTEMIC_MECHANISMS:
                    if systemic not in mechanisms:
                        mechanisms.append(systemic)
                        evidence.setdefault(
                            systemic.value,
                            "applies via Exchange Rulebook Rule 6.3(c)/7.1; the product "
                            "terms do not exclude it",
                        )

            unresolved = [
                key.rsplit("/", 1)[-1]
                for key in (*report.unresolved_citations, *report.unheld_dependency_sources)
            ]
            verdict = classify_family(mechanisms, unresolved_material=unresolved)
            try:
                depth = await client.get_orderbook(market.ticker)
                book = bool(depth.yes_dollars or depth.no_dollars)
            except Exception:
                book = False

            records.append(
                FamilyRecord(
                    series=series,
                    example_ticker=market.ticker,
                    title=(market.title or "")[:70],
                    product_name=(text[:70].strip() if text else ""),
                    certified=str(
                        certification_date_from(
                            bundle.documents["contract"].text
                            if "contract" in bundle.documents
                            else None
                        )
                        or ""
                    ),
                    completeness=report.completeness.value,
                    extraction={
                        n: (
                            d.extraction_detail.status
                            if d.extraction_detail
                            else d.extraction.value
                        )
                        for n, d in bundle.documents.items()
                    },
                    hashes={n: (d.content_sha256 or "")[:16] for n, d in bundle.documents.items()},
                    unresolved_material=sorted(set(unresolved)),
                    mechanisms=[m.value for m in mechanisms],
                    evidence=evidence,
                    natural_person=bool(NATURAL_PERSON.search(corpus)),
                    excludes_fallback=excludes,
                    classification=verdict.classification.value,
                    has_book=book,
                )
            )
    if out:
        out.write_text(json.dumps([r.payload() for r in records], indent=2, sort_keys=True) + "\n")
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--families", type=int, default=35)
    parser.add_argument("--member-agreement", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    records = asyncio.run(census(args.families, args.member_agreement, args.out))
    print(f"=== {len(records)} product families examined ===\n")
    print(f"{'series':26s} {'certified':11s} {'evidence':20s} {'classification':46s} book")
    for record in records:
        print(
            f"{record.series[:26]:26s} {record.certified[:11]:11s} "
            f"{record.completeness[:20]:20s} {record.classification[:46]:46s} "
            f"{'y' if record.has_book else 'n'}"
        )

    counts: dict[str, int] = {}
    for record in records:
        counts[record.classification] = counts.get(record.classification, 0) + 1
    print("\n=== classification counts ===")
    for name, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {count:4d}  {name}")

    strict = [
        r
        for r in records
        if r.classification == FamilyClassification.STRICT_TWO_STATE_POTENTIALLY_PROVABLE.value
    ]
    print(f"\nstrict two-state potentially provable: {len(strict)}")
    for record in strict:
        print(f"  {record.series} ({record.example_ticker}) book={record.has_book}")

    excluders = [r for r in records if r.excludes_fallback]
    print(f"families whose terms attempt to exclude the general fallback: {len(excluders)}")

    if args.verbose:
        print("\n=== mechanism evidence (first family of each classification) ===")
        shown: set[str] = set()
        for record in records:
            if record.classification in shown:
                continue
            shown.add(record.classification)
            print(f"\n-- {record.series}: {record.classification}")
            for mechanism, sentence in record.evidence.items():
                print(f"   {mechanism}: {sentence[:190]}")


if __name__ == "__main__":
    main()
