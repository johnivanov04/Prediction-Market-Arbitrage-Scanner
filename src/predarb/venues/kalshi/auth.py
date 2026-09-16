"""Kalshi request signing.

One signing primitive serves both REST and the WebSocket handshake. They differ
only in the path they sign, so duplicating the implementation would let the two
drift apart -- and a signature bug that appears on only one transport is
miserable to find.

The protocol
------------
Three headers accompany every authenticated request::

    KALSHI-ACCESS-KEY        the API key id
    KALSHI-ACCESS-TIMESTAMP  unix milliseconds
    KALSHI-ACCESS-SIGNATURE  base64 RSA-PSS signature

The signed message is the concatenation::

    str(timestamp_ms) + METHOD + path

where ``METHOD`` is canonical uppercase and ``path`` is the full request path
starting at ``/trade-api/...`` with **the query string removed** and no
hostname. Signature parameters: RSA-PSS, SHA-256 digest, MGF1-SHA256, salt
length equal to the digest length, base64 encoded.

The WebSocket handshake signs the *fixed* path ``/trade-api/ws/v2`` with method
``GET``, regardless of environment -- it is not derived from the connection URL.

Secret handling
---------------
The private key is loaded from a filesystem path and never leaves this module
in serialised form. :class:`KalshiCredentials` overrides ``__repr__`` and
``__str__`` so a credential object cannot leak into a log line, an exception or
a traceback frame repr. Signatures and auth headers are redacted by
:func:`redact_headers`, which every logging path must use.
"""

from __future__ import annotations

import base64
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Self
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from predarb.clock import Clock

__all__ = [
    "HEADER_KEY",
    "HEADER_SIGNATURE",
    "HEADER_TIMESTAMP",
    "REDACTED_HEADERS",
    "WS_SIGNING_PATH",
    "AuthHeaders",
    "KalshiCredentials",
    "KalshiSigner",
    "PrivateKeyError",
    "redact_headers",
    "signing_path",
]

HEADER_KEY: Final = "KALSHI-ACCESS-KEY"
HEADER_TIMESTAMP: Final = "KALSHI-ACCESS-TIMESTAMP"
HEADER_SIGNATURE: Final = "KALSHI-ACCESS-SIGNATURE"

REDACTED_HEADERS: Final[frozenset[str]] = frozenset(
    {
        HEADER_KEY.lower(),
        HEADER_TIMESTAMP.lower(),
        HEADER_SIGNATURE.lower(),
        "authorization",
        "cookie",
        "set-cookie",
        "proxy-authorization",
    }
)

WS_SIGNING_PATH: Final = "/trade-api/ws/v2"
"""The WebSocket handshake signs this fixed path, not the connection URL's."""

_API_PATH_PREFIX: Final = "/trade-api/"


class PrivateKeyError(Exception):
    """The private key could not be loaded or is not an RSA key.

    Deliberately carries no key material: the message names the path and the
    problem, never the file's contents.
    """


def signing_path(url_or_path: str) -> str:
    """Return the exact path to sign, from a full URL or a bare path.

    Strips scheme, host and query string. This is the single place signing
    paths are constructed, because getting it wrong produces a signature that
    is valid for a message the server never computes, and the resulting 401
    looks identical to a bad key.

    ``https://external-api.kalshi.com/trade-api/v2/markets?limit=100&cursor=abc``
    signs as ``/trade-api/v2/markets`` -- not ``/markets``, and not the path
    with its query attached.
    """
    parts = urlsplit(url_or_path)
    path = parts.path
    if not path.startswith(_API_PATH_PREFIX):
        raise ValueError(
            f"refusing to sign {path!r}: a Kalshi signing path must begin with "
            f"{_API_PATH_PREFIX!r}. Pass the full request path (or full URL), not "
            "a path relative to the API base."
        )
    return path


def canonical_method(method: str) -> str:
    """Uppercase an HTTP method, rejecting anything that is not a bare token.

    Canonicalising rather than rejecting lowercase keeps callers simple, but
    the value still has to be a plain method name: a method containing a space
    or a newline could otherwise shift bytes into the signed message and change
    what was actually signed.
    """
    if not method or not method.isascii() or not method.isalpha():
        raise ValueError(f"invalid HTTP method {method!r}: expected letters only")
    return method.upper()


def build_signing_message(timestamp_ms: int, method: str, url_or_path: str) -> str:
    """Compose the exact string that gets signed."""
    if isinstance(timestamp_ms, bool) or not isinstance(timestamp_ms, int):
        raise TypeError(f"timestamp_ms must be an int, got {type(timestamp_ms).__name__}")
    return f"{timestamp_ms}{canonical_method(method)}{signing_path(url_or_path)}"


@dataclass(frozen=True, slots=True)
class AuthHeaders:
    """The three headers for one signed request, plus what they were signed over.

    ``signed_message`` is kept for tests and for diagnosing a signature
    mismatch. It contains no secret -- only a timestamp, a method and a path --
    but the signature itself is redacted from any rendering of this object.
    """

    api_key_id: str
    timestamp_ms: int
    signature: str
    signed_message: str

    def as_dict(self) -> dict[str, str]:
        return {
            HEADER_KEY: self.api_key_id,
            HEADER_TIMESTAMP: str(self.timestamp_ms),
            HEADER_SIGNATURE: self.signature,
        }

    def __repr__(self) -> str:
        return (
            f"AuthHeaders(api_key_id=<redacted>, timestamp_ms={self.timestamp_ms}, "
            f"signature=<redacted>, signed_message={self.signed_message!r})"
        )


