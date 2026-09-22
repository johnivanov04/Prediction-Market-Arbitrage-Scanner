"""A journal stand-in for replay.

:class:`~predarb.books.reconstruction.OrderBookReconstructor` journals every
frame before parsing it, which is right for live capture: the raw evidence must
be durable before anything derived from it exists.

During replay that evidence is *already* recorded -- it is what the bundle is.
Re-journalling would write a second copy of the same frames under fresh raw ids,
duplicating evidence and making the replay's provenance disagree with the
capture's for no benefit.

So replay supplies a journal that satisfies the same interface, keeps the same
counters, and writes nothing. The reconstructor is untouched: it is given a
different collaborator, not a different code path.

The raw ids it hands back are deterministic functions of the frame's position,
so two replays of the same bundle produce identical provenance.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from predarb.clock import ensure_utc
from predarb.ingest.raw_journal import RawFrameRecord

_EPOCH = datetime.fromtimestamp(0, tz=UTC)

__all__ = ["ReplayFrameJournal"]


class ReplayFrameJournal:
    """Write-nothing journal with deterministic ids, for replay only.

    Deliberately not a subclass of :class:`RawFrameJournal`: it must never be
    mistaken for something that provides durability, and inheriting would make
    it substitutable in a live path where that guarantee is required.
    """

    def __init__(self, *, connection_epoch: int = 1) -> None:
        self._connection_epoch = connection_epoch
        self._arrival_index = 0
        self.written = 0

    @property
    def path(self) -> Path:
        return Path("(replay: no journal is written)")

    @property
    def arrival_index(self) -> int:
        return self._arrival_index

    def write(
        self,
        payload: str,
        *,
        received_at: datetime | None = None,
        message_type: str | None = None,
        sid: int | None = None,
        seq: int | None = None,
        market_ticker: str | None = None,
    ) -> RawFrameRecord:
        """Return a record without persisting anything.

        Never raises: a replay cannot suffer a disk failure that the original
        capture did not, and inventing one would diverge from the history being
        reproduced. Genuine historical journal failures are replayed from the
        recorded lifecycle, not re-simulated here.
        """
        index = self._arrival_index
        self._arrival_index += 1
        self.written += 1
        moment = ensure_utc(received_at) if received_at is not None else _EPOCH
        return RawFrameRecord(
            raw_id=f"replay-{self._connection_epoch}-{index}",
            payload_sha256=hashlib.sha256(payload.encode()).hexdigest(),
            connection_epoch=self._connection_epoch,
            arrival_index=index,
            received_at=moment,
            payload=payload,
            message_type=message_type,
            sid=sid,
            seq=seq,
            market_ticker=market_ticker,
        )

    def close(self) -> None:
        return None

    def __repr__(self) -> str:
        return f"ReplayFrameJournal(epoch={self._connection_epoch}, frames={self.written})"
