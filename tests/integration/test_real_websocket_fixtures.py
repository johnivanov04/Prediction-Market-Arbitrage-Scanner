"""Regression tests over the REAL captured WebSocket frames.

These are production frames from the A-09 experiments, stored as raw bytes.
They are the evidence behind the proposed Step 4 sequence invariant, so they
must keep parsing and must keep exhibiting the properties that invariant rests
on.
"""

from __future__ import annotations

import collections
import json
from pathlib import Path
from typing import ClassVar

import pytest

from predarb.venues.kalshi.models import KalshiWsEnvelope, decode_json

pytestmark = pytest.mark.integration

REAL_DIR = Path(__file__).parent.parent / "fixtures" / "kalshi" / "websocket_real"
SYNTHETIC_DIR = Path(__file__).parent.parent / "fixtures" / "kalshi" / "websocket"


def real_files() -> list[Path]:
    return sorted(REAL_DIR.glob("*.json"))


def run_label(path: Path) -> str:
    """The experiment a fixture came from, e.g. ``experiment_a``.

    Taken as the first two underscore-separated tokens rather than by stripping
    known frame-type suffixes: a new frame type would silently fall through
    that approach and give every file its own label, splitting one real stream
    into singletons.
    """
    return "_".join(path.name.split("_")[:2])


class TestRealFixturesParse:
    def test_directory_is_populated(self):
        assert real_files(), "no real WebSocket fixtures captured"

    @pytest.mark.parametrize("path", real_files(), ids=lambda p: p.name)
    def test_frame_parses(self, path: Path) -> None:
        envelope = KalshiWsEnvelope.model_validate(decode_json(path.read_bytes()))
        assert envelope.type

    def test_snapshots_parse_as_snapshots(self):
        for path in real_files():
            envelope = KalshiWsEnvelope.model_validate(decode_json(path.read_bytes()))
            if envelope.type == "orderbook_snapshot":
                snapshot = envelope.as_snapshot()
                assert snapshot.market_ticker
                # A snapshot may legitimately carry no levels at all: two of the
                # captured markets had finished, so their books were empty. An
                # empty snapshot is real data, not a parse failure.

    def test_deltas_parse_as_deltas(self):
        found = 0
        for path in real_files():
            envelope = KalshiWsEnvelope.model_validate(decode_json(path.read_bytes()))
            if envelope.type == "orderbook_delta":
                delta = envelope.as_delta()
                assert delta.market_ticker
                assert delta.side in {"yes", "no"}
                found += 1
        assert found, "expected at least one real delta"

    def test_real_deltas_carry_both_timestamp_fields(self):
        """Every real delta carries both ``ts`` and ``ts_ms``.

        Both are documented; ``ts`` is documented as deprecated, so it is parsed
        but nothing derives from it.
        """
        for path in real_files():
            envelope = KalshiWsEnvelope.model_validate(decode_json(path.read_bytes()))
            if envelope.type == "orderbook_delta":
                delta = envelope.as_delta()
                assert delta.ts is not None
                assert delta.ts_ms is not None
                # Same instant; ts is finer-grained.
                assert abs(int(delta.ts.timestamp() * 1000) - delta.ts_ms) <= 1

    def test_both_delta_signs_were_observed(self):
        signs = set()
        for path in real_files():
            envelope = KalshiWsEnvelope.model_validate(decode_json(path.read_bytes()))
            if envelope.type == "orderbook_delta":
                signs.add(envelope.as_delta().delta_fp.is_negative)
        assert signs == {True, False}, "expected both positive and negative deltas"

    def test_subscribed_ack_carries_sid_inside_msg(self):
        """The ack's sid is in ``msg``, not on the envelope.

        The synthetic fixture guessed this shape; the real frames confirm it.
        """
        acks = [
            KalshiWsEnvelope.model_validate(decode_json(p.read_bytes()))
            for p in real_files()
            if "subscribed" in p.name
        ]
        assert acks
        for ack in acks:
            assert ack.sid is None
            assert ack.msg is not None
            assert isinstance(ack.msg.get("sid"), int)
            assert ack.msg.get("channel")

    def test_ok_frame_consumes_a_sequence_number(self):
        """An ``ok`` frame occupies a slot in the sid's sequence.

        Documented, and confirmed live. It matters for reconstruction because
        not every value in a sid's sequence is an order-book message for a
        market being tracked -- so sequence validation must precede routing.
        """
        oks = [
            KalshiWsEnvelope.model_validate(decode_json(p.read_bytes()))
            for p in real_files()
            if "_ok_" in p.name
        ]
        if not oks:
            pytest.skip("no ok frame captured in this run")
        for frame in oks:
            assert frame.sid is not None
            assert frame.seq is not None


