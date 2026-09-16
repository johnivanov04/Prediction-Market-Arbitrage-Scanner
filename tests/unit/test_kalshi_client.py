"""Tests for the read-only Kalshi REST client, using a mocked transport."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from predarb.clock import FrozenClock
from predarb.config import KalshiEnv
from predarb.venues.kalshi.auth import (
    HEADER_KEY,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    KalshiCredentials,
    KalshiSigner,
)
from predarb.venues.kalshi.client import (
    READ_ONLY_METHODS,
    KalshiReadOnlyClient,
    ReadOnlyViolationError,
    RetryPolicy,
    safe_request_log_fields,
)
from predarb.venues.kalshi.errors import (
    KalshiAuthenticationError,
    KalshiAuthorizationError,
    KalshiClockSkewError,
    KalshiError,
    KalshiNotFoundError,
    KalshiRateLimitError,
    KalshiSchemaError,
    KalshiServerError,
    KalshiTimeoutError,
)
from tests.conftest import load_raw

pytestmark = pytest.mark.unit

T0 = datetime(2026, 9, 15, 12, 0, 0, tzinfo=UTC)

MARKET_A = {"ticker": "A", "event_ticker": "E", "market_type": "binary", "status": "active"}
MARKET_B = {"ticker": "B", "event_ticker": "E", "market_type": "binary", "status": "active"}


@pytest.fixture(scope="module")
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def make_signer(rsa_key: rsa.RSAPrivateKey) -> KalshiSigner:
    return KalshiSigner(
        KalshiCredentials(api_key_id="test-key", private_key=rsa_key), FrozenClock(T0)
    )


class Recorder:
    """Captures requests and replays scripted responses."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = responses
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self.responses) - 1)
        return self.responses[index]

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def json_response(payload: object, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, content=json.dumps(payload).encode(), headers={"content-type": "application/json"}
    )


def fixture_response(name: str) -> httpx.Response:
    return httpx.Response(
        200, content=load_raw(f"rest/{name}"), headers={"content-type": "application/json"}
    )


def client_with(recorder: Recorder, **kwargs: object) -> KalshiReadOnlyClient:
    return KalshiReadOnlyClient.public(
        env=KalshiEnv.PROD,
        clock=FrozenClock(T0),
        transport=recorder.transport,
        retry_policy=RetryPolicy(max_attempts=1),
        **kwargs,
    )


class TestReadOnlyGuarantee:
    """Phase 1 must be structurally incapable of submitting an order."""

    def test_only_read_methods_are_permitted(self):
        assert frozenset({"GET", "HEAD"}) == READ_ONLY_METHODS

    @pytest.mark.parametrize(
        "forbidden",
        ["create_order", "cancel_order", "amend_order", "place_trade", "create_quote", "request"],
    )
    def test_no_trading_or_generic_request_method_exists(self, forbidden):
        assert not hasattr(KalshiReadOnlyClient, forbidden)

    def test_every_public_method_is_a_read(self):
        public = {
            name
            for name in dir(KalshiReadOnlyClient)
            if not name.startswith("_") and callable(getattr(KalshiReadOnlyClient, name))
        }
        read_prefixes = ("get_", "iter_", "list_", "discover_")
        # Constructors and lifecycle, named explicitly so a new non-read method
        # cannot slip in behind a broad prefix rule.
        non_request_members = {"public", "authenticated", "aclose", "adopt_rate_limits"}
        offenders = {
            n for n in public if not n.startswith(read_prefixes) and n not in non_request_members
        }
        assert offenders == set(), f"non-read public methods: {offenders}"

    async def test_transport_refuses_a_write_method(self):
        recorder = Recorder([json_response({})])
        async with client_with(recorder) as client:
            transport = client._transport
            with pytest.raises(ReadOnlyViolationError, match="read-only"):
                await transport._send(
                    "POST", "/portfolio/orders", path_template="/portfolio/orders"
                )
        assert recorder.requests == []  # nothing was ever sent

    async def test_no_portfolio_endpoint_is_reachable(self):
        source = __import__("inspect").getsource(
            __import__("predarb.venues.kalshi.client", fromlist=["x"])
        )
        for path in ("/portfolio", "/orders", "/fills", "/positions"):
            # /portfolio/orders appears only in refusal messages and docstrings.
            assert f'"{path}"' not in source


