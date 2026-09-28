"""Audit the governing-document dependency closure of the evidence corpus.

Two questions, one pass:

1. **Extraction quality.** Can pypdf actually read the governing PDFs this
   workflow depends on? Automated dependency discovery is only as trustworthy
   as the text it runs over, so the failure and zero-text-page counts are
   reported rather than assumed away.
2. **Completeness.** With extraction and closure in place, what verdict does
   each stored review request get now?

Read-only and public throughout. No account, order, balance or position
endpoint is touched, and no credential is read.

Governing documents the publisher blocks from this client (HTTP 429) are
supplied from a local path with ``--local name=path`` and recorded as
``OPERATOR_SUPPLIED`` -- never fetched by pretending to be a browser.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from predarb.config import Settings
from predarb.semantics.evidence import ExternalDocument
from predarb.semantics.policy import CertificateClaim, policy_for
from predarb.venues.kalshi.client import KalshiReadOnlyClient
from predarb.venues.kalshi.evidence_capture import (
    DocumentFetcher,
    GoverningSourceCache,
    capture_settlement_evidence,
)
from predarb.venues.kalshi.governing_sources import GOVERNING_SOURCE_URLS

CLAIM = CertificateClaim.STANDARD_BINARY_COMPLEMENT


def stored_requests(root: Path) -> list[dict[str, Any]]:
    directory = root / "requests"
    if not directory.exists():
        return []
    return [json.loads(p.read_text()) for p in sorted(directory.glob("*.json"))]


def extraction_row(name: str, document: ExternalDocument) -> dict[str, Any]:
    detail = document.extraction_detail
    return {
        "document": name,
        "retrieval": document.retrieval.value,
        "sha256": (document.content_sha256 or "")[:16],
        "extraction": document.extraction.value,
        "parser": detail.parser_identity if detail else "-",
        "pages": detail.page_count if detail else 0,
        "zero_text_pages": len(detail.pages_with_no_text) if detail else 0,
        "chars": detail.character_count if detail else 0,
        "text_sha256": (detail.text_sha256 or "")[:12] if detail else "-",
        "warnings": list(detail.warnings) if detail else [],
        "manual_review": document.requires_manual_viewing,
    }


async def run(tickers: list[str], local: dict[str, Path]) -> dict[str, Any]:
    now = datetime.now(tz=UTC)
    fetcher = DocumentFetcher()
    cache = GoverningSourceCache(fetcher)
    sources = dict(await cache.sources(at=now))
    for name, path in local.items():
        url = GOVERNING_SOURCE_URLS.get(name, f"file://{path}")
        sources[name] = fetcher.from_local_file(url=url, path=path, at=now)

    policy = policy_for(CLAIM)
    corpus: list[dict[str, Any]] = [
        extraction_row(name, document) for name, document in sorted(sources.items())
    ]
    markets: list[dict[str, Any]] = []

    async with KalshiReadOnlyClient.public(env=Settings().kalshi_env) as client:
        for ticker in tickers:
            bundle = await capture_settlement_evidence(
                client, ticker, at=now, governing_sources=sources
            )
            corpus.extend(
                extraction_row(f"{ticker}/{name}", document)
                for name, document in sorted(bundle.documents.items())
            )
            report = policy.assess(bundle)
            markets.append(
                {
                    "ticker": ticker,
                    "completeness": report.completeness.value,
                    "closures": dict(report.dependency_closures),
                    "unknown_closures": list(report.unknown_dependency_closures),
                    "unheld_sources": list(report.unheld_dependency_sources),
                    "unresolved_citations": list(report.unresolved_citations),
                    "manual_viewing": {
                        k: v[:16] for k, v in report.manual_viewing_required.items()
                    },
                    "references": [
                        {
                            "parent": parent,
                            "closure": dependency_set.closure.value,
                            "citations": [d.describe() for d in dependency_set.flatten()],
                        }
                        for parent, dependency_set in sorted(bundle.dependencies.items())
                    ],
                }
            )
    return {"generated_at": now.isoformat(), "corpus": corpus, "markets": markets}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tickers", nargs="*", help="Market tickers to re-assess.")
    parser.add_argument("--root", type=Path, default=Path("data/semantics"))
    parser.add_argument(
        "--local",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="Operator-supplied governing document, e.g. member_agreement=/tmp/ma.pdf",
    )
    parser.add_argument("--out", type=Path, help="Write the full result as JSON here.")
    args = parser.parse_args()

    local = {}
    for entry in args.local:
        name, _, path = entry.partition("=")
        local[name] = Path(path)

    tickers = args.tickers or sorted({r["market_ticker"] for r in stored_requests(args.root)})
    result = asyncio.run(run(tickers, local))

    print("=== PDF EXTRACTION CORPUS ===")
    print(
        f"{'document':52s} {'retrieval':17s} {'extract':8s} "
        f"{'pages':>5s} {'0-text':>6s} {'chars':>7s}"
    )
    for row in result["corpus"]:
        print(
            f"{row['document']:52s} {row['retrieval']:17s} {row['extraction']:8s} "
            f"{row['pages']:>5d} {row['zero_text_pages']:>6d} {row['chars']:>7d}"
        )
        for warning in row["warnings"]:
            print(f"    ! {warning[:110]}")

    attempted = len(result["corpus"])
    clean = sum(1 for r in result["corpus"] if r["extraction"] == "CLEAN")
    partial = sum(1 for r in result["corpus"] if r["extraction"] == "PARTIAL")
    failed = sum(1 for r in result["corpus"] if r["extraction"] == "FAILED")
    print(
        f"\nattempted={attempted} clean={clean} partial={partial} failed={failed} "
        f"manual_review={sum(1 for r in result['corpus'] if r['manual_review'])}"
    )

    print("\n=== COMPLETENESS ===")
    for market in result["markets"]:
        print(f"\n{market['ticker']}: {market['completeness']}")
        print(f"  closures            : {market['closures']}")
        print(f"  unknown closures    : {market['unknown_closures']}")
        print(f"  unheld sources      : {market['unheld_sources']}")
        print(f"  unresolved citations: {market['unresolved_citations']}")
        for entry in market["references"]:
            if entry["citations"]:
                print(f"  -- {entry['parent']} [{entry['closure']}]")
                for citation in entry["citations"]:
                    print(f"       {citation}")

    if args.out:
        args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
