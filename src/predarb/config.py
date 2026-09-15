"""Runtime configuration.

Settings are read from the environment (and ``.env``) with a ``PREDARB_``
prefix. Two rules are enforced here rather than left to convention:

1. Endpoint URLs are derived from a single ``kalshi_env`` selector, so a demo
   key can never be pointed at production by editing one URL and forgetting
   another.
2. There is no order-submission configuration of any kind. Phase 1 has no
   code path that places an order, and the config surface reflects that.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import ClassVar

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["KalshiEndpoints", "KalshiEnv", "Settings"]


class KalshiEnv(StrEnum):
    DEMO = "demo"
    PROD = "prod"


class KalshiEndpoints:
    """Base URLs per environment, verified against current Kalshi docs.

    See ``docs/api_assumptions.md`` (A-01, A-02). The production websocket path
    is flagged there as an open question: the docs show the demo socket at
    ``/trade-api/ws/v2`` but render the production socket as a bare host. The
    value below therefore carries the path for both and is overridable, and the
    connector logs the resolved URL so a mismatch is obvious immediately.
    """

    # Read-only maps: these are safety-relevant constants, not defaults to be
    # mutated at runtime.
    _REST: ClassVar[Mapping[KalshiEnv, str]] = MappingProxyType(
        {
            KalshiEnv.PROD: "https://external-api.kalshi.com/trade-api/v2",
            KalshiEnv.DEMO: "https://external-api.demo.kalshi.co/trade-api/v2",
        }
    )
    _WS: ClassVar[Mapping[KalshiEnv, str]] = MappingProxyType(
        {
            KalshiEnv.PROD: "wss://external-api-ws.kalshi.com/trade-api/ws/v2",
            KalshiEnv.DEMO: "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2",
        }
    )

    @classmethod
    def rest(cls, env: KalshiEnv) -> str:
        return cls._REST[env]

    @classmethod
    def websocket(cls, env: KalshiEnv) -> str:
        return cls._WS[env]


class Settings(BaseSettings):
    """Process configuration."""

    model_config = SettingsConfigDict(
        env_prefix="PREDARB_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # --- Venue -------------------------------------------------------------
    kalshi_env: KalshiEnv = KalshiEnv.DEMO
    kalshi_api_key_id: str | None = None
    kalshi_private_key_path: Path | None = None
    kalshi_rest_base_url: str | None = None
    kalshi_ws_url: str | None = None

    # --- Storage -----------------------------------------------------------
    database_url: str = "postgresql+psycopg://predarb:predarb@localhost:5432/predarb"
    raw_journal_dir: Path = Path("./data/raw")

    # --- Safety ------------------------------------------------------------
    max_book_age_ms: int = Field(default=2000, gt=0)
    """A book older than this is ineligible for any arbitrage alert."""

    book_validation_interval_s: int = Field(default=60, ge=0)
    """Periodic REST cross-check of reconstructed books. 0 disables."""

    # --- Logging -----------------------------------------------------------
    log_level: str = "INFO"
    log_format: str = "json"

    # --- Tests -------------------------------------------------------------
    allow_live_tests: bool = False

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, value: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = value.upper()
        if upper not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}, got {value!r}")
        return upper

    @property
    def rest_base_url(self) -> str:
        return self.kalshi_rest_base_url or KalshiEndpoints.rest(self.kalshi_env)

    @property
    def ws_url(self) -> str:
        return self.kalshi_ws_url or KalshiEndpoints.websocket(self.kalshi_env)

    @property
    def has_credentials(self) -> bool:
        """Whether signed requests are possible.

        Public market data does not need credentials, so the collector runs
        without them; only private endpoints check this.
        """
        return self.kalshi_api_key_id is not None and self.kalshi_private_key_path is not None
