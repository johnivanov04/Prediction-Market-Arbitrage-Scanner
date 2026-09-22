"""Capture a read-only live session as a replay bundle, then replay it offline.

Run with::

    uv run python tools/capture_replay_bundle.py --seconds 90 --markets 4

Three halves, which is one more than it sounds
----------------------------------------------
**Capture.** Connects read-only and records every input the live system
consumed as an :class:`Observation`: the detector plan, market metadata, the fee
configuration in force, explicit snapshots of the local settlement and relation
certificate registries, connection lifecycle, subscription acks and frames.

**Live processing.** Each recorded observation is fed immediately into the
production :class:`EvaluationCoordinator`, against a knowledge base that grows
*incrementally* -- it holds only what has been observed so far. The resulting
:class:`DecisionRecord` list is the validation oracle. It is written beside the
bundle, never into it: a bundle holds observations, and a decision derived from
observations is output, not evidence.

**Replay.** The bundle is reloaded from disk and replayed offline through the
same coordinator, but against a knowledge base built from the *entire* stream up
front. That asymmetry is the point. The replay has every future observation
indexed and available; only point-in-time horizon filtering stops it from using
them. If that filtering leaked, the replay would reach a different verdict from
the live run, which had no choice but to be honest. Agreement is therefore
evidence of no lookahead, not a tautology.

Book state is compared as well, through an independently driven reconstructor,
so a transport-level regression cannot hide behind matching verdicts.

Safety: read-only public market data plus local research files. No order,
balance, position or fill endpoint is touched, and nothing credential-bearing is
recorded into the bundle.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import socket
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from predarb.books.reconstruction import OrderBookReconstructor
from predarb.books.state import BookIntegrity
from predarb.clock import SystemClock
from predarb.config import Settings
from predarb.domain.models import VenueInstrument
from predarb.domain.money import Quantity
from predarb.ingest.book_collector import default_journal_path
from predarb.ingest.raw_journal import RawFrameJournal
from predarb.replay.bundle import load_bundle, write_bundle
from predarb.replay.decision import DecisionRecord, compare_decisions
from predarb.replay.engine import ReplayEngine
from predarb.replay.journal import ReplayFrameJournal
from predarb.replay.knowledge import KnowledgeBase
from predarb.replay.live import LiveSession
from predarb.replay.observation import Observation, ObservationKind, ObservationStream
from predarb.replay.plan import BasketPlan, ContextRefreshPolicy, DetectorPlan
from predarb.semantics.registry import CertificateRegistry, RelationRegistry
from predarb.venues.kalshi.client import KalshiReadOnlyClient
from predarb.venues.kalshi.evidence_capture import capture_settlement_evidence
from predarb.venues.kalshi.market_discovery import discover_active_markets
from predarb.venues.kalshi.normalize import build_fee_timeline, normalize_market
from predarb.venues.kalshi.relation_capture import capture_relation_evidence
from predarb.venues.kalshi.replay_context import KalshiReplayContext
from predarb.venues.kalshi.session import websocket_client
from predarb.venues.kalshi.websocket import WebSocketIdle

ROOT = Path(__file__).resolve().parent.parent
BUNDLE_DIR = ROOT / "data" / "replay"
JOURNAL_DIR = ROOT / "data" / "raw"
SEMANTICS_ROOT = ROOT / "data" / "semantics"
RESULTS_PATH = ROOT / "replay_comparison.json"

MIN_BASKET_MEMBERS = 2


# ---------------------------------------------------------------------------
# Live session
# ---------------------------------------------------------------------------


@dataclass
class SessionCapture:
    """Observations, and the live decisions they produced as they arrived.

    Recording and processing are one step on purpose. The session's knowledge
    is the production :class:`LiveSession`'s, which is fed one observation at a
    time, so at any moment it contains exactly what the live system had.
    Nothing here can see forward.
    """

    session: LiveSession
    observations: list[Observation] = field(default_factory=list)
    live_book_states: dict[int, dict[str, str]] = field(default_factory=dict)
    """Ordinal -> {ticker: book state digest} as the LIVE reconstructor saw it."""

    @classmethod
    def for_plan(cls, plan: DetectorPlan, journal: RawFrameJournal) -> SessionCapture:
        knowledge = KnowledgeBase()
        return cls(
            session=LiveSession.over(
                plan,
                knowledge,
                KalshiReplayContext(knowledge=knowledge, plan=plan),
                reconstructor=OrderBookReconstructor(journal=journal),
            )
        )

    @property
    def plan(self) -> DetectorPlan:
        return self.session.plan

    @property
    def live_decisions(self) -> list[DecisionRecord]:
        """Validation oracle. Written beside the bundle, never inside it."""
        return self.session.decisions

    def record(
        self,
        kind: ObservationKind,
        payload: dict[str, Any],
        *,
        observed_at: datetime,
        epoch: int | None = None,
    ) -> int:
        """Record one observation and process it exactly as the live path does."""
        observation = Observation(
            ordinal=len(self.observations),
            observed_at=observed_at,
            kind=kind,
            payload=payload,
            source="live-capture",
            connection_epoch=epoch,
        )
        self.observations.append(observation)
        self.session.observe(observation)
        self.live_book_states[observation.ordinal] = book_state(self.session.reconstructor)
        return observation.ordinal

    @property
    def stream(self) -> ObservationStream:
        return ObservationStream.from_iterable(self.observations)


def book_state(reconstructor: OrderBookReconstructor) -> dict[str, str]:
    """A comparable digest of every book's economically relevant state.

    Integrity, provenance and the exact levels -- everything a detector would
    read. Deliberately not the whole object: raw journal ids legitimately differ
    between a capture and its replay, and comparing them would manufacture a
    mismatch that means nothing.
    """
    states: dict[str, str] = {}
    for ticker, view in reconstructor.books().items():
        yes = ";".join(f"{lv.price.to_str()}@{lv.quantity.to_str()}" for lv in view.yes_bids)
        no = ";".join(f"{lv.price.to_str()}@{lv.quantity.to_str()}" for lv in view.no_bids)
        states[ticker] = (
            f"{view.integrity.value}|epoch={view.provenance.connection_epoch}"
            f"|sid={view.provenance.sid}|seq={view.provenance.latest_seq}"
            f"|applied={view.provenance.applied_frames}|yes=[{yes}]|no=[{no}]"
        )
    return states


# ---------------------------------------------------------------------------
# Session-start context
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FetchedMarket:
    """One market as the venue sent it, and as we understood it.

    Both halves are kept. The normalised instrument is what the detector reads;
    the raw payload is what the venue actually said. Recording only the
    normalised view would make every recorded fact a statement about our own
    parser as much as about the venue, and a later normalisation fix could never
    be re-run against the original evidence.
    """

    instrument: VenueInstrument
    raw: dict[str, Any]
    fetched_at: datetime


async def fetch_markets(
    client: KalshiReadOnlyClient, tickers: Sequence[str], clock: SystemClock
) -> tuple[list[FetchedMarket], list[str]]:
    """Market metadata, plus the tickers that could not be resolved.

    Failures are returned rather than swallowed: a market we could not fetch is
    not in the plan, and saying so is different from silently monitoring fewer
    markets than requested.
    """
    fetched: list[FetchedMarket] = []
    failures: list[str] = []
    for ticker in tickers:
        try:
            market = await client.get_market(ticker)
            instrument = normalize_market(market)
        except Exception as exc:
            failures.append(f"{ticker}: {type(exc).__name__}: {exc}")
            continue
        fetched.append(
            FetchedMarket(
                instrument=instrument,
                raw=market.model_dump(mode="json"),
                fetched_at=clock.now(),
            )
        )
    return fetched, failures


async def expand_basket_events(
    client: KalshiReadOnlyClient, event_tickers: Sequence[str]
) -> tuple[list[str], list[str]]:
    """Every market the venue lists under these events.

    A-46: this listing is not proven exhaustive, and a relation certificate is
    only ever issued over the exact member set a reviewer saw. So this decides
    what the session *monitors*, and nothing about what may be claimed.
    A-47: members arrive nested under ``event.markets``, not beside it.
    """
    tickers: list[str] = []
    failures: list[str] = []
    for event_ticker in event_tickers:
        try:
            envelope = await client.get_event(event_ticker, with_nested_markets=True)
        except Exception as exc:
            failures.append(f"event {event_ticker}: {type(exc).__name__}: {exc}")
            continue
        tickers.extend(market.ticker for market in envelope.member_markets)
    return tickers, failures


def build_plan(
    fetched: Sequence[FetchedMarket],
    *,
    quantities: tuple[Quantity, ...],
    refresh_policy: ContextRefreshPolicy,
    balance_precision: str,
    notes: str,
) -> DetectorPlan:
    """What this session monitors. Recorded, so replay cannot invent its own.

    Baskets are formed only where the session is watching two or more markets of
    the same event. A one-member basket is not an AT_MOST_ONE question.
    """
    by_event: dict[str, list[str]] = {}
    for entry in fetched:
        by_event.setdefault(entry.instrument.event_ticker, []).append(entry.instrument.ticker)
    baskets = tuple(
        BasketPlan(event_ticker=event, members=tuple(members))
        for event, members in sorted(by_event.items())
        if event and len(members) >= MIN_BASKET_MEMBERS
    )
    return DetectorPlan(
        binary_complement_markets=tuple(entry.instrument.ticker for entry in fetched),
        baskets=baskets,
        quantities=quantities,
        refresh_policy=refresh_policy,
        balance_precision=balance_precision,
        notes=notes,
    )


def metadata_payload(entry: FetchedMarket) -> dict[str, Any]:
    """Public market metadata only. No account, order or credential material.

    Carries the normalised fields the resolver reads *and* the raw response they
    were derived from, with the normaliser named, so a recorded fact can always
    be traced back to what the venue said.
    """
    instrument = entry.instrument
    return {
        "source": f"GET /markets/{instrument.ticker}",
        "normalizer": "predarb.venues.kalshi.normalize.normalize_market",
        "raw": entry.raw,
        "ticker": instrument.ticker,
        "event_ticker": instrument.event_ticker,
        "title": instrument.title,
        "status": instrument.status_raw,
        "notional": (
            instrument.notional_value.to_str() if instrument.notional_value is not None else None
        ),
        "market_type": instrument.market_type_raw,
        "settlement_kind": instrument.settlement_kind.value,
        "rules_hash": instrument.rules_hash,
        "yes_sub_title": instrument.yes_sub_title,
        "no_sub_title": instrument.no_sub_title,
        "can_close_early": instrument.can_close_early,
        "exclusion_reasons": list(instrument.exclusion_reasons),
    }


async def record_market_context(
    capture: SessionCapture, fetched: Sequence[FetchedMarket], failures: Sequence[str]
) -> None:
    """Metadata per market, then one explicit "we queried /markets" snapshot."""
    for entry in fetched:
        capture.record(
            ObservationKind.MARKET_METADATA,
            metadata_payload(entry),
            observed_at=entry.fetched_at,
        )
    capture.record(
        ObservationKind.METADATA_SNAPSHOT,
        {
            "subjects": [entry.instrument.ticker for entry in fetched],
            "source": "GET /markets/{ticker}",
            "resolved": [entry.instrument.ticker for entry in fetched],
            "unresolved": list(failures),
        },
        observed_at=fetched[-1].fetched_at if fetched else datetime.now(tz=UTC),
    )


async def record_fee_context(
    capture: SessionCapture,
    client: KalshiReadOnlyClient,
    fetched: Sequence[FetchedMarket],
    clock: SystemClock,
) -> None:
    """The fee configuration in force per market, plus what was consulted.

    Resolution goes through the production :class:`FeeTimeline`, so series base,
    event override and scheduled changes keep their real precedence. The
    *resolved* answer is what the session held; the scheduled changes it knew
    about are recorded in the snapshot as knowledge, not as applicable records,
    because applying them here would duplicate precedence logic that already
    exists in exactly one place.

    Context is resolved once, at session start. The plan's refresh policy is
    what declares when that stops counting as current -- a session outliving it
    reports INCOMPLETE_STALE_CONTEXT rather than quietly reusing old fees.
    """
    events = sorted({entry.instrument.event_ticker for entry in fetched if entry.instrument})
    consulted: list[str] = []
    errors: list[str] = []
    scheduled: list[dict[str, Any]] = []
    resolved_for: list[str] = []
    raw_inputs: list[dict[str, Any]] = []

    for event_ticker in events:
        members = [e for e in fetched if e.instrument.event_ticker == event_ticker]
        try:
            envelope = await client.get_event(event_ticker)
        except Exception as exc:
            errors.append(f"event {event_ticker}: {type(exc).__name__}: {exc}")
            continue
        event = envelope.event
        consulted.append(f"GET /events/{event_ticker}")

        series_ticker = event.series_ticker
        try:
            series = await client.get_series(series_ticker)
        except Exception as exc:
            errors.append(f"series {series_ticker}: {type(exc).__name__}: {exc}")
            continue
        consulted.append(f"GET /series/{series_ticker}")

        try:
            series_changes = (
                await client.get_series_fee_changes(
                    series_ticker=series_ticker, show_historical=True
                )
            ).series_fee_change_arr
            consulted.append(f"GET /series/fee_changes?series_ticker={series_ticker}")
        except Exception as exc:
            errors.append(f"series fee changes {series_ticker}: {type(exc).__name__}: {exc}")
            series_changes = ()
        try:
            event_changes = (
                await client.get_event_fee_changes(event_ticker=event_ticker)
            ).event_fee_changes
            consulted.append(f"GET /events/fee_changes?event_ticker={event_ticker}")
        except Exception as exc:
            errors.append(f"event fee changes {event_ticker}: {type(exc).__name__}: {exc}")
            event_changes = ()

        raw_inputs.append(
            {
                "event_ticker": event_ticker,
                "series_ticker": series_ticker,
                "series": series.model_dump(mode="json"),
                "event": event.model_dump(mode="json"),
                "series_changes": [c.model_dump(mode="json") for c in series_changes],
                "event_changes": [c.model_dump(mode="json") for c in event_changes],
            }
        )
        timeline = build_fee_timeline(
            series=series,
            event=event,
            series_changes=tuple(series_changes),
            event_changes=tuple(event_changes),
        )
        at = clock.now()
        resolved = timeline.resolve_at(at)
        scheduled.extend(
            {
                "scope": change.scope.value,
                "scope_ticker": change.scope_ticker,
                "scheduled_ts": change.scheduled_ts.isoformat(),
                "fee_type": change.fee_type_raw,
                "multiplier": str(change.multiplier) if change.multiplier is not None else None,
            }
            for change in (*timeline.series_changes, *timeline.event_changes)
            if change.scheduled_ts > at
        )
        if resolved is None:
            # Known absent: the source was consulted and publishes no usable fee
            # configuration for this scope. The snapshot below records that.
            continue
        for member in members:
            capture.record(
                ObservationKind.FEE_OBSERVATION,
                {
                    # The market this record answers for ...
                    "subject_ticker": member.instrument.ticker,
                    # ... and where the configuration actually came from.
                    "scope": resolved.configuration.scope.value,
                    "scope_ticker": resolved.configuration.scope_ticker,
                    "fee_type": resolved.configuration.fee_type_raw,
                    "multiplier": str(resolved.configuration.multiplier),
                    "effective_from": (
                        resolved.effective_from.isoformat()
                        if resolved.effective_from is not None
                        else None
                    ),
                    "provenance": resolved.provenance,
                    "affects_markets": [member.instrument.ticker],
                },
                observed_at=at,
            )
            resolved_for.append(member.instrument.ticker)

    capture.record(
        ObservationKind.FEE_KNOWLEDGE_SNAPSHOT,
        {
            "subjects": [entry.instrument.ticker for entry in fetched],
            "resolved": sorted(resolved_for),
            "unresolved": sorted(
                {entry.instrument.ticker for entry in fetched} - set(resolved_for)
            ),
            "sources": consulted,
            "resolver": "predarb.venues.kalshi.normalize.build_fee_timeline",
            # The raw inputs the resolved configurations were derived from, so a
            # later change to precedence handling can be re-checked against what
            # the venue actually returned rather than against our reading of it.
            "raw_inputs": raw_inputs,
            "scheduled_changes_known_not_yet_effective": scheduled,
            "errors": errors,
        },
        observed_at=clock.now(),
    )


def certificate_payload(record: Any) -> dict[str, Any]:
    """A stored settlement certificate, flattened to what replay resolves from."""
    certificate = record.certificate
    return {
        "market_ticker": certificate.market_ticker,
        "certificate_id": record.certificate_id,
        "claim": record.claim.value,
        "notional": certificate.notional.to_str(),
        "rules_hash": certificate.rules_hash,
        "evidence_digest": certificate.evidence_fingerprint.digest,
        "fingerprint": {
            "schema_version": certificate.evidence_fingerprint.schema_version,
            "components": dict(certificate.evidence_fingerprint.components),
            "digest": certificate.evidence_fingerprint.digest,
        },
        "verified_by": certificate.verified_by,
        "verification_method": certificate.verification_method,
        "verified_at": certificate.verified_at.isoformat(),
        "valid_from": certificate.valid_from.isoformat(),
        "status": certificate.status.value,
        "evidence": certificate.evidence,
        "affects_markets": [certificate.market_ticker],
    }


def relation_payload(record: Any) -> dict[str, Any]:
    certificate = record.certificate
    return {
        "event_ticker": certificate.event_ticker,
        "certificate_id": certificate.certificate_id,
        "selected_members": list(certificate.selected_members),
        "snapshot_id": certificate.snapshot_id,
        "member_settlement_fingerprints": dict(certificate.member_settlement_fingerprints),
        "evidence_digest": certificate.evidence_fingerprint.digest,
        "fingerprint": {
            "schema_version": certificate.evidence_fingerprint.schema_version,
            "components": dict(certificate.evidence_fingerprint.components),
            "digest": certificate.evidence_fingerprint.digest,
        },
        "status": certificate.status.value,
        "reviewer": certificate.reviewer,
        "reviewed_at": certificate.reviewed_at.isoformat(),
        "issued_at": certificate.issued_at.isoformat(),
        "valid_from": certificate.valid_from.isoformat(),
        "policy_schema_version": certificate.policy_schema_version,
        "evidence": certificate.evidence,
        "affects_markets": list(certificate.selected_members),
    }


def record_settlement_registry(
    capture: SessionCapture, registry: CertificateRegistry, tickers: Sequence[str], at: datetime
) -> list[str]:
    """Snapshot the local settlement-certificate registry. Empty is a result.

    An empty list here is not an omission -- it is the statement "we looked, and
    there is no certificate for these markets". Without it, replay could not
    tell that apart from a registry nobody consulted, and would have to refuse
    to conclude anything.
    """
    certified: list[str] = []
    records = [r for t in tickers for r in registry.list_certificates(market_ticker=t)]
    capture.record(
        ObservationKind.SETTLEMENT_REGISTRY_SNAPSHOT,
        {
            "subjects": list(tickers),
            "source": f"local settlement certificate registry at {registry.root}",
            "certificates": [
                {
                    "market_ticker": r.market_ticker,
                    "certificate_id": r.certificate_id,
                    "claim": r.claim.value,
                    "issued_at": r.issued_at.isoformat(),
                }
                for r in records
            ],
            "affects_markets": list(tickers),
        },
        observed_at=at,
    )
    for record in records:
        capture.record(
            ObservationKind.SETTLEMENT_CERTIFICATE,
            certificate_payload(record),
            observed_at=at,
        )
        certified.append(record.market_ticker)
    return sorted(set(certified))


def record_relation_registry(
    capture: SessionCapture,
    registry: RelationRegistry,
    baskets: Sequence[BasketPlan],
    at: datetime,
) -> list[tuple[str, tuple[str, ...]]]:
    """Snapshot the local relation registry, per event. Empty is a result."""
    if not baskets:
        return []
    events = [basket.event_ticker for basket in baskets]
    members = sorted({m for basket in baskets for m in basket.members})
    records = [r for e in events for r in registry.list_certificates(event_ticker=e)]
    capture.record(
        ObservationKind.RELATION_REGISTRY_SNAPSHOT,
        {
            "subjects": events,
            "source": f"local relation certificate registry at {registry.root}",
            "certificates": [
                {
                    "event_ticker": r.event_ticker,
                    "certificate_id": r.certificate_id,
                    "selected_members": list(r.selected_members),
                    "issued_at": r.certificate.issued_at.isoformat(),
                }
                for r in records
            ],
            "affects_markets": members,
        },
        observed_at=at,
    )
    for record in records:
        capture.record(
            ObservationKind.RELATION_CERTIFICATE, relation_payload(record), observed_at=at
        )
    # The exact member set each certificate covers -- a relation is a claim
    # about that set and no other, so evidence must be captured over the same.
    return sorted({(r.event_ticker, tuple(r.selected_members)) for r in records})


async def record_evidence_for_certified(
    capture: SessionCapture,
    client: KalshiReadOnlyClient,
    certified_markets: Sequence[str],
    certified_relations: Sequence[tuple[str, tuple[str, ...]]],
    clock: SystemClock,
) -> list[str]:
    """Current evidence for anything a certificate claims to describe.

    Only where a certificate exists, because that is the only place the answer
    changes anything: a certificate is usable exactly while current evidence
    still matches the evidence it was reviewed against. Skipping this would not
    be neutral -- the resolver has no fallback and would report evidence as
    unavailable, blocking the certificate. That is the correct failure, and
    fetching the evidence is how it is avoided honestly.

    Only the fingerprint is recorded. Contract documents belong to the exchange
    and are not copied into a bundle.
    """
    errors: list[str] = []
    for ticker in certified_markets:
        try:
            bundle = await capture_settlement_evidence(client, ticker, at=clock.now())
        except Exception as exc:
            errors.append(f"settlement evidence {ticker}: {type(exc).__name__}: {exc}")
            continue
        fingerprint = bundle.fingerprint()
        capture.record(
            ObservationKind.SETTLEMENT_EVIDENCE,
            {
                "market_ticker": ticker,
                "snapshot_id": bundle.snapshot_id,
                "digest": fingerprint.digest,
                "fingerprint": {
                    "schema_version": fingerprint.schema_version,
                    "components": dict(fingerprint.components),
                    "digest": fingerprint.digest,
                },
                "capture_errors": list(bundle.capture_errors),
                "affects_markets": [ticker],
            },
            observed_at=bundle.captured_at,
        )
    for event_ticker, selected_members in certified_relations:
        try:
            relation_bundle = await capture_relation_evidence(
                client,
                event_ticker=event_ticker,
                selected_members=list(selected_members),
                at=clock.now(),
            )
        except Exception as exc:
            errors.append(f"relation evidence {event_ticker}: {type(exc).__name__}: {exc}")
            continue
        relation_fingerprint = relation_bundle.fingerprint()
        capture.record(
            ObservationKind.RELATION_EVIDENCE,
            {
                "event_ticker": event_ticker,
                "snapshot_id": relation_bundle.snapshot_id,
                "digest": relation_fingerprint.digest,
                "fingerprint": {
                    "schema_version": relation_fingerprint.schema_version,
                    "components": dict(relation_fingerprint.components),
                    "digest": relation_fingerprint.digest,
                },
                "affects_markets": list(relation_bundle.selected_members),
            },
            observed_at=relation_bundle.captured_at,
        )
    return errors


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


async def select_tickers(
    client: KalshiReadOnlyClient, args: argparse.Namespace
) -> tuple[list[str], list[str]]:
    selected: list[str] = list(args.market or [])
    failures: list[str] = []
    if args.basket_event:
        expanded, event_failures = await expand_basket_events(client, args.basket_event)
        selected.extend(expanded)
        failures.extend(event_failures)
    if not selected:
        candidates, _ = await discover_active_markets(
            client, count=args.markets, series_limit=args.series, now=datetime.now(tz=UTC)
        )
        selected.extend(c.ticker for c in candidates)
    # Preserve first-seen order; an explicit --market should stay first.
    seen: dict[str, None] = {}
    for ticker in selected:
        seen.setdefault(ticker, None)
    return list(seen), failures


async def capture_session(settings: Settings, args: argparse.Namespace) -> SessionCapture:
    """Run one read-only connection, recording every input consumed."""
    clock = SystemClock()

    async with KalshiReadOnlyClient.public(env=settings.kalshi_env) as client:
        tickers, selection_failures = await select_tickers(client, args)
        print(f"Markets: {tickers}")
        fetched, metadata_failures = await fetch_markets(client, tickers, clock)
        if not fetched:
            raise SystemExit("no market metadata could be fetched; nothing to capture")

        plan = build_plan(
            fetched,
            quantities=tuple(Quantity.from_value(q) for q in args.quantity),
            refresh_policy=ContextRefreshPolicy(),
            balance_precision=args.precision,
            notes="prospective read-only capture for Step 10 replay verification",
        )
        print(f"Plan: {plan.describe()}")
        for basket in plan.baskets:
            print(f"  basket {basket.event_ticker}: {list(basket.members)}")

        journal = RawFrameJournal(
            default_journal_path(JOURNAL_DIR, now=datetime.now(tz=UTC)), connection_epoch=1
        )
        capture = SessionCapture.for_plan(plan, journal)

        capture.record(
            ObservationKind.DETECTOR_PLAN,
            {
                **plan.to_payload(),
                "selection_failures": selection_failures,
                "metadata_failures": metadata_failures,
            },
            observed_at=clock.now(),
        )
        await record_market_context(capture, fetched, metadata_failures)
        await record_fee_context(capture, client, fetched, clock)

        settlement_registry = CertificateRegistry(args.semantics_root)
        relation_registry = RelationRegistry(args.semantics_root)
        now = clock.now()
        certified_markets = record_settlement_registry(
            capture, settlement_registry, [e.instrument.ticker for e in fetched], now
        )
        certified_relations = record_relation_registry(
            capture, relation_registry, plan.baskets, clock.now()
        )
        evidence_errors = await record_evidence_for_certified(
            capture, client, certified_markets, certified_relations, clock
        )
        for problem in evidence_errors:
            print(f"  !! {problem}")

        print(
            f"  context: {len(capture.observations)} observations, "
            f"{len(certified_markets)} certified market(s), "
            f"{len(certified_relations)} certified relation(s)"
        )

    tightest = min(
        plan.refresh_policy.market_metadata,
        plan.refresh_policy.fee_knowledge,
        plan.refresh_policy.settlement_knowledge,
        plan.refresh_policy.relation_knowledge,
    )
    if timedelta(seconds=args.seconds) > tightest:
        print(
            f"  !! session length {args.seconds:.0f}s exceeds the tightest refresh "
            f"window ({tightest}); later decisions will report stale context"
        )

    client_ws = websocket_client(settings, clock)
    tickers = list(plan.monitored_markets)
    idle_polls = 0

    print(f"\nCapturing for {args.seconds:.0f}s ...")
    try:
        await client_ws.connect()
        capture.record(
            ObservationKind.CONNECTION_OPENED, {"epoch": 1}, observed_at=clock.now(), epoch=1
        )

        await client_ws.subscribe_orderbook(tickers)
        deadline = asyncio.get_running_loop().time() + args.seconds

        while asyncio.get_running_loop().time() < deadline:
            remaining = deadline - asyncio.get_running_loop().time()
            try:
                frame = await client_ws.receive(timeout=min(5.0, max(0.1, remaining)))
            except WebSocketIdle:
                # A quiet market, not a broken socket. The live collector pings
                # and continues here, so a capture that recorded a failure
                # instead would bake a connection lifetime into the bundle that
                # the live path never had -- and the replay would faithfully
                # reproduce the wrong history.
                idle_polls += 1
                with contextlib.suppress(Exception):
                    await client_ws.ping(timeout=10.0)
                continue
            except TimeoutError:
                continue
            except Exception as exc:
                print(f"  connection error: {type(exc).__name__}")
                capture.record(
                    ObservationKind.CONNECTION_FAILED,
                    {"reason": type(exc).__name__},
                    observed_at=clock.now(),
                    epoch=1,
                )
                break

            envelope = frame.envelope
            if envelope is not None and envelope.type == "subscribed":
                sid = (envelope.msg or {}).get("sid")
                if sid is not None:
                    capture.record(
                        ObservationKind.SUBSCRIBED,
                        {
                            "sid": int(sid),
                            "channel": (envelope.msg or {}).get("channel", "orderbook_delta"),
                            "markets": tickers,
                        },
                        observed_at=frame.received_at,
                        epoch=1,
                    )
                    continue

            ticker_hint = None
            if envelope is not None and envelope.msg:
                ticker_hint = envelope.msg.get("market_ticker")
            capture.record(
                ObservationKind.FRAME_RECEIVED,
                {"raw": frame.raw, "market_ticker": ticker_hint},
                observed_at=frame.received_at,
                epoch=1,
            )
    finally:
        with contextlib.suppress(Exception):
            await client_ws.close()
        capture.record(
            ObservationKind.CONNECTION_CLOSED,
            {"reason": "capture complete"},
            observed_at=clock.now(),
            epoch=1,
        )
        journal.close()
        print(
            f"  {len(capture.observations)} observations, {idle_polls} idle polls, "
            f"{len(capture.live_decisions)} live decisions"
        )

    return capture


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


class NetworkBlockedError(AssertionError):
    """Replay attempted a network call. That is a failure, not a fallback."""


@contextlib.contextmanager
def no_network() -> Any:
    """Make every socket connection raise, for the duration of a replay.

    A replay that can reach the API can silently repair a gap in its own
    evidence, and the run would look complete because it *was* completed -- with
    facts nobody held at the time. Blocking the socket turns that from a
    reasoning error into a crash.
    """
    original = socket.socket.connect
    original_ex = socket.socket.connect_ex
    original_create = socket.create_connection

    def blocked(*_args: Any, **_kwargs: Any) -> Any:
        raise NetworkBlockedError("replay attempted a network connection")

    socket.socket.connect = blocked  # type: ignore[method-assign]
    socket.socket.connect_ex = blocked  # type: ignore[method-assign]
    socket.create_connection = blocked
    try:
        yield
    finally:
        socket.socket.connect = original  # type: ignore[method-assign]
        socket.socket.connect_ex = original_ex  # type: ignore[method-assign]
        socket.create_connection = original_create


def replay_books(bundle_root: Path, capture: SessionCapture) -> dict[str, Any]:
    """Replay transport only, through an independent reconstructor."""
    bundle = load_bundle(bundle_root)
    reconstructor = OrderBookReconstructor(journal=ReplayFrameJournal())  # type: ignore[arg-type]

    mismatches: list[dict[str, Any]] = []
    compared = 0
    for observation in bundle.stream:
        kind, payload = observation.kind, observation.payload
        if kind is ObservationKind.CONNECTION_OPENED:
            reconstructor.open_connection(observation.observed_at)
        elif kind is ObservationKind.SUBSCRIBED:
            reconstructor.note_subscribed(
                sid=int(payload["sid"]),
                channel=str(payload.get("channel", "orderbook_delta")),
                markets=set(payload.get("markets", [])),
                at=observation.observed_at,
            )
        elif kind is ObservationKind.FRAME_RECEIVED:
            reconstructor.handle_frame(str(payload["raw"]), observation.observed_at)
        elif kind in {ObservationKind.CONNECTION_CLOSED, ObservationKind.CONNECTION_FAILED}:
            reconstructor.close_connection(
                observation.observed_at, reason=str(payload.get("reason", "closed"))
            )
        else:
            continue

        expected = capture.live_book_states.get(observation.ordinal)
        if expected is None:
            continue
        compared += 1
        actual = book_state(reconstructor)
        if actual != expected:
            differing = sorted(
                t for t in set(expected) | set(actual) if expected.get(t) != actual.get(t)
            )
            mismatches.append(
                {
                    "ordinal": observation.ordinal,
                    "kind": kind.value,
                    "markets": differing,
                    "live": {t: expected.get(t) for t in differing},
                    "replay": {t: actual.get(t) for t in differing},
                }
            )

    final = book_state(reconstructor)
    return {
        "states_compared": compared,
        "book_state_mismatches": len(mismatches),
        "book_mismatch_detail": mismatches[:5],
        "final_books": len(final),
        "final_valid_books": sum(
            1 for state in final.values() if state.startswith(BookIntegrity.VALID.value)
        ),
    }


def replay_decisions(bundle_root: Path, capture: SessionCapture) -> dict[str, Any]:
    """Replay economics offline and compare decision for decision.

    The replay's knowledge base is built from the whole stream at once, so it
    *holds* every later observation. Matching the live run -- which could not --
    is what makes this a no-lookahead result rather than a restatement.
    """
    bundle = load_bundle(bundle_root)
    plan = DetectorPlan.from_payload(bundle.manifest.detector_plan)
    engine = ReplayEngine(
        plan=plan,
        provider=KalshiReplayContext(knowledge=KnowledgeBase.from_stream(bundle.stream), plan=plan),
    )
    result = engine.run(bundle)
    mismatches = compare_decisions(capture.live_decisions, result.decisions)
    return {
        "bundle_id": bundle.manifest.bundle_id,
        "integrity": bundle.integrity.integrity.value,
        "observations": len(bundle.stream),
        "supports_economic_replay": bundle.manifest.supports_economic_replay,
        "live_decisions": len(capture.live_decisions),
        "replay_decisions": len(result.decisions),
        "detectors_run": result.detector_run_count,
        "decision_mismatches": len(mismatches),
        "decision_mismatch_detail": list(mismatches[:10]),
        "replay_digest": result.digest,
        "classification_counts": dict(result.classification_counts),
        "completeness": result.completeness.as_dict(),
        "warnings": list(result.warnings),
    }


def oracle_payload(capture: SessionCapture) -> dict[str, Any]:
    return {
        "note": (
            "Validation oracle: DecisionRecords derived live. Derived output, not "
            "source evidence -- deliberately kept outside the bundle."
        ),
        "plan": capture.plan.to_payload(),
        "decisions": [decision.comparable() for decision in capture.live_decisions],
    }


# ---------------------------------------------------------------------------


async def main() -> None:
    parser = argparse.ArgumentParser(description="Capture a replay bundle and verify it")
    parser.add_argument("--market", action="append", metavar="TICKER")
    parser.add_argument(
        "--basket-event",
        action="append",
        metavar="EVENT_TICKER",
        help="Monitor every listed market of this event as an AT_MOST_ONE basket.",
    )
    parser.add_argument("--markets", type=int, default=4)
    parser.add_argument("--series", type=int, default=250)
    parser.add_argument("--seconds", type=float, default=90.0)
    parser.add_argument("--quantity", action="append", default=None, metavar="QTY")
    parser.add_argument(
        "--precision",
        choices=("direct", "non-direct", "unknown-conservative"),
        default="unknown-conservative",
    )
    parser.add_argument("--semantics-root", type=Path, default=SEMANTICS_ROOT)
    parser.add_argument("--bundle", type=Path, default=None)
    args = parser.parse_args()
    args.quantity = args.quantity or ["1.00"]

    settings = Settings()
    capture = await capture_session(settings, args)

    stream = capture.stream
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    bundle_root = args.bundle or (BUNDLE_DIR / f"session-{stamp}")
    oracle_path = bundle_root.parent / f"{bundle_root.name}.oracle.json"

    manifest = write_bundle(
        bundle_root,
        stream,
        markets=capture.plan.monitored_markets,
        detector_plan=capture.plan.to_payload(),
        notes="prospective read-only capture for Step 10 replay verification",
    )
    bundle_root.parent.mkdir(parents=True, exist_ok=True)
    oracle_path.write_text(json.dumps(oracle_payload(capture), indent=2, default=str) + "\n")

    print(f"\nWrote bundle {bundle_root}")
    print(f"  {manifest.describe()}")
    for kind, count in manifest.observation_counts_by_kind.items():
        print(f"      {count:>6}  {kind}")
    print(f"  oracle (NOT bundle input): {oracle_path}")

    print("\nReplaying offline, with the network blocked ...")
    with no_network():
        comparison = {
            **replay_books(bundle_root, capture),
            **replay_decisions(bundle_root, capture),
        }
    RESULTS_PATH.write_text(json.dumps(comparison, indent=2, default=str) + "\n")

    print("\n=== LIVE vs REPLAY ===")
    for key, value in comparison.items():
        if key.endswith("_detail") or key == "completeness":
            continue
        print(f"  {key}: {value}")
    if comparison["book_state_mismatches"]:
        print("\n  !! book-state mismatches -- investigate, do not normalise:")
        for entry in comparison["book_mismatch_detail"]:
            print(f"     #{entry['ordinal']} {entry['kind']} {entry['markets']}")
    if comparison["decision_mismatches"]:
        print("\n  !! decision mismatches -- investigate, do not normalise:")
        for entry in comparison["decision_mismatch_detail"]:
            print(f"     {entry}")
    if not comparison["book_state_mismatches"] and not comparison["decision_mismatches"]:
        print(
            f"\n  0 mismatches: {comparison['states_compared']} book states and "
            f"{comparison['live_decisions']} decisions agreed."
        )
    print(f"\nWrote {RESULTS_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
