"""Machine-readable proof that a market's payoff semantics are known.

Why this exists
---------------
A detector cannot prove a state-independent payoff without knowing, exactly,
what the contract pays in every state it can reach. Nothing in the market
metadata establishes that.

In particular **``market_type == "binary"`` is not sufficient evidence** that the
allowed settlement values are exactly ``{0, notional}``. Kalshi's own rules
acknowledge markets that can resolve to a fair-market or otherwise non-standard
value -- did-not-play provisions being the documented example -- and those
resolutions are governed by the per-series contract terms, not by the
``market_type`` field. A contract can carry ``market_type == "binary"`` and
still have a third terminal value that no two-state payoff table describes.

So a certificate is an assertion about the *rules text*, made deliberately,
recorded with its evidence, and bound to the exact rules it was made against.

Never inferred
--------------
A certificate is never derived from a title, ticker, category, subtitle or
``market_type``. Those describe a market; they do not specify its settlement.
Construction requires an explicit verification method and evidence, and the
default status for anything not explicitly verified is a blocking one.

Bound to an evidence fingerprint
--------------------------------
A certificate names the :class:`SettlementEvidenceFingerprint` it was verified
against, and is **INVALIDATED** at the point of use if the current fingerprint
differs or cannot be established. A venue may rewrite a market's terms after a
proof was made, and a stale semantic proof is worse than no proof, because it
looks authoritative.

The fingerprint spans more than the rules text -- notional, market type,
early-close conditions, expiration metadata, settlement sources, external
contract documents. Binding to ``rules_hash`` alone would permit exactly the
failure this design exists to prevent: rules byte-identical, materially relevant
settlement evidence changed, certificate silently still valid. ``rules_hash``
remains a separately visible field because it is the component a reviewer reads
first and the one most useful in a diff.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final

from predarb.clock import ensure_utc
from predarb.domain.money import Price, Quantity
from predarb.domain.payoff import (
    PayoffModel,
    Position,
    SettlementState,
    TabulatedPayoff,
)
from predarb.semantics.fingerprint import SettlementEvidenceFingerprint

_BINARY_STATE_COUNT: Final = 2
"""A complement has exactly two states: one for each side of the contract."""

__all__ = [
    "CertificateStatus",
    "SettlementCertificate",
    "SettlementModel",
    "standard_binary_complement",
]


class SettlementModel(StrEnum):
    """The shape of a market's terminal payoff."""

    STANDARD_BINARY_COMPLEMENT = "STANDARD_BINARY_COMPLEMENT"
    """Exactly two states; YES and NO payouts sum to the notional in both. The
    only model Phase 1 will build a contractual proof on."""

    SCALAR = "SCALAR"
    """Settles on a range. Representable here, but Phase 1 proves nothing about
    it: a complete payoff table would be needed and none is established."""

    NON_STANDARD = "NON_STANDARD"
    """Known to admit a value outside the simple two-state table -- a
    fair-market resolution, a did-not-play adjustment, a 50/50 split, a void or
    refund. Explicitly recorded so it is blocked rather than forgotten."""

    UNKNOWN = "UNKNOWN"
    """Rules not read, or read and not understood. Fails closed."""


class CertificateStatus(StrEnum):
    """Whether this certificate may be relied on."""

    VERIFIED = "VERIFIED"
    """Someone read the rules, recorded evidence, and asserts the payoff table
    is complete and correct for that exact rules text."""

    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    """Not yet established. The default for anything unexamined."""

    REJECTED = "REJECTED"
    """Examined and found not to admit a provable payoff table."""

    INVALIDATED = "INVALIDATED"
    """Was verified, but the rules have changed since. Never auto-revived."""

    @property
    def permits_proof(self) -> bool:
        return self is CertificateStatus.VERIFIED


