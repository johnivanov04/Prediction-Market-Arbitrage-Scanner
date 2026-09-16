"""Controlled live observation of Kalshi WebSocket ``sid``/``seq`` behaviour.

This is the instrument for open question A-09: *what is the scope of ``seq``?*
Until that is settled, book reconstruction cannot implement gap detection,
because "seq != previous + 1" only means a gap if you know what the counter
counts.

Run with credentials configured::

    uv run python tools/ws_sequence_experiment.py

Experiments
-----------
A. One connection, one subscription, several actively-updating markets.
   Does ``seq`` advance per subscription, or per market?
B. One connection, two subscriptions. Do the ``sid`` values differ, and does
   each ``seq`` stay independently monotonic while frames interleave?
C. ``update_subscription``: add a market, then remove one. Does sequencing
   change, and does a snapshot arrive for the added market?
D. Disconnect and reconnect. What ``sid`` and initial ``seq`` appear, and does
   any state carry across sessions?

Discipline
----------
The harness **describes** and does not conclude. It records what arrived and
prints descriptive statistics; interpreting them into an invariant is a
deliberate, written act in ``docs/api_assumptions.md``, separated into
DOCUMENTED / OBSERVED / INFERRED / UNKNOWN.

Safety
------
Read-only. It subscribes to public market-data channels and sends no order of
any kind. Handshake headers are never printed or stored. Captured frames are
public market-data bodies only.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from predarb.config import Settings
from predarb.venues.kalshi.auth import PrivateKeyError
from predarb.venues.kalshi.models import KalshiMarketsPage, decode_json
from predarb.venues.kalshi.session import websocket_client
from predarb.venues.kalshi.websocket import KalshiWebSocketClient, ObservedFrame, SequenceObserver

FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "kalshi"
REAL_WS_DIR = FIXTURE_ROOT / "websocket_real"
RESULTS_PATH = Path(__file__).resolve().parent.parent / "ws_experiment_results.json"

PUBLIC_REST = "https://external-api.kalshi.com/trade-api/v2"


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


async def find_active_markets(count: int = 4) -> list[str]:
    """Pick markets that are actually quoting, so deltas actually arrive.

    An idle market produces a snapshot and then silence, which teaches nothing
    about sequencing.
    """
    headers = {"Accept": "application/json", "User-Agent": "predarb-ws-experiment/0.1"}
    candidates: list[tuple[float, str]] = []
    async with httpx.AsyncClient(base_url=PUBLIC_REST, headers=headers, timeout=60) as client:
        cursor: str | None = None
        for _ in range(6):
            params: dict[str, Any] = {"limit": 1000, "status": "open"}
            if cursor:
                params["cursor"] = cursor
            response = await client.get("/markets", params=params)
            if response.status_code != 200:
                break
            payload = decode_json(response.content)
            page = KalshiMarketsPage.model_validate(payload)
            for market in page.markets:
                if market.mve_collection_ticker:
                    continue  # combo markets barely quote
                size = 0.0
                if market.yes_bid_size_fp:
                    size += float(market.yes_bid_size_fp.as_decimal())
                if market.no_bid_size_fp:
                    size += float(market.no_bid_size_fp.as_decimal())
                volume = float(market.volume_24h_fp.as_decimal()) if market.volume_24h_fp else 0.0
                if size > 0:
                    candidates.append((volume, market.ticker))
            cursor = page.cursor
            if not cursor:
                break
            await asyncio.sleep(0.1)
    candidates.sort(reverse=True)
    return [ticker for _, ticker in candidates[:count]]


def save_frames(frames: list[ObservedFrame], label: str) -> list[dict[str, Any]]:
    """Persist real market-data frames as fixtures, with provenance.

    Only the message body is written -- never handshake headers, which is where
    credentials live.
    """
    REAL_WS_DIR.mkdir(parents=True, exist_ok=True)
    saved: list[dict[str, Any]] = []
    for index, frame in enumerate(frames):
        if frame.envelope is None:
            continue
        name = f"{label}_{index:03d}_{frame.envelope.type}.json"
        path = REAL_WS_DIR / name
        path.write_bytes(frame.raw.encode())
        saved.append(
            {
                "file": f"websocket_real/{name}",
                "kind": "websocket",
                "source": "REAL",
                "channel": "orderbook_delta",
                "type": frame.envelope.type,
                "sid": frame.envelope.sid,
                "seq": frame.envelope.seq,
                "received_at_utc": frame.received_at.isoformat().replace("+00:00", "Z"),
                "raw_unmodified": True,
                "notes": "Public market-data body only. No handshake headers captured.",
            }
        )
    return saved


async def drain(
    client: KalshiWebSocketClient, *, seconds: float, limit: int = 400
) -> list[ObservedFrame]:
    """Collect frames for a bounded period."""
    frames: list[ObservedFrame] = []
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline and len(frames) < limit:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        try:
            frame = await asyncio.wait_for(client.receive(), timeout=min(remaining, 10))
        except (TimeoutError, Exception):
            break
        frames.append(frame)
    return frames


def summarise(frames: list[ObservedFrame]) -> dict[str, Any]:
    observer = SequenceObserver()
    for frame in frames:
        if frame.envelope is not None:
            observer.observe(frame.envelope)
    report = observer.report()
    report["unparsed"] = sum(1 for f in frames if f.envelope is None)
    report["first_20_arrivals"] = [
        {"sid": sid, "seq": seq, "ticker": ticker, "type": kind}
        for sid, seq, ticker, kind in observer.arrival_order[:20]
    ]
    return report


async def experiment_a(settings: Settings, tickers: list[str]) -> dict[str, Any]:
    """One connection, one subscription, several markets."""
    print(f"\n[A] one subscription over {len(tickers)} markets: {tickers}")
    async with websocket_client(settings) as ws:
        await ws.subscribe_orderbook(tickers)
        frames = await drain(ws, seconds=45)
    print(f"    {len(frames)} frames")
    result = summarise(frames)
    result["fixtures"] = save_frames(frames[:12], "experiment_a")
    result["tickers"] = tickers
    return result


async def experiment_b(settings: Settings, tickers: list[str]) -> dict[str, Any]:
    """One connection, two subscriptions."""
    split = max(1, len(tickers) // 2)
    first, second = tickers[:split], tickers[split:] or tickers[:1]
    print(f"\n[B] two subscriptions: {first} and {second}")
    async with websocket_client(settings) as ws:
        await ws.subscribe_orderbook(first)
        await ws.subscribe_orderbook(second)
        frames = await drain(ws, seconds=45)
    print(f"    {len(frames)} frames")
    result = summarise(frames)
    result["subscription_groups"] = {"first": first, "second": second}
    return result


async def experiment_c(settings: Settings, tickers: list[str]) -> dict[str, Any]:
    """update_subscription: add then remove a market."""
    if len(tickers) < 2:
        return {"skipped": "need at least two markets"}
    base, extra = tickers[:1], tickers[1:2]
    print(f"\n[C] subscribe {base}, add {extra}, then remove it")
    async with websocket_client(settings) as ws:
        await ws.subscribe_orderbook(base)
        before = await drain(ws, seconds=15)
        sid = next(
            (f.envelope.sid for f in before if f.envelope and f.envelope.sid is not None), None
        )
        added: list[ObservedFrame] = []
        removed: list[ObservedFrame] = []
        if sid is not None:
            await ws.update_subscription(sid, action="add_markets", market_tickers=extra)
            added = await drain(ws, seconds=15)
            await ws.update_subscription(sid, action="delete_markets", market_tickers=extra)
            removed = await drain(ws, seconds=10)
    return {
        "sid_observed": sid,
        "before_update": summarise(before),
        "after_add": summarise(added),
        "after_remove": summarise(removed),
        "snapshot_after_add": sum(
            1 for f in added if f.envelope and f.envelope.type == "orderbook_snapshot"
        ),
    }


async def experiment_d(settings: Settings, tickers: list[str]) -> dict[str, Any]:
    """Disconnect and reconnect."""
    print("\n[D] connect, disconnect, reconnect")
    async with websocket_client(settings) as ws:
        await ws.subscribe_orderbook(tickers[:2])
        first = await drain(ws, seconds=20)
    async with websocket_client(settings) as ws:
        await ws.subscribe_orderbook(tickers[:2])
        second = await drain(ws, seconds=20)
    return {
        "session_1": summarise(first),
        "session_2": summarise(second),
        "session_1_first_seq": next(
            (f.envelope.seq for f in first if f.envelope and f.envelope.seq is not None), None
        ),
        "session_2_first_seq": next(
            (f.envelope.seq for f in second if f.envelope and f.envelope.seq is not None), None
        ),
        "session_1_sids": sorted({f.envelope.sid for f in first if f.envelope and f.envelope.sid}),
        "session_2_sids": sorted({f.envelope.sid for f in second if f.envelope and f.envelope.sid}),
    }


async def main() -> None:
    settings = Settings()
    if not settings.has_credentials:
        print(
            "No Kalshi credentials configured.\n\n"
            "The WebSocket requires authentication (verified: HTTP 401 without it),\n"
            "so these experiments cannot run. Set:\n\n"
            "  KALSHI_API_KEY_ID=<your key id>\n"
            "  KALSHI_PRIVATE_KEY_PATH=<path to the RSA private key .pem>\n"
            "  KALSHI_ENVIRONMENT=production\n\n"
            "Keep the key file outside the repository, e.g.\n"
            "  ~/.config/predarb/kalshi/kalshi-key.pem   (chmod 600)\n"
        )
        raise SystemExit(2)

    try:
        _ = websocket_client(settings)
    except PrivateKeyError as exc:
        print(f"Could not load credentials: {exc}")
        raise SystemExit(2) from None

    print("Finding actively-quoting markets ...")
    tickers = await find_active_markets(4)
    if not tickers:
        print("No actively quoting markets found; try again during market hours.")
        raise SystemExit(1)
    print(f"  using {tickers}")

    results: dict[str, Any] = {
        "generated_at_utc": _utc_now(),
        "environment": settings.kalshi_env.value,
        "ws_url": settings.ws_url,
        "note": (
            "Descriptive observations only. Interpretation into an invariant is "
            "recorded separately in docs/api_assumptions.md."
        ),
    }
    results["experiment_a"] = await experiment_a(settings, tickers)
    results["experiment_b"] = await experiment_b(settings, tickers)
    results["experiment_c"] = await experiment_c(settings, tickers)
    results["experiment_d"] = await experiment_d(settings, tickers)

    RESULTS_PATH.write_text(json.dumps(results, indent=2, default=str) + "\n")
    print(f"\nWrote {RESULTS_PATH}")
    print("\n=== SUMMARY ===")
    for name in ("experiment_a", "experiment_b"):
        block = results[name]
        print(f"\n{name}: frame types {Counter(block.get('frame_types', {}))}")
        for sid, stats in block.get("per_sid", {}).items():
            print(f"  sid={sid}: {stats}")
        for key, stats in block.get("per_sid_market", {}).items():
            print(f"  {key}: {stats}")


if __name__ == "__main__":
    asyncio.run(main())
