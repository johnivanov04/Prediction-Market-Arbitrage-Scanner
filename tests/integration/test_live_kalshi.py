"""Live tests against the real Kalshi API.

Excluded from the default suite (``-m "not live"`` in ``pyproject.toml``). Run
deliberately with::

    uv run pytest -m live

Read-only throughout: every call is a GET through the read-only client, which
has no write path. Tests requiring credentials skip cleanly when none are
configured, so a contributor without a key still gets a green run.
"""

from __future__ import annotations

import pytest

from predarb.config import KalshiEnv, Settings
from predarb.venues.kalshi.client import KalshiReadOnlyClient
from predarb.venues.kalshi.errors import KalshiNotFoundError
from predarb.venues.kalshi.normalize import normalize_price_grid
from predarb.venues.kalshi.session import read_only_client, websocket_client

pytestmark = [pytest.mark.live, pytest.mark.integration]


def settings() -> Settings:
    return Settings()


requires_credentials = pytest.mark.skipif(
    not settings().has_credentials,
    reason="no Kalshi credentials configured; set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH",
)


class TestPublicEndpoints:
    """These need no credentials."""

    async def test_exchange_status(self):
        async with KalshiReadOnlyClient.public(env=KalshiEnv.PROD) as client:
            status = await client.get_exchange_status()
        assert status.exchange_active is not None

    async def test_get_series(self):
        async with KalshiReadOnlyClient.public(env=KalshiEnv.PROD) as client:
            series = await client.get_series("KXHIGHNY")
        assert series.ticker == "KXHIGHNY"
        assert series.fee_type

    async def test_pagination_follows_cursors(self):
        async with KalshiReadOnlyClient.public(env=KalshiEnv.PROD) as client:
            tickers = set()
            async for market in client.iter_markets(limit=100, status="open"):
                tickers.add(market.ticker)
                if len(tickers) >= 250:
                    break
        assert len(tickers) >= 250  # more than one page, all distinct

    async def test_orderbook_prices_are_on_the_market_grid(self):
        """Every quoted price must land on the instrument's own tick grid."""
        async with KalshiReadOnlyClient.public(env=KalshiEnv.PROD) as client:
            target = None
            async for market in client.iter_markets(limit=500, status="open"):
                has_size = (market.yes_bid_size_fp and not market.yes_bid_size_fp.is_zero) or (
                    market.no_bid_size_fp and not market.no_bid_size_fp.is_zero
                )
                if has_size and market.price_ranges:
                    target = market
                    break
            if target is None:
                pytest.skip("no quoting market found in sample")
            book = await client.get_orderbook(target.ticker)

        grid = normalize_price_grid(target.price_ranges, target.price_level_structure)
        assert grid is not None
        for price, _ in book.yes_dollars + book.no_dollars:
            assert grid.is_valid_price(price), f"{price} is off-grid for {target.ticker}"

    async def test_unknown_ticker_maps_to_not_found(self):
        async with KalshiReadOnlyClient.public(env=KalshiEnv.PROD) as client:
            with pytest.raises(KalshiNotFoundError):
                await client.get_market("DEFINITELY-NOT-A-REAL-TICKER-XYZ")

    async def test_authenticated_endpoint_refused_without_credentials(self):
        async with KalshiReadOnlyClient.public(env=KalshiEnv.PROD) as client:
            with pytest.raises(Exception, match="requires authentication"):
                await client.get_account_limits()


@requires_credentials
class TestAuthenticatedEndpoints:
    """These need credentials, and touch no financial account data."""

    async def test_discover_rate_limits(self):
        async with read_only_client(settings(), authenticate=True) as client:
            limits = await client.discover_rate_limits()
        # Values are account-specific and deliberately not asserted against
        # constants -- only their structure is checked.
        assert limits.read.refill_rate > 0
        assert limits.read.bucket_capacity > 0
        assert limits.write.refill_rate > 0
        assert limits.costs.is_discovered
        assert limits.costs.default_cost > 0

    async def test_websocket_handshake_and_subscription(self):
        ws = websocket_client(settings())
        try:
            await ws.connect()
            assert ws.is_connected
            async with KalshiReadOnlyClient.public(env=settings().kalshi_env) as rest:
                ticker = None
                async for market in rest.iter_markets(limit=200, status="open"):
                    if market.yes_bid_size_fp and not market.yes_bid_size_fp.is_zero:
                        ticker = market.ticker
                        break
            if ticker is None:
                pytest.skip("no quoting market found")
            await ws.subscribe_orderbook([ticker])
            frames = [frame async for frame in ws.frames(limit=3)]
            assert frames
            assert any(f.parsed for f in frames)
        finally:
            await ws.close()
        assert not ws.is_connected
