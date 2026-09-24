"""The Phase-1 collector: one read-only session, all three detectors, durable.

Run with::

    uv run python tools/collect.py --minutes 45 --markets 6

At session start it captures the full context set -- market metadata, resolved
fee configuration, and explicit snapshots of the local settlement and relation
registries including empty ones. Then it connects, journals every frame, drives
the production coordinator, and persists source evidence before derived
decisions.

Certification stays a hard gate. There is no ``--skip-certificates``,
``--assume-binary`` or ``--trust-mutually-exclusive``: with no certificate the
correct verdict is a determinate semantic block, and a flag that bypassed it
would produce candidates that look identical to proven ones.

Read-only public market data plus local research files. No order, balance,
position or fill endpoint is reachable from here.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib

# Reuse the Step 10 capture services rather than writing a second copy: the
# context a decision needs is the same whether it lands in a bundle or a
# catalogue, and two capturers would drift.
import importlib.util
import json
import os
import resource
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from predarb.clock import SystemClock
from predarb.config import Settings
from predarb.domain.money import Quantity
from predarb.ingest.book_collector import default_journal_path
from predarb.ingest.collector import DurableCollector, QueueOverflowError
from predarb.ingest.raw_journal import RawFrameJournal
from predarb.replay.knowledge import KnowledgeBase
from predarb.replay.observation import ObservationKind
from predarb.replay.plan import ContextRefreshPolicy, DetectorPlan
from predarb.semantics.registry import CertificateRegistry, RelationRegistry
from predarb.storage.catalogue import Catalogue, SourcePersistenceError
from predarb.storage.engine import DEFAULT_DATABASE_PATH, connect, create_catalogue_engine
from predarb.storage.migrations import migrate
from predarb.storage.schema import SCHEMA_VERSION
from predarb.storage.verification import verify_session_decisions
from predarb.venues.kalshi.client import KalshiReadOnlyClient
from predarb.venues.kalshi.replay_context import KalshiReplayContext
from predarb.venues.kalshi.session import websocket_client
from predarb.venues.kalshi.websocket import WebSocketIdle

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "capture_replay_bundle", ROOT / "tools" / "capture_replay_bundle.py"
)
assert _spec is not None and _spec.loader is not None
_capture = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _capture
_spec.loader.exec_module(_capture)

JOURNAL_DIR = ROOT / "data" / "raw"
SEMANTICS_ROOT = ROOT / "data" / "semantics"
RESULTS_PATH = ROOT / "soak_report.json"


def _kalshi_provider(knowledge: KnowledgeBase, plan: DetectorPlan) -> KalshiReplayContext:
    """The venue resolver, injected so ``storage`` stays a leaf layer."""
    return KalshiReplayContext(knowledge=knowledge, plan=plan)


class _CollectorCapture:
    """Adapts the Step 10 context capturers onto the durable collector.

    They expect a ``record(kind, payload, observed_at=..., epoch=...)`` sink.
    Here that sink persists source evidence and drives the coordinator.
    """

    def __init__(self, collector: DurableCollector) -> None:
        self.collector = collector
        self.session = collector.session
        self.observations: list[Any] = []

    @property
    def plan(self) -> DetectorPlan:
        return self.collector.plan

    def record(
        self,
        kind: ObservationKind,
        payload: dict[str, Any],
        *,
        observed_at: datetime,
        epoch: int | None = None,
    ) -> int:
        observation = self.collector.next_observation(
            kind, payload, observed_at=observed_at, epoch=epoch
        )
        self.collector.ingest(observation)
        self.observations.append(observation)
        return observation.ordinal


async def capture_context(
    capture: _CollectorCapture,
    client: KalshiReadOnlyClient,
    *,
    fetched: list[Any],
    failures: list[str],
    clock: SystemClock,
    semantics_root: Path,
) -> None:
    """Metadata, fees and both registry snapshots, before the socket opens."""
    capture.record(
        ObservationKind.DETECTOR_PLAN,
        {**capture.plan.to_payload(), "metadata_failures": failures},
        observed_at=clock.now(),
    )
    await _capture.record_market_context(capture, fetched, failures)
    await _capture.record_fee_context(capture, client, fetched, clock)

    settlement = CertificateRegistry(semantics_root)
    relations = RelationRegistry(semantics_root)
    certified_markets = _capture.record_settlement_registry(
        capture, settlement, [entry.instrument.ticker for entry in fetched], clock.now()
    )
    certified_relations = _capture.record_relation_registry(
        capture, relations, capture.plan.all_baskets, clock.now()
    )
    errors = await _capture.record_evidence_for_certified(
        capture, client, certified_markets, certified_relations, clock
    )
    for problem in errors:
        print(f"  !! {problem}")
    print(
        f"  context: {len(capture.observations)} observation(s), "
        f"{len(certified_markets)} certified market(s), "
        f"{len(certified_relations)} certified relation(s)"
    )


def resource_usage() -> dict[str, Any]:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    # macOS reports maxrss in bytes, Linux in kilobytes.
    divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
    return {
        "cpu_user_s": round(usage.ru_utime, 2),
        "cpu_system_s": round(usage.ru_stime, 2),
        "peak_rss_mb": round(usage.ru_maxrss / divisor, 1),
    }


def directory_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


async def run_soak(settings: Settings, args: argparse.Namespace) -> dict[str, Any]:
    clock = SystemClock()
    engine = create_catalogue_engine(args.database)
    migrate(engine)

    async with KalshiReadOnlyClient.public(env=settings.kalshi_env) as client:
        tickers, selection_failures = await _capture.select_tickers(client, args)
        print(f"Markets: {tickers}")
        fetched, metadata_failures = await _capture.fetch_markets(client, tickers, clock)
        if not fetched:
            raise SystemExit("no market metadata could be fetched; nothing to collect")

        plan = _build_plan(fetched, args)
        print(f"Plan: {plan.describe()}")
        for basket in plan.baskets:
            print(f"  AT_MOST_ONE basket {basket.event_ticker}: {list(basket.members)}")
        for basket in plan.yes_baskets:
            print(f"  AT_LEAST_ONE basket {basket.event_ticker}: {list(basket.members)}")

        started_at = clock.now()
        journal_path = default_journal_path(JOURNAL_DIR, now=datetime.now(tz=UTC))
        journal = RawFrameJournal(journal_path, connection_epoch=1)

        with connect(engine) as connection:
            stored = Catalogue(connection).open_session(
                detector_plan=plan.to_payload(),
                started_at=started_at,
                environment=str(settings.kalshi_env),
                schema_version=SCHEMA_VERSION,
                raw_journal_path=str(journal_path),
                notes="Phase-1 soak",
            )
        print(f"Session: {stored.session_id[:12]}  catalogue={args.database}")

        collector = DurableCollector(
            engine=engine,
            plan=plan,
            clock=clock,
            session_id=stored.session_id,
            journal=journal,
            max_queue_depth=args.max_queue_depth,
        )
        capture = _CollectorCapture(collector)
        await capture_context(
            capture,
            client,
            fetched=fetched,
            failures=metadata_failures + selection_failures,
            clock=clock,
            semantics_root=args.semantics_root,
        )

    metrics = await _stream(settings, args, collector, clock, plan)
    collector.finish()
    journal.close()

    print("\nVerifying decisions by replaying the stored source observations ...")
    verification = verify_session_decisions(
        engine,
        stored.session_id,
        provider_factory=_kalshi_provider,
        verified_at=clock.now(),
    )
    print(f"  {verification.describe()}")

    with connect(engine) as connection:
        catalogue = Catalogue(connection)
        decisions = catalogue.decision_count(stored.session_id)
        report: dict[str, Any] = {
            "generated_at": datetime.now(tz=UTC).isoformat(),
            "note": "derived research output; not source evidence",
            "session_id": stored.session_id,
            "schema_version": SCHEMA_VERSION,
            "plan": plan.to_payload(),
            "markets": list(plan.monitored_markets),
            "soak_seconds": round((clock.now() - started_at).total_seconds(), 1),
            "collector": metrics,
            "storage": {
                "observations": catalogue.observation_count(stored.session_id),
                "decisions": decisions,
                "database_bytes": directory_bytes(Path(args.database)),
                "raw_journal_bytes": directory_bytes(journal_path),
                "health": catalogue.health_counts(stored.session_id),
            },
            "decisions": {
                "by_detector": catalogue.detector_counts(stored.session_id),
                "by_classification": catalogue.classification_counts(stored.session_id),
            },
            "replay_verification": verification.payload(),
            "resources": resource_usage(),
        }
    engine.dispose()
    return report


def _build_plan(fetched: list[Any], args: argparse.Namespace) -> DetectorPlan:
    """The exact monitoring configuration, stated rather than inferred.

    Baskets are formed only where the session watches two or more markets of
    one event. Both relation families are planned over the same groups: they
    need different certificates, and which one exists is a fact about the
    registry, not something to guess at plan time.
    """
    base = _capture.build_plan(
        fetched,
        quantities=tuple(Quantity.from_value(q) for q in args.quantity),
        refresh_policy=ContextRefreshPolicy(),
        balance_precision=args.precision,
        notes="Phase-1 soak",
    )
    plan: DetectorPlan = replace(base, yes_baskets=base.baskets if args.at_least_one else ())
    return plan


async def _stream(
    settings: Settings,
    args: argparse.Namespace,
    collector: DurableCollector,
    clock: SystemClock,
    plan: DetectorPlan,
) -> dict[str, Any]:
    """The live loop. Fails closed on anything it cannot durably record."""
    client_ws = websocket_client(settings, clock)
    tickers = list(plan.monitored_markets)
    idle_polls = reconnects = ping_failures = 0
    started = time.monotonic()
    last_report = started

    print(f"\nCollecting for {args.minutes:.0f} minute(s) ...")
    try:
        await client_ws.connect()
        collector.ingest(
            collector.next_observation(ObservationKind.CONNECTION_OPENED, {"epoch": 1}, epoch=1)
        )
        await client_ws.subscribe_orderbook(tickers)
        deadline = started + args.minutes * 60

        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                frame = await client_ws.receive(timeout=min(5.0, max(0.1, remaining)))
            except WebSocketIdle:
                idle_polls += 1
                try:
                    await client_ws.ping(timeout=10.0)
                except Exception:
                    ping_failures += 1
                continue
            except TimeoutError:
                continue
            except Exception as exc:
                print(f"  connection error: {type(exc).__name__}")
                collector.ingest(
                    collector.next_observation(
                        ObservationKind.CONNECTION_FAILED,
                        {"reason": type(exc).__name__},
                        epoch=1,
                    )
                )
                reconnects += 1
                break

            envelope = frame.envelope
            if envelope is not None and envelope.type == "subscribed":
                sid = (envelope.msg or {}).get("sid")
                if sid is not None:
                    collector.ingest(
                        collector.next_observation(
                            ObservationKind.SUBSCRIBED,
                            {
                                "sid": int(sid),
                                "channel": (envelope.msg or {}).get("channel", "orderbook_delta"),
                                "markets": tickers,
                            },
                            observed_at=frame.received_at,
                            epoch=1,
                        )
                    )
                    continue

            hint = (envelope.msg or {}).get("market_ticker") if envelope else None
            collector.ingest(
                collector.next_observation(
                    ObservationKind.FRAME_RECEIVED,
                    {"raw": frame.raw, "market_ticker": hint},
                    observed_at=frame.received_at,
                    epoch=1,
                )
            )

            if time.monotonic() - last_report >= args.progress_seconds:
                last_report = time.monotonic()
                elapsed = (time.monotonic() - started) / 60
                print(
                    f"  [{elapsed:5.1f}m] {collector.metrics.frames} frames, "
                    f"{collector.metrics.decisions_produced} decisions, "
                    f"queue hwm {collector.metrics.queue_high_water_mark}"
                )
    except (SourcePersistenceError, QueueOverflowError) as exc:
        print(f"\n  !! collector stopped: {exc}")
        collector.finish(status="FAILED_CLOSED")
        raise
    finally:
        with contextlib.suppress(Exception):
            await client_ws.close()
        collector.ingest(
            collector.next_observation(
                ObservationKind.CONNECTION_CLOSED, {"reason": "soak complete"}, epoch=1
            )
        )

    return {
        **collector.metrics.payload(),
        "idle_polls": idle_polls,
        "ping_failures": ping_failures,
        "reconnects": reconnects,
        "healthy": collector.health.healthy,
        "health_reason": collector.health.reason,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="Phase-1 durable collector")
    parser.add_argument("--market", action="append", metavar="TICKER")
    parser.add_argument("--basket-event", action="append", metavar="EVENT_TICKER")
    parser.add_argument("--markets", type=int, default=6)
    parser.add_argument("--series", type=int, default=250)
    parser.add_argument("--minutes", type=float, default=30.0)
    parser.add_argument("--quantity", action="append", default=None)
    parser.add_argument(
        "--precision",
        choices=("direct", "non-direct", "unknown-conservative"),
        default="unknown-conservative",
    )
    parser.add_argument("--at-least-one", action="store_true", default=True)
    parser.add_argument("--max-queue-depth", type=int, default=10_000)
    parser.add_argument("--progress-seconds", type=float, default=120.0)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--semantics-root", type=Path, default=SEMANTICS_ROOT)
    parser.add_argument("--out", type=Path, default=RESULTS_PATH)
    args = parser.parse_args()
    args.quantity = args.quantity or ["1.00"]
    args.seconds = args.minutes * 60

    report = await run_soak(Settings(), args)
    args.out.write_text(json.dumps(report, indent=2, default=str) + "\n")

    print("\n=== SOAK ===")
    for key in ("session_id", "soak_seconds", "markets"):
        print(f"  {key}: {report[key]}")
    for section in ("collector", "storage", "decisions", "resources"):
        print(f"  {section}: {json.dumps(report[section], default=str)[:400]}")
    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    asyncio.run(main())
