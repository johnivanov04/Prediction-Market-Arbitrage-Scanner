"""Capture real Kalshi API payloads as test fixtures.

Run with::

    uv run python tools/capture_fixtures.py

This is a development tool, not part of the shipped package. It is re-runnable:
each run overwrites the fixture files and rewrites the manifest.

Safety
------
Only **public, unauthenticated** market-data endpoints are contacted. No
credentials are read, sent or stored, so a captured payload cannot contain a
key id, a signature or any account-scoped data. The tool asserts this by
constructing its HTTP client with a fixed header set containing no
authorisation header.

Fidelity
--------
Response bodies are written as **raw bytes, exactly as received**. They are not
pretty-printed, re-serialised or key-sorted, because the fixtures are used for
raw-byte regression tests and any rewrite would destroy that. The manifest
records a SHA-256 of each body so drift is detectable.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

API_BASE = "https://external-api.kalshi.com/trade-api/v2"
FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "kalshi"
REST_DIR = FIXTURE_ROOT / "rest"
MANIFEST = FIXTURE_ROOT / "manifest.json"

# Headers are fixed here so it is auditable that no credential is ever sent.
PUBLIC_HEADERS = {"Accept": "application/json", "User-Agent": "predarb-fixture-capture/0.1"}

_FORBIDDEN_HEADER_FRAGMENTS = ("kalshi-access", "authorization", "cookie")

# Pacing. The Basic read budget is 200 tokens/sec at 10 tokens per request,
# i.e. ~20 req/s; 12 req/s leaves ample headroom for a shared key.
_MIN_INTERVAL_S = 0.08
_INITIAL_BACKOFF_S = 1.0
_MAX_BACKOFF_S = 16.0
_MAX_RETRIES = 6


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


class Capturer:
    def __init__(self) -> None:
        for name in PUBLIC_HEADERS:
            if any(f in name.lower() for f in _FORBIDDEN_HEADER_FRAGMENTS):
                raise RuntimeError(f"refusing to capture with credential header {name!r}")
        self.client = httpx.Client(base_url=API_BASE, headers=PUBLIC_HEADERS, timeout=60)
        self.entries: list[dict[str, Any]] = []
        self._last_request_at = 0.0

    def close(self) -> None:
        self.client.close()

    def get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        """GET with simple pacing and 429 backoff.

        Kalshi's read bucket refills continuously and a 429 carries no penalty
        (A-21), so backing off and retrying is the documented-correct response.
        Pacing keeps a capture run well inside the Basic tier's budget.
        """
        delay = _MIN_INTERVAL_S
        for attempt in range(_MAX_RETRIES):
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < _MIN_INTERVAL_S:
                time.sleep(_MIN_INTERVAL_S - elapsed)
            response = self.client.get(path, params=params)
            self._last_request_at = time.monotonic()
            if response.status_code != 429:
                return response
            delay = min(delay * 2, _MAX_BACKOFF_S) if attempt else _INITIAL_BACKOFF_S
            print(f"  .. 429 on {path}; backing off {delay:.1f}s")
            time.sleep(delay)
        return response

    def capture(
        self,
        *,
        filename: str,
        path: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        tickers: dict[str, str] | None = None,
        notes: str = "",
    ) -> httpx.Response | None:
        response = self.get(path, params)
        if response.status_code != 200:
            print(f"  !! {endpoint} -> HTTP {response.status_code}; not captured")
            return None
        body = response.content
        target = REST_DIR / filename
        target.write_bytes(body)
        self.entries.append(
            {
                "file": f"rest/{filename}",
                "kind": "rest",
                "source": "REAL",
                "endpoint": endpoint,
                "request_url": str(response.request.url),
                "query": params or {},
                "tickers": tickers or {},
                "http_status": response.status_code,
                "content_type": response.headers.get("content-type", ""),
                "captured_at_utc": _utc_now(),
                "raw_unmodified": True,
                "byte_length": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
                "api_base": API_BASE,
                "notes": notes,
            }
        )
        print(f"  ok {filename}  ({len(body)} bytes)")
        return response

    def write_manifest(self, extra: list[dict[str, Any]]) -> None:
        manifest = {
            "description": (
                "Provenance for Kalshi API fixtures. REST entries are real, unmodified "
                "response bodies from the public production API. WebSocket entries are "
                "SYNTHETIC and labelled as such -- see websocket/README.md."
            ),
            "api_base": API_BASE,
            "generated_at_utc": _utc_now(),
            "generator": "tools/capture_fixtures.py",
            "credential_safety": (
                "Captured with a fixed public header set containing no authorisation "
                "header. Public market-data endpoints only. No account, portfolio, "
                "order, fill or balance data is contacted or stored."
            ),
            "fixtures": self.entries + extra,
        }
        MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")
        print(f"\nmanifest -> {MANIFEST} ({len(manifest['fixtures'])} entries)")


def _score(market: dict[str, Any]) -> float:
    """Rank candidates so the captured order book actually has depth in it.

    Ranking by traded volume does not work: a heavily-traded market that has
    since closed has an empty book. What predicts resting depth is *current*
    quoted size, so score on the displayed bid sizes and liquidity instead.

    This is float arithmetic on purpose -- it only orders candidates for
    capture and never touches a price, a payoff or a fee.
    """
    if market.get("status") != "active":
        return -1.0
    # liquidity_dollars is not a usable signal here: markets with a deep book
    # were observed reporting "0.0000". The displayed bid sizes are, and both
    # may be absent.
    try:
        return float(market.get("yes_bid_size_fp") or 0) + float(market.get("no_bid_size_fp") or 0)
    except (TypeError, ValueError):
        return 0.0


def _find_market_by_structure(cap: Capturer, wanted: set[str]) -> dict[str, str]:
    """Find the most-traded market available for each price_level_structure.

    Two passes, because the two listings have very different populations: the
    default ``/markets`` ordering is dominated by multivariate combo markets
    (which carry ``center_deci_edge_centi_cent``), while per-series listings
    surface the ordinary single-market series (``linear_cent``,
    ``tapered_deci_cent``). Searching only one of them misses a structure.

    Candidates are ranked by traded volume so the captured order book is not
    empty; an empty book is a legitimate fixture but a poor primary one.
    """
    best: dict[str, tuple[float, str]] = {}

    def offer(market: dict[str, Any]) -> None:
        structure = market.get("price_level_structure")
        if structure not in wanted:
            return
        # Skip combo/provisional markets as the primary example where possible:
        # they are excluded from Phase 1 detection anyway.
        penalty = 0.0 if not market.get("mve_collection_ticker") else -1.0
        score = _score(market) + penalty
        if structure not in best or score > best[structure][0]:
            best[structure] = (score, market["ticker"])

    # Pass 1: default listing (combo-heavy). Deep enough to reach the handful of
    # center_deci_edge_centi_cent markets that carry any resting size at all --
    # that structure was observed only on multivariate combo markets, and almost
    # all of them quote nothing.
    cursor: str | None = None
    for _ in range(10):
        params: dict[str, Any] = {"limit": 1000, "status": "open"}
        if cursor:
            params["cursor"] = cursor
        data = cap.get("/markets", params).json()
        batch = data.get("markets", [])
        if not batch:
            break
        for market in batch:
            offer(market)
        cursor = data.get("cursor")
        if not cursor:
            break

    # Pass 2: per-series listings (single-market series).
    series_tickers: list[str] = []
    cursor = None
    for _ in range(10):
        params = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = cap.get("/series", params).json()
        series_batch = data.get("series", [])
        if not series_batch:
            break
        series_tickers += [s["ticker"] for s in series_batch]
        cursor = data.get("cursor")
        if not cursor:
            break
    for ticker in series_tickers[:500]:
        try:
            data = cap.get("/markets", {"series_ticker": ticker, "limit": 50}).json()
        except (httpx.HTTPError, ValueError):
            continue
        for market in data.get("markets", []):
            offer(market)

    return {structure: ticker for structure, (_, ticker) in best.items()}


def main() -> None:
    REST_DIR.mkdir(parents=True, exist_ok=True)
    cap = Capturer()
    try:
        print("Locating markets covering each price_level_structure ...")
        structures = _find_market_by_structure(
            cap, {"linear_cent", "tapered_deci_cent", "center_deci_edge_centi_cent"}
        )
        print(f"  {structures}")

        print("\nCapturing REST fixtures ...")
        cap.capture(
            filename="series_KXHIGHNY.json",
            path="/series/KXHIGHNY",
            endpoint="GET /series/{series_ticker}",
            tickers={"series": "KXHIGHNY"},
            notes=(
                "Weather series. fee_type=quadratic, fee_multiplier=1, settlement_sources present."
            ),
        )
        cap.capture(
            filename="event_mutually_exclusive.json",
            path="/events/KXNEXTNATOSECGEN-99",
            endpoint="GET /events/{event_ticker}",
            tickers={"event": "KXNEXTNATOSECGEN-99"},
            notes="mutually_exclusive=true, collateral_return_type=MECNET.",
        )
        cap.capture(
            filename="event_not_mutually_exclusive.json",
            path="/events/KXELONMARS-99",
            endpoint="GET /events/{event_ticker}",
            tickers={"event": "KXELONMARS-99"},
            notes="mutually_exclusive=false, empty collateral_return_type.",
        )
        cap.capture(
            filename="events_list.json",
            path="/events",
            endpoint="GET /events",
            params={"limit": 5, "status": "open"},
            notes="Event list page, for pagination/cursor shape.",
        )
        cap.capture(
            filename="markets_list.json",
            path="/markets",
            endpoint="GET /markets",
            params={"limit": 5, "status": "open"},
            notes="Market list page, for pagination/cursor shape.",
        )
        for structure, ticker in sorted(structures.items()):
            cap.capture(
                filename=f"market_{structure}.json",
                path=f"/markets/{ticker}",
                endpoint="GET /markets/{ticker}",
                tickers={"market": ticker},
                notes=f"Single market with price_level_structure={structure}.",
            )
            cap.capture(
                filename=f"orderbook_{structure}.json",
                path=f"/markets/{ticker}/orderbook",
                endpoint="GET /markets/{ticker}/orderbook",
                tickers={"market": ticker},
                notes=f"Order book for a {structure} market. Bids only; yes_dollars + no_dollars.",
            )
        # An empty book is a required test case, so capture one deliberately
        # rather than relying on a primary fixture happening to be empty.
        empty = cap.get("/markets", {"limit": 200, "status": "open"}).json()
        for market in empty.get("markets", []):
            if (market.get("yes_bid_size_fp") in (None, "0.00")) and (
                market.get("no_bid_size_fp") in (None, "0.00")
            ):
                cap.capture(
                    filename="orderbook_empty.json",
                    path=f"/markets/{market['ticker']}/orderbook",
                    endpoint="GET /markets/{ticker}/orderbook",
                    tickers={"market": market["ticker"]},
                    notes="Order book with no resting interest on either side.",
                )
                break

        settled = cap.get("/markets", {"limit": 1, "status": "settled"}).json()
        if settled.get("markets"):
            settled_ticker = settled["markets"][0]["ticker"]
            cap.capture(
                filename="market_settled.json",
                path=f"/markets/{settled_ticker}",
                endpoint="GET /markets/{ticker}",
                tickers={"market": settled_ticker},
                notes="Settled market: result and settlement_value_dollars populated.",
            )
        cap.capture(
            filename="series_fee_changes.json",
            path="/series/fee_changes",
            endpoint="GET /series/fee_changes",
            params={"show_historical": "true"},
            notes=(
                "Scheduled series fee changes. NOTE: without show_historical=true this "
                "returns an empty array. Contains the undocumented fee_type value "
                "'margin_market_maker_program_fees' and fee_multiplier values 0 and 0.5."
            ),
        )
        cap.capture(
            filename="series_fee_changes_empty.json",
            path="/series/fee_changes",
            endpoint="GET /series/fee_changes",
            params={"series_ticker": "KXHIGHNY"},
            notes="Empty-result shape for the same endpoint.",
        )
        cap.capture(
            filename="events_fee_changes.json",
            path="/events/fee_changes",
            endpoint="GET /events/fee_changes",
            notes="Event-level fee overrides with scheduled_ts. Note plural 'events'.",
        )
        cap.capture(
            filename="exchange_status.json",
            path="/exchange/status",
            endpoint="GET /exchange/status",
            notes="Exchange and per-shard trading status.",
        )

        websocket_entries = [
            {
                "file": f"websocket/{p.name}",
                "kind": "websocket",
                "source": "SYNTHETIC",
                "endpoint": "wss://external-api-ws.kalshi.com/trade-api/ws/v2",
                "channel": "orderbook_delta",
                "captured_at_utc": None,
                "raw_unmodified": False,
                "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                "byte_length": p.stat().st_size,
                "notes": (
                    "SYNTHETIC. Derived from the shapes in the official WebSocket "
                    "documentation. The production WebSocket requires authentication "
                    "(verified: HTTP 401 without credentials), and no API key is "
                    "configured, so no real message could be captured."
                ),
            }
            for p in sorted((FIXTURE_ROOT / "websocket").glob("*.json"))
        ]
        cap.write_manifest(websocket_entries)
    finally:
        cap.close()


if __name__ == "__main__":
    main()