@dataclass(frozen=True, slots=True)
class SettlementCertificate:
    """A dated, evidence-bearing assertion about one market's payoff table.

    ``yes_payoff`` and ``no_payoff`` are complete tables over ``allowed_states``.
    For ``STANDARD_BINARY_COMPLEMENT`` the constructor additionally *checks*
    that they sum to the notional in every state, so a certificate cannot claim
    that model while describing something else.
    """

    market_ticker: str
    evidence_fingerprint: SettlementEvidenceFingerprint
    """The full settlement evidence this proof was made against."""

    rules_hash: str
    """The rules-text component, kept visible for audit and diffs.

    Not the binding: :attr:`evidence_fingerprint` is. A matching rules hash
    alongside a differing fingerprint means something *other* than the rules
    moved, and the certificate is still invalid."""

    notional: Price
    settlement_model: SettlementModel
    allowed_states: tuple[SettlementState, ...]
    yes_payoff: PayoffModel
    no_payoff: PayoffModel
    status: CertificateStatus
    evidence: str
    verified_by: str
    verification_method: str
    verified_at: datetime
    valid_from: datetime
    valid_to: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "verified_at", ensure_utc(self.verified_at))
        object.__setattr__(self, "valid_from", ensure_utc(self.valid_from))
        if self.valid_to is not None:
            object.__setattr__(self, "valid_to", ensure_utc(self.valid_to))
            if self.valid_to < self.valid_from:
                raise ValueError(f"{self.market_ticker}: certificate expires before it begins")
        if not self.allowed_states:
            raise ValueError(f"{self.market_ticker}: no allowed settlement states")
        if len(set(self.allowed_states)) != len(self.allowed_states):
            raise ValueError(f"{self.market_ticker}: duplicate allowed settlement states")
        if self.status is CertificateStatus.VERIFIED:
            if not self.evidence.strip():
                raise ValueError(
                    f"{self.market_ticker}: a VERIFIED certificate must carry evidence; "
                    "a bare assertion is not a proof"
                )
            if not self.verification_method.strip() or not self.verified_by.strip():
                raise ValueError(
                    f"{self.market_ticker}: a VERIFIED certificate must record who "
                    "verified it and how"
                )
            if not self.rules_hash.strip():
                raise ValueError(
                    f"{self.market_ticker}: a VERIFIED certificate must record a rules "
                    "hash component for audit"
                )
        if self.settlement_model is SettlementModel.STANDARD_BINARY_COMPLEMENT:
            self._check_complement()

    def _check_complement(self) -> None:
        """YES + NO must pay exactly the notional in every allowed state.

        This is the invariant the binary detector relies on; checking it here
        means a mislabelled certificate fails at construction rather than
        silently producing a wrong worst case.
        """
        if len(self.allowed_states) != _BINARY_STATE_COUNT:
            raise ValueError(
                f"{self.market_ticker}: STANDARD_BINARY_COMPLEMENT needs exactly two "
                f"states, got {[s.name for s in self.allowed_states]}"
            )
        for state in self.allowed_states:
            # Summed in exact price units: Price deliberately has no __add__,
            # because adding two prices is meaningless outside this one check.
            total_units = (
                self.yes_payoff.payoff_per_contract(state).units
                + self.no_payoff.payoff_per_contract(state).units
            )
            if total_units != self.notional.units:
                raise ValueError(
                    f"{self.market_ticker}: in state {state.name!r} YES+NO pays "
                    f"{Price.from_units(total_units)}, not the notional "
                    f"{self.notional}; this is not a complement"
                )

    def status_at(
        self,
        *,
        current_fingerprint: SettlementEvidenceFingerprint | None,
        at: datetime,
    ) -> CertificateStatus:
        """The status that actually applies right now.

        An evidence mismatch downgrades a VERIFIED certificate to INVALIDATED,
        and unavailable current evidence is treated the same way: if we cannot
        *show* the evidence is unchanged, we must not assume it is.
        """
        moment = ensure_utc(at)
        if self.status is not CertificateStatus.VERIFIED:
            return self.status
        if not self.evidence_fingerprint.matches(current_fingerprint):
            return CertificateStatus.INVALIDATED
        if moment < self.valid_from:
            return CertificateStatus.REVIEW_REQUIRED
        if self.valid_to is not None and moment > self.valid_to:
            return CertificateStatus.REVIEW_REQUIRED
        return CertificateStatus.VERIFIED

    def permits_proof_at(
        self,
        *,
        current_fingerprint: SettlementEvidenceFingerprint | None,
        at: datetime,
    ) -> bool:
        return self.status_at(current_fingerprint=current_fingerprint, at=at).permits_proof

    def blocking_reason(
        self,
        *,
        current_fingerprint: SettlementEvidenceFingerprint | None,
        at: datetime,
    ) -> str | None:
        """Why this certificate cannot be relied on, or ``None`` if it can.

        Names the specific components that moved where it can, because "the
        evidence changed" does not tell a reviewer whether to re-read the
        contract or merely re-approve a cosmetic field.
        """
        effective = self.status_at(current_fingerprint=current_fingerprint, at=at)
        if effective.permits_proof:
            return None
        if effective is CertificateStatus.INVALIDATED:
            if current_fingerprint is None:
                return (
                    f"{self.market_ticker}: current settlement evidence is unavailable, "
                    f"so the certificate for {self.evidence_fingerprint.short} cannot be "
                    "shown to still apply"
                )
            drift = self.evidence_fingerprint.diff(current_fingerprint)
            return (
                f"{self.market_ticker}: settlement evidence has changed since "
                f"verification ({self.evidence_fingerprint.short} -> "
                f"{current_fingerprint.short}); {drift.describe()}"
            )
        return (
            f"{self.market_ticker}: settlement certificate is {effective.value} "
            f"(model {self.settlement_model.value})"
        )

    def positions_for(
        self, *, yes_quantity: Quantity, no_quantity: Quantity
    ) -> tuple[Position, ...]:
        """Build payoff positions from this certificate's own tables.

        The detector never writes a payoff table itself; it asks the certificate.
        That keeps "what the contract pays" in one place, next to the evidence
        for it.
        """
        return (
            Position(label="YES", quantity=yes_quantity, payoff=self.yes_payoff),
            Position(label="NO", quantity=no_quantity, payoff=self.no_payoff),
        )

    @property
    def identity(self) -> str:
        """Short, stable identifier for audit output."""
        return f"{self.market_ticker}@{self.evidence_fingerprint.short}"