class TestRealFixturesSupportTheInvariant:
    """Properties that are well-defined on a *sampled* subset of frames.

    The fixtures are a selection, stored in filename order, so arrival order is
    not recoverable from them and contiguity across the whole set is not
    expected. What is still checkable -- and what the invariant rests on -- is
    that within one connection's sid, every stored frame has a distinct seq,
    and that a complete captured run of snapshots is contiguous.
    """

    def _by_sid(self) -> dict[tuple[str, int], list[tuple[int, str]]]:
        grouped: dict[tuple[str, int], list[tuple[int, str]]] = {}
        for path in real_files():
            envelope = KalshiWsEnvelope.model_validate(decode_json(path.read_bytes()))
            if envelope.seq is None or envelope.sid is None:
                continue
            grouped.setdefault((run_label(path), envelope.sid), []).append(
                (envelope.seq, envelope.type)
            )
        return grouped

    def test_seq_values_are_unique_within_a_sid(self):
        """No two frames from one sid share a sequence number.

        A shared value would mean seq is not a per-sid counter at all.
        """
        for key, entries in self._by_sid().items():
            seqs = [seq for seq, _ in entries]
            assert len(seqs) == len(set(seqs)), f"{key} has duplicate seq values: {seqs}"

    def test_captured_snapshot_run_is_contiguous(self):
        """The snapshots opening a subscription are numbered 1, 2, 3, ... with
        no holes -- one per subscribed market, in the sid's single sequence."""
        checked = 0
        for key, entries in self._by_sid().items():
            snapshots = sorted(seq for seq, kind in entries if kind == "orderbook_snapshot")
            if len(snapshots) < 2:
                continue
            assert snapshots == list(range(snapshots[0], snapshots[0] + len(snapshots))), (
                f"{key} snapshot seqs are not contiguous: {snapshots}"
            )
            assert snapshots[0] == 1, f"{key} snapshots do not start at 1: {snapshots}"
            checked += 1
        assert checked, "expected at least one captured snapshot run"

    def test_captured_delta_run_is_contiguous(self):
        for key, entries in self._by_sid().items():
            deltas = sorted(seq for seq, kind in entries if kind == "orderbook_delta")
            if len(deltas) < 2:
                continue
            assert deltas == list(range(deltas[0], deltas[0] + len(deltas))), (
                f"{key} delta seqs are not contiguous: {deltas}"
            )

    def test_a_second_sid_restarts_at_one(self):
        """Evidence that seq is per-sid, not connection-global.

        The trade-channel subscription opens its own sequence at 1 while the
        order-book subscription on the same socket is already far past it.
        """
        grouped = self._by_sid()
        second_sids = {k: v for k, v in grouped.items() if k[1] != 1}
        if not second_sids:
            pytest.skip("no second-sid frame captured in this run")
        for key, entries in second_sids.items():
            assert min(seq for seq, _ in entries) == 1, f"{key} did not start at 1"


class TestRealAndSyntheticAreSeparated:
    def test_directories_are_distinct(self):
        assert REAL_DIR.name == "websocket_real"
        assert SYNTHETIC_DIR.name == "websocket"

    def test_synthetic_files_are_named_synthetic(self):
        for path in SYNTHETIC_DIR.glob("*.json"):
            assert path.name.startswith("synthetic_")

    def test_real_files_are_not_named_synthetic(self):
        for path in real_files():
            assert not path.name.startswith("synthetic_")

    def test_manifest_labels_every_real_frame(self):
        manifest = json.loads((REAL_DIR.parent / "manifest.json").read_text())
        entries = {e["file"]: e for e in manifest["fixtures"]}
        for path in real_files():
            key = f"websocket_real/{path.name}"
            assert key in entries, f"{key} missing from manifest"
            assert entries[key]["source"] == "REAL"
            assert entries[key]["captured_at_utc"]
            assert entries[key]["raw_unmodified"] is True

    def test_manifest_labels_synthetic_frames_as_synthetic(self):
        manifest = json.loads((REAL_DIR.parent / "manifest.json").read_text())
        for entry in manifest["fixtures"]:
            if entry["file"].startswith("websocket/"):
                assert entry["source"] == "SYNTHETIC"


class TestRealFixtureCredentialSafety:
    FORBIDDEN_KEYS: ClassVar[set[str]] = {
        "authorization",
        "cookie",
        "api_key_id",
        "private_key",
        "signature",
        "token",
        "user_id",
        "member_id",
        "account_id",
        "balance",
        "positions",
        "orders",
        "fills",
        "order_id",
        "client_order_id",
        "subaccount",
    }

    def _walk(self, node: object, path: str = "") -> list[str]:
        hits: list[str] = []
        if isinstance(node, dict):
            for key, value in node.items():
                if key.lower() in self.FORBIDDEN_KEYS:
                    hits.append(f"{path}.{key}")
                hits += self._walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                hits += self._walk(value, f"{path}[{index}]")
        return hits

    @pytest.mark.parametrize("path", real_files(), ids=lambda p: p.name)
    def test_no_credential_or_account_data(self, path: Path) -> None:
        text = path.read_text()
        assert "KALSHI-ACCESS" not in text
        assert "BEGIN" not in text
        assert self._walk(json.loads(text)) == []

    def test_only_market_data_frame_types_are_stored(self) -> None:
        types = collections.Counter(json.loads(p.read_text()).get("type") for p in real_files())
        allowed = {"orderbook_snapshot", "orderbook_delta", "subscribed", "ok", "trade", "ticker"}
        assert set(types) <= allowed, f"unexpected frame types stored: {set(types) - allowed}"
