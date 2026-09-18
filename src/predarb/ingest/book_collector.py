"""Live collector: one socket, one reader, one authoritative ordering.

The single-reader rule
----------------------
Exactly one coroutine receives from the socket and drives reconstruction, and it
does so synchronously per frame::

    receive -> journal -> sequence -> route -> mutate

Frames are never fanned into concurrent tasks. Arrival order is economically
meaningful -- it is the order the venue applied the updates in -- and any
concurrency inside this loop would reorder mutations while leaving the sequence
numbers looking fine, which is the one failure the whole design exists to
prevent. Parallelism belongs downstream of reconstruction, where consumers hold
immutable views.

Recovery
--------
On an integrity violation the collector drops the connection and opens a new
epoch, resubscribes, and waits for fresh snapshots. It does not attempt to patch
the gap. See :mod:`predarb.books.recovery` for why.

Read-only
---------
Subscribes to public market-data channels and sends nothing else. There is no
order path here or anywhere beneath it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from predarb.books.events import ReconstructionEvent
from predarb.books.reconstruction import OrderBookReconstructor
from predarb.books.recovery import RecoveryAction, RecoveryCoordinator, RecoveryPolicy
from predarb.clock import Clock, SystemClock
from predarb.config import Settings
from predarb.ingest.raw_journal import RawFrameJournal
from predarb.logging import get_logger
from predarb.venues.kalshi.errors import KalshiTransportError
from predarb.venues.kalshi.session import websocket_client
from predarb.venues.kalshi.websocket import (
    KalshiWebSocketClient,
    ObservedFrame,
    WebSocketIdle,
)

__all__ = ["BookCollector", "CollectorStats", "default_journal_path"]

logger = get_logger(__name__)

_IDLE_POLL_S: Final = 5.0
_PING_TIMEOUT_S: Final = 10.0
"""How long to wait for a Pong before treating the socket as unproven."""
"""How long to wait for an application frame before re-checking liveness.

