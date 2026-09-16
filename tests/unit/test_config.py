"""Tests for configuration, credential naming and environment selection."""

from __future__ import annotations

from pathlib import Path

import pytest

from predarb.config import KalshiEndpoints, KalshiEnv, Settings

pytestmark = pytest.mark.unit


def isolated_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    """Build Settings with no .env in scope.

    Isolates by changing the working directory rather than passing the private
    ``_env_file`` keyword, so the real construction path is what gets tested.
    """
    monkeypatch.chdir(tmp_path)
    return Settings()


class TestEnvironmentParsing:
    @pytest.mark.parametrize("raw", ["prod", "production", "PRODUCTION", "live", " Prod "])
    def test_production_spellings(self, raw):
        assert KalshiEnv(raw) is KalshiEnv.PROD

    @pytest.mark.parametrize("raw", ["demo", "DEMO", "sandbox", "test"])
    def test_demo_spellings(self, raw):
        assert KalshiEnv(raw) is KalshiEnv.DEMO

    def test_unknown_value_rejected(self):
        # Silently defaulting would send a production key to demo.
        with pytest.raises(ValueError, match="not a valid KalshiEnv"):
            KalshiEnv("staging")


class TestEndpointSelection:
    def test_production_urls(self):
        assert KalshiEndpoints.rest(KalshiEnv.PROD) == (
            "https://external-api.kalshi.com/trade-api/v2"
        )
        assert KalshiEndpoints.websocket(KalshiEnv.PROD) == (
            "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
        )

    def test_demo_urls(self):
        assert KalshiEndpoints.rest(KalshiEnv.DEMO) == (
            "https://external-api.demo.kalshi.co/trade-api/v2"
        )
        assert KalshiEndpoints.websocket(KalshiEnv.DEMO) == (
            "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"
        )

    def test_environments_use_different_hosts(self):
        # Note the differing TLDs: .com for production, .co for demo.
        assert KalshiEndpoints.rest(KalshiEnv.PROD) != KalshiEndpoints.rest(KalshiEnv.DEMO)

    def test_one_selector_drives_both_urls(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A single switch moves REST and WebSocket together.

        Editing one URL and forgetting the other is how a production key ends
        up pointed at demo.
        """
        monkeypatch.setenv("KALSHI_ENVIRONMENT", "production")
        settings = isolated_settings(monkeypatch, tmp_path)
        assert "external-api.kalshi.com" in settings.rest_base_url
        assert "external-api-ws.kalshi.com" in settings.ws_url


class TestCredentialNaming:
    def test_plain_kalshi_names_are_accepted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("KALSHI_API_KEY_ID", "abc-123")
        monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(tmp_path / "key.pem"))
        settings = isolated_settings(monkeypatch, tmp_path)
        assert settings.kalshi_api_key_id == "abc-123"
        assert settings.has_credentials

    def test_predarb_prefixed_names_are_accepted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("PREDARB_KALSHI_API_KEY_ID", "abc-123")
        monkeypatch.setenv("PREDARB_KALSHI_PRIVATE_KEY_PATH", str(tmp_path / "key.pem"))
        settings = isolated_settings(monkeypatch, tmp_path)
        assert settings.kalshi_api_key_id == "abc-123"

    def test_no_credentials_is_a_valid_state(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        for name in (
            "KALSHI_API_KEY_ID",
            "KALSHI_PRIVATE_KEY_PATH",
            "PREDARB_KALSHI_API_KEY_ID",
            "PREDARB_KALSHI_PRIVATE_KEY_PATH",
        ):
            monkeypatch.delenv(name, raising=False)
        settings = isolated_settings(monkeypatch, tmp_path)
        # Public market data needs no credentials, so this must not be an error.
        assert not settings.has_credentials

    def test_there_is_no_inline_private_key_setting(self):
        # The key is loaded from a path, never stored in configuration.
        field_names = set(Settings.model_fields)
        for forbidden in ("kalshi_private_key", "private_key_pem", "kalshi_private_key_pem"):
            assert forbidden not in field_names


class TestNoTradingConfiguration:
    def test_no_order_or_trading_settings_exist(self):
        field_names = set(Settings.model_fields)
        for forbidden in ("enable_trading", "allow_orders", "max_order_size", "trading_enabled"):
            assert forbidden not in field_names
