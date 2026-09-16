"""Tests for the WebSocket client and the sequence observer.

No live socket is opened. Frame handling is tested against the stored fixtures,
and command construction against a fake connection.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import cast

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from websockets.asyncio.client import ClientConnection

from predarb.clock import FrozenClock
from predarb.config import KalshiEndpoints, KalshiEnv
from predarb.venues.kalshi.auth import (
    HEADER_KEY,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    WS_SIGNING_PATH,
    KalshiCredentials,
    KalshiSigner,
)
from predarb.venues.kalshi.models import KalshiWsEnvelope, decode_json
from predarb.venues.kalshi.websocket import (
    KalshiWebSocketClient,
    SequenceObserver,
)
from tests.conftest import load_raw

pytestmark = pytest.mark.unit

T0 = datetime(2026, 9, 15, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def signer(rsa_key: rsa.RSAPrivateKey) -> KalshiSigner:
    return KalshiSigner(
        KalshiCredentials(api_key_id="ws-key", private_key=rsa_key), FrozenClock(T0)
    )


class FakeConnection:
    """Stands in for a websockets ClientConnection."""

    def __init__(self, incoming: list[str] | None = None) -> None:
        self.sent: list[str] = []
        self.incoming = list(incoming or [])
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str:
        if not self.incoming:
            raise AssertionError("no more frames scripted")
        return self.incoming.pop(0)

    async def close(self) -> None:
        self.closed = True


def attach(client: KalshiWebSocketClient, connection: FakeConnection) -> FakeConnection:
    """Inject a fake connection.

    The cast is confined to this helper so the duck-typed stand-in does not
    require an ignore comment at every call site.
    """
    client._connection = cast("ClientConnection", connection)
    return connection


def envelope_from(name: str) -> KalshiWsEnvelope:
    return KalshiWsEnvelope.model_validate(decode_json(load_raw(f"websocket/{name}")))


class TestHandshakeSigning:
    def test_headers_have_the_three_required_fields(self, signer):
        headers = signer.websocket_headers()
        assert set(headers) == {HEADER_KEY, HEADER_TIMESTAMP, HEADER_SIGNATURE}

    def test_signs_the_fixed_websocket_path(self, signer):
        headers = signer.websocket_headers()
        timestamp = headers[HEADER_TIMESTAMP]
        expected = signer.sign("GET", WS_SIGNING_PATH, timestamp_ms=int(timestamp))
        assert expected.signed_message == f"{timestamp}GET/trade-api/ws/v2"

    def test_signing_path_is_not_derived_from_the_connection_url(self, signer):
        # Demo and production sign the same path even though the hosts differ.
        assert KalshiEndpoints.websocket(KalshiEnv.PROD) != KalshiEndpoints.websocket(
            KalshiEnv.DEMO
        )
        headers = signer.websocket_headers()
        message = signer.sign(
            "GET", WS_SIGNING_PATH, timestamp_ms=int(headers[HEADER_TIMESTAMP])
        ).signed_message
        assert message.endswith("/trade-api/ws/v2")
        assert "kalshi.com" not in message

    def test_production_url(self):
        assert KalshiEndpoints.websocket(KalshiEnv.PROD) == (
            "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
        )


class TestCommandConstruction:
    async def test_subscribe_message_shape(self, signer):
        client = KalshiWebSocketClient(signer=signer, clock=FrozenClock(T0))
        connection = attach(client, FakeConnection())
        await client.subscribe_orderbook(["A", "B"])
        sent = json.loads(connection.sent[0])
        assert sent == {
            "id": 1,
            "cmd": "subscribe",
            "params": {"channels": ["orderbook_delta"], "market_tickers": ["A", "B"]},
        }

    async def test_command_ids_start_at_one_and_increment(self, signer):
        client = KalshiWebSocketClient(signer=signer, clock=FrozenClock(T0))
        connection = attach(client, FakeConnection())
        await client.subscribe_orderbook(["A"])
        await client.subscribe_orderbook(["B"])
        ids = [json.loads(m)["id"] for m in connection.sent]
        assert ids == [1, 2]

    async def test_empty_ticker_list_rejected(self, signer):
        client = KalshiWebSocketClient(signer=signer, clock=FrozenClock(T0))
        attach(client, FakeConnection())
        with pytest.raises(ValueError, match="at least one market ticker"):
            await client.subscribe_orderbook([])

    async def test_update_subscription_shape(self, signer):
        client = KalshiWebSocketClient(signer=signer, clock=FrozenClock(T0))
        connection = attach(client, FakeConnection())
        await client.update_subscription(7, action="add_markets", market_tickers=["C"])
        sent = json.loads(connection.sent[0])
        assert sent["cmd"] == "update_subscription"
        assert sent["params"] == {"sids": [7], "action": "add_markets", "market_tickers": ["C"]}

    async def test_unsubscribe_shape(self, signer):
        client = KalshiWebSocketClient(signer=signer, clock=FrozenClock(T0))
        connection = attach(client, FakeConnection())
        await client.unsubscribe([1, 2])
        assert json.loads(connection.sent[0])["params"] == {"sids": [1, 2]}

    async def test_sending_without_a_connection_raises(self, signer):
        client = KalshiWebSocketClient(signer=signer, clock=FrozenClock(T0))
        with pytest.raises(Exception, match="not connected"):
            await client.subscribe_orderbook(["A"])


class TestFrameHandling:
    async def test_parses_a_snapshot_frame(self, signer):
        raw = load_raw("websocket/synthetic_orderbook_snapshot.json").decode()
        client = KalshiWebSocketClient(signer=signer, clock=FrozenClock(T0))
        attach(client, FakeConnection([raw]))
        frame = await client.receive()
        assert frame.parsed
        assert frame.envelope is not None
        assert frame.envelope.type == "orderbook_snapshot"
        assert frame.received_at == T0

    async def test_keeps_the_raw_text_for_fixture_capture(self, signer):
        raw = load_raw("websocket/synthetic_orderbook_delta_yes_negative.json").decode()
        client = KalshiWebSocketClient(signer=signer, clock=FrozenClock(T0))
        attach(client, FakeConnection([raw]))
        frame = await client.receive()
        assert json.loads(frame.raw) == json.loads(raw)

    async def test_malformed_frame_is_returned_not_discarded(self, signer):
        # An unparseable frame is still evidence about the protocol.
        client = KalshiWebSocketClient(signer=signer, clock=FrozenClock(T0))
        attach(client, FakeConnection(["{not json"]))
        frame = await client.receive()
        assert not frame.parsed
        assert frame.parse_error
        assert frame.raw == "{not json"

    async def test_close_is_idempotent(self, signer):
        client = KalshiWebSocketClient(signer=signer, clock=FrozenClock(T0))
        connection = attach(client, FakeConnection())
        await client.close()
        await client.close()
        assert connection.closed
        assert not client.is_connected

    def test_repr_does_not_leak_credentials(self, signer):
        client = KalshiWebSocketClient(signer=signer, clock=FrozenClock(T0))
        assert "ws-key" not in repr(client)


class TestSequenceObserver:
    """The observer records evidence and draws no conclusions."""

    def test_records_frames_per_sid(self):
        observer = SequenceObserver()
        for name in (
            "synthetic_orderbook_snapshot.json",
            "synthetic_orderbook_delta_yes_negative.json",
            "synthetic_orderbook_delta_no_positive.json",
        ):
            observer.observe(envelope_from(name))
        report = observer.report()
        assert report["per_sid"][2]["count"] == 3
        assert report["per_sid"][2]["first"] == 2
        assert report["per_sid"][2]["last"] == 4

    def test_detects_contiguity_descriptively(self):
        observer = SequenceObserver()
        for seq in (1, 2, 3):
            observer.observe(KalshiWsEnvelope.model_validate({"type": "d", "sid": 1, "seq": seq}))
        assert observer.report()["per_sid"][1]["contiguous_plus_one"] is True

    def test_reports_non_contiguity_without_calling_it_a_gap(self):
        # Whether a missing number is a gap depends on the scope of seq, which
        # is unverified. The observer describes; it does not conclude.
        observer = SequenceObserver()
        for seq in (1, 2, 7):
            observer.observe(KalshiWsEnvelope.model_validate({"type": "d", "sid": 1, "seq": seq}))
        stats = observer.report()["per_sid"][1]
        assert stats["contiguous_plus_one"] is False
        assert stats["strictly_increasing"] is True
        assert stats["span"] == 7
        assert stats["count"] == 3

    def test_tracks_per_market_and_per_sid_market(self):
        observer = SequenceObserver()
        for seq, ticker in ((1, "A"), (2, "B"), (3, "A")):
            observer.observe(
                KalshiWsEnvelope.model_validate(
                    {"type": "d", "sid": 5, "seq": seq, "msg": {"market_ticker": ticker}}
                )
            )
        report = observer.report()
        assert report["per_market"]["A"]["count"] == 2
        assert report["per_market"]["B"]["count"] == 1
        assert report["per_sid_market"]["sid=5|A"]["count"] == 2

    def test_records_arrival_order(self):
        observer = SequenceObserver()
        for seq in (3, 1, 2):
            observer.observe(KalshiWsEnvelope.model_validate({"type": "d", "sid": 1, "seq": seq}))
        assert [entry[1] for entry in observer.arrival_order] == [3, 1, 2]
        # Out-of-order arrival is recorded, not corrected.
        assert observer.report()["per_sid"][1]["strictly_increasing"] is False

    def test_counts_frame_types(self):
        observer = SequenceObserver()
        observer.observe(envelope_from("synthetic_orderbook_snapshot.json"))
        observer.observe(envelope_from("synthetic_orderbook_delta_yes_negative.json"))
        observer.observe(envelope_from("synthetic_subscribed_ack.json"))
        types = observer.report()["frame_types"]
        assert types["orderbook_snapshot"] == 1
        assert types["orderbook_delta"] == 1
        assert types["subscribed"] == 1

    def test_frames_without_seq_are_counted_but_not_sequenced(self):
        observer = SequenceObserver()
        observer.observe(envelope_from("synthetic_subscribed_ack.json"))
        report = observer.report()
        assert report["total_frames"] == 1
        assert report["per_sid"] == {}

    def test_empty_report(self):
        assert SequenceObserver().report()["total_frames"] == 0


class TestNoGapDetectionYet:
    """Step 3 must not ship a sequence invariant it cannot justify."""

    def test_observer_exposes_no_gap_api(self):
        for forbidden in ("has_gap", "detect_gap", "gaps", "is_contiguous", "validate"):
            assert not hasattr(SequenceObserver, forbidden)

    def test_client_exposes_no_reconstruction_api(self):
        for forbidden in ("apply_delta", "rebuild_book", "book", "resnapshot"):
            assert not hasattr(KalshiWebSocketClient, forbidden)
