"""The capture tool's offline halves, exercised without a network.

``tools/capture_replay_bundle.py`` is operator-run and its live path needs a
socket, but the parts that decide *what a session monitors* and *what lands in
the bundle* are pure. Those are the parts that can quietly put the wrong thing
in a dataset, so they are tested here rather than trusted to a manual run.
"""

from __future__ import annotations

import importlib.util
import json
import socket
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from predarb.domain.enums import SettlementKind, VenueId
from predarb.domain.models import VenueInstrument
from predarb.domain.money import Price, Quantity
from predarb.replay.bundle import load_bundle, write_bundle
from predarb.replay.observation import ObservationKind
from predarb.replay.plan import ContextRefreshPolicy
from predarb.venues.kalshi.models import KalshiMarket
from predarb.venues.kalshi.normalize import normalize_market
from tests.integration.replay_fixtures import (
    BASKET_PLAN,
    BINARY_PLAN,
    basket_stream,
    binary_stream,
    run_live,
)

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]


def _load_tool() -> ModuleType:
    """Import the operator script by path; ``tools/`` is not a package."""
    path = ROOT / "tools" / "capture_replay_bundle.py"
    spec = importlib.util.spec_from_file_location("capture_replay_bundle", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tool = _load_tool()


def instrument(ticker: str, event: str) -> VenueInstrument:
    return VenueInstrument(
        venue=VenueId.KALSHI,
        ticker=ticker,
        event_ticker=event,
        title=f"{ticker} title",
        status_raw="active",
        settlement_kind=SettlementKind.BINARY,
        market_type_raw="binary",
        notional_value=Price.from_value("1.0000"),
        price_grid=None,
        yes_sub_title=None,
        no_sub_title=None,
        rules_primary=None,
        rules_secondary=None,
        rules_hash="r" * 64,
        open_time=None,
        close_time=None,
        expected_expiration_time=None,
        latest_expiration_time=None,
        settlement_timer_seconds=None,
        result_raw=None,
        settlement_value=None,
        can_close_early=None,
        yes_bid=None,
        yes_ask=None,
        no_bid=None,
        no_ask=None,
        yes_bid_size=None,
        yes_ask_size=None,
    )


def fetched(*pairs: tuple[str, str]) -> list[Any]:
    at = datetime(2026, 9, 22, tzinfo=UTC)
    return [
        tool.FetchedMarket(
            instrument=instrument(ticker, event),
            raw={"ticker": ticker, "event_ticker": event, "status": "active"},
            fetched_at=at,
        )
        for ticker, event in pairs
    ]


def plan_over(*pairs: tuple[str, str]) -> Any:
    return tool.build_plan(
        fetched(*pairs),
        quantities=(Quantity.from_value("1.00"),),
        refresh_policy=ContextRefreshPolicy(),
        balance_precision="unknown-conservative",
        notes="test",
    )


class TestPlanConstruction:
    def test_every_fetched_market_is_monitored_for_complements(self) -> None:
        plan = plan_over(("A", "E1"), ("B", "E1"), ("C", "E2"))
        assert plan.binary_complement_markets == ("A", "B", "C")

    def test_an_event_with_two_monitored_markets_becomes_a_basket(self) -> None:
        plan = plan_over(("A", "E1"), ("B", "E1"), ("C", "E2"))
        assert [b.event_ticker for b in plan.baskets] == ["E1"]
        assert plan.baskets[0].members == ("A", "B")

    def test_a_lone_market_is_not_a_basket(self) -> None:
        """One market is not an AT_MOST_ONE question about anything."""
        plan = plan_over(("C", "E2"))
        assert plan.baskets == ()

    def test_the_plan_survives_a_payload_round_trip(self) -> None:
        plan = plan_over(("A", "E1"), ("B", "E1"))
        restored = type(plan).from_payload(json.loads(json.dumps(plan.to_payload())))
        assert restored == plan


class TestMetadataPayload:
    def test_it_carries_what_the_resolver_reads(self) -> None:
        payload = tool.metadata_payload(fetched(("A", "E1"))[0])
        assert payload["ticker"] == "A"
        assert payload["event_ticker"] == "E1"
        assert payload["notional"] == "1.0000"
        assert payload["settlement_kind"] == SettlementKind.BINARY.value

    def test_it_keeps_the_raw_response_beside_the_normalised_view(self) -> None:
        """Recording only our reading would make the bundle a parser artefact."""
        payload = tool.metadata_payload(fetched(("A", "E1"))[0])
        assert payload["raw"]["ticker"] == "A"
        assert payload["normalizer"].endswith("normalize_market")
        assert payload["source"] == "GET /markets/A"

    def test_a_real_market_payload_carries_nothing_credential_shaped(self) -> None:
        """Bundles hold public market data and nothing else.

        Checked against a real recorded ``/markets`` response rather than a
        hand-written dict, because the risk is a field the venue sends that we
        did not think to look for.
        """
        raw = json.loads(
            (
                ROOT / "tests" / "fixtures" / "kalshi" / "rest" / "market_linear_cent.json"
            ).read_text()
        )
        market = raw.get("market", raw)
        entry = tool.FetchedMarket(
            instrument=normalize_market(KalshiMarket.model_validate(market)),
            raw=market,
            fetched_at=datetime(2026, 9, 22, tzinfo=UTC),
        )
        serialized = json.dumps(tool.metadata_payload(entry)).lower()

        for word in (
            "kalshi-access",
            "signature",
            "private_key",
            "-----begin",
            "authorization",
            "bearer",
            "api_key",
            "member_id",
            "user_id",
            "portfolio",
            "position",
            "order_id",
            "fill_id",
        ):
            assert word not in serialized, f"{word!r} reached a bundle payload"


class TestOracleSeparation:
    def test_decisions_are_written_beside_the_bundle_not_inside_it(self, tmp_path: Path) -> None:
        """A decision is derived output. A bundle holds evidence.

        Writing verdicts into the dataset they were derived from would let a
        later replay 'agree' with a conclusion it had been handed.
        """
        root = tmp_path / "session"
        write_bundle(root, binary_stream(), detector_plan=BINARY_PLAN.to_payload())
        bundle = load_bundle(root)

        kinds = {observation.kind for observation in bundle.stream}
        assert ObservationKind.DETECTOR_PLAN not in kinds or all(
            "classification" not in observation.payload for observation in bundle.stream
        )
        assert not list(root.glob("*oracle*"))

    def test_the_oracle_payload_says_what_it_is(self) -> None:
        session = run_live(BASKET_PLAN, basket_stream())
        capture = type("_C", (), {"plan": BASKET_PLAN, "live_decisions": session.decisions})()
        payload = tool.oracle_payload(capture)

        assert "not source evidence" in payload["note"]
        assert payload["plan"]["baskets"][0]["members"] == ["A", "B", "C"]
        assert len(payload["decisions"]) == len(session.decisions)


class TestNetworkBlock:
    def test_it_blocks_connections_and_restores_afterwards(self) -> None:
        original = socket.socket.connect
        with tool.no_network(), pytest.raises(tool.NetworkBlockedError):
            socket.create_connection(("127.0.0.1", 9))
        assert socket.socket.connect is original

    def test_it_restores_even_when_the_body_raises(self) -> None:
        original = socket.create_connection
        with pytest.raises(ValueError, match="boom"), tool.no_network():
            raise ValueError("boom")
        assert socket.create_connection is original


class TestSessionLengthAgainstRefreshPolicy:
    def test_the_default_policy_is_tighter_than_a_short_capture(self) -> None:
        """A 90s capture cannot outlive its own context freshness window."""
        policy = ContextRefreshPolicy()
        tightest = min(
            policy.market_metadata,
            policy.fee_knowledge,
            policy.settlement_knowledge,
            policy.relation_knowledge,
        )
        assert timedelta(seconds=90) < tightest
