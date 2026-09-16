"""Wiring between configuration and the Kalshi adapter.

Kept separate so :mod:`predarb.config` stays free of venue imports: settings
describe *what* to connect to, and this module knows *how*. It is also the one
place that turns configuration into a loaded private key, which keeps the
credential path short and easy to audit.
"""

from __future__ import annotations

from predarb.clock import Clock, SystemClock
from predarb.config import Settings
from predarb.venues.kalshi.auth import KalshiCredentials, KalshiSigner, PrivateKeyError
from predarb.venues.kalshi.client import KalshiReadOnlyClient
from predarb.venues.kalshi.websocket import KalshiWebSocketClient

__all__ = [
    "credentials_from_settings",
    "read_only_client",
    "signer_from_settings",
    "websocket_client",
]


def credentials_from_settings(settings: Settings) -> KalshiCredentials:
    """Load the RSA key named by configuration.

    The key is read from its path at call time. It is never stored in
    configuration, copied into fixtures, or written into the repository.
    """
    if settings.kalshi_api_key_id is None or settings.kalshi_private_key_path is None:
        raise PrivateKeyError(
            "Kalshi credentials are not configured. Set KALSHI_API_KEY_ID and "
            "KALSHI_PRIVATE_KEY_PATH (or their PREDARB_-prefixed equivalents). "
            "Public market data needs no credentials."
        )
    return KalshiCredentials.from_file(
        api_key_id=settings.kalshi_api_key_id,
        private_key_path=settings.kalshi_private_key_path,
    )


def signer_from_settings(settings: Settings, clock: Clock | None = None) -> KalshiSigner:
    return KalshiSigner(credentials_from_settings(settings), clock or SystemClock())


def read_only_client(
    settings: Settings, clock: Clock | None = None, *, authenticate: bool = False
) -> KalshiReadOnlyClient:
    """Build a client for the configured environment.

    ``authenticate`` is opt-in: public market data needs no credentials, so the
    default keeps the credential path out of ordinary research use entirely.
    """
    resolved_clock = clock or SystemClock()
    if not authenticate:
        return KalshiReadOnlyClient.public(env=settings.kalshi_env, clock=resolved_clock)
    return KalshiReadOnlyClient.authenticated(
        signer=signer_from_settings(settings, resolved_clock),
        env=settings.kalshi_env,
        clock=resolved_clock,
    )


def websocket_client(settings: Settings, clock: Clock | None = None) -> KalshiWebSocketClient:
    """Build a WebSocket client. Always authenticated -- the socket requires it."""
    resolved_clock = clock or SystemClock()
    return KalshiWebSocketClient(
        signer=signer_from_settings(settings, resolved_clock),
        env=settings.kalshi_env,
        clock=resolved_clock,
    )
