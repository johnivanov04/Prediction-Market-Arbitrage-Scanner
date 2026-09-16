"""Tests for Kalshi request signing.

No real credentials are required or used. Every test generates an ephemeral RSA
key in-process, so the suite can verify signatures end to end without any secret
existing on disk or in the repository.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from predarb.clock import FrozenClock
from predarb.venues.kalshi.auth import (
    HEADER_KEY,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    WS_SIGNING_PATH,
    KalshiCredentials,
    KalshiSigner,
    PrivateKeyError,
    build_signing_message,
    canonical_method,
    redact_headers,
    signing_path,
)

pytestmark = pytest.mark.unit

FIXED_TIME = datetime(2026, 9, 15, 12, 0, 0, tzinfo=UTC)
FIXED_MS = int(FIXED_TIME.timestamp() * 1000)


@pytest.fixture(scope="module")
def rsa_key() -> rsa.RSAPrivateKey:
    """An ephemeral key. Generated per test session, never persisted."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def signer(rsa_key: rsa.RSAPrivateKey) -> KalshiSigner:
    credentials = KalshiCredentials(api_key_id="test-key-id", private_key=rsa_key)
    return KalshiSigner(credentials, FrozenClock(FIXED_TIME))


def verify(key: rsa.RSAPrivateKey, message: str, signature_b64: str) -> None:
    """Verify with the paired public key, using the documented parameters."""
    key.public_key().verify(
        base64.b64decode(signature_b64),
        message.encode("utf-8"),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


class TestSigningPath:
    def test_strips_host_and_query(self):
        url = "https://external-api.kalshi.com/trade-api/v2/markets?limit=100&cursor=abc"
        assert signing_path(url) == "/trade-api/v2/markets"

    def test_does_not_return_the_base_relative_path(self):
        url = "https://external-api.kalshi.com/trade-api/v2/markets?limit=100&cursor=abc"
        assert signing_path(url) != "/markets"

    def test_does_not_retain_the_query(self):
        url = "https://external-api.kalshi.com/trade-api/v2/markets?limit=100&cursor=abc"
        assert signing_path(url) != "/trade-api/v2/markets?limit=100&cursor=abc"

    def test_accepts_a_bare_path(self):
        assert signing_path("/trade-api/v2/markets") == "/trade-api/v2/markets"

    def test_documented_example_from_the_api_docs(self):
        assert (
            signing_path("/trade-api/v2/portfolio/orders?limit=5")
            == "/trade-api/v2/portfolio/orders"
        )

    def test_websocket_path(self):
        assert signing_path(WS_SIGNING_PATH) == "/trade-api/ws/v2"

    @pytest.mark.parametrize("bad", ["/markets", "/v2/markets", "", "/api/trade-api/v2/markets"])
    def test_rejects_a_path_missing_the_api_prefix(self, bad):
        # A path relative to the API base signs a message the server never
        # computes, and the resulting 401 looks exactly like a bad key.
        with pytest.raises(ValueError, match="must begin with"):
            signing_path(bad)

    def test_fragment_is_dropped(self):
        assert signing_path("/trade-api/v2/markets#frag") == "/trade-api/v2/markets"


class TestCanonicalMethod:
    @pytest.mark.parametrize(("raw", "expected"), [("get", "GET"), ("GeT", "GET"), ("GET", "GET")])
    def test_lowercase_is_canonicalised(self, raw, expected):
        assert canonical_method(raw) == expected

    @pytest.mark.parametrize("bad", ["", "GET ", "GE T", "GET\n", "G3T", "GET/POST"])
    def test_non_token_methods_rejected(self, bad):
        # A method containing whitespace could shift bytes inside the signed
        # message and change what was actually signed.
        with pytest.raises(ValueError, match="invalid HTTP method"):
            canonical_method(bad)


class TestSigningMessage:
    def test_exact_bytes(self):
        message = build_signing_message(FIXED_MS, "GET", "/trade-api/v2/markets")
        assert message == f"{FIXED_MS}GET/trade-api/v2/markets"

    def test_concatenation_order_is_timestamp_method_path(self):
        message = build_signing_message(1700000000000, "get", "/trade-api/v2/exchange/status")
        assert message == "1700000000000GET/trade-api/v2/exchange/status"

    def test_query_excluded_from_the_signed_message(self):
        url = "https://external-api.kalshi.com/trade-api/v2/markets?limit=100&cursor=abc"
        assert build_signing_message(1, "GET", url) == "1GET/trade-api/v2/markets"

    def test_websocket_message(self):
        assert build_signing_message(FIXED_MS, "GET", WS_SIGNING_PATH) == (
            f"{FIXED_MS}GET/trade-api/ws/v2"
        )

    def test_float_timestamp_rejected(self):
        with pytest.raises(TypeError):
            build_signing_message(1.5, "GET", "/trade-api/v2/markets")  # type: ignore[arg-type]

    def test_bool_timestamp_rejected(self):
        with pytest.raises(TypeError):
            build_signing_message(True, "GET", "/trade-api/v2/markets")


class TestSignatureVerification:
    def test_signature_verifies_with_the_paired_public_key(self, signer, rsa_key):
        headers = signer.sign("GET", "/trade-api/v2/markets")
        verify(rsa_key, headers.signed_message, headers.signature)

    def test_wrong_path_fails_verification(self, signer, rsa_key):
        headers = signer.sign("GET", "/trade-api/v2/markets")
        tampered = build_signing_message(headers.timestamp_ms, "GET", "/trade-api/v2/events")
        with pytest.raises(InvalidSignature):
            verify(rsa_key, tampered, headers.signature)

    def test_wrong_timestamp_fails_verification(self, signer, rsa_key):
        headers = signer.sign("GET", "/trade-api/v2/markets")
        tampered = build_signing_message(headers.timestamp_ms + 1, "GET", "/trade-api/v2/markets")
        with pytest.raises(InvalidSignature):
            verify(rsa_key, tampered, headers.signature)

    def test_wrong_method_fails_verification(self, signer, rsa_key):
        headers = signer.sign("GET", "/trade-api/v2/markets")
        tampered = build_signing_message(headers.timestamp_ms, "HEAD", "/trade-api/v2/markets")
        with pytest.raises(InvalidSignature):
            verify(rsa_key, tampered, headers.signature)

    def test_query_inclusion_fails_verification(self, signer, rsa_key):
        # Proves the query really is excluded: signing the path-with-query
        # produces a different message that will not verify.
        headers = signer.sign("GET", "/trade-api/v2/markets?limit=100")
        wrong = f"{headers.timestamp_ms}GET/trade-api/v2/markets?limit=100"
        with pytest.raises(InvalidSignature):
            verify(rsa_key, wrong, headers.signature)

    def test_signature_is_base64(self, signer):
        headers = signer.sign("GET", "/trade-api/v2/markets")
        assert base64.b64encode(base64.b64decode(headers.signature)).decode() == headers.signature


class TestSignerBehaviour:
    def test_uses_the_injected_clock(self, signer):
        assert signer.sign("GET", "/trade-api/v2/markets").timestamp_ms == FIXED_MS

    def test_explicit_timestamp_overrides_the_clock(self, signer):
        assert signer.sign("GET", "/trade-api/v2/markets", timestamp_ms=42).timestamp_ms == 42

    def test_advancing_the_clock_changes_the_timestamp(self, rsa_key):
        clock = FrozenClock(FIXED_TIME)
        s = KalshiSigner(KalshiCredentials(api_key_id="k", private_key=rsa_key), clock)
        first = s.sign("GET", "/trade-api/v2/markets")
        clock.advance_ms(1500)
        second = s.sign("GET", "/trade-api/v2/markets")
        assert second.timestamp_ms == first.timestamp_ms + 1500

    def test_each_call_signs_afresh(self, rsa_key):
        """A retry must never reuse a signature.

        The timestamp is inside the signed message, so a reused header set
        eventually reads to the server as a replay of an old request.
        """
        clock = FrozenClock(FIXED_TIME)
        s = KalshiSigner(KalshiCredentials(api_key_id="k", private_key=rsa_key), clock)
        first = s.rest_headers("GET", "/trade-api/v2/markets")
        clock.advance_ms(1000)
        second = s.rest_headers("GET", "/trade-api/v2/markets")
        assert first[HEADER_TIMESTAMP] != second[HEADER_TIMESTAMP]
        assert first[HEADER_SIGNATURE] != second[HEADER_SIGNATURE]

    def test_rsa_pss_is_randomised_so_signatures_differ_even_when_identical(self, signer):
        # PSS salts each signature, so two signatures over the same message
        # differ. Both must still verify.
        a = signer.sign("GET", "/trade-api/v2/markets")
        b = signer.sign("GET", "/trade-api/v2/markets")
        assert a.signed_message == b.signed_message
        assert a.signature != b.signature

    def test_rest_headers_shape(self, signer):
        headers = signer.rest_headers("GET", "/trade-api/v2/markets")
        assert set(headers) == {HEADER_KEY, HEADER_TIMESTAMP, HEADER_SIGNATURE}
        assert headers[HEADER_KEY] == "test-key-id"

    def test_websocket_headers_sign_the_fixed_path(self, signer, rsa_key):
        headers = signer.websocket_headers()
        expected = f"{headers[HEADER_TIMESTAMP]}GET/trade-api/ws/v2"
        verify(rsa_key, expected, headers[HEADER_SIGNATURE])

    def test_rest_and_websocket_share_one_implementation(self, signer):
        # Same primitive, different path -- so they cannot drift apart.
        rest = signer.sign("GET", "/trade-api/v2/markets")
        ws = signer.sign("GET", WS_SIGNING_PATH)
        assert rest.timestamp_ms == ws.timestamp_ms
        assert rest.signed_message != ws.signed_message


class TestSecretRedaction:
    def test_credentials_repr_hides_everything(self, rsa_key):
        credentials = KalshiCredentials(api_key_id="super-secret-id", private_key=rsa_key)
        assert "super-secret-id" not in repr(credentials)
        assert "super-secret-id" not in str(credentials)
        assert "<redacted>" in repr(credentials)

    def test_credentials_in_an_fstring_do_not_leak(self, rsa_key):
        credentials = KalshiCredentials(api_key_id="super-secret-id", private_key=rsa_key)
        assert "super-secret-id" not in f"credentials={credentials}"

    def test_signer_repr_hides_credentials(self, signer):
        assert "test-key-id" not in repr(signer)

    def test_auth_headers_repr_hides_key_and_signature(self, signer):
        headers = signer.sign("GET", "/trade-api/v2/markets")
        rendered = repr(headers)
        assert headers.signature not in rendered
        assert "test-key-id" not in rendered

    @pytest.mark.parametrize(
        "header",
        [HEADER_KEY, HEADER_SIGNATURE, HEADER_TIMESTAMP, "Authorization", "Cookie"],
    )
    def test_redact_headers_masks_credentials(self, header):
        assert redact_headers({header: "sensitive-value"})[header] == "<redacted>"

    def test_redaction_is_case_insensitive(self):
        assert redact_headers({"kalshi-access-signature": "x"})["kalshi-access-signature"] == (
            "<redacted>"
        )

    def test_non_credential_headers_survive(self):
        result = redact_headers({"Accept": "application/json", HEADER_KEY: "secret"})
        assert result["Accept"] == "application/json"
        assert result[HEADER_KEY] == "<redacted>"

    def test_redaction_tolerates_non_dict_input(self):
        assert redact_headers(None) == {}
        assert redact_headers([("Accept", "json")]) == {"Accept": "json"}

    def test_signed_message_carries_no_secret(self, signer):
        # It holds a timestamp, a method and a path -- safe to log, and useful
        # for diagnosing a signature mismatch.
        message = signer.sign("GET", "/trade-api/v2/markets").signed_message
        assert "test-key-id" not in message


class TestCredentialLoading:
    def test_loads_a_pem_key_from_disk(self, tmp_path: Path, rsa_key: rsa.RSAPrivateKey) -> None:
        pem = rsa_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        key_file = tmp_path / "key.pem"
        key_file.write_bytes(pem)
        key_file.chmod(0o600)
        credentials = KalshiCredentials.from_file(api_key_id="abc", private_key_path=key_file)
        assert credentials.api_key_id == "abc"
        assert credentials.permission_warning is None

    def test_warns_on_permissive_permissions(
        self, tmp_path: Path, rsa_key: rsa.RSAPrivateKey
    ) -> None:
        pem = rsa_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        key_file = tmp_path / "key.pem"
        key_file.write_bytes(pem)
        key_file.chmod(0o644)
        credentials = KalshiCredentials.from_file(api_key_id="abc", private_key_path=key_file)
        # A warning, never a refusal: permission bits mean different things
        # across platforms and a false refusal to start would be worse.
        assert credentials.permission_warning is not None
        assert "chmod 600" in credentials.permission_warning

    def test_missing_file_raises_without_leaking_contents(self, tmp_path: Path) -> None:
        with pytest.raises(PrivateKeyError, match="not found"):
            KalshiCredentials.from_file(api_key_id="abc", private_key_path=tmp_path / "nope.pem")

    def test_malformed_key_error_does_not_echo_file_contents(self, tmp_path: Path) -> None:
        key_file = tmp_path / "bad.pem"
        key_file.write_text(
            "-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n-----END PRIVATE KEY-----"
        )
        with pytest.raises(PrivateKeyError) as exc_info:
            KalshiCredentials.from_file(api_key_id="abc", private_key_path=key_file)
        assert "not-a-real-key" not in str(exc_info.value)

    def test_empty_api_key_id_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(PrivateKeyError, match="api_key_id is empty"):
            KalshiCredentials.from_file(api_key_id="", private_key_path=tmp_path / "k.pem")

    def test_non_rsa_key_rejected(self, tmp_path: Path) -> None:
        key = ed25519.Ed25519PrivateKey.generate()
        pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        key_file = tmp_path / "ed.pem"
        key_file.write_bytes(pem)
        with pytest.raises(PrivateKeyError, match="requires an RSA key"):
            KalshiCredentials.from_file(api_key_id="abc", private_key_path=key_file)
