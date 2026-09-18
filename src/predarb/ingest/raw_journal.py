"""Append-only raw WebSocket frame journal.

Purpose: an opportunity claimed months from now must be reproducible from the
bytes that produced it. That means the *exact* payload, not a re-serialisation
of a parsed model -- if our parser has a bug, a re-serialised record preserves
the bug rather than the evidence.

Format: JSONL
-------------
One JSON object per line, with the original frame carried verbatim as a string
in ``payload``. Chosen over Parquet for this step deliberately:

* **Simple to audit.** A line is a record. You can read it, diff it, and grep it
  without tooling, which matters for a file whose purpose is evidence.
* **Record boundaries make tail recovery straightforward.** A newline delimits
  each record, so a reader can consume everything up to the last complete line
  and identify precisely where the file stops being trustworthy.
* **Losslessness is easy to verify.** The payload is the original text and the
  SHA-256 beside it is over the original bytes.
* **Streaming Parquet is a different problem.** Columnar writers buffer, and
  getting crash-safety and back-pressure right there is real work that would be
  done now for volumes we do not yet have.

What this does **not** give us
-------------------------------
Append-only JSONL is not crash-*safe* in any strong sense, and it would be wrong
to describe it that way. Each record is written and flushed out of the process,
which survives a process crash, but a flush is not an ``fsync``: a machine or
kernel failure can still lose an OS-buffered tail. ``fsync`` per frame is
available via ``fsync=True`` and is **off by default** -- adding a syscall per
frame to defend against a failure mode we have not measured would be premature.

Why the weaker guarantee is acceptable here:

* **A lost tail cannot corrupt a live session's authority.** Losing the process
  destroys the connection, and a new connection is a new epoch requiring fresh
  snapshots. No book survives to be quietly trusted against a truncated record.
* **The risk is historical, not live**: a replay over an interrupted file must
  not mistake a partial record for data.

So the burden falls on the *reader*. :func:`read_journal` rejects a malformed
line outright, and :func:`read_journal_tolerant` exists for forensic use: it
returns the complete prefix plus an explicit description of the incomplete tail,
rather than silently treating a half-written line as valid.

Failure policy
--------------
**A frame that cannot be journalled must not be quietly dropped.** Losing
messages while continuing to present a confident book is the worst outcome this
system can produce, so a write failure raises :class:`JournalError`, and the
reconstructor treats that as fatal for the affected books.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Self

__all__ = [
    "JOURNAL_SCHEMA_VERSION",
    "JournalError",
    "JournalStats",
    "JournalTail",
    "RawFrameJournal",
    "RawFrameRecord",
    "extract_index_fields",
    "read_journal",
    "read_journal_tolerant",
]

JOURNAL_SCHEMA_VERSION: Final = 1
COLLECTOR_VERSION: Final = "predarb-ws-collector/0.1"


class JournalError(Exception):
    """The journal could not durably record a frame.

    Fatal by design. See the module docstring: a book whose inputs were not all
    recorded cannot be audited, so it must stop being scan-eligible.
    """


@dataclass(frozen=True, slots=True)
class RawFrameRecord:
    """One journalled frame.

    ``payload`` is the original text exactly as received. Everything else is
    either local provenance or a value extracted for indexing -- the extracted
    fields are a convenience for later querying and are never the source of
    truth, which is always ``payload``.
    """

    raw_id: str
    connection_epoch: int
    arrival_index: int
    received_at: datetime
    payload: str
    payload_sha256: str
    message_type: str | None = None
    sid: int | None = None
    seq: int | None = None
    market_ticker: str | None = None
    collector_version: str = COLLECTOR_VERSION
    schema_version: int = JOURNAL_SCHEMA_VERSION

    def to_json_line(self) -> str:
        return json.dumps(
            {
                "raw_id": self.raw_id,
                "connection_epoch": self.connection_epoch,
                "arrival_index": self.arrival_index,
                "received_at": self.received_at.isoformat().replace("+00:00", "Z"),
                "message_type": self.message_type,
                "sid": self.sid,
                "seq": self.seq,
                "market_ticker": self.market_ticker,
                "payload_sha256": self.payload_sha256,
                "collector_version": self.collector_version,
                "schema_version": self.schema_version,
                "payload": self.payload,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json_line(cls, line: str) -> RawFrameRecord:
        data = json.loads(line)
        return cls(
            raw_id=data["raw_id"],
            connection_epoch=data["connection_epoch"],
            arrival_index=data["arrival_index"],
            received_at=datetime.fromisoformat(data["received_at"].replace("Z", "+00:00")),
            payload=data["payload"],
            payload_sha256=data["payload_sha256"],
            message_type=data.get("message_type"),
            sid=data.get("sid"),
            seq=data.get("seq"),
            market_ticker=data.get("market_ticker"),
            collector_version=data.get("collector_version", COLLECTOR_VERSION),
            schema_version=data.get("schema_version", JOURNAL_SCHEMA_VERSION),
        )

    def verify(self) -> bool:
        """Whether the stored hash still matches the stored payload."""
        return hashlib.sha256(self.payload.encode("utf-8")).hexdigest() == self.payload_sha256


@dataclass(slots=True)
class JournalStats:
    records_written: int = 0
    bytes_written: int = 0
    write_failures: int = 0
    last_error: str | None = None


class RawFrameJournal:
    """Writes frames to an append-only JSONL file.

    Opened in append mode and never truncated, so a re-run adds to history
    rather than replacing it. Each write is flushed immediately: buffering would
    trade the guarantee this class exists to provide for throughput we do not
    need.
    """

    __slots__ = ("_arrival_index", "_fsync", "_handle", "_path", "_stats", "connection_epoch")

    def __init__(self, path: Path | str, *, connection_epoch: int, fsync: bool = False) -> None:
        self._path = Path(path)
        self.connection_epoch = connection_epoch
        self._arrival_index = 0
        self._stats = JournalStats()
        # fsync per record is correct for durability across power loss but costs
        # a syscall per frame. Off by default: the flush already survives a
        # process crash, which is the failure this journal is guarding against.
        self._fsync = fsync
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self._path.open("a", encoding="utf-8")
        except OSError as exc:
            raise JournalError(f"could not open journal at {self._path}: {exc}") from exc

    @property
    def path(self) -> Path:
        return self._path

    @property
    def stats(self) -> JournalStats:
        return self._stats

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
        """Record one frame, returning its journal entry.

        Raises :class:`JournalError` rather than returning an error, so a
        caller cannot accidentally proceed as though the frame were recorded.
        """
        record = RawFrameRecord(
            raw_id=uuid.uuid4().hex,
            connection_epoch=self.connection_epoch,
            arrival_index=self._arrival_index,
            received_at=received_at or datetime.now(tz=UTC),
            payload=payload,
            payload_sha256=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            message_type=message_type,
            sid=sid,
            seq=seq,
            market_ticker=market_ticker,
        )
        line = record.to_json_line()
        try:
            written = self._handle.write(line + "\n")
            self._handle.flush()
            if self._fsync:
                os.fsync(self._handle.fileno())
        except (OSError, ValueError) as exc:
            self._stats.write_failures += 1
            self._stats.last_error = f"{type(exc).__name__}: {exc}"
            raise JournalError(
                f"failed to journal frame {record.raw_id} "
                f"(arrival {record.arrival_index}): {type(exc).__name__}"
            ) from exc

        self._arrival_index += 1
        self._stats.records_written += 1
        self._stats.bytes_written += written + 1
        return record

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._handle.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def __repr__(self) -> str:
        return (
            f"RawFrameJournal(path={self._path!s}, epoch={self.connection_epoch}, "
            f"records={self._stats.records_written})"
        )


def read_journal(path: Path | str) -> list[RawFrameRecord]:
    """Read every record back, in written order.

    Used by replay and by the fixture-replay tests. Loads into memory, which is
    fine at Phase 1 volumes; a streaming reader can come with compaction.
    """
    records: list[RawFrameRecord] = []
    with Path(path).open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                records.append(RawFrameRecord.from_json_line(stripped))
            except (json.JSONDecodeError, KeyError) as exc:
                raise JournalError(
                    f"{path}:{line_number} is not a valid journal record: {type(exc).__name__}"
                ) from exc
    return records


@dataclass(frozen=True, slots=True)
class JournalTail:
    """What a reader found at the end of an interrupted file."""

    complete_records: int
    incomplete_final_line: str | None
    """The trailing partial line, if the file ends mid-record."""

    corrupt_line_number: int | None = None
    """Set when a malformed line appears *before* the end of the file.

    A bad line in the middle is not an interrupted write -- something has
    damaged the file -- so it is fatal rather than a recoverable tail."""

    @property
    def is_clean(self) -> bool:
        return self.incomplete_final_line is None and self.corrupt_line_number is None


def read_journal_tolerant(path: Path | str) -> tuple[list[RawFrameRecord], JournalTail]:
    """Read a journal that may have been interrupted mid-write.

    Returns every complete record plus a description of what, if anything, is
    wrong at the end. A truncated final line is reported as an incomplete tail
    rather than raising, so forensic tooling can use the good prefix.

    A malformed line **before** the end still raises: that is corruption, not an
    interrupted write, and the records after it cannot be trusted to mean what
    they say.
    """
    file_path = Path(path)
    records: list[RawFrameRecord] = []
    lines = file_path.read_text(encoding="utf-8").splitlines(keepends=True)
    for index, raw_line in enumerate(lines, start=1):
        stripped = raw_line.strip()
        if not stripped:
            continue
        is_final = index == len(lines)
        complete = raw_line.endswith("\n")
        try:
            records.append(RawFrameRecord.from_json_line(stripped))
        except (json.JSONDecodeError, KeyError) as exc:
            if is_final and not complete:
                # The process died mid-write. Everything before this is intact.
                return records, JournalTail(
                    complete_records=len(records), incomplete_final_line=stripped
                )
            raise JournalError(
                f"{file_path}:{index} is corrupt (not an interrupted final write): "
                f"{type(exc).__name__}"
            ) from exc
        if is_final and not complete:
            # Parsed, but the newline is missing: the write was cut short even
            # though what landed happens to be valid JSON. Flagged, not trusted.
            return records[:-1], JournalTail(
                complete_records=len(records) - 1, incomplete_final_line=stripped
            )
    return records, JournalTail(complete_records=len(records), incomplete_final_line=None)


def extract_index_fields(payload: str) -> dict[str, Any]:
    """Best-effort extraction of sid/seq/type/ticker for journal indexing.

    Deliberately forgiving: a frame we cannot parse must still be journalled,
    because an unparseable frame is exactly the evidence worth keeping. Parsing
    failures yield no index fields rather than an exception.
    """
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    message = data.get("msg")
    ticker = message.get("market_ticker") if isinstance(message, dict) else None
    return {
        "message_type": data.get("type") if isinstance(data.get("type"), str) else None,
        "sid": data.get("sid") if isinstance(data.get("sid"), int) else None,
        "seq": data.get("seq") if isinstance(data.get("seq"), int) else None,
        "market_ticker": ticker if isinstance(ticker, str) else None,
    }