class TestSuccessfulRequests:
    async def test_get_market(self):
        recorder = Recorder([fixture_response("market_linear_cent.json")])
        async with client_with(recorder) as client:
            market = await client.get_market("KXTEST")
        assert market.ticker
        assert recorder.requests[0].url.path.endswith("/markets/KXTEST")

    async def test_get_orderbook(self):
        recorder = Recorder([fixture_response("orderbook_linear_cent.json")])
        async with client_with(recorder) as client:
            book = await client.get_orderbook("KXTEST")
        assert book.no_dollars

    async def test_get_series(self):
        recorder = Recorder([fixture_response("series_KXHIGHNY.json")])
        async with client_with(recorder) as client:
            series = await client.get_series("KXHIGHNY")
        assert series.ticker == "KXHIGHNY"

    async def test_get_exchange_status(self):
        recorder = Recorder([fixture_response("exchange_status.json")])
        async with client_with(recorder) as client:
            status = await client.get_exchange_status()
        assert status.exchange_active is True

    async def test_query_params_are_sent_but_not_part_of_the_path(self):
        recorder = Recorder([fixture_response("orderbook_linear_cent.json")])
        async with client_with(recorder) as client:
            await client.get_orderbook("KXTEST", depth=5)
        request = recorder.requests[0]
        assert request.url.params["depth"] == "5"
        assert "?" not in request.url.path

    async def test_fee_changes_defaults_to_show_historical(self):
        # Without it the venue returns an empty array and a caller would
        # conclude no fee change has ever happened.
        recorder = Recorder([fixture_response("series_fee_changes.json")])
        async with client_with(recorder) as client:
            await client.get_series_fee_changes()
        assert recorder.requests[0].url.params["show_historical"] == "true"

    async def test_decimal_safe_parsing_of_financial_numbers(self):
        recorder = Recorder([fixture_response("series_KXHIGHNY.json")])
        async with client_with(recorder) as client:
            series = await client.get_series("KXHIGHNY")
        assert isinstance(series.fee_multiplier, Decimal)


class TestErrorMapping:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (401, KalshiAuthenticationError),
            (403, KalshiAuthorizationError),
            (404, KalshiNotFoundError),
            (429, KalshiRateLimitError),
            (500, KalshiServerError),
            (503, KalshiServerError),
        ],
    )
    async def test_status_maps_to_typed_error(self, status, expected):
        recorder = Recorder([httpx.Response(status, text="{}")])
        async with client_with(recorder) as client:
            with pytest.raises(expected):
                await client.get_market("X")

    async def test_401_mentioning_timestamp_is_classified_as_clock_skew(self):
        recorder = Recorder([httpx.Response(401, text='{"error":"timestamp too old"}')])
        async with client_with(recorder) as client:
            with pytest.raises(KalshiClockSkewError):
                await client.get_market("X")

    async def test_malformed_json_raises_schema_error(self):
        recorder = Recorder([httpx.Response(200, content=b"not json at all")])
        async with client_with(recorder) as client:
            with pytest.raises(KalshiSchemaError, match="not valid JSON"):
                await client.get_market("X")

    async def test_valid_json_wrong_schema_raises_schema_error(self):
        recorder = Recorder([json_response({"unexpected": "shape"})])
        async with client_with(recorder) as client:
            with pytest.raises(KalshiSchemaError, match="did not match"):
                await client.get_market("X")

    async def test_financially_malformed_field_is_fatal(self):
        payload = {
            "market": {
                "ticker": "X",
                "event_ticker": "E",
                "market_type": "binary",
                "status": "active",
                "yes_bid_dollars": "0.12345",
            }
        }
        recorder = Recorder([json_response(payload)])
        async with client_with(recorder) as client:
            with pytest.raises(KalshiSchemaError):
                await client.get_market("X")

    async def test_timeout_raises_typed_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow", request=request)

        client = KalshiReadOnlyClient.public(
            clock=FrozenClock(T0),
            transport=httpx.MockTransport(handler),
            retry_policy=RetryPolicy(max_attempts=1),
        )
        async with client:
            with pytest.raises(KalshiTimeoutError):
                await client.get_market("X")

    async def test_error_does_not_contain_credentials(self, rsa_key):
        recorder = Recorder([httpx.Response(403, text='{"error":"forbidden"}')])
        client = KalshiReadOnlyClient.authenticated(
            signer=make_signer(rsa_key),
            clock=FrozenClock(T0),
            transport=recorder.transport,
            retry_policy=RetryPolicy(max_attempts=1),
        )
        async with client:
            with pytest.raises(KalshiAuthorizationError) as exc_info:
                await client.get_account_limits()
        rendered = str(exc_info.value)
        assert "test-key" not in rendered
        assert "KALSHI-ACCESS" not in rendered


