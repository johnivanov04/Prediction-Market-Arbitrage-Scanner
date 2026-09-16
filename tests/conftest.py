"""Shared fixture-loading helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from predarb.venues.kalshi.models import decode_json

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "kalshi"
REST_DIR = FIXTURE_ROOT / "rest"
WS_DIR = FIXTURE_ROOT / "websocket"
REAL_WS_DIR = FIXTURE_ROOT / "websocket_real"


def load_raw(relative: str) -> bytes:
    """Read a fixture as raw bytes, exactly as captured."""
    return (FIXTURE_ROOT / relative).read_bytes()


def load_payload(relative: str) -> Any:
    """Read and decode a fixture the way production code must decode it.

    Uses :func:`decode_json` (``parse_float=Decimal``) so tests exercise the
    same decoding path as the collector rather than a more forgiving one.
    """
    return decode_json(load_raw(relative))


@pytest.fixture(scope="session")
def manifest() -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURE_ROOT / "manifest.json").read_text())
    return data


def rest_fixture_names() -> list[str]:
    return sorted(p.name for p in REST_DIR.glob("*.json"))


def ws_fixture_names() -> list[str]:
    return sorted(p.name for p in WS_DIR.glob("*.json"))


def real_ws_fixture_names() -> list[str]:
    """Real captured WebSocket frames, kept separate from the synthetic ones."""
    return sorted(p.name for p in REAL_WS_DIR.glob("*.json"))
