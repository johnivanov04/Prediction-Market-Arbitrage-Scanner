"""``predarb replay`` end to end, with the network hard-blocked.

Blocking rather than mocking. A replay that can reach the API is able to repair
a gap in its own evidence and finish looking complete, using facts nobody held
at the time. These tests make that a crash instead of a silent success, so the
guarantee is enforced by the runtime and not by a promise in a docstring.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from predarb.cli.main import app
from predarb.replay.bundle import write_bundle
from predarb.replay.observation import ObservationKind
from tests.integration.replay_fixtures import (
    BASKET_PLAN,
    BINARY_PLAN,
    basket_stream,
    binary_stream,
)

pytestmark = pytest.mark.integration

runner = CliRunner()


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every socket path a client could take raises instead of connecting."""

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("replay attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


@pytest.fixture
def binary_bundle(tmp_path: Path) -> Path:
    root = tmp_path / "binary"
    write_bundle(
        root,
        binary_stream(),
        markets=BINARY_PLAN.monitored_markets,
        detector_plan=BINARY_PLAN.to_payload(),
    )
    return root


@pytest.fixture
def basket_bundle(tmp_path: Path) -> Path:
    root = tmp_path / "basket"
    write_bundle(
        root,
        basket_stream(),
        markets=BASKET_PLAN.monitored_markets,
        detector_plan=BASKET_PLAN.to_payload(),
    )
    return root


class TestReplayRun:
    def test_it_produces_binary_decisions_offline(self, binary_bundle: Path) -> None:
        result = runner.invoke(app, ["replay", "run", str(binary_bundle)])
        assert result.exit_code == 0, result.output
        assert "BINARY_COMPLEMENT: 6 decision(s)" in result.output
        assert "PROVEN_CONTRACTUAL_ARBITRAGE" in result.output
        assert "network     : 0 calls" in result.output

    def test_it_produces_basket_decisions_offline(self, basket_bundle: Path) -> None:
        result = runner.invoke(app, ["replay", "run", str(basket_bundle)])
        assert result.exit_code == 0, result.output
        assert "AT_MOST_ONE_BASKET: 11 decision(s)" in result.output
        assert "RELATION_KNOWLEDGE     COMPLETE" in result.output

    def test_relation_knowledge_is_not_required_without_baskets(self, binary_bundle: Path) -> None:
        result = runner.invoke(app, ["replay", "run", str(binary_bundle)])
        assert "RELATION_KNOWLEDGE     NOT_REQUIRED" in result.output

    def test_the_report_records_every_decision(self, basket_bundle: Path, tmp_path: Path) -> None:
        report = tmp_path / "report.json"
        result = runner.invoke(app, ["replay", "run", str(basket_bundle), "--report", str(report)])
        assert result.exit_code == 0, result.output

        payload = json.loads(report.read_text())
        assert payload["network_calls"] == 0
        assert payload["mode"] == "AS_KNOWN_AT_TIME"
        assert len(payload["decisions"]) == 11
        assert payload["plan"]["baskets"][0]["members"] == ["A", "B", "C"]
        assert "derived output" in payload["note"]

    def test_the_digest_is_stable_across_runs(self, binary_bundle: Path) -> None:
        first = runner.invoke(app, ["replay", "run", str(binary_bundle)])
        second = runner.invoke(app, ["replay", "run", str(binary_bundle)])
        digests = [line for line in first.output.splitlines() if line.strip().startswith("digest")]
        assert digests
        assert digests == [
            line for line in second.output.splitlines() if line.strip().startswith("digest")
        ]

    def test_a_bundle_without_a_plan_refuses_rather_than_guessing(self, tmp_path: Path) -> None:
        """No plan means nothing says what the session was watching.

        Inferring one here would let the replay evaluate work the live system
        never did, so it exits non-zero and says what to re-capture.
        """
        root = tmp_path / "planless"
        write_bundle(root, binary_stream())
        result = runner.invoke(app, ["replay", "run", str(root)])
        assert result.exit_code == 1
        assert "no detector plan" in result.output


class TestReplayInspectAndVerify:
    def test_inspect_describes_the_bundle(self, basket_bundle: Path) -> None:
        result = runner.invoke(app, ["replay", "inspect", str(basket_bundle)])
        assert result.exit_code == 0, result.output
        assert "economic-replay-ready" in result.output
        assert ObservationKind.RELATION_REGISTRY_SNAPSHOT.value in result.output

    def test_verify_passes_on_an_intact_bundle(self, basket_bundle: Path) -> None:
        result = runner.invoke(app, ["replay", "verify", str(basket_bundle)])
        assert result.exit_code == 0, result.output
        assert "VERIFIED" in result.output

    def test_verify_fails_closed_on_an_edited_bundle(self, basket_bundle: Path) -> None:
        path = basket_bundle / "observations.jsonl"
        lines = path.read_text().splitlines(keepends=True)
        edited = next(i for i, line in enumerate(lines) if "0.4000" in line)
        lines[edited] = lines[edited].replace("0.4000", "0.9999")
        path.write_text("".join(lines))

        result = runner.invoke(app, ["replay", "verify", str(basket_bundle)])
        assert result.exit_code == 1
        assert "has been altered" in result.output


class TestReplayKnowledge:
    def test_it_reports_explicit_knowledge_snapshots(self, basket_bundle: Path) -> None:
        result = runner.invoke(app, ["replay", "knowledge", str(basket_bundle)])
        assert result.exit_code == 0, result.output
        assert "explicit knowledge snapshots" in result.output
        assert ObservationKind.SETTLEMENT_REGISTRY_SNAPSHOT.value in result.output
        assert "relation_certificates    1" in result.output
