"""Async Kalshi REST client -- read-only by construction.

Safety model
------------
Phase 1 must be structurally incapable of submitting an order, not merely
missing the code to do so. Three layers enforce that:

1. **The public surface has no write method.** :class:`KalshiReadOnlyClient`
   exposes named GET operations and nothing else. There is no public
   ``request(method, path, ...)`` escape hatch that would make a POST to
   ``/portfolio/orders`` a one-liner.
2. **The transport refuses non-read methods.** ``_Transport`` accepts a method
   argument for architectural reasons, but it is private *and* it raises
   :class:`ReadOnlyViolationError` for anything outside ``{GET, HEAD}``. Adding
   a write method would require deliberately disabling that guard, which is a
   visible, reviewable act rather than an oversight.
3. **Retries are read-only by policy.** The retry layer only ever retries
   methods it knows are idempotent. It will not grow into a generic
   "retry anything" primitive that a future write path could inherit.

A key with write scope changes none of this. Read scope is sufficient and
preferred; write scope simply goes unused.

Public vs authenticated
-----------------------
Kalshi serves series, events, markets and order books **without** credentials,
so public research works with no key configured. Credentials are required only
for the WebSocket and for the ``/account`` limit endpoints. The client reflects
that: ``authenticate=True`` is requested per call, and calling an authenticated
endpoint without credentials raises a clear error rather than sending an
unsigned request and puzzling over the 401.
"""

from __future__ import annotations

import asyncio
import random
import uuid
from collections.abc import AsyncIterator, Mapping
from types import TracebackType
from typing import Any, Final, Self
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ValidationError

from predarb.clock import Clock, SystemClock
from predarb.config import KalshiEndpoints, KalshiEnv
from predarb.logging import get_logger
from predarb.venues.kalshi.auth import KalshiSigner, redact_headers
from predarb.venues.kalshi.errors import (
    KalshiError,
    KalshiRateLimitError,
    KalshiSchemaError,
    KalshiServerError,
    KalshiTimeoutError,
    KalshiTransportError,
    classify_http_error,
)
from predarb.venues.kalshi.models import (
    KalshiAccountLimits,
    KalshiEndpointCosts,
    KalshiEvent,
    KalshiEventEnvelope,
    KalshiEventFeeChangesResponse,
    KalshiEventsPage,
    KalshiExchangeStatus,
    KalshiHistoricalCutoff,
    KalshiMarket,
    KalshiMarketEnvelope,
    KalshiMarketsPage,
    KalshiOrderbook,
    KalshiOrderbookEnvelope,
    KalshiSeries,
    KalshiSeriesEnvelope,
    KalshiSeriesFeeChangesResponse,
    decode_json,
)
from predarb.venues.kalshi.pagination import Page, paginate
from predarb.venues.kalshi.rate_limit import (
    BucketLimit,
    ConservativePolicy,
    DiscoveredRateLimits,
    EndpointCost,
    EndpointCostRegistry,
    RateLimitPolicy,
    TokenBucketPolicy,
    TrafficKind,
)

__all__ = [
    "READ_ONLY_METHODS",
    "KalshiReadOnlyClient",
    "ReadOnlyViolationError",
    "RetryPolicy",
]

logger = get_logger(__name__)

READ_ONLY_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD"})
"""The only HTTP methods this adapter will ever send in Phase 1."""

_DEFAULT_TIMEOUT: Final = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
_DEFAULT_LIMITS: Final = httpx.Limits(max_connections=10, max_keepalive_connections=5)


class ReadOnlyViolationError(KalshiError):
    """A non-read HTTP method was attempted.

    This is a programming error, not a runtime condition. Phase 1 places no
    orders, and reaching this means something tried to.
    """


