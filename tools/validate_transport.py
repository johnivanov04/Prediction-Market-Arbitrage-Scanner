"""Live, read-only validation of the Kalshi transport layer.

Run with::

    uv run python tools/validate_transport.py

Two halves:

**Public** (always runs, no credentials). Exercises the read-only REST client
against production: typed responses, cursor pagination, query construction,
error mapping on a deliberately bad ticker, and that no request carries auth
headers.

**Authenticated** (only when credentials are configured). Fetches
``/account/limits`` and ``/account/endpoint_costs`` to discover the real rate
limit, then opens the WebSocket and subscribes.

Safety
------
Read-only throughout: every call is a GET, and the client has no write path.
No balance, position, order or fill endpoint is contacted. Discovered account
limits are **printed locally and never written to a fixture**, because they are
account-specific and would become a misleading "universal constant" if
committed.
"""

from __future__ import annotations

import asyncio
from typing import Any

from predarb.config import Settings
from predarb.venues.kalshi.errors import KalshiNotFoundError
from predarb.venues.kalshi.session import read_only_client, websocket_client

TICKED = "  ok  "
FAILED = "  !!  "


async def validate_public(settings: Settings) -> list[str]:
    failures: list[str] = []
    print(f"\n=== PUBLIC REST ({settings.kalshi_env.value}) ===")
    async with read_only_client(settings) as client:
        if client.has_credentials:
            failures.append("public client should not carry credentials")

        status = await client.get_exchange_status()
        print(
            f"{TICKED}exchange status: active={status.exchange_active}, "
            f"shards={len(status.exchange_index_statuses)}"
        )

        series = await client.get_series("KXHIGHNY")
        print(
            f"{TICKED}series KXHIGHNY: fee_type={series.fee_type}, "
            f"multiplier={series.fee_multiplier}"
        )

        markets: list[Any] = []
        async for market in client.iter_markets(limit=200, status="open"):
            markets.append(market)
            if len(markets) >= 400:
                break
        print(f"{TICKED}paginated {len(markets)} markets across pages")

        quoting = next(
            (
                m
                for m in markets
                if (m.yes_bid_size_fp and not m.yes_bid_size_fp.is_zero)
                or (m.no_bid_size_fp and not m.no_bid_size_fp.is_zero)
            ),
            None,
        )
        if quoting is not None:
            book = await client.get_orderbook(quoting.ticker)
            print(
                f"{TICKED}orderbook {quoting.ticker}: "
                f"{len(book.yes_dollars)} yes / {len(book.no_dollars)} no levels"
            )
        else:
            print(f"{TICKED}no quoting market in sample; orderbook check skipped")

        fee_changes = await client.get_series_fee_changes()
        print(
            f"{TICKED}series fee changes: {len(fee_changes.series_fee_change_arr)} "
            "(show_historical defaulted to true)"
        )

        event_fees = await client.get_event_fee_changes()
        print(f"{TICKED}event fee changes: {len(event_fees.event_fee_changes)}")

        try:
            await client.get_market("DEFINITELY-NOT-A-REAL-TICKER-XYZ")
        except KalshiNotFoundError:
            print(f"{TICKED}404 mapped to KalshiNotFoundError")
        except Exception as exc:
            failures.append(f"bad ticker raised {type(exc).__name__}, expected KalshiNotFoundError")

        try:
            await client.get_account_limits()
        except Exception as exc:
            if "requires authentication" in str(exc):
                print(f"{TICKED}authenticated endpoint refused without credentials")
            else:
                failures.append(f"unexpected error for unauthenticated call: {exc}")
    return failures


async def validate_authenticated(settings: Settings) -> list[str]:
    failures: list[str] = []
    print("\n=== AUTHENTICATED REST ===")
    async with read_only_client(settings, authenticate=True) as client:
        limits = await client.discover_rate_limits()
        # Printed locally only. Account-specific: never committed as a constant.
        print(f"{TICKED}usage tier          : {limits.usage_tier}")
        print(
            f"{TICKED}read  refill/capacity: {limits.read.refill_rate}/s, "
            f"{limits.read.bucket_capacity}"
        )
        print(
            f"{TICKED}write refill/capacity: {limits.write.refill_rate}/s, "
            f"{limits.write.bucket_capacity}"
        )
        print(f"{TICKED}default endpoint cost: {limits.costs.default_cost}")
        print(f"{TICKED}non-default costs    : {len(limits.costs.overrides)}")
        for (method, path), cost in sorted(limits.costs.overrides.items())[:15]:
            print(f"        {method:6s} {path:50s} {cost}")
        client.adopt_rate_limits(limits)
        print(f"{TICKED}adopted the discovered budget")

    print("\n=== WEBSOCKET HANDSHAKE ===")
    ws = websocket_client(settings)
    try:
        await ws.connect()
        print(f"{TICKED}connected to {ws.url}")
        await ws.subscribe_orderbook(["KXHIGHNY-26SEP16-T75"])
        received = 0
        async for frame in ws.frames(limit=5):
            received += 1
            if frame.envelope is not None:
                print(
                    f"{TICKED}frame type={frame.envelope.type} "
                    f"sid={frame.envelope.sid} seq={frame.envelope.seq}"
                )
        if received == 0:
            failures.append("connected but received no frames")
    except Exception as exc:
        failures.append(f"websocket: {type(exc).__name__}: {exc}")
    finally:
        await ws.close()
        print(f"{TICKED}closed cleanly")
    return failures


async def main() -> None:
    settings = Settings()
    failures = await validate_public(settings)

    if settings.has_credentials:
        failures += await validate_authenticated(settings)
    else:
        print("\n=== AUTHENTICATED CHECKS SKIPPED ===")
        print("  No credentials configured. Public market data needs none.")
        print("  To enable: set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH.")

    print("\n=== RESULT ===")
    if failures:
        for failure in failures:
            print(f"{FAILED}{failure}")
        raise SystemExit(1)
    print("  all checks passed")


if __name__ == "__main__":
    asyncio.run(main())
