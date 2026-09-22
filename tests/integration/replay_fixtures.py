"""Synthetic replay bundles driven through the real engine and resolver.

No custom test resolvers: these build observation streams in the shapes the
Kalshi context resolver actually reads, so the tests exercise the production
path rather than a parallel one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from predarb.domain.money import Quantity
from predarb.replay.engine import ReplayEngine, ReplayResult
from predarb.replay.knowledge import KnowledgeBase
from predarb.replay.live import LiveSession
from predarb.replay.observation import Observation, ObservationKind, ObservationStream
from predarb.replay.plan import BasketPlan, ContextRefreshPolicy, DetectorPlan
from predarb.semantics.fingerprint import SettlementEvidenceFingerprint
from predarb.venues.kalshi.replay_context import KalshiReplayContext

T0 = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
EVENT = "EVT"
HASH = "h" * 64
GENEROUS = ContextRefreshPolicy(
    market_metadata=timedelta(days=1),
    fee_knowledge=timedelta(days=1),
    settlement_knowledge=timedelta(days=1),
    relation_knowledge=timedelta(days=1),
)


def evidence_digest(marker: str) -> str:
    """The digest the resolver derives from a recorded evidence marker."""
    return SettlementEvidenceFingerprint.over({"recorded_digest": marker}).digest


class StreamBuilder:
    """Appends observations in capture order, assigning ordinals."""

    def __init__(self, start: datetime = T0) -> None:
        self._records: list[Observation] = []
        self._start = start

    def add(
        self,
        kind: ObservationKind,
        payload: dict[str, Any],
        *,
        offset: int,
        epoch: int | None = None,
    ) -> StreamBuilder:
        self._records.append(
            Observation(
                ordinal=len(self._records),
                observed_at=self._start + timedelta(seconds=offset),
                kind=kind,
                payload=payload,
                source="synthetic",
                connection_epoch=epoch,
            )
        )
        return self

    def build(self) -> ObservationStream:
        return ObservationStream.from_iterable(self._records)


def snapshot_frame(ticker: str, seq: int, yes_bid: str, no_bid: str) -> str:
    return (
        f'{{"type": "orderbook_snapshot", "sid": 1, "seq": {seq}, "msg": {{'
        f'"market_ticker": "{ticker}", '
        f'"yes_dollars_fp": [["{yes_bid}", "50.00"]], '
        f'"no_dollars_fp": [["{no_bid}", "50.00"]]}}}}'
    )


def metadata(ticker: str) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "event_ticker": EVENT,
        "notional": "1.0000",
        "market_type": "binary",
        "settlement_kind": "BINARY",
        "rules_hash": HASH,
        "status": "active",
    }


def fee(ticker: str, *, multiplier: str = "1", **extra: Any) -> dict[str, Any]:
    return {
        "scope_ticker": ticker,
        "multiplier": multiplier,
        "fee_type": "quadratic",
        "scope": "SERIES",
        "provenance": f"recorded series base for {ticker}",
        "affects_markets": [ticker],
        **extra,
    }


def certificate(ticker: str, marker: str = "v1") -> dict[str, Any]:
    return {
        "market_ticker": ticker,
        "certificate_id": f"cert-{ticker}",
        "notional": "1.0000",
        "rules_hash": HASH,
        "evidence_digest": marker,
        "verified_by": "synthetic reviewer",
        "verification_method": "synthetic fixture",
        "status": "VERIFIED",
        "evidence": "synthetic fixture evidence",
        "affects_markets": [ticker],
    }


def build_engine(plan: DetectorPlan, stream: ObservationStream) -> ReplayEngine:
    """The production engine with the production Kalshi resolver."""
    return ReplayEngine(
        plan=plan,
        provider=KalshiReplayContext(knowledge=KnowledgeBase.from_stream(stream), plan=plan),
    )


def run(plan: DetectorPlan, stream: ObservationStream) -> ReplayResult:
    return build_engine(plan, stream).run_stream(stream)


def run_live(plan: DetectorPlan, stream: ObservationStream) -> LiveSession:
    """Process the stream forward, exactly as the capture tool does.

    The knowledge base starts empty and grows one observation at a time, so the
    session cannot consult anything it has not yet been handed. This is the
    control the replay is measured against.
    """
    knowledge = KnowledgeBase()
    session = LiveSession.over(plan, knowledge, KalshiReplayContext(knowledge=knowledge, plan=plan))
    for observation in stream:
        session.observe(observation)
    return session


# ---------------------------------------------------------------------------
# Binary complement: no certificate -> certified -> proven -> fee change -> drift
# ---------------------------------------------------------------------------

BINARY_TICKER = "MKT"
BINARY_PLAN = DetectorPlan(
    binary_complement_markets=(BINARY_TICKER,),
    quantities=(Quantity.from_value("1.00"),),
    refresh_policy=GENEROUS,
    balance_precision="direct",
)


def binary_stream() -> ObservationStream:
    b = StreamBuilder()
    b.add(ObservationKind.CONNECTION_OPENED, {"epoch": 1}, offset=0, epoch=1)
    b.add(ObservationKind.MARKET_METADATA, metadata(BINARY_TICKER), offset=1)
    b.add(
        ObservationKind.METADATA_SNAPSHOT,
        {"subjects": [BINARY_TICKER], "source": "GET /markets"},
        offset=1,
    )
    b.add(ObservationKind.FEE_OBSERVATION, fee(BINARY_TICKER), offset=2)
    b.add(
        ObservationKind.FEE_KNOWLEDGE_SNAPSHOT,
        {"subjects": [BINARY_TICKER], "changes": [], "source": "GET /series/fee_changes"},
        offset=2,
    )
    b.add(
        ObservationKind.SETTLEMENT_EVIDENCE,
        {"market_ticker": BINARY_TICKER, "digest": "v1"},
        offset=3,
    )
    # T0: registry explicitly empty -- known absence, not a gap.
    b.add(
        ObservationKind.SETTLEMENT_REGISTRY_SNAPSHOT,
        {"subjects": [BINARY_TICKER], "certificates": [], "affects_markets": [BINARY_TICKER]},
        offset=3,
    )
    b.add(
        ObservationKind.SUBSCRIBED,
        {"sid": 1, "channel": "orderbook_delta", "markets": [BINARY_TICKER]},
        offset=4,
        epoch=1,
    )
    b.add(
        ObservationKind.FRAME_RECEIVED,
        {
            "raw": snapshot_frame(BINARY_TICKER, 1, "0.6000", "0.5000"),
            "market_ticker": BINARY_TICKER,
        },
        offset=5,
        epoch=1,
    )
    # T1: certificate issued.
    b.add(ObservationKind.SETTLEMENT_CERTIFICATE, certificate(BINARY_TICKER), offset=10)
    # T2: the book moves.
    b.add(
        ObservationKind.FRAME_RECEIVED,
        {
            "raw": snapshot_frame(BINARY_TICKER, 2, "0.7000", "0.5000"),
            "market_ticker": BINARY_TICKER,
        },
        offset=20,
        epoch=1,
    )
    # T3: a fee change becomes effective.
    b.add(
        ObservationKind.FEE_OBSERVATION,
        fee(
            BINARY_TICKER,
            multiplier="0.5",
            effective_from=(T0 + timedelta(seconds=25)).isoformat(),
            provenance="recorded scheduled change",
        ),
        offset=30,
    )
    # T4: evidence drift observed.
    b.add(
        ObservationKind.SETTLEMENT_EVIDENCE,
        {
            "market_ticker": BINARY_TICKER,
            "digest": "v2-drifted",
            "affects_markets": [BINARY_TICKER],
        },
        offset=40,
    )
    b.add(ObservationKind.CONNECTION_CLOSED, {"reason": "done"}, offset=50, epoch=1)
    return b.build()


# ---------------------------------------------------------------------------
# AT_MOST_ONE basket: empty relation registry -> relation -> members -> proven
# -> book change -> fee change -> member drift
# ---------------------------------------------------------------------------

BASKET_MEMBERS = ("A", "B", "C")
BASKET_PLAN = DetectorPlan(
    baskets=(BasketPlan(event_ticker=EVENT, members=BASKET_MEMBERS),),
    quantities=(Quantity.from_value("1.00"),),
    refresh_policy=GENEROUS,
    balance_precision="direct",
)


def relation_certificate(marker: str = "rel-v1") -> dict[str, Any]:
    return {
        "event_ticker": EVENT,
        "certificate_id": "relcert-1",
        "selected_members": list(BASKET_MEMBERS),
        "member_settlement_fingerprints": {t: evidence_digest("v1") for t in BASKET_MEMBERS},
        "evidence_digest": marker,
        "status": "VERIFIED",
        "reviewer": "synthetic reviewer",
        "evidence": "synthetic relation fixture",
        "affects_markets": list(BASKET_MEMBERS),
    }


def basket_stream() -> ObservationStream:
    b = StreamBuilder()
    b.add(ObservationKind.CONNECTION_OPENED, {"epoch": 1}, offset=0, epoch=1)
    for ticker in BASKET_MEMBERS:
        b.add(ObservationKind.MARKET_METADATA, metadata(ticker), offset=1)
        b.add(ObservationKind.FEE_OBSERVATION, fee(ticker), offset=1)
        b.add(
            ObservationKind.SETTLEMENT_EVIDENCE,
            {"market_ticker": ticker, "digest": "v1"},
            offset=1,
        )
    b.add(
        ObservationKind.METADATA_SNAPSHOT,
        {"subjects": list(BASKET_MEMBERS)},
        offset=2,
    )
    b.add(
        ObservationKind.FEE_KNOWLEDGE_SNAPSHOT,
        {"subjects": list(BASKET_MEMBERS), "changes": []},
        offset=2,
    )
    # T0: both registries explicitly empty.
    b.add(
        ObservationKind.SETTLEMENT_REGISTRY_SNAPSHOT,
        {"subjects": list(BASKET_MEMBERS), "certificates": []},
        offset=2,
    )
    b.add(
        ObservationKind.RELATION_REGISTRY_SNAPSHOT,
        {"subjects": [EVENT], "certificates": [], "affects_markets": list(BASKET_MEMBERS)},
        offset=2,
    )
    # Current relation evidence, captured in its own right. A relation
    # certificate is only usable against evidence we actually checked; without
    # this the resolver reports no current evidence and the detector blocks.
    b.add(
        ObservationKind.RELATION_EVIDENCE,
        {"event_ticker": EVENT, "digest": "rel-v1", "affects_markets": list(BASKET_MEMBERS)},
        offset=2,
    )
    b.add(
        ObservationKind.SUBSCRIBED,
        {"sid": 1, "channel": "orderbook_delta", "markets": list(BASKET_MEMBERS)},
        offset=3,
        epoch=1,
    )
    # A basket profits exactly while the members' YES bids sum above the
    # notional: cost is sum(N - yes_bid_i) against a worst case of (n-1) x N.
    # 0.40 x 3 = 1.20 > 1.00, so each NO leg costs 0.60 and three cost 1.80.
    for index, ticker in enumerate(BASKET_MEMBERS):
        b.add(
            ObservationKind.FRAME_RECEIVED,
            {"raw": snapshot_frame(ticker, index + 1, "0.4000", "0.5000"), "market_ticker": ticker},
            offset=4 + index,
            epoch=1,
        )
    # T1: relation certificate, members still uncertified.
    b.add(ObservationKind.RELATION_CERTIFICATE, relation_certificate(), offset=10)
    # T2: member certificates issued -> the basket detector can run.
    for ticker in BASKET_MEMBERS:
        b.add(ObservationKind.SETTLEMENT_CERTIFICATE, certificate(ticker), offset=15)
    b.add(
        ObservationKind.FRAME_RECEIVED,
        {"raw": snapshot_frame("A", 4, "0.4000", "0.5000"), "market_ticker": "A"},
        offset=16,
        epoch=1,
    )
    # T3: one member's book worsens -> economics update. seq stays dense within
    # the sid, as the venue guarantees (A-09).
    b.add(
        ObservationKind.FRAME_RECEIVED,
        # 0.10 + 0.40 + 0.40 = 0.90 < 1.00, so the basket stops being profitable.
        {"raw": snapshot_frame("A", 5, "0.1000", "0.8000"), "market_ticker": "A"},
        offset=20,
        epoch=1,
    )
    # T4: a member's evidence drifts -> relation goes stale.
    b.add(
        ObservationKind.SETTLEMENT_EVIDENCE,
        {"market_ticker": "B", "digest": "v2-drifted", "affects_markets": ["B"]},
        offset=30,
    )
    b.add(ObservationKind.CONNECTION_CLOSED, {"reason": "done"}, offset=40, epoch=1)
    return b.build()