def redact_headers(headers: object) -> dict[str, str]:
    """Return headers with credential values replaced by ``<redacted>``.

    Every logging or error path that touches headers must go through this. The
    match is case-insensitive because HTTP header names are.
    """
    if not isinstance(headers, dict):
        try:
            items = dict(headers)  # type: ignore[call-overload]
        except (TypeError, ValueError):
            return {}
    else:
        items = headers
    return {
        str(name): ("<redacted>" if str(name).lower() in REDACTED_HEADERS else str(value))
        for name, value in items.items()
    }


def _warn_on_permissive_key_file(path: Path) -> str | None:
    """Return a warning if the key file looks world/group readable.

    Best effort by design. Permission bits mean different things across
    platforms and filesystems (network mounts, Windows, containers), so this
    warns and never blocks -- a false refusal to start would be worse than a
    missing warning.
    """
    try:
        mode = path.stat().st_mode
    except OSError:
        return None
    if mode & (stat.S_IRGRP | stat.S_IROTH | stat.S_IWGRP | stat.S_IWOTH):
        return (
            f"private key file {path} is readable or writable beyond its owner "
            f"(mode {stat.filemode(mode)}); consider chmod 600"
        )
    return None


@dataclass(frozen=True, slots=True)
class KalshiCredentials:
    """An API key id and its RSA private key.

    Never rendered. ``__repr__`` and ``__str__`` are overridden so that a
    credential object appearing in a log line, an f-string, an exception
    message or a traceback frame cannot disclose either field.
    """

    api_key_id: str
    private_key: rsa.RSAPrivateKey
    permission_warning: str | None = None

    @classmethod
    def from_file(
        cls, *, api_key_id: str, private_key_path: Path | str, password: bytes | None = None
    ) -> Self:
        """Load an RSA private key from a PEM file.

        The key stays on disk until this point and is never copied into
        configuration, fixtures or the repository.
        """
        path = Path(private_key_path).expanduser()
        if not api_key_id:
            raise PrivateKeyError("api_key_id is empty")
        if not path.is_file():
            raise PrivateKeyError(f"private key file not found: {path}")
        warning = _warn_on_permissive_key_file(path)
        try:
            key = serialization.load_pem_private_key(path.read_bytes(), password=password)
        except (ValueError, TypeError) as exc:
            # Deliberately does not echo the file contents or the underlying
            # message, either of which could quote key material.
            raise PrivateKeyError(
                f"could not load a PEM private key from {path} "
                f"({type(exc).__name__}); check the file is an unencrypted "
                "PEM-encoded RSA private key"
            ) from None
        if not isinstance(key, rsa.RSAPrivateKey):
            raise PrivateKeyError(
                f"key at {path} is {type(key).__name__}, but Kalshi requires an RSA key"
            )
        return cls(api_key_id=api_key_id, private_key=key, permission_warning=warning)

    def __repr__(self) -> str:
        return "KalshiCredentials(api_key_id=<redacted>, private_key=<redacted>)"

    def __str__(self) -> str:
        return self.__repr__()


class KalshiSigner:
    """Signs requests for one set of credentials.

    Takes a :class:`~predarb.clock.Clock` rather than reading the wall clock
    itself. Authentication depends on a timestamp, so hidden ``datetime.now()``
    calls would make signing untestable and would leave no seam for future
    clock-skew correction.
    """

    __slots__ = ("_clock", "_credentials")

    def __init__(self, credentials: KalshiCredentials, clock: Clock) -> None:
        self._credentials = credentials
        self._clock = clock

    @property
    def api_key_id(self) -> str:
        return self._credentials.api_key_id

    @property
    def permission_warning(self) -> str | None:
        return self._credentials.permission_warning

    def current_timestamp_ms(self) -> int:
        return int(self._clock.now().timestamp() * 1000)

    def sign(
        self, method: str, url_or_path: str, *, timestamp_ms: int | None = None
    ) -> AuthHeaders:
        """Produce auth headers for one request.

        ``timestamp_ms`` is normally omitted and read from the clock. Every call
        signs afresh: a retry must never reuse a previous signature, because the
        timestamp is part of the signed message and a stale one reads to the
        server as a replay.
        """
        stamp = self.current_timestamp_ms() if timestamp_ms is None else timestamp_ms
        message = build_signing_message(stamp, method, url_or_path)
        signature = self._credentials.private_key.sign(
            message.encode("utf-8"),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return AuthHeaders(
            api_key_id=self._credentials.api_key_id,
            timestamp_ms=stamp,
            signature=base64.b64encode(signature).decode("ascii"),
            signed_message=message,
        )

    def rest_headers(self, method: str, url_or_path: str) -> dict[str, str]:
        """Auth headers for a REST call."""
        return self.sign(method, url_or_path).as_dict()

    def websocket_headers(self) -> dict[str, str]:
        """Auth headers for the WebSocket handshake.

        Always signs ``GET`` over the fixed path ``/trade-api/ws/v2``; the
        connection URL's own path is not used (see ``docs/api_assumptions.md``
        A-03).
        """
        return self.sign("GET", WS_SIGNING_PATH).as_dict()

    def __repr__(self) -> str:
        return "KalshiSigner(credentials=<redacted>)"
