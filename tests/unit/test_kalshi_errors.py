"""Tests for error classification and credential-safe diagnostics."""

from __future__ import annotations

import pytest

from predarb.venues.kalshi.auth import HEADER_KEY, HEADER_SIGNATURE
from predarb.venues.kalshi.errors import (
    KalshiAuthenticationError,
    KalshiAuthorizationError,
    KalshiClientError,
    KalshiClockSkewError,
    KalshiError,
    KalshiNotFoundError,
    KalshiRateLimitError,
    KalshiSchemaError,
    KalshiServerError,
    KalshiTimeoutError,
    KalshiTransportError,
    classify_http_error,
)

pytestmark = pytest.mark.unit


class TestClassification:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (400, KalshiClientError),
            (401, KalshiAuthenticationError),
            (403, KalshiAuthorizationError),
            (404, KalshiNotFoundError),
            (409, KalshiClientError),
            (429, KalshiRateLimitError),
            (500, KalshiServerError),
            (502, KalshiServerError),
            (503, KalshiServerError),
        ],
    )
    def test_status_maps_to_type(self, status, expected):
        error = classify_http_error(status_code=status, endpoint="/markets")
        assert isinstance(error, expected)
        assert error.status_code == status

    def test_all_errors_share_a_base(self):
        assert isinstance(classify_http_error(status_code=404, endpoint="/x"), KalshiError)

    def test_timeout_is_a_transport_error(self):
        assert issubclass(KalshiTimeoutError, KalshiTransportError)

    def test_clock_skew_is_an_authentication_error(self):
        # So a caller can catch the general case without knowing the subtype.
        assert issubclass(KalshiClockSkewError, KalshiAuthenticationError)


class TestAuthenticationCauseSeparation:
    """The remedies differ completely, so the causes are separated."""

    @pytest.mark.parametrize(
        "body",
        [
            '{"error":"timestamp too old"}',
            '{"error":"request timestamp is in the future"}',
            '{"error":"signature expired"}',
            '{"message":"clock skew detected"}',
        ],
    )
    def test_timestamp_wording_signals_clock_skew(self, body):
        error = classify_http_error(status_code=401, endpoint="/account/limits", body=body)
        assert isinstance(error, KalshiClockSkewError)

    @pytest.mark.parametrize(
        "body", ['{"error":"invalid signature"}', '{"error":"unknown key"}', ""]
    )
    def test_other_wording_stays_a_plain_auth_error(self, body):
        error = classify_http_error(status_code=401, endpoint="/account/limits", body=body)
        assert isinstance(error, KalshiAuthenticationError)
        assert not isinstance(error, KalshiClockSkewError)

    def test_no_tolerance_window_is_asserted(self):
        # Current docs state no tolerance, so the message must not invent one.
        error = classify_http_error(
            status_code=401, endpoint="/x", body='{"error":"timestamp too old"}'
        )
        message = str(error)
        for invented in ("seconds", "minute", "window", "tolerance"):
            assert invented not in message.lower()

    def test_auth_error_message_names_the_likely_causes(self):
        message = str(classify_http_error(status_code=401, endpoint="/x"))
        assert "signing path" in message
        assert "environment" in message

    def test_authorization_error_points_at_scope(self):
        assert "scope" in str(classify_http_error(status_code=403, endpoint="/x"))


class TestRateLimitError:
    def test_no_retry_after_by_default(self):
        # Current docs: 429s carry neither Retry-After nor X-RateLimit-*.
        error = classify_http_error(status_code=429, endpoint="/markets")
        assert isinstance(error, KalshiRateLimitError)
        assert error.retry_after_s is None

    def test_reads_retry_after_if_it_appears(self):
        error = classify_http_error(
            status_code=429, endpoint="/markets", headers={"Retry-After": "2.5"}
        )
        assert isinstance(error, KalshiRateLimitError)
        assert error.retry_after_s == 2.5

    def test_unparseable_retry_after_is_ignored(self):
        error = classify_http_error(
            status_code=429, endpoint="/markets", headers={"Retry-After": "soon"}
        )
        assert isinstance(error, KalshiRateLimitError)
        assert error.retry_after_s is None


class TestCredentialSafety:
    def test_auth_headers_never_reach_the_error(self):
        error = classify_http_error(
            status_code=429,
            endpoint="/markets",
            headers={HEADER_KEY: "secret-id", HEADER_SIGNATURE: "secret-sig"},
        )
        rendered = str(error)
        assert "secret-id" not in rendered
        assert "secret-sig" not in rendered

    def test_errors_do_not_store_headers_at_all(self):
        error = classify_http_error(
            status_code=500, endpoint="/markets", headers={HEADER_SIGNATURE: "sig"}
        )
        assert not hasattr(error, "headers")
        assert not hasattr(error, "request_headers")

    def test_body_excerpt_is_bounded(self):
        error = classify_http_error(status_code=500, endpoint="/x", body="A" * 5000)
        assert len(error.body_excerpt) <= 500


class TestDiagnostics:
    def test_endpoint_template_is_recorded(self):
        error = classify_http_error(status_code=404, endpoint="/markets/{ticker}/orderbook")
        # A template rather than a concrete path, so errors aggregate usefully.
        assert error.endpoint == "/markets/{ticker}/orderbook"
        assert "{ticker}" in str(error)

    def test_request_id_is_included_when_present(self):
        error = classify_http_error(status_code=500, endpoint="/x", request_id="abc123")
        assert "abc123" in str(error)
        assert error.request_id == "abc123"

    def test_body_excerpt_appears_in_the_message(self):
        error = classify_http_error(status_code=400, endpoint="/x", body='{"error":"bad ticker"}')
        assert "bad ticker" in str(error)

    def test_schema_error_is_not_an_http_error(self):
        # A 200 we cannot parse needs a code change, not a retry.
        error = KalshiSchemaError("bad shape", endpoint="/markets")
        assert isinstance(error, KalshiError)
        assert not hasattr(error, "status_code")

    def test_schema_error_records_endpoint_and_excerpt(self):
        error = KalshiSchemaError("bad shape", endpoint="/markets", body_excerpt="x" * 900)
        assert error.endpoint == "/markets"
        assert len(error.body_excerpt) <= 500
