"""Controlled live observation of Kalshi WebSocket ``sid``/``seq`` behaviour.

The instrument for open question A-09: *what is ``seq`` scoped to, and is it
dense?* Until both are settled, book reconstruction cannot implement gap
detection, because ``seq != previous + 1`` only means a gap if every integer is
used.

Usage::

    uv run python tools/ws_sequence_experiment.py
    uv run python tools/ws_sequence_experiment.py --market TICKER1 --market TICKER2
    uv run python tools/ws_sequence_experiment.py --seconds 90 --markets 6

Experiments
-----------
A. One connection, one subscription, several active markets.
B. One connection, two subscriptions.
C. ``update_subscription``: add a market, then remove it.
D. Disconnect and reconnect.

Discipline
----------
Describes; does not conclude. Density and integrity statistics are computed for
every candidate scope and printed side by side. Turning them into an invariant
is a deliberate written act in ``docs/api_assumptions.md``.

Safety
------
Read-only. Subscribes to public market-data channels and sends no order. The
API key id, private key, signatures and handshake headers are never printed or
stored; captured fixtures contain public message bodies only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from predarb.config import Settings
from predarb.venues.kalshi.market_discovery import discover_active_markets
from predarb.venues.kalshi.sequence_analysis import (
    SequenceRecord,
    analyse_all_scopes,
    rank_scopes,
)
from predarb.venues.kalshi.session import read_only_client, websocket_client
from predarb.venues.kalshi.websocket import KalshiWebSocketClient, ObservedFrame

ROOT = Path(__file__).resolve().parent.parent
REAL_WS_DIR = ROOT / "tests" / "fixtures" / "kalshi" / "websocket_real"
RESULTS_PATH = ROOT / "ws_experiment_results.json"


class _Runs:
    """Collects observations across every experiment in one process run.

    A tiny holder rather than module globals, so the session counter and the
    record list cannot be mutated from two places by accident.
    """

    def __init__(self) -> None:
        self.session_counter = 0
        self.records: list[SequenceRecord] = []
        self.fixtures_cleared = False

    def next_session(self) -> int:
        self.session_counter += 1
        return self.session_counter


RUNS = _Runs()


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def record_frames(session: int, frames: list[ObservedFrame], start_index: int = 0) -> int:
    """Turn observed frames into sequence records, preserving arrival order."""
    index = start_index
    for frame in frames:
        envelope = frame.envelope
        if envelope is None:
            index += 1
            continue
        ticker = ""
        if envelope.msg:
            raw = envelope.msg.get("market_ticker")
            ticker = raw if isinstance(raw, str) else ""
        RUNS.records.append(
            SequenceRecord(
                session=session,
                arrival_index=index,
                sid=envelope.sid,
                seq=envelope.seq,
                market_ticker=ticker,
                message_type=envelope.type,
            )
        )
        index += 1
    return index


def frame_rows(session: int, frames: list[ObservedFrame]) -> list[dict[str, Any]]:
    """Full per-frame log: session, arrival, sid, seq, ticker, type, timestamps."""
    rows: list[dict[str, Any]] = []
    for index, frame in enumerate(frames):
        envelope = frame.envelope
        exchange_ts = None
        ticker = ""
        if envelope is not None and envelope.msg:
            raw_ts = envelope.msg.get("ts_ms")
            exchange_ts = raw_ts if isinstance(raw_ts, int) else None
            raw_ticker = envelope.msg.get("market_ticker")
            ticker = raw_ticker if isinstance(raw_ticker, str) else ""
        rows.append(
            {
                "session": session,
                "arrival_index": index,
                "sid": envelope.sid if envelope else None,
                "seq": envelope.seq if envelope else None,
                "market_ticker": ticker,
                "type": envelope.type if envelope else "UNPARSED",
                "exchange_ts_ms": exchange_ts,
                "received_at": frame.received_at.isoformat().replace("+00:00", "Z"),
            }
        )
    return rows


def save_fixtures(frames: list[ObservedFrame], label: str, limit: int = 8) -> list[dict[str, Any]]:
    """Persist real public market-data frames, with provenance.

    Only the message body is written. Handshake headers, where credentials
    live, never reach this function.

    The directory is cleared on the first save of a run. Two runs writing the
    same filenames would otherwise interleave frames from different sessions
    into one apparent stream -- which looks exactly like a sequence violation
    and would corrupt the evidence these fixtures exist to preserve.
    """
    if not RUNS.fixtures_cleared and REAL_WS_DIR.exists():
        for stale in REAL_WS_DIR.glob("*.json"):
            stale.unlink()
    RUNS.fixtures_cleared = True
    REAL_WS_DIR.mkdir(parents=True, exist_ok=True)
    saved: list[dict[str, Any]] = []
    by_type: dict[str, int] = {}
    for frame in frames:
        if frame.envelope is None:
            continue
        kind = frame.envelope.type
        seen = by_type.get(kind, 0)
        if seen >= limit:
            continue
        by_type[kind] = seen + 1
        name = f"{label}_{kind}_{seen:02d}.json"
        (REAL_WS_DIR / name).write_bytes(frame.raw.encode())
        saved.append(
            {
                "file": f"websocket_real/{name}",
                "kind": "websocket",
                "source": "REAL",
                "channel": "orderbook_delta",
                "type": kind,
                "sid": frame.envelope.sid,
                "seq": frame.envelope.seq,
                "received_at_utc": frame.received_at.isoformat().replace("+00:00", "Z"),
                "raw_unmodified": True,
                "notes": "Public market-data body only. No handshake headers captured.",
            }
        )
    return saved


async def drain(
    client: KalshiWebSocketClient, *, seconds: float, limit: int = 2000
) -> list[ObservedFrame]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    frames: list[ObservedFrame] = []
    while loop.time() < deadline and len(frames) < limit:
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            frames.append(await asyncio.wait_for(client.receive(), timeout=min(remaining, 15)))
        except TimeoutError:
            break
        except Exception as exc:
            print(f"    receive stopped: {type(exc).__name__}")
            break
    return frames


async def experiment_a(settings: Settings, tickers: list[str], seconds: float) -> dict[str, Any]:
    print(f"\n[A] ONE subscription over {len(tickers)} markets")
    session = RUNS.next_session()
    async with websocket_client(settings) as ws:
        await ws.subscribe_orderbook(tickers)
        frames = await drain(ws, seconds=seconds)
    record_frames(session, frames)
    print(f"    {len(frames)} frames")
    sids = sorted({f.envelope.sid for f in frames if f.envelope and f.envelope.sid is not None})
    return {
        "session": session,
        "tickers": tickers,
        "frames": len(frames),
        "distinct_sids": sids,
        "all_markets_share_one_sid": len(sids) == 1,
        "rows": frame_rows(session, frames),
        "fixtures": save_fixtures(frames, "experiment_a"),
    }


async def experiment_b(settings: Settings, tickers: list[str], seconds: float) -> dict[str, Any]:
    """Two subscriptions on one connection, on two DIFFERENT channels.

    Subscribing twice to the *same* channel does not create a second sid: the
    server merges the markets into the existing subscription (observed -- both
    subscribe commands returned frames under sid=1). Distinct sids require
    distinct channels, so this subscribes to ``orderbook_delta`` and ``trade``.

    That distinction is the whole point of the experiment: only with two live
    sids on one socket can per-sid numbering be told apart from a
    connection-global counter.
    """
    print("\n[B] TWO subscriptions on ONE connection, across two channels")
    session = RUNS.next_session()
    async with websocket_client(settings) as ws:
        await ws.subscribe_orderbook(tickers)
        await ws._send_command(
            {"cmd": "subscribe", "params": {"channels": ["trade"], "market_tickers": tickers}}
        )
        frames = await drain(ws, seconds=seconds)
    record_frames(session, frames)
    print(f"    {len(frames)} frames")
    sids = sorted({f.envelope.sid for f in frames if f.envelope and f.envelope.sid is not None})
    per_sid_first: dict[int, int] = {}
    for frame in frames:
        envelope = frame.envelope
        if envelope and envelope.sid is not None and envelope.seq is not None:
            per_sid_first.setdefault(envelope.sid, envelope.seq)
    return {
        "session": session,
        "tickers": tickers,
        "channels": ["orderbook_delta", "trade"],
        "frames": len(frames),
        "distinct_sids": sids,
        "sids_are_distinct": len(sids) >= 2,
        "first_seq_per_sid": per_sid_first,
        "each_sid_starts_at_one": all(v == 1 for v in per_sid_first.values()),
        "rows": frame_rows(session, frames),
        "fixtures": save_fixtures(frames, "experiment_b", limit=4),
    }


async def experiment_c(settings: Settings, tickers: list[str], seconds: float) -> dict[str, Any]:
    if len(tickers) < 2:
        return {"skipped": "need at least two markets"}
    base, extra = tickers[:1], tickers[1:2]
    print(f"\n[C] subscribe {base}, add {extra}, then remove it")
    session = RUNS.next_session()
    index = 0
    async with websocket_client(settings) as ws:
        await ws.subscribe_orderbook(base)
        before = await drain(ws, seconds=seconds / 3)
        index = record_frames(session, before, index)
        sid = next(
            (f.envelope.sid for f in before if f.envelope and f.envelope.sid is not None), None
        )
        added: list[ObservedFrame] = []
        removed: list[ObservedFrame] = []
        if sid is not None:
            await ws.update_subscription(sid, action="add_markets", market_tickers=extra)
            added = await drain(ws, seconds=seconds / 3)
            index = record_frames(session, added, index)
            await ws.update_subscription(sid, action="delete_markets", market_tickers=extra)
            removed = await drain(ws, seconds=seconds / 3)
            record_frames(session, removed, index)
    added_tickers = {
        f.envelope.msg.get("market_ticker") for f in added if f.envelope and f.envelope.msg
    }
    removed_tickers = {
        f.envelope.msg.get("market_ticker") for f in removed if f.envelope and f.envelope.msg
    }
    return {
        "session": session,
        "sid": sid,
        "base_market": base[0],
        "added_market": extra[0],
        "frames_before": len(before),
        "frames_after_add": len(added),
        "frames_after_remove": len(removed),
        "snapshot_after_add": sum(
            1 for f in added if f.envelope and f.envelope.type == "orderbook_snapshot"
        ),
        "tickers_seen_after_add": sorted(t for t in added_tickers if t),
        "tickers_seen_after_remove": sorted(t for t in removed_tickers if t),
        "added_market_still_present_after_remove": extra[0] in removed_tickers,
        "rows": frame_rows(session, before + added + removed),
    }


async def experiment_d(settings: Settings, tickers: list[str], seconds: float) -> dict[str, Any]:
    print("\n[D] connect, disconnect, reconnect with the same subscription")
    sessions = []
    for round_index in (1, 2):
        session = RUNS.next_session()
        async with websocket_client(settings) as ws:
            await ws.subscribe_orderbook(tickers[:2])
            frames = await drain(ws, seconds=seconds / 2)
        record_frames(session, frames)
        first_seq = next(
            (f.envelope.seq for f in frames if f.envelope and f.envelope.seq is not None), None
        )
        snapshot_seq = next(
            (
                f.envelope.seq
                for f in frames
                if f.envelope and f.envelope.type == "orderbook_snapshot"
            ),
            None,
        )
        sessions.append(
            {
                "round": round_index,
                "session": session,
                "frames": len(frames),
                "sids": sorted(
                    {f.envelope.sid for f in frames if f.envelope and f.envelope.sid is not None}
                ),
                "first_seq": first_seq,
                "first_snapshot_seq": snapshot_seq,
                "rows": frame_rows(session, frames)[:20],
            }
        )
        print(f"    session {session}: {len(frames)} frames, first seq={first_seq}")
    return {"sessions": sessions}


def snapshot_relationship() -> dict[str, Any]:
    """Does a snapshot open the same numbering stream the deltas continue?"""
    by_stream: dict[tuple[object, ...], list[SequenceRecord]] = {}
    for record in sorted(RUNS.records, key=lambda r: (r.session, r.arrival_index)):
        if record.seq is None or record.sid is None:
            continue
        by_stream.setdefault((record.session, record.sid), []).append(record)

    findings = []
    for key, records in by_stream.items():
        snapshots = [r for r in records if r.message_type == "orderbook_snapshot"]
        if not snapshots:
            continue
        first_snapshot = snapshots[0]
        following = [r for r in records if r.arrival_index > first_snapshot.arrival_index]
        next_record = following[0] if following else None
        findings.append(
            {
                "stream": list(key),
                "snapshot_seq": first_snapshot.seq,
                "snapshot_count": len(snapshots),
                "snapshot_seqs": [s.seq for s in snapshots],
                "next_seq_after_snapshot": next_record.seq if next_record else None,
                "next_type_after_snapshot": next_record.message_type if next_record else None,
                "continues_by_one": (
                    next_record is not None
                    and next_record.seq is not None
                    and first_snapshot.seq is not None
                    and next_record.seq == first_snapshot.seq + 1
                ),
            }
        )
    return {"streams": findings}


def print_density_table() -> dict[str, Any]:
    analyses = analyse_all_scopes(RUNS.records)
    ranked = rank_scopes(analyses)
    print("\n=== SEQUENCE DENSITY BY CANDIDATE SCOPE ===")
    print(
        f"{'scope':14s} {'streams':>8s} {'pairs':>8s} {'+1':>8s} "
        f"{'skips':>7s} {'dups':>6s} {'dec':>5s} {'density':>9s}"
    )
    for scope, _ in ranked:
        a = analyses[scope]
        density = "n/a" if a.overall_density is None else f"{a.overall_density:.4f}"
        print(
            f"{scope.value:14s} {len(a.streams):>8d} {a.total_pairs:>8d} "
            f"{a.total_increments_of_one:>8d} {a.total_skips:>7d} "
            f"{a.total_duplicates:>6d} {a.total_decreases:>5d} {density:>9s}"
        )
    return {
        "ranked": [(s.value, score) for s, score in ranked],
        "analyses": {s.value: a.summary() for s, a in analyses.items()},
    }


async def resolve_markets(settings: Settings, args: argparse.Namespace) -> list[str]:
    if args.market:
        print(f"Using {len(args.market)} explicitly supplied market(s)")
        return list(args.market)
    print("Discovering active markets (per-series; the default listing is ~100% combos) ...")
    async with read_only_client(settings) as client:
        candidates, stats = await discover_active_markets(
            client, count=args.markets, series_limit=args.series, now=datetime.now(tz=UTC)
        )
    print("\n  selection statistics:")
    for key, value in stats.items():
        print(f"    {key:22s} {value}")
    if not candidates:
        print("\n  No candidate scored above zero. Nothing to observe.")
        return []
    print("\n  top candidates:")
    for candidate in candidates:
        print(f"    {candidate.describe()}")
    return [c.ticker for c in candidates]


async def main() -> None:
    parser = argparse.ArgumentParser(description="Kalshi WebSocket sid/seq experiments")
    parser.add_argument(
        "--market",
        action="append",
        metavar="TICKER",
        help="explicit public market ticker; repeatable, skips auto-discovery",
    )
    parser.add_argument("--markets", type=int, default=6, help="how many to auto-discover")
    parser.add_argument("--series", type=int, default=500, help="series to scan when discovering")
    parser.add_argument("--seconds", type=float, default=60.0, help="observation seconds per run")
    parser.add_argument(
        "--experiment", action="append", choices=["a", "b", "c", "d"], help="run only these"
    )
    args = parser.parse_args()

    settings = Settings()
    if not settings.has_credentials:
        print(
            "No Kalshi credentials configured.\n\n"
            "The WebSocket requires authentication (verified: HTTP 401 without it).\n"
            "Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH, keeping the key file\n"
            "outside the repository (e.g. ~/.config/predarb/kalshi/, chmod 600).\n"
        )
        raise SystemExit(2)

    tickers = await resolve_markets(settings, args)
    if not tickers:
        raise SystemExit(1)
    print(f"\nObserving: {tickers}")

    chosen = set(args.experiment or ["a", "b", "c", "d"])
    results: dict[str, Any] = {
        "generated_at_utc": _utc_now(),
        "environment": settings.kalshi_env.value,
        "ws_url": settings.ws_url,
        "markets": tickers,
        "observation_seconds": args.seconds,
        "note": (
            "Descriptive observations only. Interpretation into an invariant is "
            "recorded separately in docs/api_assumptions.md."
        ),
    }
    if "a" in chosen:
        results["experiment_a"] = await experiment_a(settings, tickers, args.seconds)
    if "b" in chosen:
        results["experiment_b"] = await experiment_b(settings, tickers, args.seconds)
    if "c" in chosen:
        results["experiment_c"] = await experiment_c(settings, tickers, args.seconds)
    if "d" in chosen:
        results["experiment_d"] = await experiment_d(settings, tickers, args.seconds)

    results["total_records"] = len(RUNS.records)
    results["density"] = print_density_table()
    results["snapshot_relationship"] = snapshot_relationship()

    RESULTS_PATH.write_text(json.dumps(results, indent=2, default=str) + "\n")
    print(f"\nTotal sequence records: {len(RUNS.records)}")
    print(f"Wrote {RESULTS_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