Short so liveness is evaluated often; a timeout here means the market is quiet,
not that the socket is broken."""


@dataclass(slots=True)
class CollectorStats:
    """Run-level counters, distinct from per-connection reconstruction health."""

    connections: int = 0
    reconnects: int = 0
    frames: int = 0
    stopped_reason: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    events: list[ReconstructionEvent] = field(default_factory=list)
    final_books: list[dict[str, object]] = field(default_factory=list)
    """Book state captured while the connection was still live."""


class BookCollector:
    """Drives a live WebSocket feed into reconstructed books.

    One instance owns one socket at a time. Call :meth:`run` and it will connect,
    subscribe, read until told to stop, and reconnect on integrity loss.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        markets: Sequence[str],
        journal_path: Path,
        clock: Clock | None = None,
        recovery_policy: RecoveryPolicy | None = None,
    ) -> None:
        if not markets:
            raise ValueError("at least one market ticker is required")
        self._settings = settings
        self._markets = list(markets)
        self._journal_path = journal_path
        self._clock = clock or SystemClock()
        self._recovery = RecoveryCoordinator(policy=recovery_policy or RecoveryPolicy())
        self.stats = CollectorStats()
        self._reconstructor: OrderBookReconstructor | None = None
        self._journal: RawFrameJournal | None = None
        self._stop = asyncio.Event()
        self._last_reconnect_reason = ""

    @property
    def reconstructor(self) -> OrderBookReconstructor | None:
        return self._reconstructor

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self, *, duration_s: float | None = None) -> CollectorStats:
        """Collect until ``duration_s`` elapses, stop is requested, or recovery gives up."""
        self.stats.started_at = self._clock.now()
        deadline = (
            asyncio.get_running_loop().time() + duration_s if duration_s is not None else None
        )

        while not self._stop.is_set():
            if deadline is not None and asyncio.get_running_loop().time() >= deadline:
                break
            action = await self._run_one_connection(deadline)
            if action is RecoveryAction.STOP:
                self.stats.stopped_reason = (
                    self._recovery.last_request.detail if self._recovery.last_request else "stopped"
                )
                break
            if action is RecoveryAction.RECONNECT:
                request = self._recovery.last_request
                backoff = request.backoff.total_seconds() if request else 1.0
                self.stats.reconnects += 1
                logger.warning(
                    "collector.reconnecting",
                    attempt=request.attempt if request else 0,
                    backoff_s=round(backoff, 2),
                    reason=(request.detail if request else self._last_reconnect_reason),
                )
                await asyncio.sleep(backoff)
                continue
            break  # clean exit

        self.stats.ended_at = self._clock.now()
        return self.stats

    async def _run_one_connection(self, deadline: float | None) -> RecoveryAction:
        """One connection's lifetime: connect, read, tear down, decide next."""
        journal = RawFrameJournal(self._journal_path, connection_epoch=self.stats.connections + 1)
        self._journal = journal
        pending: list[RecoveryAction] = []

        def sink(event: ReconstructionEvent) -> None:
            self.stats.events.append(event)
            if event.is_failure:
                logger.warning("collector.event", **event.log_fields())
            request = self._recovery.observe(event)
            if request is not None and request.action is not RecoveryAction.NONE:
                pending.append(request.action)

        reconstructor = OrderBookReconstructor(journal=journal, event_sink=sink)
        self._reconstructor = reconstructor

        client: KalshiWebSocketClient | None = None
        connection_failed: str | None = None
        try:
            client = websocket_client(self._settings, self._clock)
            await client.connect()
            self.stats.connections += 1
            reconstructor.open_connection(self._clock.now())
            await client.subscribe_orderbook(self._markets)
            connection_failed = await self._read_loop(client, reconstructor, pending, deadline)
        except Exception as exc:
            logger.warning("collector.connection_error", error=type(exc).__name__)
            connection_failed = f"error: {type(exc).__name__}"
        finally:
            # Snapshot the books *before* teardown. Closing the connection
            # correctly invalidates every book it sourced, so a report taken
            # afterwards would show INTEGRITY_UNKNOWN everywhere and say nothing
            # about how the run actually went.
            self.stats.final_books = self._capture_books(reconstructor)
            if client is not None:
                await client.close()
            reconstructor.close_connection(self._clock.now(), reason="collector shutdown")
            journal.close()

        if pending:
            if RecoveryAction.STOP in pending:
                return RecoveryAction.STOP
            return RecoveryAction.RECONNECT
        if self._stop.is_set() or self._deadline_passed(deadline):
            return RecoveryAction.NONE
        if connection_failed is not None:
            self._last_reconnect_reason = connection_failed
            return RecoveryAction.RECONNECT
        return RecoveryAction.NONE

    async def _read_loop(
        self,
        client: KalshiWebSocketClient,
        reconstructor: OrderBookReconstructor,
        pending: list[RecoveryAction],
        deadline: float | None,
    ) -> str | None:
        """Read frames in arrival order. Returns a failure reason, or ``None``.

        This is the single authoritative reader. Each frame is handled to
        completion before the next is taken off the socket, so reconstruction
        sees exactly the order the venue sent.
        """
        sid_seen = False
        idle_polls = 0

        while not self._stop.is_set() and not pending:
            if self._deadline_passed(deadline):
                return None
            try:
                frame = await client.receive(timeout=_IDLE_POLL_S)
            except WebSocketIdle:
                # A quiet market, not a broken socket: the protocol's ping/pong
                # keeps the connection alive with no application traffic, so
                # idleness must not trigger a reconnect. Liveness is judged
                # separately, and only that can end the connection here.
                idle_polls += 1
                # Ask the transport directly rather than inferring health from
                # silence: a Pong proves the socket works even when the market
                # has nothing to say.
                rtt = await client.ping(timeout=_PING_TIMEOUT_S)
                now = self._clock.now()
                if rtt is not None:
                    reconstructor.record_transport_signal(now)
                    continue
                if not reconstructor.connection_is_healthy(now):
                    logger.warning(
                        "collector.liveness_lost",
                        idle_polls=idle_polls,
                        state=reconstructor.liveness_state(now).value,
                    )
                    return "no pong and liveness window exceeded"
                continue
            except KalshiTransportError as exc:
                logger.warning("collector.receive_failed", error=type(exc).__name__)
                return f"transport: {type(exc).__name__}"

            if not sid_seen:
                sid_seen = self._register_subscription(reconstructor, frame)
            reconstructor.handle_frame(frame.raw, frame.received_at)
            self.stats.frames += 1

        return None

    def _register_subscription(
        self, reconstructor: OrderBookReconstructor, frame: ObservedFrame
    ) -> bool:
        """Register the sid the server actually assigned.

        Taken from the server's ``subscribed`` acknowledgement rather than
        assumed, because the server is free to merge same-channel subscriptions
        into an existing sid (A-09).
        """
        envelope = frame.envelope
        if envelope is None or envelope.type != "subscribed" or envelope.msg is None:
            return False
        sid = envelope.msg.get("sid")
        channel = envelope.msg.get("channel")
        if not isinstance(sid, int) or not isinstance(channel, str):
            return False
        reconstructor.note_subscribed(
            sid=sid, channel=channel, markets=set(self._markets), at=frame.received_at
        )
        return True

    def _capture_books(self, reconstructor: OrderBookReconstructor) -> list[dict[str, object]]:
        """Book state as it stood while the connection was still live."""
        now = self._clock.now()
        return [
            {
                "ticker": ticker,
                "integrity": view.integrity.value,
                "yes_levels": len(view.yes_bids),
                "no_levels": len(view.no_bids),
                "latest_seq": view.provenance.latest_seq,
                "snapshot_seq": view.provenance.snapshot_seq,
                "applied_frames": view.provenance.applied_frames,
                "scan_eligible": reconstructor.scan_eligible(ticker, now),
                "last_change_at": (
                    view.last_change_at.isoformat() if view.last_change_at else None
                ),
            }
            for ticker, view in reconstructor.books().items()
        ]

    @staticmethod
    def _deadline_passed(deadline: float | None) -> bool:
        return deadline is not None and asyncio.get_running_loop().time() >= deadline

    def summary(self) -> dict[str, object]:
        """Run summary for the live validator and for logs."""
        reconstructor = self._reconstructor
        summary: dict[str, object] = {
            "connections": self.stats.connections,
            "reconnects": self.stats.reconnects,
            "frames": self.stats.frames,
            "stopped_reason": self.stats.stopped_reason,
        }
        if reconstructor is not None:
            summary["health"] = reconstructor.health.as_dict()
            summary["integrity"] = reconstructor.integrity_counts()
            summary["journal_healthy"] = reconstructor.journal_healthy
        if self._journal is not None:
            summary["journal_records"] = self._journal.stats.records_written
            summary["journal_bytes"] = self._journal.stats.bytes_written
        summary["books"] = self.stats.final_books
        counts: dict[str, int] = {}
        for event in self.stats.events:
            counts[event.kind.value] = counts.get(event.kind.value, 0) + 1
        summary["event_counts"] = counts
        return summary


def default_journal_path(root: Path, *, now: datetime | None = None) -> Path:
    """A per-run journal file under ``root``.

    Timestamped rather than a single rolling file, so one run's evidence is
    never interleaved with another's.
    """
    stamp = (now or datetime.now(tz=UTC)).strftime("%Y%m%dT%H%M%SZ")
    return root / f"ws-frames-{stamp}.jsonl"