class TestAuthentication:
    async def test_public_endpoints_need_no_credentials(self):
        recorder = Recorder([fixture_response("market_linear_cent.json")])
        async with client_with(recorder) as client:
            assert not client.has_credentials
            await client.get_market("X")
        assert HEADER_KEY not in recorder.requests[0].headers

    async def test_authenticated_endpoint_without_credentials_raises_clearly(self):
        recorder = Recorder([json_response({})])
        async with client_with(recorder) as client:
            with pytest.raises(KalshiError, match="requires authentication"):
                await client.get_account_limits()
        assert recorder.requests == []  # never sent unsigned

    async def test_auth_headers_are_attached_when_required(self, rsa_key):
        payload = {
            "usage_tier": "basic",
            "read": {"refill_rate": 200, "bucket_capacity": 200},
            "write": {"refill_rate": 100, "bucket_capacity": 100},
        }
        recorder = Recorder([json_response(payload)])
        client = KalshiReadOnlyClient.authenticated(
            signer=make_signer(rsa_key),
            clock=FrozenClock(T0),
            transport=recorder.transport,
            retry_policy=RetryPolicy(max_attempts=1),
        )
        async with client:
            limits = await client.get_account_limits()
        assert limits.usage_tier == "basic"
        headers = recorder.requests[0].headers
        assert headers[HEADER_KEY] == "test-key"
        assert headers[HEADER_SIGNATURE]

    async def test_public_calls_are_not_signed_even_with_credentials(self, rsa_key):
        recorder = Recorder([fixture_response("market_linear_cent.json")])
        client = KalshiReadOnlyClient.authenticated(
            signer=make_signer(rsa_key),
            clock=FrozenClock(T0),
            transport=recorder.transport,
            retry_policy=RetryPolicy(max_attempts=1),
        )
        async with client:
            await client.get_market("X")
        assert HEADER_KEY not in recorder.requests[0].headers

    async def test_signed_path_includes_the_api_prefix(self, rsa_key):
        payload = {"default_cost": 10, "endpoint_costs": []}
        recorder = Recorder([json_response(payload)])
        signer = make_signer(rsa_key)
        client = KalshiReadOnlyClient.authenticated(
            signer=signer,
            clock=FrozenClock(T0),
            transport=recorder.transport,
            retry_policy=RetryPolicy(max_attempts=1),
        )
        async with client:
            await client.get_endpoint_costs()
        # Reproduce the signature the client should have produced.
        timestamp = int(recorder.requests[0].headers[HEADER_TIMESTAMP])
        expected = signer.sign(
            "GET", "/trade-api/v2/account/endpoint_costs", timestamp_ms=timestamp
        )
        assert expected.signed_message == f"{timestamp}GET/trade-api/v2/account/endpoint_costs"


