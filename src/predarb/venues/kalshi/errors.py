"""Typed Kalshi transport errors.

Two concerns shape this module.

**Diagnosability.** A failure should say enough to act on: which endpoint, which
status, what the venue said. Endpoints are recorded as *templates*
(``/markets/{ticker}/orderbook``) rather than concrete paths so that errors
aggregate usefully in logs and metrics.

**Credential safety.** Errors are the classic leak path -- a traceback that
dumps the request often dumps its headers with it. No error here carries request
headers, and the one place that accepts them runs them through
:func:`~predarb.venues.kalshi.auth.redact_headers` first.

Authentication failures are separated by likely *cause*, because the remedies
differ completely: a bad key means fix your credentials, a clock problem means
fix your clock, and a scope problem means the key works but is not allowed to do
this. Guessing between them wastes a lot of time.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any, Final

from predarb.venues.kalshi.auth import redact_headers

__all__ = [
    "KalshiAuthenticationError",
    "KalshiAuthorizationError",
    "KalshiClientError",
    "KalshiClockSkewError",
    "KalshiError",
    "KalshiNotFoundError",
    "KalshiRateLimitError",
    "KalshiSchemaError",
    "KalshiServerError",
    "KalshiTimeoutError",
    "KalshiTransportError",
    "classify_http_error",
]

_MAX_BODY_EXCERPT = 500


class KalshiError(Exception):
    """Base for every Kalshi adapter failure."""


class KalshiTransportError(KalshiError):
    """A request failed at the network layer (connection, DNS, TLS, protocol)."""


class KalshiTimeoutError(KalshiTransportError):
    """A request exceeded its timeout budget."""


class KalshiHttpError(KalshiError):
    """Base for failures carrying an HTTP response.

    Holds the status, the endpoint template and a bounded excerpt of the body.
    It deliberately holds **no request headers**: the body of a Kalshi error is
    a JSON error document, whereas the headers are where the signature lives.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        endpoint: str,
        body_excerpt: str = "",
        request_id: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.endpoint = endpoint
        self.body_excerpt = body_excerpt[:_MAX_BODY_EXCERPT]
        self.request_id = request_id
        detail = f"{message} (HTTP {status_code} on {endpoint}"
        if request_id:
            detail += f", request_id={request_id}"
        detail += ")"
        if self.body_excerpt:
            detail += f": {self.body_excerpt}"
        super().__init__(detail)


class KalshiClientError(KalshiHttpError):
    """A 4xx that is not one of the more specific cases below."""


class KalshiAuthenticationError(KalshiHttpError):
    """401 -- the request was not accepted as authentic.

    Usually one of: no credentials sent, wrong API key id, a signature over the
    wrong message (the signing path is the usual culprit), or credentials from
    the other environment. Kalshi credentials are environment-specific, so a
    production key against demo fails exactly like a bad key.
    """


class KalshiClockSkewError(KalshiAuthenticationError):
    """401 that looks like a timestamp problem rather than a bad key.

    Raised only when the venue's own error text points at the timestamp. We do
    **not** assert a tolerance window: the current documentation does not state
    one, so claiming a number would be inventing a guarantee.
    """


class KalshiAuthorizationError(KalshiHttpError):
    """403 -- authenticated, but not permitted.

    In Phase 1 this should only ever mean the key lacks read scope for the
    endpoint. Phase 1 never calls a write endpoint, so a 403 is not an
    accidental trading attempt.
    """


class KalshiNotFoundError(KalshiHttpError):
    """404 -- no such resource, or a path that does not exist.

    Worth reading literally: a guessed path returns this. ``/series/{t}/fee_changes``
    404s while ``/series/fee_changes?series_ticker=`` succeeds.
    """