def standard_binary_complement(
    *,
    market_ticker: str,
    evidence_fingerprint: SettlementEvidenceFingerprint,
    rules_hash: str,
    notional: Price,
    evidence: str,
    verified_by: str,
    verification_method: str,
    verified_at: datetime,
    valid_from: datetime,
    valid_to: datetime | None = None,
    status: CertificateStatus = CertificateStatus.VERIFIED,
    yes_state: str = "YES",
    no_state: str = "NO",
) -> SettlementCertificate:
    """Construct the ordinary two-state complement certificate.

    Still requires evidence and a named verification method: this helper spares
    the caller writing two payoff tables, not the obligation to have read the
    rules.
    """
    states = (SettlementState(yes_state), SettlementState(no_state))
    return SettlementCertificate(
        market_ticker=market_ticker,
        evidence_fingerprint=evidence_fingerprint,
        rules_hash=rules_hash,
        notional=notional,
        settlement_model=SettlementModel.STANDARD_BINARY_COMPLEMENT,
        allowed_states=states,
        yes_payoff=TabulatedPayoff.binary(
            winning_state=yes_state, losing_state=no_state, notional=notional
        ),
        no_payoff=TabulatedPayoff.binary(
            winning_state=no_state, losing_state=yes_state, notional=notional
        ),
        status=status,
        evidence=evidence,
        verified_by=verified_by,
        verification_method=verification_method,
        verified_at=verified_at,
        valid_from=valid_from,
        valid_to=valid_to,
    )