class TestRetries:
    async def test_retries_a_429_then_succeeds(self):
        recorder = Recorder(
            [httpx.Response(429, text="{}"), fixture_response("market_linear_cent.json")]
        )
        client = KalshiReadOnlyClient.public(
            clock=FrozenClock(T0),
            transport=recorder.transport,
            retry_policy=RetryPolicy(max_attempts=3, base_delay_s=0.0, jitter=0.0),
        )
        async with client:
            market = await client.get_market("X")
        assert market.ticker
        assert len(recorder.requests) == 2

    async def test_gives_up_after_max_attempts(self):
        recorder = Recorder([httpx.Response(429, text="{}")])
        client = KalshiReadOnlyClient.public(
            clock=FrozenClock(T0),
            transport=recorder.transport,
            retry_policy=RetryPolicy(max_attempts=3, base_delay_s=0.0, jitter=0.0),
        )
        async with client:
            with pytest.raises(KalshiRateLimitError):
                await client.get_market("X")
        assert len(recorder.requests) == 3

    async def test_does_not_retry_a_404(self):
        recorder = Recorder([httpx.Response(404, text="{}")])
        client = KalshiReadOnlyClient.public(
            clock=FrozenClock(T0),
            transport=recorder.transport,
            retry_policy=RetryPolicy(max_attempts=3, base_delay_s=0.0, jitter=0.0),
        )
        async with client:
            with pytest.raises(KalshiNotFoundError):
                await client.get_market("X")
        assert len(recorder.requests) == 1

    async def test_each_retry_is_signed_afresh(self, rsa_key):
        """A retry must never replay a stale timestamp or signature."""
        payload = {"default_cost": 10, "endpoint_costs": []}
        recorder = Recorder([httpx.Response(429, text="{}"), json_response(payload)])
        clock = FrozenClock(T0)
        signer = KalshiSigner(KalshiCredentials(api_key_id="test-key", private_key=rsa_key), clock)

        class AdvancingPolicy(RetryPolicy):
            def delay_for(self, attempt: int, error: Exception | None = None) -> float:  # noqa: ARG002
                clock.advance_ms(1000)  # simulate time passing during backoff
                return 0.0

        client = KalshiReadOnlyClient.authenticated(
            signer=signer,
            clock=clock,
            transport=recorder.transport,
            retry_policy=AdvancingPolicy(max_attempts=3, base_delay_s=0.0, jitter=0.0),
        )
        async with client:
            await client.get_endpoint_costs()

        first, second = recorder.requests[0].headers, recorder.requests[1].headers
        assert first[HEADER_TIMESTAMP] != second[HEADER_TIMESTAMP]
        assert first[HEADER_SIGNATURE] != second[HEADER_SIGNATURE]

    def test_retry_policy_refuses_non_read_methods(self):
        policy = RetryPolicy()
        error = KalshiRateLimitError("limited", endpoint="/x")
        assert policy.is_retryable("GET", error)
        assert not policy.is_retryable("POST", error)
        assert not policy.is_retryable("DELETE", error)

    def test_backoff_grows_and_is_capped(self):
        policy = RetryPolicy(base_delay_s=1.0, max_delay_s=4.0, jitter=0.0)
        assert policy.delay_for(1) == 1.0
        assert policy.delay_for(2) == 2.0
        assert policy.delay_for(3) == 4.0
        assert policy.delay_for(10) == 4.0

    def test_jitter_spreads_retries(self):
        policy = RetryPolicy(base_delay_s=1.0, jitter=0.5)
        delays = {policy.delay_for(2) for _ in range(20)}
        assert len(delays) > 1

    def test_invalid_max_attempts_rejected(self):
        with pytest.raises(ValueError, match="at least 1"):
            RetryPolicy(max_attempts=0)


