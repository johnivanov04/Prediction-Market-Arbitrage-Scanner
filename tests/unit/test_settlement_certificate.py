"""Tests for the settlement certificate: the semantic gate.

The certificate is the only thing standing between "these prices look cheap"
and a contractual-arbitrage claim, so most of these tests are about refusing to
issue or honour one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from predarb.domain.money import Price, Quantity
from predarb.domain.payoff import SettlementState, TabulatedPayoff
from predarb.semantics import certificate as certificate_module
from predarb.semantics.certificate import (
    CertificateStatus,
    SettlementCertificate,
    SettlementModel,
    standard_binary_complement,
)
from predarb.semantics.fingerprint import (
    SettlementEvidenceFingerprint,
    synthetic_fingerprint,
)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
DOLLAR = Price.from_value("1.0000")
HASH = "a" * 64
OTHER_HASH = "b" * 64

# Three components, so a test can move one that is NOT the rules text.
FINGERPRINT = synthetic_fingerprint(
    market_rules_hash=HASH, market_notional="1.0000", series_settlement_sources=("official",)
)
SOURCE_DRIFTED = synthetic_fingerprint(
    market_rules_hash=HASH, market_notional="1.0000", series_settlement_sources=("replaced",)
)
RULES_DRIFTED = synthetic_fingerprint(
    market_rules_hash=OTHER_HASH,
    market_notional="1.0000",
    series_settlement_sources=("official",),
)


def certificate(
    *,
    fingerprint: SettlementEvidenceFingerprint | None = None,
    rules_hash: str = HASH,
    status: CertificateStatus = CertificateStatus.VERIFIED,
    valid_from: datetime = T0,
    valid_to: datetime | None = None,
    notional: Price = DOLLAR,
    ticker: str = "MKT",
) -> SettlementCertificate:
    return standard_binary_complement(
        market_ticker=ticker,
        evidence_fingerprint=fingerprint or FINGERPRINT,
        rules_hash=rules_hash,
        notional=notional,
        evidence="Read contract terms rev 3: settles YES or NO, no DNP clause.",
        verified_by="research",
        verification_method="manual rules review",
        verified_at=T0,
        valid_from=valid_from,
        valid_to=valid_to,
        status=status,
    )


class TestComplementInvariant:
    """A certificate cannot claim the complement model while describing something else."""

    def test_a_standard_certificate_is_accepted(self):
        cert = certificate()
        assert cert.settlement_model is SettlementModel.STANDARD_BINARY_COMPLEMENT
        assert {s.name for s in cert.allowed_states} == {"YES", "NO"}

    def test_yes_plus_no_equals_notional_in_every_state(self):
        cert = certificate()
        for state in cert.allowed_states:
            total = (
                cert.yes_payoff.payoff_per_contract(state).units
                + cert.no_payoff.payoff_per_contract(state).units
            )
            assert total == DOLLAR.units

    def test_a_non_complementary_table_is_refused(self):
        """Payouts that do not sum to the notional are not a complement."""
        with pytest.raises(ValueError, match="this is not a complement"):
            SettlementCertificate(
                market_ticker="MKT",
                evidence_fingerprint=FINGERPRINT,
                rules_hash=HASH,
                notional=DOLLAR,
                settlement_model=SettlementModel.STANDARD_BINARY_COMPLEMENT,
                allowed_states=(SettlementState("YES"), SettlementState("NO")),
                yes_payoff=TabulatedPayoff({"YES": DOLLAR, "NO": Price.from_units(0)}),
                no_payoff=TabulatedPayoff(
                    {"YES": Price.from_units(0), "NO": Price.from_value("0.9000")}
                ),
                status=CertificateStatus.VERIFIED,
                evidence="e",
                verified_by="r",
                verification_method="m",
                verified_at=T0,
                valid_from=T0,
            )

    def test_a_third_state_is_refused_for_the_complement_model(self):
        """A DNP or void state means this is not a two-state complement."""
        with pytest.raises(ValueError, match="needs exactly two"):
            SettlementCertificate(
                market_ticker="MKT",
                evidence_fingerprint=FINGERPRINT,
                rules_hash=HASH,
                notional=DOLLAR,
                settlement_model=SettlementModel.STANDARD_BINARY_COMPLEMENT,
                allowed_states=(
                    SettlementState("YES"),
                    SettlementState("NO"),
                    SettlementState("VOID"),
                ),
                yes_payoff=TabulatedPayoff(
                    {"YES": DOLLAR, "NO": Price.from_units(0), "VOID": Price.from_units(0)}
                ),
                no_payoff=TabulatedPayoff(
                    {"YES": Price.from_units(0), "NO": DOLLAR, "VOID": Price.from_units(0)}
                ),
                status=CertificateStatus.VERIFIED,
                evidence="e",
                verified_by="r",
                verification_method="m",
                verified_at=T0,
                valid_from=T0,
            )

    def test_a_non_dollar_notional_complement_is_fine(self):
        cert = certificate(notional=Price.from_value("5.0000"))
        assert cert.notional == Price.from_value("5.0000")


class TestEvidenceIsMandatory:
    """A VERIFIED certificate is an assertion someone must stand behind."""

    @pytest.mark.parametrize("field", ["evidence", "verified_by", "verification_method"])
    def test_a_verified_certificate_needs_its_provenance(self, field):
        kwargs = {
            "market_ticker": "MKT",
            "evidence_fingerprint": FINGERPRINT,
            "rules_hash": HASH,
            "notional": DOLLAR,
            "evidence": "read the rules",
            "verified_by": "research",
            "verification_method": "manual review",
            "verified_at": T0,
            "valid_from": T0,
        }
        kwargs[field] = "   "
        with pytest.raises(ValueError, match="VERIFIED certificate must"):
            standard_binary_complement(**kwargs)  # type: ignore[arg-type]

    def test_a_verified_certificate_records_a_rules_hash_component(self):
        """Kept for audit even though the binding is the fingerprint."""
        with pytest.raises(ValueError, match="must record a rules hash"):
            certificate(rules_hash="")

    def test_an_unverified_certificate_needs_no_evidence(self):
        """Recording "we have not checked this" must stay cheap."""
        cert = standard_binary_complement(
            market_ticker="MKT",
            evidence_fingerprint=FINGERPRINT,
            rules_hash="",
            notional=DOLLAR,
            evidence="",
            verified_by="",
            verification_method="",
            verified_at=T0,
            valid_from=T0,
            status=CertificateStatus.REVIEW_REQUIRED,
        )
        assert not cert.status.permits_proof


class TestEvidenceInvalidation:
    """A stale semantic proof is worse than none: it looks authoritative."""

    def test_a_matching_fingerprint_permits_proof(self):
        cert = certificate()
        assert cert.status_at(current_fingerprint=FINGERPRINT, at=T0) is CertificateStatus.VERIFIED
        assert cert.permits_proof_at(current_fingerprint=FINGERPRINT, at=T0)
        assert cert.blocking_reason(current_fingerprint=FINGERPRINT, at=T0) is None

    def test_changed_evidence_invalidates(self):
        cert = certificate()
        assert (
            cert.status_at(current_fingerprint=SOURCE_DRIFTED, at=T0)
            is CertificateStatus.INVALIDATED
        )
        assert not cert.permits_proof_at(current_fingerprint=SOURCE_DRIFTED, at=T0)

    def test_matching_rules_with_drifted_evidence_still_invalidates(self):
        """The entire reason the binding is wider than the rules hash.

        Both fingerprints carry a byte-identical rules component; only a
        settlement source moved. Binding to ``rules_hash`` would have called
        this unchanged and left the certificate silently valid.
        """
        assert FINGERPRINT.component("market_rules_hash") == SOURCE_DRIFTED.component(
            "market_rules_hash"
        )
        assert FINGERPRINT.digest != SOURCE_DRIFTED.digest
        cert = certificate()
        assert cert.rules_hash == HASH
        assert not cert.permits_proof_at(current_fingerprint=SOURCE_DRIFTED, at=T0)

    def test_a_rules_change_also_invalidates(self):
        cert = certificate()
        assert not cert.permits_proof_at(current_fingerprint=RULES_DRIFTED, at=T0)

    def test_the_reason_names_the_components_that_moved(self):
        reason = certificate().blocking_reason(current_fingerprint=SOURCE_DRIFTED, at=T0)
        assert reason is not None
        assert "settlement evidence has changed" in reason
        assert "series_settlement_sources" in reason
        assert "market_rules_hash" not in reason.split("CHANGED:")[-1]

    def test_missing_current_evidence_fails_closed(self):
        """Unavailable evidence is not matching evidence."""
        cert = certificate()
        assert cert.status_at(current_fingerprint=None, at=T0) is CertificateStatus.INVALIDATED
        reason = cert.blocking_reason(current_fingerprint=None, at=T0)
        assert reason is not None
        assert "unavailable" in reason

    def test_a_schema_version_change_invalidates(self):
        """A re-encoding is not proof that the evidence is the same."""
        reencoded = SettlementEvidenceFingerprint.over(
            {
                "market_rules_hash": HASH,
                "market_notional": "1.0000",
                "series_settlement_sources": ("official",),
            },
            schema_version="settlement-evidence/2",
        )
        assert not certificate().permits_proof_at(current_fingerprint=reencoded, at=T0)

    def test_invalidation_is_never_auto_revived(self):
        """Re-presenting the original evidence must be the only way back.

        The certificate is a pure function of what it is asked, so there is no
        cached "was valid once" state to exploit.
        """
        cert = certificate()
        assert not cert.permits_proof_at(current_fingerprint=SOURCE_DRIFTED, at=T0)
        assert cert.permits_proof_at(current_fingerprint=FINGERPRINT, at=T0)

    def test_the_rules_hash_remains_visible_for_audit(self):
        """Demoted from the binding, not removed: it is the first thing read."""
        assert certificate().rules_hash == HASH


class TestValidityWindow:
    def test_before_valid_from_requires_review(self):
        cert = certificate(valid_from=T0 + timedelta(days=1))
        assert (
            cert.status_at(current_fingerprint=FINGERPRINT, at=T0)
            is CertificateStatus.REVIEW_REQUIRED
        )

    def test_after_valid_to_requires_review(self):
        cert = certificate(valid_to=T0 + timedelta(days=1))
        assert cert.permits_proof_at(current_fingerprint=FINGERPRINT, at=T0)
        assert not cert.permits_proof_at(current_fingerprint=FINGERPRINT, at=T0 + timedelta(days=2))

    def test_an_expiry_before_the_start_is_refused(self):
        with pytest.raises(ValueError, match="expires before it begins"):
            certificate(valid_to=T0 - timedelta(days=1))

    def test_timestamps_are_normalised_to_utc(self):
        cert = certificate()
        assert cert.verified_at.tzinfo is UTC
        assert cert.valid_from.tzinfo is UTC


class TestBlockingStatuses:
    @pytest.mark.parametrize(
        "status",
        [
            CertificateStatus.REVIEW_REQUIRED,
            CertificateStatus.REJECTED,
            CertificateStatus.INVALIDATED,
        ],
    )
    def test_only_verified_permits_proof(self, status):
        assert not status.permits_proof
        cert = certificate(status=status)
        assert not cert.permits_proof_at(current_fingerprint=FINGERPRINT, at=T0)

    def test_the_reason_names_the_status_and_model(self):
        reason = certificate(status=CertificateStatus.REVIEW_REQUIRED).blocking_reason(
            current_fingerprint=FINGERPRINT, at=T0
        )
        assert reason is not None
        assert "REVIEW_REQUIRED" in reason
        assert "STANDARD_BINARY_COMPLEMENT" in reason


class TestPositions:
    def test_positions_come_from_the_certificate_not_the_detector(self):
        """The payoff table lives next to the evidence for it."""
        positions = certificate().positions_for(
            yes_quantity=Quantity.from_value("2.00"), no_quantity=Quantity.from_value("2.00")
        )
        assert [p.label for p in positions] == ["YES", "NO"]
        assert positions[0].quantity == Quantity.from_value("2.00")
        assert positions[1].quantity == Quantity.from_value("2.00")

    def test_identity_is_stable_and_short(self):
        assert certificate().identity == f"MKT@{FINGERPRINT.short}"


class TestNeverInferred:
    def test_there_is_no_constructor_from_market_metadata(self):
        """market_type == 'binary' must never mint a certificate.

        Kalshi's own rules admit markets that resolve to a fair-market or
        did-not-play value despite a binary market_type, so the field cannot
        establish a two-state payoff table.
        """
        for forbidden in (
            "from_instrument",
            "from_market",
            "from_market_type",
            "infer",
            "from_title",
            "auto_certify",
        ):
            assert not hasattr(certificate_module, forbidden)
            assert not hasattr(SettlementCertificate, forbidden)

    def test_non_standard_models_exist_to_be_blocked(self):
        for model in (
            SettlementModel.SCALAR,
            SettlementModel.NON_STANDARD,
            SettlementModel.UNKNOWN,
        ):
            assert model is not SettlementModel.STANDARD_BINARY_COMPLEMENT
