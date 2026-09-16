"""Fixture regression, manifest integrity and credential safety.

These are the tests that keep the captured corpus trustworthy over time:

* every stored fixture still parses with the current models, so a schema change
  on our side that breaks real payloads fails here rather than in production;
* the manifest's recorded hashes still match the files, so a fixture cannot be
  edited without the provenance record noticing;
* no fixture contains anything credential-shaped.
"""

from __future__ import annotations

import hashlib
import json
import re

import pytest
from pydantic import BaseModel

from predarb.venues.kalshi.models import KalshiWsEnvelope, decode_json
from tests.conftest import FIXTURE_ROOT, load_raw, rest_fixture_names, ws_fixture_names
from tests.unit.test_kalshi_wire_models import REST_MODELS

pytestmark = pytest.mark.integration


class TestManifestIntegrity:
    def test_manifest_lists_every_fixture_file(self, manifest):
        listed = {entry["file"] for entry in manifest["fixtures"]}
        on_disk = {f"rest/{n}" for n in rest_fixture_names()} | {
            f"websocket/{n}" for n in ws_fixture_names()
        }
        assert on_disk == listed

    def test_recorded_hashes_match_the_files(self, manifest):
        for entry in manifest["fixtures"]:
            actual = hashlib.sha256(load_raw(entry["file"])).hexdigest()
            assert actual == entry["sha256"], f"{entry['file']} changed since capture"

    def test_every_entry_has_provenance(self, manifest):
        for entry in manifest["fixtures"]:
            assert entry["source"] in {"REAL", "SYNTHETIC"}
            assert entry["endpoint"]
            assert "raw_unmodified" in entry
            if entry["source"] == "REAL":
                assert entry["captured_at_utc"], f"{entry['file']} has no capture time"
                assert entry["raw_unmodified"] is True
                assert entry["http_status"] == 200

    def test_synthetic_fixtures_are_labelled_everywhere(self, manifest):
        for entry in manifest["fixtures"]:
            if entry["source"] == "SYNTHETIC":
                # Labelled in the manifest, in the path, and in the filename.
                assert entry["file"].startswith("websocket/")
                assert "synthetic" in entry["file"].rsplit("/", 1)[-1]

    def test_no_real_websocket_fixture_is_claimed(self, manifest):
        # The production socket returns 401 without credentials, so a fixture
        # claiming to be a real captured message would be a fabrication.
        ws_entries = [e for e in manifest["fixtures"] if e["file"].startswith("websocket/")]
        assert ws_entries
        assert all(e["source"] == "SYNTHETIC" for e in ws_entries)

    def test_websocket_readme_exists(self):
        readme = (FIXTURE_ROOT / "websocket" / "README.md").read_text()
        assert "SYNTHETIC" in readme
        assert "401" in readme


class TestAllFixturesStillParse:
    @pytest.mark.parametrize("name", rest_fixture_names())
    def test_rest_fixture_parses(self, name):
        path = f"rest/{name}"
        assert path in REST_MODELS, f"{path} has no model mapping; add one"
        model: type[BaseModel] = REST_MODELS[path]
        model.model_validate(decode_json(load_raw(path)))

    @pytest.mark.parametrize("name", ws_fixture_names())
    def test_websocket_fixture_parses(self, name):
        KalshiWsEnvelope.model_validate(decode_json(load_raw(f"websocket/{name}")))

    def test_every_fixture_is_valid_json(self):
        for entry in (FIXTURE_ROOT / "rest").glob("*.json"):
            json.loads(entry.read_text())


FORBIDDEN_KEYS = {
    "kalshi-access-key",
    "kalshi-access-signature",
    "kalshi-access-timestamp",
    "authorization",
    "cookie",
    "set-cookie",
    "api_key",
    "apikey",
    "access_key",
    "private_key",
    "secret",
    "password",
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
    "portfolio",
}

CREDENTIAL_PATTERNS = {
    "PEM private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "Bearer token": re.compile(r"[Bb]earer\s+[A-Za-z0-9._\-]{10,}"),
    "JWT": re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\."),
    "AWS access key": re.compile(r"AKIA[0-9A-Z]{16}"),
}


def _forbidden_keys(node: object, path: str = "") -> list[str]:
    hits: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key.lower() in FORBIDDEN_KEYS:
                hits.append(f"{path}.{key}")
            hits += _forbidden_keys(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            hits += _forbidden_keys(value, f"{path}[{index}]")
    return hits


class TestCredentialSafety:
    """Fixtures must contain public market data and nothing else."""

    @pytest.mark.parametrize(
        "relative",
        [f"rest/{n}" for n in rest_fixture_names()]
        + [f"websocket/{n}" for n in ws_fixture_names()],
    )
    def test_no_credential_shaped_content(self, relative):
        text = load_raw(relative).decode()
        for name, pattern in CREDENTIAL_PATTERNS.items():
            assert not pattern.search(text), f"{relative} matches {name}"

    @pytest.mark.parametrize(
        "relative",
        [f"rest/{n}" for n in rest_fixture_names()]
        + [f"websocket/{n}" for n in ws_fixture_names()],
    )
    def test_no_account_scoped_keys(self, relative):
        hits = _forbidden_keys(json.loads(load_raw(relative)))
        assert hits == [], f"{relative} contains account-scoped keys: {hits}"

    def test_manifest_itself_is_clean(self):
        text = (FIXTURE_ROOT / "manifest.json").read_text()
        for name, pattern in CREDENTIAL_PATTERNS.items():
            assert not pattern.search(text), f"manifest matches {name}"