class TestPaginationIntegration:
    async def test_iter_markets_follows_cursors(self):
        page1 = json_response(
            {
                "markets": [
                    {
                        "ticker": "A",
                        "event_ticker": "E",
                        "market_type": "binary",
                        "status": "active",
                    }
                ],
                "cursor": "NEXT",
            }
        )
        page2 = json_response(
            {
                "markets": [
                    {
                        "ticker": "B",
                        "event_ticker": "E",
                        "market_type": "binary",
                        "status": "active",
                    }
                ],
                "cursor": "",
            }
        )
        recorder = Recorder([page1, page2])
        async with client_with(recorder) as client:
            tickers = [m.ticker async for m in client.iter_markets(limit=1)]
        assert tickers == ["A", "B"]
        assert recorder.requests[1].url.params["cursor"] == "NEXT"

    async def test_filters_are_passed_through_as_query_params(self):
        recorder = Recorder([json_response({"markets": [], "cursor": None})])
        async with client_with(recorder) as client:
            _ = [m async for m in client.iter_markets(series_ticker="KXHIGHNY", status="open")]
        params = recorder.requests[0].url.params
        assert params["series_ticker"] == "KXHIGHNY"
        assert params["status"] == "open"


class TestRateLimitDiscovery:
    async def test_discovers_limits_and_costs_from_the_venue(self, rsa_key):
        limits_payload = {
            "usage_tier": "advanced",
            "read": {"refill_rate": 300, "bucket_capacity": 600},
            "write": {"refill_rate": 300, "bucket_capacity": 600},
        }
        costs_payload = {
            "default_cost": 10,
            "endpoint_costs": [{"method": "GET", "path": "/markets", "cost": 2}],
        }
        recorder = Recorder([json_response(limits_payload), json_response(costs_payload)])
        client = KalshiReadOnlyClient.authenticated(
            signer=make_signer(rsa_key),
            clock=FrozenClock(T0),
            transport=recorder.transport,
            retry_policy=RetryPolicy(max_attempts=1),
        )
        async with client:
            discovered = await client.discover_rate_limits()
        assert discovered.usage_tier == "advanced"
        assert discovered.read.bucket_capacity == 600
        assert discovered.costs.is_discovered
        assert discovered.costs.cost_for("GET", "/markets") == 2
        assert discovered.costs.cost_for("GET", "/events") == 10

    async def test_missing_buckets_raise_schema_error(self, rsa_key):
        recorder = Recorder(
            [json_response({"usage_tier": "basic"}), json_response({"default_cost": 10})]
        )
        client = KalshiReadOnlyClient.authenticated(
            signer=make_signer(rsa_key),
            clock=FrozenClock(T0),
            transport=recorder.transport,
            retry_policy=RetryPolicy(max_attempts=1),
        )
        async with client:
            with pytest.raises(KalshiSchemaError, match="read and write buckets"):
                await client.discover_rate_limits()


class TestLogRedaction:
    def test_safe_log_fields_redact_auth_headers(self):
        fields = safe_request_log_fields(
            method="get",
            endpoint="/markets",
            status=200,
            headers={
                HEADER_KEY: "secret-id",
                HEADER_SIGNATURE: "sig",
                "Accept": "application/json",
            },
        )
        assert fields["headers"][HEADER_KEY] == "<redacted>"
        assert fields["headers"][HEADER_SIGNATURE] == "<redacted>"
        assert fields["headers"]["Accept"] == "application/json"
        assert fields["method"] == "GET"

    def test_client_repr_does_not_leak(self, rsa_key):
        client = KalshiReadOnlyClient.authenticated(
            signer=make_signer(rsa_key), clock=FrozenClock(T0)
        )
        assert "test-key" not in repr(client)


class TestLifecycle:
    async def test_context_manager_closes(self):
        recorder = Recorder([fixture_response("market_linear_cent.json")])
        client = client_with(recorder)
        async with client:
            await client.get_market("X")
        # Closing twice must be safe.
        await client.aclose()
