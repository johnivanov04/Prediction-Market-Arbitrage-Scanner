"""Authenticated Kalshi WebSocket client -- market data only.

Scope
-----
This step establishes the connection, subscribes, and hands back parsed frames.
It deliberately does **not** reconstruct books. Reconstruction depends on
knowing what ``seq`` actually guarantees, and that is still unverified
(``docs/api_assumptions.md`` A-09) -- writing gap detection before the evidence
exists is how a plausible-but-wrong invariant gets baked in.

So this module records ``sid`` and ``seq`` faithfully and interprets neither.
:class:`SequenceObserver` collects evidence *about* them without acting on it.

Authentication
--------------
The handshake carries the same three headers as REST, signed over the fixed
path ``/trade-api/ws/v2`` with method ``GET`` -- not the connection URL's path.
Signing goes through the shared :class:`~predarb.venues.kalshi.auth.KalshiSigner`,
so REST and WebSocket cannot drift apart.

The socket requires credentials even for public market-data channels: an
unauthenticated connect is rejected with HTTP 401 (verified, A-24).

Logging
-------
Handshake headers are never logged. Raw frames may be logged at DEBUG, and they
are safe to log because a market-data frame carries no credential -- but the
headers that opened the connection are redacted at every point they could be
rendered.
"""

from __future__ import annotations

import asyncio
import json
import ssl
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from itertools import pairwise
from types import TracebackType
from typing import Any, Final, Self

import certifi
import websockets
from websockets.asyncio.client import ClientConnection

from predarb.clock import Clock, SystemClock
from predarb.config import KalshiEndpoints, KalshiEnv
from predarb.logging import get_logger
from predarb.venues.kalshi.auth import KalshiSigner
from predarb.venues.kalshi.errors import KalshiError, KalshiTransportError
from predarb.venues.kalshi.models import KalshiWsEnvelope, decode_json

__all__ = [
    "ORDERBOOK_DELTA_CHANNEL",
    "KalshiWebSocketClient",
    "ObservedFrame",
    "SequenceObserver",
    "WebSocketAuthError",
]

logger = get_logger(__name__)

ORDERBOOK_DELTA_CHANNEL: Final = "orderbook_delta"
_DEFAULT_OPEN_TIMEOUT: Final = 20.0
_DEFAULT_RECV_TIMEOUT: Final = 30.0


class WebSocketAuthError(KalshiError):
    """The handshake was rejected.

    Most often: no credentials, credentials from the other environment, or a
    signature over the wrong path. Kalshi keys are environment-specific, so a
    production key against demo fails exactly like an invalid key.
    """


@dataclass(frozen=True, slots=True)
class ObservedFrame:
    """One received frame with local provenance.

    Keeps the raw text alongside the parsed envelope so an observation can be
    written to a fixture byte-for-byte, and so a parse failure is still
    evidence rather than a lost message.
    """

    received_at: datetime
    raw: str
    envelope: KalshiWsEnvelope | None
    parse_error: str | None = None

    @property
    def parsed(self) -> bool:
        return self.envelope is not None


