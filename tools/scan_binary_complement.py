"""Live, read-only canary scan for same-market YES+NO complements.

Run with::

    uv run python tools/scan_binary_complement.py --seconds 120 --markets 8

What this is for
----------------
Kalshi's matching engine mints complementary pairs, so a persistent, executable,
fee-surviving YES+NO violation inside one market should be rare. A scan that
reports many large ones is far more likely to have found a bug in **our** stack
-- complement arithmetic, book inversion, sequence corruption, stale state,
duplicated liquidity, fee handling -- than free money at the venue. Zero proven
candidates is the expected, healthy result.

The semantic gate is not negotiable
-----------------------------------
Certificates come from the local registry (``predarb certificates``), and are
issued only by the human review workflow. This tool never mints one. There is
deliberately **no ``--skip-certification`` flag**: a market with no active
certificate, or one whose evidence has drifted since review, emits
``BLOCKED_SETTLEMENT_SEMANTICS`` and no economics.

Evidence is captured fresh for every market on every run and compared against
the certificate that was issued. A certificate that no longer matches current
evidence is reported as ``CERTIFICATE_STALE`` rather than quietly used.

To keep the funnel measurable anyway, the scan *also* computes what each market
**would** classify as if its semantics were verified. Those figures are labelled
hypothetical throughout and are never emitted as candidates. They exist so that
"blocked by semantics" does not hide whether the economics were even close.

Safety: read-only. Public market data and public metadata only. Places no order,
previews no order, and touches no balance, position, order or fill endpoint.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass, field
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
from predarb.detectors.binary_complement import evaluate_quantity, search_binary_complement
from predarb.domain.enums import MarketSide
from predarb.domain.models import VenueInstrument
from predarb.domain.money import Money, Quantity
from predarb.ingest.book_collector import BookCollector, default_journal_path
from predarb.opportunities.models import Classification
from predarb.semantics.certificate import CertificateStatus, standard_binary_complement
from predarb.semantics.evidence import SettlementEvidenceBundle
from predarb.semantics.fingerprint import SettlementEvidenceFingerprint
from predarb.semantics.policy import CertificateClaim
from predarb.semantics.registry import CertificateRegistry
from predarb.venues.kalshi.client import KalshiReadOnlyClient
from predarb.venues.kalshi.evidence_capture import capture_settlement_evidence
from predarb.venues.kalshi.fee_model import BalancePrecision
from predarb.venues.kalshi.fees import leg_fee_bounds
from predarb.venues.kalshi.market_discovery import discover_active_markets
from predarb.venues.kalshi.normalize import build_fee_timeline, normalize_market

ROOT = Path(__file__).resolve().parent.parent
JOURNAL_DIR = ROOT / "data" / "raw"
RESULTS_PATH = ROOT / "binary_complement_scan.json"
REVIEW_PATH = ROOT / "certificate_review_requests.json"


@dataclass
class ScanMetrics:
    """The funnel, counted honestly.

    ``proven_candidates`` counts only results actually emitted as arbitrage
    claims. The hypothetical counters describe what the economics would have
    supported had semantics been verified; they are diagnostics, not findings.
    """

    markets_monitored: int = 0
    markets_with_valid_books: int = 0
    quantities_evaluated: int = 0
    gross_complement_candidates: int = 0
    hypothetical_by_classification: dict[str, int] = field(default_factory=dict)
    blocked_by_settlement_semantics: int = 0
    blocked_by_fee_semantics: int = 0
    blocked_by_book_integrity: int = 0
    blocked_by_liquidity_collision: int = 0
    insufficient_depth: int = 0
    proven_candidates: int = 0
    markets_without_certificate: int = 0
    certificates_stale: int = 0
    best_gross_margin: Money | None = None
    """The most favourable margin actually observed, **including negative ones**.

    Not floored at zero. A zero here would read as "a break-even complement was
    seen", when in fact every market may have been comfortably uncrossed -- a
    very different picture of how close the venue came to a violation."""

    best_gross_margin_market: str | None = None
    largest_proven_profit_floor: Money | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "markets_monitored": self.markets_monitored,
            "markets_with_valid_books": self.markets_with_valid_books,
            "quantities_evaluated": self.quantities_evaluated,
            "gross_complement_candidates": self.gross_complement_candidates,
            "blocked_by_settlement_semantics": self.blocked_by_settlement_semantics,
            "blocked_by_fee_semantics": self.blocked_by_fee_semantics,
            "blocked_by_book_integrity": self.blocked_by_book_integrity,
            "blocked_by_liquidity_collision": self.blocked_by_liquidity_collision,
            "insufficient_depth": self.insufficient_depth,
            "proven_candidates_emitted": self.proven_candidates,
            "markets_without_certificate": self.markets_without_certificate,
            "certificates_stale": self.certificates_stale,
            "hypothetical": {
                "note": (
                    "what these markets would classify as IF their settlement "
                    "semantics were verified; not findings, and never emitted"
                ),
                # Every evaluation is accounted for. An earlier revision tallied
                # only three outcomes and silently dropped the rest, which made
                # a fully fee-blocked sweep look like an empty one.
                "by_classification": dict(sorted(self.hypothetical_by_classification.items())),
                "total": sum(self.hypothetical_by_classification.values()),
            },
            "best_gross_pre_fee_margin": (
                None if self.best_gross_margin is None else self.best_gross_margin.to_str()
            ),
            "best_gross_pre_fee_margin_market": self.best_gross_margin_market,
            "any_gross_complement_observed": self.gross_complement_candidates > 0,
            "largest_proven_profit_lower_bound": (
                None
                if self.largest_proven_profit_floor is None
                else self.largest_proven_profit_floor.to_str()
            ),
        }


def gross_complement_margin(
    view: BookView,
    instrument: VenueInstrument,
    context: ExecutionContext,
    quantity: Quantity,
    at: datetime,
) -> Money | None:
    """Payout minus gross acquisition cost, before any fee. ``None`` if unfillable.

    Deliberately pre-fee and pre-semantics: this is the "is it even worth
    looking at" number, and calling it anything stronger would be wrong.
    """
    if instrument.notional_value is None:
        return None
    try:
        yes = build_execution_curve(view, instrument, context, MarketSide.YES, at=at)
        no = build_execution_curve(view, instrument, context, MarketSide.NO, at=at)
    except (BookNotExecutableError, UnsupportedExecutionSemanticsError):
        return None
    yes_quote = yes.quote_up_to(quantity, at=at)
    no_quote = no.quote_up_to(quantity, at=at)
    if not (yes_quote.fully_fillable and no_quote.fully_fillable):
        return None
    payout = instrument.notional_value * quantity
    return payout - (yes_quote.gross_cost + no_quote.gross_cost)


async def resolve_markets(settings: Settings, args: argparse.Namespace) -> list[str]:
    if args.market:
        return list(args.market)
    print(f"Discovering up to {args.markets} active markets ...")
    async with KalshiReadOnlyClient.public(env=settings.kalshi_env) as client:
        candidates, _ = await discover_active_markets(
            client, count=args.markets, series_limit=args.series, now=datetime.now(tz=UTC)
        )
    for candidate in candidates:
        print(f"    {candidate.describe()}")
    return [c.ticker for c in candidates]


async def fetch_context(
    settings: Settings, tickers: list[str], at: datetime
) -> tuple[dict[str, VenueInstrument], dict[str, Any]]:
    """Instrument metadata plus a point-in-time fee configuration per market."""
    instruments: dict[str, VenueInstrument] = {}
    fee_configs: dict[str, Any] = {}
    async with KalshiReadOnlyClient.public(env=settings.kalshi_env) as client:
        series_changes = (
            await client.get_series_fee_changes(show_historical=True)
        ).series_fee_change_arr
        event_changes = (await client.get_event_fee_changes()).event_fee_changes
        for ticker in tickers:
            try:
                market = await client.get_market(ticker)
                instrument = normalize_market(market)
                instruments[ticker] = instrument
                event = (await client.get_event(instrument.event_ticker)).event
                series = await client.get_series(event.series_ticker or "")
                timeline = build_fee_timeline(
                    series=series,
                    event=event,
                    series_changes=series_changes,
                    event_changes=event_changes,
                )
                fee_configs[ticker] = timeline.resolve_at(at)
            except Exception as exc:
                print(f"  !! {ticker}: {type(exc).__name__}: {str(exc)[:110]}")
    return instruments, fee_configs


async def capture_evidence(
    settings: Settings, tickers: list[str], *, fetch_documents: bool
) -> dict[str, SettlementEvidenceBundle]:
    """Capture current settlement evidence for every market being scanned.

    Fresh every run: a certificate is only usable against the evidence that
    exists now, so a cached fingerprint would defeat drift detection entirely.
    """
    bundles: dict[str, SettlementEvidenceBundle] = {}
    async with KalshiReadOnlyClient.public(env=settings.kalshi_env) as client:
        for ticker in tickers:
            try:
                bundles[ticker] = await capture_settlement_evidence(
                    client, ticker, fetch_documents=fetch_documents
                )
            except Exception as exc:
                print(f"  !! evidence capture failed for {ticker}: {type(exc).__name__}: {exc}")
    return bundles


def scan_market(
    *,
    view: BookView,
    instrument: VenueInstrument,
    fee_config: Any,
    context: ExecutionContext,
    precision: BalancePrecision,
    max_quantity: Quantity,
    at: datetime,
    metrics: ScanMetrics,
    registry: CertificateRegistry,
    evidence: SettlementEvidenceBundle | None,
) -> dict[str, Any]:
    """Evaluate one market, emitting only what the semantic gate permits."""
    row: dict[str, Any] = {"ticker": view.market_ticker, "integrity": view.integrity.value}

    margin = gross_complement_margin(view, instrument, context, Quantity.from_value("1.00"), at)
    row["gross_margin_at_1_contract"] = None if margin is None else margin.to_str()
    if margin is not None:
        if margin.units > 0:
            metrics.gross_complement_candidates += 1
        if metrics.best_gross_margin is None or margin > metrics.best_gross_margin:
            metrics.best_gross_margin = margin
            metrics.best_gross_margin_market = view.market_ticker

    if fee_config is None or instrument.rules_hash is None:
        row["emitted"] = Classification.BLOCKED_SETTLEMENT_SEMANTICS.value
        row["reason"] = "no point-in-time fee configuration or no rules hash"
        metrics.blocked_by_settlement_semantics += 1
        return row

    # The certificate we actually emit with: never verified, because nobody has
    # read this market's rules.
    fingerprint: SettlementEvidenceFingerprint | None = (
        evidence.fingerprint() if evidence is not None else None
    )
    row["evidence_fingerprint"] = fingerprint.short if fingerprint else None
    quoter = _quoter(fee_config, precision)

    # The registry is the only source of certificates. No flag bypasses it.
    active = registry.active_at(
        market_ticker=instrument.ticker,
        claim=CertificateClaim.STANDARD_BINARY_COMPLEMENT,
        current_fingerprint=fingerprint,
        at=at,
    )
    history = registry.history_for(instrument.ticker)
    if active is not None:
        certificate = active.certificate
        row["certificate"] = active.certificate_id[:12]
    else:
        if history:
            # Reviewed once, but not against the evidence in force today.
            row["warning"] = "CERTIFICATE_STALE"
            metrics.certificates_stale += 1
            row["stale_certificates"] = [r.certificate_id[:12] for r in history]
        else:
            metrics.markets_without_certificate += 1
        certificate = standard_binary_complement(
            market_ticker=instrument.ticker,
            evidence_fingerprint=fingerprint
            or SettlementEvidenceFingerprint.over({"unavailable": instrument.ticker}),
            rules_hash=instrument.rules_hash or "",
            notional=instrument.notional_value,  # type: ignore[arg-type]
            evidence="",
            verified_by="",
            verification_method="",
            verified_at=at,
            valid_from=at,
            status=CertificateStatus.REVIEW_REQUIRED,
        )
    emitted = evaluate_quantity(
        instrument=instrument,
        view=view,
        certificate=certificate,
        current_evidence_fingerprint=fingerprint,
        context=context,
        fee_quoter=quoter,
        quantity=Quantity.from_value("1.00"),
        at=at,
    )
    row["emitted"] = emitted.classification.value
    row["reason"] = emitted.blocking_reason
    _count(emitted.classification, metrics)
    if emitted.is_arbitrage_claim:
        metrics.proven_candidates += 1

    # Hypothetical funnel: same inputs, semantics assumed. Diagnostics only.
    hypothetical = standard_binary_complement(
        market_ticker=instrument.ticker,
        evidence_fingerprint=fingerprint
        or SettlementEvidenceFingerprint.over({"unavailable": instrument.ticker}),
        rules_hash=instrument.rules_hash,
        notional=instrument.notional_value,  # type: ignore[arg-type]
        evidence="HYPOTHETICAL: assumed for funnel diagnostics; nobody read these rules.",
        verified_by="scan diagnostics",
        verification_method="ASSUMED -- not a real verification",
        verified_at=at,
        valid_from=at,
    )
    search = search_binary_complement(
        instrument=instrument,
        view=view,
        certificate=hypothetical,
        current_evidence_fingerprint=fingerprint or hypothetical.evidence_fingerprint,
        context=context,
        fee_quoter=quoter,
        min_quantity=Quantity.from_value("0.01"),
        max_quantity=max_quantity,
        at=at,
    )
    metrics.quantities_evaluated += search.evaluated_quantity_count
    row["hypothetical"] = {
        "evaluated": search.evaluated_quantity_count,
        "interval": f"[{search.search_min_quantity}, {search.search_max_quantity}]",
        "would_be_proven": len(search.proven_candidates),
    }
    # Counted over every evaluation, not just the retained ones: retention is a
    # memory decision and must not shape the funnel.
    counts = search.classification_counts
    for classification, count in counts.items():
        metrics.hypothetical_by_classification[classification.value] = (
            metrics.hypothetical_by_classification.get(classification.value, 0) + count
        )
    row["hypothetical"]["by_classification"] = {
        classification.value: count for classification, count in counts.items()
    }
    row["hypothetical"]["warnings"] = list(search.warnings)
    for result in search.proven_candidates:
        floor = result.profit.profit_lower_bound if result.profit else Money.zero()
        if metrics.largest_proven_profit_floor is None or (
            floor > metrics.largest_proven_profit_floor
        ):
            metrics.largest_proven_profit_floor = floor
    return row


def _quoter(fee_config: Any, precision: BalancePrecision) -> Any:
    def quote_fees(quote: Any) -> Any:
        return leg_fee_bounds(quote, fee_config, precision)

    return quote_fees


def _count(classification: Classification, metrics: ScanMetrics) -> None:
    mapping = {
        Classification.BLOCKED_SETTLEMENT_SEMANTICS: "blocked_by_settlement_semantics",
        Classification.BLOCKED_FEE_SEMANTICS: "blocked_by_fee_semantics",
        Classification.BLOCKED_BOOK_INTEGRITY: "blocked_by_book_integrity",
        Classification.BLOCKED_LIQUIDITY_COLLISION: "blocked_by_liquidity_collision",
        Classification.INSUFFICIENT_DEPTH: "insufficient_depth",
    }
    attribute = mapping.get(classification)
    if attribute:
        setattr(metrics, attribute, getattr(metrics, attribute) + 1)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only binary-complement canary scan")
    parser.add_argument("--market", action="append", metavar="TICKER")
    parser.add_argument("--markets", type=int, default=6)
    parser.add_argument("--series", type=int, default=250)
    parser.add_argument("--seconds", type=float, default=90.0)
    parser.add_argument(
        "--max-quantity",
        default="2.00",
        help="search cap in contracts; the caller chooses it, there is no universal cap",
    )
    parser.add_argument(
        "--semantics-root",
        type=Path,
        default=Path("./data/semantics"),
        help="local certificate/evidence store",
    )
    parser.add_argument(
        "--skip-documents",
        action="store_true",
        help="do not fetch external contract documents during evidence capture",
    )
    parser.add_argument(
        "--member-class",
        choices=["direct", "non-direct", "unknown"],
        default="unknown",
        help="balance precision assumption; 'unknown' uses the conservative bound",
    )
    args = parser.parse_args()

    settings = Settings()
    precision = {
        "direct": BalancePrecision.direct_member,
        "non-direct": BalancePrecision.non_direct_member,
        "unknown": BalancePrecision.unknown_member,
    }[args.member_class]()

    tickers = await resolve_markets(settings, args)
    if not tickers:
        print("No markets resolved; nothing to scan.")
        return

    started = datetime.now(tz=UTC)
    instruments, fee_configs = await fetch_context(settings, tickers, started)

    print(f"\nCapturing settlement evidence for {len(tickers)} markets ...")
    bundles = await capture_evidence(settings, tickers, fetch_documents=not args.skip_documents)
    registry = CertificateRegistry(args.semantics_root)
    for bundle in bundles.values():
        registry.store_evidence(bundle)

    print(f"\nCollecting books for {args.seconds:.0f}s ...")
    collector = BookCollector(
        settings=settings,
        markets=tickers,
        journal_path=default_journal_path(JOURNAL_DIR, now=started),
    )
    stats = await collector.run(duration_s=args.seconds)

    at = datetime.now(tz=UTC)
    if stats.final_epoch is None:
        # No connection epoch means no book was ever established under a known
        # connection, so nothing can be quoted authoritatively. Say so rather
        # than inventing an epoch.
        print("\nNo connection epoch was established; no book can be quoted. Nothing scanned.")
        return
    context = ExecutionContext(current_connection_epoch=stats.final_epoch)
    metrics = ScanMetrics(markets_monitored=len(tickers))
    rows: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []

    for ticker, view in stats.final_views.items():
        instrument = instruments.get(ticker)
        if instrument is None:
            continue
        if view.integrity.value == "VALID":
            metrics.markets_with_valid_books += 1
        rows.append(
            scan_market(
                view=view,
                instrument=instrument,
                fee_config=fee_configs.get(ticker),
                context=context,
                precision=precision,
                max_quantity=Quantity.from_value(args.max_quantity),
                at=at,
                metrics=metrics,
                registry=registry,
                evidence=bundles.get(ticker),
            )
        )
        review.append(
            {
                "ticker": ticker,
                "rules_hash": instrument.rules_hash,
                "evidence_fingerprint": (
                    bundles[ticker].fingerprint().digest if ticker in bundles else None
                ),
                "evidence_snapshot_id": (
                    bundles[ticker].snapshot_id if ticker in bundles else None
                ),
                "market_type_raw": instrument.market_type_raw,
                "notional": (
                    instrument.notional_value.to_str() if instrument.notional_value else None
                ),
                "rules_primary": instrument.rules_primary,
                "rules_secondary": instrument.rules_secondary,
                "note": (
                    "A human must read these terms before a VERIFIED certificate may be "
                    "issued. market_type alone is not evidence: Kalshi rules admit "
                    "fair-market and did-not-play resolutions that no two-state payoff "
                    "table describes."
                ),
            }
        )

    report = {
        "generated_at": at.isoformat(),
        "environment": settings.kalshi_env.value,
        "member_class_assumption": args.member_class,
        "semantics_root": str(args.semantics_root),
        "balance_precision": precision.describe(),
        "search_max_quantity": args.max_quantity,
        "observation_seconds": args.seconds,
        "connection_epoch": stats.final_epoch,
        "metrics": metrics.as_dict(),
        "markets": rows,
        "collector": collector.summary(),
    }
    RESULTS_PATH.write_text(json.dumps(report, indent=2, default=str) + "\n")
    REVIEW_PATH.write_text(json.dumps(review, indent=2, default=str) + "\n")

    print("\n=== SCAN METRICS ===")
    for key, value in metrics.as_dict().items():
        if isinstance(value, dict):
            print(f"  {key}:")
            for inner, inner_value in value.items():
                print(f"      {inner}: {inner_value}")
        else:
            print(f"  {key}: {value}")
    print(f"\nEmitted proven candidates: {metrics.proven_candidates}")
    if metrics.proven_candidates == 0:
        print("  Zero is the expected result. It is a finding, not a failure.")
    else:
        print(
            "  !! Investigate OUR stack first: complement arithmetic, book inversion,\n"
            "     sequence corruption, stale state, duplicated liquidity, fee handling."
        )
    print(f"\nWrote {RESULTS_PATH}")
    print(f"Wrote {REVIEW_PATH} ({len(review)} markets awaiting human rules review)")
    print(
        "\nCertificates come from the local registry only; there is no --skip-certification flag."
    )


if __name__ == "__main__":
    asyncio.run(main())