class RetryPolicy:
    """Bounded exponential backoff with jitter, for reads only.

    Kalshi's 429 carries no penalty and no ``Retry-After``: the bucket simply
    refills, so backing off and retrying is the documented-correct response.

    ``is_retryable`` checks the HTTP method, not just the error, on purpose.
    Retrying a non-idempotent request can duplicate its effect, and a retry
    layer that does not know the difference is exactly the sort of primitive
    that becomes dangerous the moment someone adds a write path. Any future
    write retry must opt in explicitly with an idempotency policy.
    """

    __slots__ = ("base_delay_s", "jitter", "max_attempts", "max_delay_s")

    def __init__(
        self,
        *,
        max_attempts: int = 4,
        base_delay_s: float = 0.5,
        max_delay_s: float = 16.0,
        jitter: float = 0.25,
    ) -> None:
        if max_attempts < 1:
            raise ValueError(f"max_attempts must be at least 1, got {max_attempts}")
        self.max_attempts = max_attempts
        self.base_delay_s = base_delay_s
        self.max_delay_s = max_delay_s
        self.jitter = jitter

    def is_retryable(self, method: str, error: Exception) -> bool:
        if method.upper() not in READ_ONLY_METHODS:
            return False
        return isinstance(
            error,
            KalshiRateLimitError | KalshiServerError | KalshiTimeoutError | KalshiTransportError,
        )

    def delay_for(self, attempt: int, error: Exception | None = None) -> float:
        """Delay before ``attempt`` (1-based index of the *next* try)."""
        if isinstance(error, KalshiRateLimitError) and error.retry_after_s is not None:
            return min(error.retry_after_s, self.max_delay_s)
        raw = self.base_delay_s * (2 ** max(0, attempt - 1))
        capped = min(raw, self.max_delay_s)
        # Full-width jitter around the capped delay, so concurrent clients that
        # were rate limited together do not retry in lockstep.
        spread = capped * self.jitter
        jittered: float = capped + random.uniform(-spread, spread)
        return max(0.0, jittered)


class _Transport:
    """PRIVATE low-level HTTP transport.

    Not exported and not reachable from application code. It takes a method
    argument because signing, cost lookup and retry classification all need one,
    but it refuses anything outside :data:`READ_ONLY_METHODS`, so the
    flexibility cannot become a trading path.
    """

    def __init__(
        self,
        *,
        base_url: str,
        signer: KalshiSigner | None,
        clock: Clock,
        rate_policy: RateLimitPolicy,
        retry_policy: RetryPolicy,
        timeout: httpx.Timeout,
        limits: httpx.Limits,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._signer = signer
        self._clock = clock
        self._rate_policy = rate_policy
        self._retry_policy = retry_policy
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            limits=limits,
            transport=transport,
            headers={"Accept": "application/json", "User-Agent": "predarb/0.1 (read-only)"},
            follow_redirects=False,
        )

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def has_credentials(self) -> bool:
        return self._signer is not None

    def set_rate_policy(self, policy: RateLimitPolicy) -> None:
        """Swap the pacing policy, e.g. after discovering the real budget."""
        self._rate_policy = policy

    async def aclose(self) -> None:
        await self._client.aclose()

    def _full_path(self, path: str) -> str:
        """The absolute request path, which is also what gets signed."""
        return urlsplit(self._base_url).path.rstrip("/") + path

    async def get_json(
        self,
        path: str,
        *,
        path_template: str,
        params: Mapping[str, Any] | None = None,
        authenticate: bool = False,
    ) -> Any:
        """Issue a GET and return the Decimal-safe decoded body."""
        return await self._send(
            "GET", path, path_template=path_template, params=params, authenticate=authenticate
        )

    async def _send(
        self,
        method: str,
        path: str,
        *,
        path_template: str,
        params: Mapping[str, Any] | None = None,
        authenticate: bool = False,
    ) -> Any:
        canonical = method.upper()
        if canonical not in READ_ONLY_METHODS:
            raise ReadOnlyViolationError(
                f"refusing to send {canonical} {path_template}: this adapter is "
                "read-only. Phase 1 submits no orders and has no write path."
            )
        if authenticate and self._signer is None:
            raise KalshiError(
                f"{path_template} requires authentication, but no credentials are "
                "configured. Set the API key id and private key path, or call a "
                "public endpoint instead."
            )

        correlation_id = uuid.uuid4().hex[:12]
        last_error: Exception | None = None

        for attempt in range(1, self._retry_policy.max_attempts + 1):
            await self._rate_policy.acquire(canonical, path_template, TrafficKind.READ)
            try:
                # A fresh signature every attempt. The timestamp is part of the
                # signed message, so reusing headers from a previous attempt
                # would present the server with a stale -- and eventually
                # replayed -- timestamp.
                headers: dict[str, str] = {}
                if authenticate and self._signer is not None:
                    headers = self._signer.rest_headers(canonical, self._full_path(path))

                response = await self._client.request(
                    canonical, path, params=dict(params or {}), headers=headers
                )
                error = self._error_for(response, path_template, correlation_id)
                if error is None:
                    return self._decode(response, path_template)
                last_error = error
            except httpx.TimeoutException as exc:
                last_error = KalshiTimeoutError(f"timeout on {path_template}: {type(exc).__name__}")
            except httpx.HTTPError as exc:
                last_error = KalshiTransportError(
                    f"transport failure on {path_template}: {type(exc).__name__}"
                )
            finally:
                self._rate_policy.release()

            if attempt >= self._retry_policy.max_attempts or not self._retry_policy.is_retryable(
                canonical, last_error
            ):
                raise last_error

            delay = self._retry_policy.delay_for(attempt, last_error)
            logger.warning(
                "kalshi.retry",
                endpoint=path_template,
                attempt=attempt,
                delay_s=round(delay, 3),
                error=type(last_error).__name__,
                correlation_id=correlation_id,
            )
            await _sleep(delay)

        raise last_error if last_error else KalshiTransportError(f"failed on {path_template}")

    def _error_for(
        self, response: httpx.Response, path_template: str, correlation_id: str
    ) -> Exception | None:
        if response.is_success:
            return None
        # Only the body and status reach the error. Request headers are never
        # attached: that is where the signature lives.
        return classify_http_error(
            status_code=response.status_code,
            endpoint=path_template,
            body=response.text,
            headers=response.headers,
            request_id=correlation_id,
        )

    def _decode(self, response: httpx.Response, path_template: str) -> Any:
        try:
            return decode_json(response.content)
        except ValueError as exc:
            raise KalshiSchemaError(
                f"response was not valid JSON ({type(exc).__name__})",
                endpoint=path_template,
                body_excerpt=response.text,
            ) from None

    def __repr__(self) -> str:
        return f"_Transport(base_url={self._base_url!r}, authenticated={self.has_credentials})"


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _validate[M: BaseModel](model: type[M], payload: Any, endpoint: str) -> M:
    """Parse a payload, converting schema failures into a typed error.

    Generic so callers keep their concrete model type: a schema failure should
    be a typed ``KalshiSchemaError``, not an ``Any`` that silently propagates.
    """
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise KalshiSchemaError(
            f"response did not match {model.__name__}: {exc.error_count()} validation error(s)",
            endpoint=endpoint,
            body_excerpt=str(exc)[:400],
        ) from None