@dataclass
class SequenceObserver:
    """Collects ``sid``/``seq`` evidence without acting on it.

    This is the instrument for the A-09 experiments. It records what was seen
    per subscription and per market and computes descriptive statistics; it
    makes **no** claim about what ``seq`` guarantees and performs no gap
    detection. Deciding that a missing number is a gap requires knowing the
    scope of the counter, which is exactly what is still unknown.
    """

    per_sid: dict[int, list[int]] = field(default_factory=dict)
    per_sid_market: dict[tuple[int, str], list[int]] = field(default_factory=dict)
    per_market: dict[str, list[int]] = field(default_factory=dict)
    frame_types: dict[str, int] = field(default_factory=dict)
    arrival_order: list[tuple[int | None, int | None, str, str]] = field(default_factory=list)

    def observe(self, envelope: KalshiWsEnvelope) -> None:
        self.frame_types[envelope.type] = self.frame_types.get(envelope.type, 0) + 1
        ticker = ""
        if envelope.msg:
            raw_ticker = envelope.msg.get("market_ticker")
            ticker = raw_ticker if isinstance(raw_ticker, str) else ""
        self.arrival_order.append((envelope.sid, envelope.seq, ticker, envelope.type))
        if envelope.seq is None:
            return
        if envelope.sid is not None:
            self.per_sid.setdefault(envelope.sid, []).append(envelope.seq)
            if ticker:
                self.per_sid_market.setdefault((envelope.sid, ticker), []).append(envelope.seq)
        if ticker:
            self.per_market.setdefault(ticker, []).append(envelope.seq)

    @staticmethod
    def _describe(values: Sequence[int]) -> dict[str, Any]:
        if not values:
            return {"count": 0}
        ordered = list(values)
        strictly_increasing = all(b > a for a, b in pairwise(ordered))
        contiguous = all(b == a + 1 for a, b in pairwise(ordered))
        return {
            "count": len(ordered),
            "first": ordered[0],
            "last": ordered[-1],
            "min": min(ordered),
            "max": max(ordered),
            "distinct": len(set(ordered)),
            "strictly_increasing": strictly_increasing,
            "contiguous_plus_one": contiguous,
            "span": max(ordered) - min(ordered) + 1,
        }

    def report(self) -> dict[str, Any]:
        """A descriptive summary. Describes; concludes nothing."""
        return {
            "frame_types": dict(self.frame_types),
            "per_sid": {sid: self._describe(v) for sid, v in sorted(self.per_sid.items())},
            "per_sid_market": {
                f"sid={sid}|{ticker}": self._describe(v)
                for (sid, ticker), v in sorted(self.per_sid_market.items())
            },
            "per_market": {t: self._describe(v) for t, v in sorted(self.per_market.items())},
            "total_frames": len(self.arrival_order),
        }


