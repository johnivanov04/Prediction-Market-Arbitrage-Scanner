"""Bundle integrity: fail closed, never quietly empty.

An incomplete replay that reports "no opportunities" is indistinguishable from
a clean one that genuinely found none, and reads as reassurance. Every test here
is about refusing to produce that.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from predarb.replay.bundle import (
    BUNDLE_SCHEMA_VERSION,
    MANIFEST_FILE,
    OBSERVATIONS_FILE,
    BundleIntegrity,
    BundleIntegrityError,
    load_bundle,
    write_bundle,
)
from predarb.replay.observation import Observation, ObservationKind, ObservationStream

pytestmark = pytest.mark.unit

T0 = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


def stream(count: int = 5) -> ObservationStream:
    return ObservationStream.from_iterable(
        Observation(
            ordinal=i,
            observed_at=T0 + timedelta(seconds=i),
            kind=ObservationKind.FRAME_RECEIVED,
            payload={"raw": f'{{"seq": {i}}}', "market_ticker": "MKT"},
            connection_epoch=1,
        )
        for i in range(count)
    )


class TestRoundTrip:
    def test_a_written_bundle_loads_back_identically(self, tmp_path: Path) -> None:
        original = stream()
        write_bundle(tmp_path, original, markets=("MKT",))
        loaded = load_bundle(tmp_path)
        assert loaded.integrity.integrity is BundleIntegrity.VERIFIED
        assert loaded.stream.content_hash() == original.content_hash()
        assert [o.ordinal for o in loaded.stream] == list(range(5))

    def test_the_manifest_describes_the_dataset(self, tmp_path: Path) -> None:
        manifest = write_bundle(tmp_path, stream(), markets=("MKT",))
        assert manifest.observation_count == 5
        assert manifest.markets == ("MKT",)
        assert manifest.connection_epochs == (1,)
        assert manifest.observation_counts_by_kind == {"FRAME_RECEIVED": 5}

    def test_observation_timestamps_survive_untouched(self, tmp_path: Path) -> None:
        """Bundle assembly must not re-date what it contains."""
        write_bundle(tmp_path, stream(), created_at=T0 + timedelta(days=30))
        loaded = load_bundle(tmp_path)
        assert loaded.stream.observations[0].observed_at == T0
        assert loaded.manifest.created_at == T0 + timedelta(days=30)

    def test_the_manifest_says_creation_time_is_not_knowledge_time(self, tmp_path: Path) -> None:
        """In the payload, not just in code: stamping assembly time onto old
        observations would make the whole dataset "known" at assembly."""
        write_bundle(tmp_path, stream())
        payload = json.loads((tmp_path / MANIFEST_FILE).read_text())
        assert "NOT the knowledge time" in payload["created_at_note"]

    def test_an_empty_bundle_round_trips(self, tmp_path: Path) -> None:
        write_bundle(tmp_path, ObservationStream())
        loaded = load_bundle(tmp_path)
        assert len(loaded.stream) == 0
        assert loaded.permits_replay


class TestIntegrityFailures:
    def test_a_missing_manifest_fails_closed(self, tmp_path: Path) -> None:
        write_bundle(tmp_path, stream())
        (tmp_path / MANIFEST_FILE).unlink()
        with pytest.raises(BundleIntegrityError, match=re.escape("no manifest.json")):
            load_bundle(tmp_path)

    def test_a_missing_observations_file_fails_closed(self, tmp_path: Path) -> None:
        write_bundle(tmp_path, stream())
        (tmp_path / OBSERVATIONS_FILE).unlink()
        with pytest.raises(BundleIntegrityError, match="missing"):
            load_bundle(tmp_path)

    def test_an_edited_observation_fails_the_hash_check(self, tmp_path: Path) -> None:
        write_bundle(tmp_path, stream())
        path = tmp_path / OBSERVATIONS_FILE
        lines = path.read_text().splitlines()
        body = json.loads(lines[2])
        body["payload"]["raw"] = '{"seq": 999}'
        lines[2] = json.dumps(body, sort_keys=True)
        path.write_text("\n".join(lines) + "\n")
        with pytest.raises(BundleIntegrityError, match="does not match"):
            load_bundle(tmp_path)

    def test_a_corrupt_middle_record_fails_closed(self, tmp_path: Path) -> None:
        """Damage in the middle means the replay cannot speak for the period."""
        write_bundle(tmp_path, stream())
        path = tmp_path / OBSERVATIONS_FILE
        lines = path.read_text().splitlines()
        lines[2] = "{not json"
        path.write_text("\n".join(lines) + "\n")
        with pytest.raises(BundleIntegrityError, match="corrupt"):
            load_bundle(tmp_path)

    def test_an_unknown_schema_version_fails_closed(self, tmp_path: Path) -> None:
        write_bundle(tmp_path, stream())
        manifest_path = tmp_path / MANIFEST_FILE
        payload = json.loads(manifest_path.read_text())
        payload["schema_version"] = "replay-bundle/99"
        manifest_path.write_text(json.dumps(payload))
        with pytest.raises(BundleIntegrityError, match="refusing to guess"):
            load_bundle(tmp_path)

    def test_a_mismatched_observation_count_fails_closed(self, tmp_path: Path) -> None:
        write_bundle(tmp_path, stream())
        manifest_path = tmp_path / MANIFEST_FILE
        payload = json.loads(manifest_path.read_text())
        payload["observation_count"] = 99
        manifest_path.write_text(json.dumps(payload))
        with pytest.raises(BundleIntegrityError):
            load_bundle(tmp_path)

    def test_the_schema_version_is_pinned(self):
        assert BUNDLE_SCHEMA_VERSION == "replay-bundle/1"


class TestTruncatedTail:
    """A capture killed mid-write is ordinary and recoverable."""

    def _truncate(self, tmp_path: Path) -> None:
        write_bundle(tmp_path, stream())
        path = tmp_path / OBSERVATIONS_FILE
        text = path.read_text()
        path.write_text(text[: -len(text.splitlines()[-1]) // 2])

    def test_a_truncated_tail_is_usable_but_labelled(self, tmp_path: Path) -> None:
        self._truncate(tmp_path)
        loaded = load_bundle(tmp_path)
        assert loaded.integrity.integrity is BundleIntegrity.TRUNCATED_TAIL
        assert loaded.permits_replay
        assert "ends earlier" in loaded.integrity.detail

    def test_the_prefix_is_still_read(self, tmp_path: Path) -> None:
        self._truncate(tmp_path)
        loaded = load_bundle(tmp_path)
        assert len(loaded.stream) >= 4

    def test_a_truncated_tail_can_be_refused_explicitly(self, tmp_path: Path) -> None:
        self._truncate(tmp_path)
        with pytest.raises(BundleIntegrityError):
            load_bundle(tmp_path, tolerate_truncated_tail=False)


class TestIntegrityGating:
    @pytest.mark.parametrize(
        "integrity",
        [
            BundleIntegrity.HASH_MISMATCH,
            BundleIntegrity.CORRUPT_RECORD,
            BundleIntegrity.MISSING_FILE,
            BundleIntegrity.UNKNOWN_SCHEMA,
        ],
    )
    def test_damaged_bundles_do_not_permit_replay(self, integrity):
        assert not integrity.permits_replay

    @pytest.mark.parametrize(
        "integrity", [BundleIntegrity.VERIFIED, BundleIntegrity.TRUNCATED_TAIL]
    )
    def test_usable_bundles_do(self, integrity):
        assert integrity.permits_replay
