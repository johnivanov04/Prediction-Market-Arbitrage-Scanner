"""Tests for the append-only raw frame journal."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from predarb.ingest.raw_journal import (
    JournalError,
    RawFrameJournal,
    RawFrameRecord,
    extract_index_fields,
    read_journal,
    read_journal_tolerant,
)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC)
FRAME = '{"type":"orderbook_delta","sid":1,"seq":5,"msg":{"market_ticker":"X","delta_fp":"-1.00"}}'


class TestLosslessness:
    def test_payload_round_trips_byte_for_byte(self, tmp_path: Path) -> None:
        """The stored payload is the original text, not a re-serialisation.

        A re-serialised record would preserve any parser bug rather than the
        evidence, which defeats the point of keeping raw frames.
        """
        path = tmp_path / "j.jsonl"
        with RawFrameJournal(path, connection_epoch=1) as journal:
            journal.write(FRAME, received_at=T0)
        [record] = read_journal(path)
        assert record.payload == FRAME

    def test_hash_matches_payload(self, tmp_path: Path) -> None:
        path = tmp_path / "j.jsonl"
        with RawFrameJournal(path, connection_epoch=1) as journal:
            journal.write(FRAME, received_at=T0)
        assert read_journal(path)[0].verify()

    def test_unusual_payload_survives(self, tmp_path: Path) -> None:
        weird = '{"a":"line\\nbreak \\u00e9 \\"quoted\\" \\\\slash"}'
        path = tmp_path / "j.jsonl"
        with RawFrameJournal(path, connection_epoch=1) as journal:
            journal.write(weird, received_at=T0)
        assert read_journal(path)[0].payload == weird

    def test_unparseable_payload_is_still_journalled(self, tmp_path: Path) -> None:
        # An unparseable frame is exactly the evidence worth keeping.
        path = tmp_path / "j.jsonl"
        with RawFrameJournal(path, connection_epoch=1) as journal:
            journal.write("{not json", received_at=T0)
        assert read_journal(path)[0].payload == "{not json"


class TestAppendOnly:
    def test_records_accumulate_in_order(self, tmp_path: Path) -> None:
        path = tmp_path / "j.jsonl"
        with RawFrameJournal(path, connection_epoch=1) as journal:
            for index in range(5):
                journal.write(f'{{"n":{index}}}', received_at=T0)
        records = read_journal(path)
        assert [r.arrival_index for r in records] == [0, 1, 2, 3, 4]

    def test_reopening_appends_rather_than_truncating(self, tmp_path: Path) -> None:
        path = tmp_path / "j.jsonl"
        with RawFrameJournal(path, connection_epoch=1) as journal:
            journal.write('{"n":1}', received_at=T0)
        with RawFrameJournal(path, connection_epoch=2) as journal:
            journal.write('{"n":2}', received_at=T0)
        records = read_journal(path)
        assert len(records) == 2
        assert [r.connection_epoch for r in records] == [1, 2]

    def test_each_record_has_a_unique_id(self, tmp_path: Path) -> None:
        path = tmp_path / "j.jsonl"
        with RawFrameJournal(path, connection_epoch=1) as journal:
            for _ in range(20):
                journal.write(FRAME, received_at=T0)
        ids = {r.raw_id for r in read_journal(path)}
        assert len(ids) == 20


class TestProvenanceFields:
    def test_index_fields_are_extracted(self, tmp_path: Path) -> None:
        path = tmp_path / "j.jsonl"
        with RawFrameJournal(path, connection_epoch=3) as journal:
            journal.write(FRAME, received_at=T0, **extract_index_fields(FRAME))
        [record] = read_journal(path)
        assert record.message_type == "orderbook_delta"
        assert record.sid == 1
        assert record.seq == 5
        assert record.market_ticker == "X"
        assert record.connection_epoch == 3

    def test_extraction_is_forgiving(self):
        # A frame we cannot parse must still be journalled.
        assert extract_index_fields("{not json") == {}
        assert extract_index_fields("[1,2,3]") == {}

    def test_extraction_ignores_wrong_types(self):
        fields = extract_index_fields('{"type":5,"sid":"x","seq":null}')
        assert fields["message_type"] is None
        assert fields["sid"] is None

    def test_schema_and_collector_version_recorded(self, tmp_path: Path) -> None:
        path = tmp_path / "j.jsonl"
        with RawFrameJournal(path, connection_epoch=1) as journal:
            journal.write(FRAME, received_at=T0)
        record = read_journal(path)[0]
        assert record.schema_version >= 1
        assert "predarb" in record.collector_version


class TestNoCredentialsRecorded:
    def test_journal_records_carry_no_header_field(self):
        fields = set(RawFrameRecord.__dataclass_fields__)
        for forbidden in ("headers", "authorization", "signature", "api_key_id"):
            assert forbidden not in fields


class TestFailurePolicy:
    def test_write_failure_raises_rather_than_dropping(self, tmp_path: Path) -> None:
        """Losing a frame silently is the worst outcome available."""
        path = tmp_path / "j.jsonl"
        journal = RawFrameJournal(path, connection_epoch=1)
        journal.close()
        with pytest.raises(JournalError):
            journal.write(FRAME, received_at=T0)
        assert journal.stats.write_failures == 1

    def test_unopenable_path_raises(self, tmp_path: Path) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory")
        with pytest.raises(JournalError, match="could not open"):
            RawFrameJournal(blocker / "nested" / "j.jsonl", connection_epoch=1)

    def test_stats_track_throughput(self, tmp_path: Path) -> None:
        path = tmp_path / "j.jsonl"
        with RawFrameJournal(path, connection_epoch=1) as journal:
            journal.write(FRAME, received_at=T0)
            journal.write(FRAME, received_at=T0)
            assert journal.stats.records_written == 2
            assert journal.stats.bytes_written > 0

    def test_corrupt_line_raises_on_read(self, tmp_path: Path) -> None:
        path = tmp_path / "j.jsonl"
        path.write_text('{"raw_id":"a"}\nnot-json\n')
        with pytest.raises(JournalError, match="not a valid journal record"):
            read_journal(path)


class TestInterruptedWrites:
    """Durability is bounded, so the reader carries the burden.

    A flush survives a process crash but is not an ``fsync``: a machine failure
    can still lose an OS-buffered tail. That is acceptable for a live session --
    losing the process destroys the connection, and a new epoch requires fresh
    snapshots, so no book survives to be trusted against a truncated record.
    What must not happen is historical tooling mistaking a partial record for
    data.
    """

    def _write_two(self, path: Path) -> None:
        with RawFrameJournal(path, connection_epoch=1) as journal:
            journal.write('{"n":1}', received_at=T0)
            journal.write('{"n":2}', received_at=T0)

    def test_truncated_final_record_is_reported_not_trusted(self, tmp_path: Path) -> None:
        path = tmp_path / "j.jsonl"
        self._write_two(path)
        # Simulate a crash mid-write: a partial line with no trailing newline.
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"raw_id":"abc","connection_epo')

        records, tail = read_journal_tolerant(path)
        assert len(records) == 2
        assert not tail.is_clean
        assert tail.incomplete_final_line is not None
        assert tail.complete_records == 2

    def test_preceding_records_remain_verifiable_after_truncation(self, tmp_path: Path) -> None:
        path = tmp_path / "j.jsonl"
        self._write_two(path)
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"raw_id":"abc","conn')

        records, _ = read_journal_tolerant(path)
        assert all(record.verify() for record in records)
        assert [record.payload for record in records] == ['{"n":1}', '{"n":2}']

    def test_valid_json_without_a_trailing_newline_is_still_flagged(self, tmp_path: Path) -> None:
        """A cut-short write that happens to be parseable is not trusted.

        Without the delimiter we cannot show the record is complete, so it is
        reported as a tail rather than returned as data.
        """
        path = tmp_path / "j.jsonl"
        self._write_two(path)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                '{"raw_id":"x","connection_epoch":1,"arrival_index":9,'
                '"received_at":"2026-01-01T00:00:00Z","payload":"{}",'
                '"payload_sha256":"deadbeef"}'
            )

        records, tail = read_journal_tolerant(path)
        assert len(records) == 2
        assert tail.incomplete_final_line is not None

    def test_corruption_in_the_middle_is_fatal(self, tmp_path: Path) -> None:
        """A bad line before the end is damage, not an interrupted write."""
        path = tmp_path / "j.jsonl"
        self._write_two(path)
        lines = path.read_text().splitlines(keepends=True)
        path.write_text(lines[0] + "GARBAGE-NOT-JSON\n" + lines[1])

        with pytest.raises(JournalError, match="corrupt"):
            read_journal_tolerant(path)

    def test_clean_file_reports_a_clean_tail(self, tmp_path: Path) -> None:
        path = tmp_path / "j.jsonl"
        self._write_two(path)
        records, tail = read_journal_tolerant(path)
        assert len(records) == 2
        assert tail.is_clean
        assert tail.incomplete_final_line is None

    def test_strict_reader_still_rejects_a_truncated_tail(self, tmp_path: Path) -> None:
        path = tmp_path / "j.jsonl"
        self._write_two(path)
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"partial')
        with pytest.raises(JournalError):
            read_journal(path)

    def test_fsync_is_available_but_off_by_default(self, tmp_path: Path) -> None:
        """Durability is opt-in; a syscall per frame is not paid by default."""
        path = tmp_path / "j.jsonl"
        with RawFrameJournal(path, connection_epoch=1, fsync=True) as journal:
            journal.write('{"n":1}', received_at=T0)
        assert read_journal(path)[0].payload == '{"n":1}'