class KalshiWebSocketClient:
    """Connects, subscribes and yields parsed market-data frames.

    Use as an async context manager so the socket is always closed::

        async with KalshiWebSocketClient(signer=signer) as ws:
            await ws.subscribe_orderbook(["TICKER-A", "TICKER-B"])
            async for frame in ws.frames(limit=50):
                ...
    """

    def __init__(
        self,
        *,
        signer: KalshiSigner,
        env: KalshiEnv = KalshiEnv.PROD,
        url: str | None = None,
        clock: Clock | None = None,
        open_timeout: float = _DEFAULT_OPEN_TIMEOUT,
        recv_timeout: float = _DEFAULT_RECV_TIMEOUT,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self._signer = signer
        self._url = url or KalshiEndpoints.websocket(env)
        self._clock = clock or SystemClock()
        self._open_timeout = open_timeout
        self._recv_timeout = recv_timeout
        # Some Python builds ship without a usable system trust store; certifi
        # is already a transitive dependency via httpx, so use it explicitly
        # rather than failing at connect time with an opaque TLS error.
        self._ssl_context = ssl_context or ssl.create_default_context(cafile=certifi.where())
        self._connection: ClientConnection | None = None
        self._next_command_id = 1
        self.observer = SequenceObserver()

    @property
    def url(self) -> str:
        return self._url

    @property
    def is_connected(self) -> bool:
        return self._connection is not None

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def connect(self) -> None:
        """Open the authenticated socket.

        Handshake headers are built here and never logged. Only the URL and the
        outcome are recorded.
        """
        headers = self._signer.websocket_headers()
        logger.info("kalshi.ws.connecting", url=self._url)
        try:
            self._connection = await websockets.connect(
                self._url,
                additional_headers=headers,
                ssl=self._ssl_context,
                open_timeout=self._open_timeout,
            )
        except websockets.InvalidStatus as exc:
            status = exc.response.status_code
            if status in (401, 403):
                raise WebSocketAuthError(
                    f"WebSocket handshake rejected with HTTP {status}. Check the API "
                    "key id, that the private key matches it, and that the "
                    "credentials belong to the environment being connected to "
                    "(Kalshi keys are environment-specific)."
                ) from None
            raise KalshiTransportError(f"WebSocket handshake failed with HTTP {status}") from None
        except (OSError, websockets.WebSocketException) as exc:
            raise KalshiTransportError(
                f"could not open WebSocket to {self._url}: {type(exc).__name__}"
            ) from None
        logger.info("kalshi.ws.connected", url=self._url)

    async def close(self) -> None:
        """Close cleanly. Safe to call more than once."""
        if self._connection is None:
            return
        try:
            await self._connection.close()
        except (OSError, websockets.WebSocketException):
            logger.debug("kalshi.ws.close_error")
        finally:
            self._connection = None
            logger.info("kalshi.ws.closed")

    def _require_connection(self) -> ClientConnection:
        if self._connection is None:
            raise KalshiError("WebSocket is not connected; call connect() first")
        return self._connection

    def _take_command_id(self) -> int:
        """Client command ids start at 1 and increment; 0 means 'no id'."""
        command_id = self._next_command_id
        self._next_command_id += 1
        return command_id

    async def _send_command(self, command: dict[str, Any]) -> int:
        connection = self._require_connection()
        command_id = self._take_command_id()
        payload = {"id": command_id, **command}
        await connection.send(json.dumps(payload))
        logger.debug("kalshi.ws.command", command=payload.get("cmd"), command_id=command_id)
        return command_id

    async def subscribe_orderbook(self, market_tickers: Sequence[str]) -> int:
        """Subscribe to ``orderbook_delta`` for one or more markets.

        Returns the client command id. The server replies with a ``subscribed``
        frame carrying the server-assigned ``sid``.
        """
        if not market_tickers:
            raise ValueError("at least one market ticker is required")
        return await self._send_command(
            {
                "cmd": "subscribe",
                "params": {
                    "channels": [ORDERBOOK_DELTA_CHANNEL],
                    "market_tickers": list(market_tickers),
                },
            }
        )

    async def update_subscription(
        self, sid: int, *, action: str, market_tickers: Sequence[str]
    ) -> int:
        """Add or drop markets on an existing subscription.

        ``action`` is the venue's own verb (``add_markets`` / ``delete_markets``).
        It is passed through rather than mapped, because the accepted values are
        not fully pinned down in the docs and guessing a synonym would produce a
        confusing server-side error.
        """
        return await self._send_command(
            {
                "cmd": "update_subscription",
                "params": {"sids": [sid], "action": action, "market_tickers": list(market_tickers)},
            }
        )

    async def unsubscribe(self, sids: Sequence[int]) -> int:
        return await self._send_command({"cmd": "unsubscribe", "params": {"sids": list(sids)}})

    async def receive(self) -> ObservedFrame:
        """Receive one frame, parse it, and record it with the observer.

        A frame that fails to parse is still returned, with ``parse_error`` set.
        Discarding it would throw away exactly the evidence needed to understand
        an unexpected message shape.
        """
        connection = self._require_connection()
        try:
            raw = await asyncio.wait_for(connection.recv(), timeout=self._recv_timeout)
        except TimeoutError:
            raise KalshiTransportError(f"no WebSocket frame within {self._recv_timeout}s") from None
        except websockets.WebSocketException as exc:
            raise KalshiTransportError(f"WebSocket receive failed: {type(exc).__name__}") from None

        text = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
        received_at = self._clock.now()
        try:
            envelope = KalshiWsEnvelope.model_validate(decode_json(text))
        except (ValueError, TypeError) as exc:
            logger.warning("kalshi.ws.unparsed_frame", error=type(exc).__name__)
            return ObservedFrame(
                received_at=received_at, raw=text, envelope=None, parse_error=type(exc).__name__
            )
        self.observer.observe(envelope)
        return ObservedFrame(received_at=received_at, raw=text, envelope=envelope)

    async def frames(self, *, limit: int | None = None) -> AsyncIterator[ObservedFrame]:
        """Yield frames until ``limit`` is reached or the socket closes."""
        count = 0
        while limit is None or count < limit:
            try:
                frame = await self.receive()
            except KalshiTransportError:
                return
            count += 1
            yield frame

    def __repr__(self) -> str:
        return f"KalshiWebSocketClient(url={self._url!r}, connected={self.is_connected})"
