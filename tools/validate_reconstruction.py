"""Live, read-only validation of order-book reconstruction.

Run with::

    uv run python tools/validate_reconstruction.py --seconds 180
    uv run python tools/validate_reconstruction.py --get-snapshot-experiment

Runs a real collector session against production and reports what happened.
**The expected normal result is zero integrity violations.** A violation here is
a data-quality finding about the feed or about our handling of it -- never an
economic event, and never an arbitrage.

Also runs the ``get_snapshot`` experiment when asked: does a requested snapshot
keep the sid, what seq does it get, does every requested market produce one, and
do deltas interleave? Evidence for a possible future recovery optimisation;
reconnect stays the default until correctness is established.

Safety: read-only throughout. Subscribes to public market-data channels and
sends no order. Credentials are used only to open the socket.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

from predarb.books.recovery import RecoveryPolicy
from predarb.config import Settings
from predarb.ingest.book_collector import BookCollector, default_journal_path
from predarb.ingest.raw_journal import read_journal
from predarb.venues.kalshi.client import KalshiReadOnlyClient
from predarb.venues.kalshi.market_discovery import discover_active_markets
from predarb.venues.kalshi.session import websocket_client
from predarb.venues.kalshi.websocket import WebSocketIdle

ROOT = Path(__file__).resolve().parent.parent
JOURNAL_DIR = ROOT / "data" / "raw"
RESULTS_PATH = ROOT / "reconstruction_validation.json"


async def resolve_markets(settings: Settings, args: argparse.Namespace) -> list[str]:
    if args.market:
        return list(args.market)
    print("Discovering active markets ...")
    async with KalshiReadOnlyClient.public(env=settings.kalshi_env) as client:
        candidates, stats = await discover_active_markets(
            client, count=args.markets, series_limit=args.series, now=datetime.now(tz=UTC)
        )
    print(
        f"  examined {stats['markets_examined']} markets across {stats['series_examined']} series"
    )
    for candidate in candidates:
        print(f"    {candidate.describe()}")
    return [c.ticker for c in candidates]


async def run_collector(settings: Settings, markets: list[str], seconds: float) -> dict[str, Any]:
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    journal_path = default_journal_path(JOURNAL_DIR)
    collector = BookCollector(
        settings=settings,
        markets=markets,
        journal_path=journal_path,
        recovery_policy=RecoveryPolicy(max_attempts=3),
    )
    print(f"\nCollecting for {seconds:.0f}s into {journal_path.name} ...")
    await collector.run(duration_s=seconds)

    summary = collector.summary()
    reconstructor = collector.reconstructor
    now = datetime.now(tz=UTC)

    records = read_journal(journal_path) if journal_path.exists() else []
    summary["journal_path"] = str(journal_path)
    summary["journal_records_on_disk"] = len(records)
    summary["journal_bytes_on_disk"] = journal_path.stat().st_size if journal_path.exists() else 0
    summary["journal_all_hashes_valid"] = all(r.verify() for r in records)
    summary["markets"] = markets
    if reconstructor is not None and reconstructor.liveness is not None:
        summary["liveness_state"] = reconstructor.liveness_state(now).value
        summary["liveness_age_s"] = round(reconstructor.liveness.age(now).total_seconds(), 2)
    return summary


async def get_snapshot_experiment(settings: Settings, markets: list[str]) -> dict[str, Any]:
    """Does ``update_subscription action=get_snapshot`` preserve the sid?"""
    print("\n=== get_snapshot experiment ===")
    observations: list[dict[str, Any]] = []
    ws = websocket_client(settings)
    try:
        await ws.connect()
        await ws.subscribe_orderbook(markets)
        sid: int | None = None

        for _ in range(60):
            try:
                frame = await ws.receive(timeout=10)
            except WebSocketIdle:
                if await ws.ping(timeout=10) is None:
                    return {"error": "connection lost while waiting for frames"}
                continue
            envelope = frame.envelope
            if envelope is None:
                continue
            if envelope.type == "subscribed" and envelope.msg:
                candidate = envelope.msg.get("sid")
                sid = candidate if isinstance(candidate, int) else None
                continue
            observations.append(_observe(envelope, "before"))
            if len([o for o in observations if o["phase"] == "before"]) >= 12:
                break

        if sid is None:
            return {"skipped": "no sid observed"}

        await ws.update_subscription(sid, action="get_snapshot", market_tickers=markets)
        for _ in range(90):
            try:
                frame = await ws.receive(timeout=10)
            except WebSocketIdle:
                if await ws.ping(timeout=10) is None:
                    break
                continue
            if frame.envelope is None:
                continue
            observations.append(_observe(frame.envelope, "after"))
            after = [o for o in observations if o["phase"] == "after"]
            if len(after) >= 30:
                break
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        await ws.close()

    after = [o for o in observations if o["phase"] == "after"]
    snapshots = [o for o in after if o["type"] == "orderbook_snapshot"]
    deltas = [o for o in after if o["type"] == "orderbook_delta"]
    seqs = [o["seq"] for o in observations if o["seq"] is not None]
    return {
        "requested_sid": sid,
        "requested_markets": markets,
        "sid_preserved": all(o["sid"] == sid for o in after if o["sid"] is not None),
        "snapshots_after_request": len(snapshots),
        "snapshot_seqs": [o["seq"] for o in snapshots],
        "snapshot_markets": sorted({o["ticker"] for o in snapshots if o["ticker"]}),
        "deltas_interleaved_after_request": len(deltas),
        "sequence_contiguous": all(b == a + 1 for a, b in pairwise(seqs)),
        "observations": observations[:40],
    }


def _observe(envelope: Any, phase: str) -> dict[str, Any]:
    ticker = ""
    if envelope.msg:
        raw = envelope.msg.get("market_ticker")
        ticker = raw if isinstance(raw, str) else ""
    return {
        "phase": phase,
        "type": envelope.type,
        "sid": envelope.sid,
        "seq": envelope.seq,
        "ticker": ticker,
    }


def report(summary: dict[str, Any]) -> None:
    print("\n================ RECONSTRUCTION VALIDATION ================")
    print(f"  connections            : {summary.get('connections')}")
    print(f"  reconnects             : {summary.get('reconnects')}")
    print(f"  frames received        : {summary.get('frames')}")
    health = summary.get("health", {})
    for key in (
        "snapshots_applied",
        "deltas_applied",
        "control_frames",
        "sequence_violations",
        "invariant_violations",
        "unknown_sequenced_frames",
        "books_invalidated",
        "journal_failures",
        "unparsed_frames",
    ):
        print(f"  {key:23s}: {health.get(key)}")
    print(f"  integrity              : {summary.get('integrity')}")
    print(f"  journal healthy        : {summary.get('journal_healthy')}")
    print(f"  journal records on disk: {summary.get('journal_records_on_disk')}")
    print(f"  journal bytes on disk  : {summary.get('journal_bytes_on_disk')}")
    print(f"  journal hashes valid   : {summary.get('journal_all_hashes_valid')}")
    print(
        f"  liveness               : {summary.get('liveness_state')} "
        f"(age {summary.get('liveness_age_s')}s)"
    )
    print("\n  books:")
    for book in summary.get("books", []):
        print(
            f"    {book['ticker']:44s} {book['integrity']:18s} "
            f"yes={book['yes_levels']:3d} no={book['no_levels']:3d} "
            f"seq={book['latest_seq']} applied={book['applied_frames']} "
            f"eligible={book['scan_eligible']}"
        )
    violations = (
        health.get("sequence_violations", 0)
        + health.get("invariant_violations", 0)
        + health.get("unknown_sequenced_frames", 0)
    )
    print(f"\n  TOTAL INTEGRITY VIOLATIONS: {violations}")
    if violations:
        print("  NOTE: a violation is a data-quality finding, never an economic signal.")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Live order-book reconstruction validation")
    parser.add_argument("--market", action="append", metavar="TICKER")
    parser.add_argument("--markets", type=int, default=4)
    parser.add_argument("--series", type=int, default=300)
    parser.add_argument("--seconds", type=float, default=120.0)
    parser.add_argument("--get-snapshot-experiment", action="store_true")
    args = parser.parse_args()

    settings = Settings()
    if not settings.has_credentials:
        print(
            "No Kalshi credentials configured. The WebSocket requires them "
            "(HTTP 401 without). Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH."
        )
        raise SystemExit(2)

    markets = await resolve_markets(settings, args)
    if not markets:
        print("No active markets found.")
        raise SystemExit(1)
    print(f"\nMarkets: {markets}")

    results: dict[str, Any] = {
        "generated_at_utc": datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
        "environment": settings.kalshi_env.value,
    }
    summary = await run_collector(settings, markets, args.seconds)
    results["collector"] = summary
    report(summary)

    if args.get_snapshot_experiment:
        results["get_snapshot"] = await get_snapshot_experiment(settings, markets[:2])
        print("\n=== get_snapshot findings ===")
        for key, value in results["get_snapshot"].items():
            if key != "observations":
                print(f"  {key}: {value}")

    RESULTS_PATH.write_text(json.dumps(results, indent=2, default=str) + "\n")
    print(f"\nWrote {RESULTS_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