class KalshiReadOnlyClient:
    """The Kalshi adapter's entire public surface.

    Every method is a read. There is deliberately no ``create_order``,
    ``cancel_order``, ``amend_order`` or generic ``request`` -- see the module
    docstring for why that is enforced rather than merely observed.

    Use as an async context manager so connections are reused and closed::

        async with KalshiReadOnlyClient.public() as client:
            book = await client.get_orderbook("KXHIGHNY-26SEP16-T75")
    """

    def __init__(
        self,
        *,
        base_url: str,
        signer: KalshiSigner | None = None,
        clock: Clock | None = None,
        rate_policy: RateLimitPolicy | None = None,
        retry_policy: RetryPolicy | None = None,
        timeout: httpx.Timeout | None = None,
        limits: httpx.Limits | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._clock = clock or SystemClock()
        self._rate_policy = rate_policy or ConservativePolicy(self._clock)
        self._transport = _Transport(
            base_url=base_url,
            signer=signer,
            clock=self._clock,
            rate_policy=self._rate_policy,
            retry_policy=retry_policy or RetryPolicy(),
            timeout=timeout or _DEFAULT_TIMEOUT,
            limits=limits or _DEFAULT_LIMITS,
            transport=transport,
        )

    # -- construction -------------------------------------------------------

    @classmethod
    def public(
        cls,
        *,
        env: KalshiEnv = KalshiEnv.PROD,
        clock: Clock | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        **kwargs: Any,
    ) -> Self:
        """A client for public market data, with no credentials.

        Uses :class:`ConservativePolicy`: bounded concurrency and backoff rather
        than a token budget, because the anonymous budget is not documented and
        the authenticated one does not necessarily describe it.
        """
        return cls(base_url=KalshiEndpoints.rest(env), clock=clock, transport=transport, **kwargs)

    @classmethod
    def authenticated(
        cls,
        *,
        signer: KalshiSigner,
        env: KalshiEnv = KalshiEnv.PROD,
        clock: Clock | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        **kwargs: Any,
    ) -> Self:
        """A client that can also reach the authenticated read endpoints.

        Credentials are environment-specific: a production key will not work
        against demo, and the resulting 401 is indistinguishable from a bad key.
        """
        return cls(
            base_url=KalshiEndpoints.rest(env),
            signer=signer,
            clock=clock,
            transport=transport,
            **kwargs,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._transport.aclose()

    @property
    def has_credentials(self) -> bool:
        return self._transport.has_credentials

    @property
    def rate_policy(self) -> RateLimitPolicy:
        return self._rate_policy

    # -- series -------------------------------------------------------------

    async def get_series(self, series_ticker: str) -> KalshiSeries:
        payload = await self._transport.get_json(
            f"/series/{series_ticker}", path_template="/series/{series_ticker}"
        )
        return _validate(KalshiSeriesEnvelope, payload, "/series/{series_ticker}").series

    async def iter_series(self, *, limit: int = 200, **filters: Any) -> AsyncIterator[KalshiSeries]:
        """Stream every series, following cursors."""

        async def fetch(cursor: str | None) -> Page[KalshiSeries]:
            params = {"limit": limit, **filters}
            if cursor:
                params["cursor"] = cursor
            payload = await self._transport.get_json(
                "/series", path_template="/series", params=params
            )
            raw_items = payload.get("series", []) if isinstance(payload, dict) else []
            items = tuple(_validate(KalshiSeries, item, "/series") for item in raw_items)
            raw_cursor = payload.get("cursor") if isinstance(payload, dict) else None
            return Page.create(items, raw_cursor)

        async for item in paginate(fetch):
            yield item

    # -- events -------------------------------------------------------------

    async def get_event(
        self, event_ticker: str, *, with_nested_markets: bool = False
    ) -> KalshiEventEnvelope:
        params = {"with_nested_markets": "true"} if with_nested_markets else None
        payload = await self._transport.get_json(
            f"/events/{event_ticker}", path_template="/events/{event_ticker}", params=params
        )
        return _validate(KalshiEventEnvelope, payload, "/events/{event_ticker}")

    async def iter_events(self, *, limit: int = 200, **filters: Any) -> AsyncIterator[KalshiEvent]:
        async def fetch(cursor: str | None) -> Page[KalshiEvent]:
            params = {"limit": limit, **filters}
            if cursor:
                params["cursor"] = cursor
            payload = await self._transport.get_json(
                "/events", path_template="/events", params=params
            )
            page = _validate(KalshiEventsPage, payload, "/events")
            return Page.create(page.events, page.cursor)

        async for item in paginate(fetch):
            yield item

    # -- markets ------------------------------------------------------------

    async def get_market(self, ticker: str) -> KalshiMarket:
        payload = await self._transport.get_json(
            f"/markets/{ticker}", path_template="/markets/{ticker}"
        )
        return _validate(KalshiMarketEnvelope, payload, "/markets/{ticker}").market

    async def get_markets_page(
        self, *, cursor: str | None = None, limit: int = 1000, **filters: Any
    ) -> Page[KalshiMarket]:
        """One page of markets. Exposed so a caller can record its cursor.

        Membership enumeration has to *prove* it reached the end of a walk, and
        the only evidence the protocol offers is the venue declining to hand
        back another cursor. That evidence is gone once pages are flattened.
        """
        params: dict[str, Any] = {"limit": limit, **filters}
        if cursor:
            params["cursor"] = cursor
        payload = await self._transport.get_json(
            "/markets", path_template="/markets", params=params
        )
        page = _validate(KalshiMarketsPage, payload, "/markets")
        return Page.create(page.markets, page.cursor)

    async def iter_markets(
        self, *, limit: int = 1000, **filters: Any
    ) -> AsyncIterator[KalshiMarket]:
        """Stream markets, following cursors.

        ``filters`` passes through venue query parameters such as
        ``series_ticker`` and ``status``. Query construction is kept here,
        separate from signing, which uses the path only.
        """

        async def fetch(cursor: str | None) -> Page[KalshiMarket]:
            return await self.get_markets_page(cursor=cursor, limit=limit, **filters)

        async for item in paginate(fetch):
            yield item

    # -- historical partition ----------------------------------------------
    #
    # Read-only, unauthenticated, and public market data only. These exist
    # because the live endpoints are documented to omit markets that settled
    # before the historical cutoff, so live enumeration alone cannot answer
    # "what markets belong to this event" (A-48).

    async def get_historical_cutoff(self) -> KalshiHistoricalCutoff:
        """The live/historical boundary as the venue currently reports it.

        Fetched rather than assumed, and fetched *before* enumeration rather
        than after: the cutoff advances over time, so a boundary read afterwards
        might not be the one the queries actually ran against.
        """
        payload = await self._transport.get_json(
            "/historical/cutoff", path_template="/historical/cutoff"
        )
        return _validate(KalshiHistoricalCutoff, payload, "/historical/cutoff")

    async def iter_historical_markets(
        self,
        *,
        limit: int = 1000,
        event_ticker: str | None = None,
        series_ticker: str | None = None,
        tickers: str | None = None,
        mve_filter: str | None = None,
    ) -> AsyncIterator[KalshiMarket]:
        """Stream archived markets, following cursors.

        The documentation states the filters here are **mutually exclusive**, so
        supplying two is refused locally rather than sent. A venue that silently
        honoured one and ignored the other would return a plausible page that
        answers a different question from the one asked -- and an enumeration
        built on it would look complete while missing members.
        """

        async def fetch(cursor: str | None) -> Page[KalshiMarket]:
            return await self.get_historical_markets_page(
                cursor=cursor,
                limit=limit,
                event_ticker=event_ticker,
                series_ticker=series_ticker,
                tickers=tickers,
                mve_filter=mve_filter,
            )

        async for item in paginate(fetch):
            yield item

    async def get_historical_markets_page(
        self,
        *,
        cursor: str | None = None,
        limit: int = 1000,
        event_ticker: str | None = None,
        series_ticker: str | None = None,
        tickers: str | None = None,
        mve_filter: str | None = None,
    ) -> Page[KalshiMarket]:
        """One page of archived markets, with the mutual-exclusion rule enforced."""
        selectors = {
            "event_ticker": event_ticker,
            "series_ticker": series_ticker,
            "tickers": tickers,
        }
        supplied = {name: value for name, value in selectors.items() if value}
        if len(supplied) > 1:
            raise ValueError(
                f"/historical/markets filters are mutually exclusive; got {sorted(supplied)}"
            )
        params: dict[str, Any] = {"limit": limit, **supplied}
        if mve_filter:
            params["mve_filter"] = mve_filter
        if cursor:
            params["cursor"] = cursor
        payload = await self._transport.get_json(
            "/historical/markets", path_template="/historical/markets", params=params
        )
        page = _validate(KalshiMarketsPage, payload, "/historical/markets")
        return Page.create(page.markets, page.cursor)

    async def get_market_payloads_page(
        self,
        *,
        historical: bool,
        cursor: str | None = None,
        limit: int = 1000,
        **filters: Any,
    ) -> tuple[tuple[dict[str, Any], ...], str | None]:
        """One page of **raw** market dicts, unvalidated, plus the next cursor.

        Exists for membership enumeration, which asks a semantic question --
        does this ticker belong to this event -- that no financial invariant
        bears on. ``KalshiMarket`` rightly refuses a negative contract count
        (A-49), but applying that refusal here made one bad page report an
        entire event as having zero members.

        Every other caller should use the validated accessors. This one returns
        exactly what the venue sent, so a narrower projection can read only the
        fields it actually needs.
        """
        path = "/historical/markets" if historical else "/markets"
        params: dict[str, Any] = {"limit": limit, **{k: v for k, v in filters.items() if v}}
        if cursor:
            params["cursor"] = cursor
        payload = await self._transport.get_json(path, path_template=path, params=params)
        if not isinstance(payload, dict):
            raise KalshiSchemaError(
                f"expected an object, got {type(payload).__name__}", endpoint=path
            )
        markets = payload.get("markets") or ()
        raw = tuple(m for m in markets if isinstance(m, dict))
        next_cursor = payload.get("cursor")
        cursor_text = next_cursor.strip() if isinstance(next_cursor, str) else None
        return raw, (cursor_text or None)

    async def get_orderbook(self, ticker: str, *, depth: int | None = None) -> KalshiOrderbook:
        params = {"depth": depth} if depth is not None else None
        payload = await self._transport.get_json(
            f"/markets/{ticker}/orderbook",
            path_template="/markets/{ticker}/orderbook",
            params=params,
        )
        return _validate(
            KalshiOrderbookEnvelope, payload, "/markets/{ticker}/orderbook"
        ).orderbook_fp

    # -- fees ---------------------------------------------------------------

    async def get_series_fee_changes(
        self, *, series_ticker: str | None = None, show_historical: bool = True
    ) -> KalshiSeriesFeeChangesResponse:
        """Scheduled series fee changes.

        ``show_historical`` defaults to **True** deliberately: without it the
        venue returns an empty array, and a caller would conclude no fee change
        has ever happened (``docs/api_assumptions.md`` A-12).
        """
        params: dict[str, Any] = {"show_historical": str(show_historical).lower()}
        if series_ticker:
            params["series_ticker"] = series_ticker
        payload = await self._transport.get_json(
            "/series/fee_changes", path_template="/series/fee_changes", params=params
        )
        return _validate(KalshiSeriesFeeChangesResponse, payload, "/series/fee_changes")

    async def get_event_fee_changes(
        self, *, event_ticker: str | None = None
    ) -> KalshiEventFeeChangesResponse:
        """Event-level fee overrides. Note the plural path segment."""
        params = {"event_ticker": event_ticker} if event_ticker else None
        payload = await self._transport.get_json(
            "/events/fee_changes", path_template="/events/fee_changes", params=params
        )
        return _validate(KalshiEventFeeChangesResponse, payload, "/events/fee_changes")

    # -- exchange -----------------------------------------------------------

    async def get_exchange_status(self) -> KalshiExchangeStatus:
        payload = await self._transport.get_json(
            "/exchange/status", path_template="/exchange/status"
        )
        return _validate(KalshiExchangeStatus, payload, "/exchange/status")

    # -- account limits (authenticated, no financial account data) ----------

    async def get_account_limits(self) -> KalshiAccountLimits:
        """The account's API tier and token buckets.

        Authenticated, but discloses no balance, position, order or fill data --
        only the rate-limit configuration. That is why Phase 1 calls this and no
        other ``/account`` route.
        """
        payload = await self._transport.get_json(
            "/account/limits", path_template="/account/limits", authenticate=True
        )
        return _validate(KalshiAccountLimits, payload, "/account/limits")

    async def get_endpoint_costs(self) -> KalshiEndpointCosts:
        """Default token cost plus the endpoints that differ from it."""
        payload = await self._transport.get_json(
            "/account/endpoint_costs", path_template="/account/endpoint_costs", authenticate=True
        )
        return _validate(KalshiEndpointCosts, payload, "/account/endpoint_costs")

    async def discover_rate_limits(self) -> DiscoveredRateLimits:
        """Fetch both halves of the rate-limit configuration from the venue.

        This is the authoritative source. Nothing here is hardcoded: the tier,
        both bucket configurations and every endpoint cost come from the server.
        """
        limits = await self.get_account_limits()
        costs = await self.get_endpoint_costs()
        if limits.read is None or limits.write is None:
            raise KalshiSchemaError(
                "account limits did not include both read and write buckets",
                endpoint="/account/limits",
            )
        return DiscoveredRateLimits(
            usage_tier=limits.usage_tier,
            read=BucketLimit(
                refill_rate=limits.read.refill_rate, bucket_capacity=limits.read.bucket_capacity
            ),
            write=BucketLimit(
                refill_rate=limits.write.refill_rate, bucket_capacity=limits.write.bucket_capacity
            ),
            costs=EndpointCostRegistry.from_response(
                default_cost=costs.default_cost,
                costs=tuple(
                    EndpointCost(method=c.method, path=c.path, cost=c.cost)
                    for c in costs.endpoint_costs
                ),
            ),
        )

    def adopt_rate_limits(self, limits: DiscoveredRateLimits) -> None:
        """Switch to the venue-reported budget after discovering it."""
        self._rate_policy = TokenBucketPolicy(limits, self._clock)
        self._transport._rate_policy = self._rate_policy

    def __repr__(self) -> str:
        return (
            f"KalshiReadOnlyClient(base_url={self._transport.base_url!r}, "
            f"authenticated={self.has_credentials})"
        )


def safe_request_log_fields(
    *, method: str, endpoint: str, status: int | None, headers: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Build log fields for an HTTP call with credentials stripped.

    Anything that logs a request must go through this. ``httpx`` and ``httpcore``
    will happily emit full headers at DEBUG, which is how a signature ends up in
    a log file.
    """
    fields: dict[str, Any] = {"method": method.upper(), "endpoint": endpoint, "status": status}
    if headers is not None:
        fields["headers"] = redact_headers(headers)
    return fields