class KalshiRateLimitError(KalshiHttpError):
    """429 -- the token bucket was empty.

    Carries no penalty on Kalshi: the bucket keeps refilling, so backing off and
    retrying is the documented-correct response. Current docs state 429s do not
    carry ``Retry-After`` or ``X-RateLimit-*`` headers, so ``retry_after_s`` is
    populated only if one unexpectedly appears.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 429,
        endpoint: str,
        body_excerpt: str = "",
        request_id: str | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(
            message,
            status_code=status_code,
            endpoint=endpoint,
            body_excerpt=body_excerpt,
            request_id=request_id,
        )
        self.retry_after_s = retry_after_s


class KalshiServerError(KalshiHttpError):
    """5xx -- the venue failed. Safe to retry a read."""


class KalshiSchemaError(KalshiError):
    """The response was not JSON, or did not match the expected schema.

    Distinct from an HTTP error on purpose: a 200 whose shape we cannot parse
    means the venue changed something, and that needs a code change rather than
    a retry.
    """

    def __init__(self, message: str, *, endpoint: str, body_excerpt: str = "") -> None:
        self.endpoint = endpoint
        self.body_excerpt = body_excerpt[:_MAX_BODY_EXCERPT]
        detail = f"{message} (endpoint {endpoint})"
        if self.body_excerpt:
            detail += f": {self.body_excerpt}"
        super().__init__(detail)


_CLOCK_HINTS = ("timestamp", "clock", "expired", "too old", "skew", "future")

_SIMPLE_STATUS_ERRORS: Final[dict[int, tuple[type[KalshiHttpError], str]]] = {
    HTTPStatus.FORBIDDEN: (
        KalshiAuthorizationError,
        "authenticated but not permitted -- the key likely lacks read scope for this endpoint",
    ),
    HTTPStatus.NOT_FOUND: (KalshiNotFoundError, "not found"),
}


def _classify_unauthorized(body: str) -> type[KalshiHttpError]:
    """Distinguish a probable clock problem from a probable credential problem.

    Only the venue's own wording drives this. We do not compare local time to a
    tolerance window, because current documentation states no tolerance and
    inventing one would be asserting a guarantee we cannot support.
    """
    lowered = body.lower()
    if any(hint in lowered for hint in _CLOCK_HINTS):
        return KalshiClockSkewError
    return KalshiAuthenticationError


def _retry_after_seconds(safe_headers: dict[str, str]) -> float | None:
    """Read ``Retry-After`` if present.

    Current docs say 429s carry neither ``Retry-After`` nor ``X-RateLimit-*``,
    so this normally returns ``None`` and the caller falls back to its own
    backoff. It is read anyway in case the venue starts sending one.
    """
    for name, value in safe_headers.items():
        if name.lower() == "retry-after":
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    return None


def classify_http_error(
    *,
    status_code: int,
    endpoint: str,
    body: str = "",
    headers: Any = None,
    request_id: str | None = None,
) -> KalshiHttpError:
    """Map an HTTP failure onto the most specific error type.

    ``headers`` is accepted only to look for a ``Retry-After``; it is redacted
    before anything else touches it, and is never stored on the error.
    """
    excerpt = body[:_MAX_BODY_EXCERPT]
    common = {
        "status_code": status_code,
        "endpoint": endpoint,
        "body_excerpt": excerpt,
        "request_id": request_id,
    }

    if status_code == HTTPStatus.UNAUTHORIZED:
        error_type = _classify_unauthorized(body)
        message = (
            "authentication rejected, and the venue's message mentions the timestamp -- "
            "check local clock sync before assuming a bad key"
            if error_type is KalshiClockSkewError
            else "authentication rejected -- check the API key id, the signing path, and "
            "that the credentials match the environment being called"
        )
        return error_type(message, **common)  # type: ignore[arg-type]

    if status_code == HTTPStatus.TOO_MANY_REQUESTS:
        safe_headers = redact_headers(headers) if headers is not None else {}
        return KalshiRateLimitError(
            "rate limited",
            endpoint=endpoint,
            body_excerpt=excerpt,
            request_id=request_id,
            retry_after_s=_retry_after_seconds(safe_headers),
        )

    simple = _SIMPLE_STATUS_ERRORS.get(status_code)
    if simple is not None:
        error_type, message = simple
        return error_type(message, **common)  # type: ignore[arg-type]

    if status_code >= HTTPStatus.INTERNAL_SERVER_ERROR:
        return KalshiServerError("venue error", **common)  # type: ignore[arg-type]

    return KalshiClientError("request rejected", **common)  # type: ignore[arg-type]
