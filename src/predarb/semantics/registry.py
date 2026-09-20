"""Issuance and the local certificate registry.

Issuance has exactly one path
-----------------------------
A live certificate can only be created by :func:`issue_certificate`, which
requires a complete evidence bundle, an APPROVED decision recorded against that
exact fingerprint and claim, and a payoff table that passes structural
validation. There is no second constructor that skips the workflow. Synthetic
test certificates live behind a separately named helper so they cannot be
mistaken for reviewed ones.

Append-only, never rewritten
----------------------------
Evidence snapshots, decisions and certificates are immutable records. When
evidence drifts, the old certificate is **not** edited to say the review never
happened -- it stays exactly as issued and becomes inapplicable to current
evidence. That distinction matters for replay: a backtest at a past instant
needs the certificate that was actually in force then, not today's opinion of
it.

Point-in-time
-------------
:meth:`CertificateRegistry.active_at` will never return a certificate issued
after the instant being asked about. A certificate created tomorrow must not
validate a decision made yesterday, or a backtest silently inherits knowledge
nobody had at the time.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from predarb.clock import ensure_utc
from predarb.domain.money import Price
from predarb.semantics.certificate import (
    CertificateStatus,
    SettlementCertificate,
    standard_binary_complement,
)
from predarb.semantics.evidence import SettlementEvidenceBundle
from predarb.semantics.fingerprint import Absent, SettlementEvidenceFingerprint
from predarb.semantics.policy import CertificateClaim, policy_for
from predarb.semantics.review import Decision, ReviewDecision, ReviewRequest

__all__ = [
    "CertificateApplicability",
    "CertificateRecord",
    "CertificateRegistry",
    "IssuanceError",
    "drift_report",
    "issue_certificate",
]


class IssuanceError(Exception):
    """The approval workflow does not permit issuing this certificate."""


class CertificateApplicability(StrEnum):
    """Whether a stored certificate may be used against *current* evidence."""

    ACTIVE = "ACTIVE"
    """Evidence matches; usable for live contractual-arbitrage classification."""

    STALE_EVIDENCE_DRIFT = "STALE_EVIDENCE_DRIFT"
    """Evidence moved since review. The record stays VERIFIED for the evidence
    it was issued against; it simply no longer describes the market today."""

    EVIDENCE_UNAVAILABLE = "EVIDENCE_UNAVAILABLE"
    """Current evidence could not be established. Fails closed."""

    NOT_YET_VALID = "NOT_YET_VALID"
    EXPIRED = "EXPIRED"

    @property
    def permits_live_use(self) -> bool:
        return self is CertificateApplicability.ACTIVE


def issue_certificate(
    *,
    bundle: SettlementEvidenceBundle,
    request: ReviewRequest,
    decision: ReviewDecision,
    notional: Price,
    issued_at: datetime,
    valid_to: datetime | None = None,
) -> SettlementCertificate:
    """Issue a certificate from an approved review, or refuse and say why.

    Every precondition is checked here rather than trusted from the caller:
    completeness, approval, fingerprint identity, claim identity, and that the
    bundle in hand is the one that was reviewed.
    """
    if decision.decision is not Decision.APPROVED:
        raise IssuanceError(f"decision is {decision.decision.value}, not APPROVED")
    blocking = decision.blocking_reason(request)
    if blocking is not None:
        raise IssuanceError(f"review does not support issuance: {blocking}")

    fingerprint = bundle.fingerprint()
    if not fingerprint.matches(request.evidence_fingerprint):
        raise IssuanceError(
            f"the bundle supplied ({fingerprint.short}) is not the one reviewed "
            f"({request.evidence_fingerprint.short})"
        )
    if bundle.snapshot_id != request.snapshot_id:
        raise IssuanceError(
            f"bundle snapshot {bundle.snapshot_id[:12]} is not the reviewed snapshot "
            f"{request.snapshot_id[:12]}"
        )
    if request.claim is not CertificateClaim.STANDARD_BINARY_COMPLEMENT:
        raise IssuanceError(f"no issuance path for claim {request.claim.value}")

    rules_hash = bundle.rules_hash
    if not rules_hash:
        raise IssuanceError("bundle carries no rules hash component")

    moment = ensure_utc(issued_at)
    return standard_binary_complement(
        market_ticker=bundle.market_ticker,
        evidence_fingerprint=fingerprint,
        rules_hash=rules_hash,
        notional=notional,
        evidence=(
            f"Snapshot {bundle.snapshot_id[:16]} captured "
            f"{bundle.captured_at.isoformat()}; policy "
            f"{request.policy_schema_version}; checklist answered in full by "
            f"{decision.reviewer}."
        ),
        verified_by=decision.reviewer,
        verification_method=(
            f"human review of request {request.request_id[:12]} against evidence "
            f"{fingerprint.short}"
        ),
        verified_at=decision.reviewed_at,
        valid_from=moment,
        valid_to=valid_to,
        status=CertificateStatus.VERIFIED,
    )


@dataclass(frozen=True, slots=True)
class CertificateRecord:
    """A stored certificate plus the trail that produced it."""

    certificate_id: str
    claim: CertificateClaim
    market_ticker: str
    snapshot_id: str
    request_id: str
    certificate: SettlementCertificate
    issued_at: datetime
    policy_schema_version: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "issued_at", ensure_utc(self.issued_at))

    def applicability(
        self,
        *,
        current_fingerprint: SettlementEvidenceFingerprint | None,
        at: datetime,
    ) -> CertificateApplicability:
        moment = ensure_utc(at)
        if current_fingerprint is None:
            return CertificateApplicability.EVIDENCE_UNAVAILABLE
        if not self.certificate.evidence_fingerprint.matches(current_fingerprint):
            return CertificateApplicability.STALE_EVIDENCE_DRIFT
        if moment < self.certificate.valid_from:
            return CertificateApplicability.NOT_YET_VALID
        if self.certificate.valid_to is not None and moment > self.certificate.valid_to:
            return CertificateApplicability.EXPIRED
        return CertificateApplicability.ACTIVE

    def describe(self) -> str:
        return (
            f"{self.certificate_id[:12]} {self.market_ticker} {self.claim.value} "
            f"issued {self.issued_at.isoformat()} evidence "
            f"{self.certificate.evidence_fingerprint.short} by "
            f"{self.certificate.verified_by}"
        )


def _certificate_id(certificate: SettlementCertificate, issued_at: datetime) -> str:
    payload = (
        f"{certificate.market_ticker}\x1e{certificate.evidence_fingerprint.digest}"
        f"\x1e{ensure_utc(issued_at).isoformat()}"
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _atomic_write(path: Path, payload: str) -> None:
    """Replace a file atomically, so a crash cannot leave a half-written index."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        Path(temporary).replace(path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


class CertificateRegistry:
    """Append-only local store of evidence, requests, decisions and certificates.

    Deliberately files rather than Postgres: these are low-volume research
    records that must remain readable and diffable by a human, and introducing a
    database dependency for them would buy nothing.

    Records are content-addressed and **never silently overwritten**. Writing a
    record that already exists with different content raises, because that means
    two different things are claiming the same identity.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.evidence_dir = root / "evidence"
        self.requests_dir = root / "requests"
        self.decisions_dir = root / "decisions"
        self.certificates_dir = root / "certificates"
        self.documents_dir = root / "documents"

    def _ensure(self) -> None:
        for directory in (
            self.evidence_dir,
            self.requests_dir,
            self.decisions_dir,
            self.certificates_dir,
            self.documents_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    # -- writing -----------------------------------------------------------

    def _write_record(self, path: Path, payload: dict[str, Any]) -> Path:
        self._ensure()
        text = json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n"
        if path.exists():
            existing = path.read_text(encoding="utf-8")
            if existing != text:
                raise ValueError(
                    f"{path.name} already exists with different content; evidence and "
                    "decisions are append-only and must never be silently overwritten"
                )
            return path
        _atomic_write(path, text)
        return path

    def store_document(self, content_sha256: str, payload: bytes) -> Path:
        """Preserve an external document's exact bytes, addressed by its hash.

        Kept out of the repository by default: a growing mirror of exchange
        contract documents is not ours to redistribute.
        """
        self._ensure()
        path = self.documents_dir / f"{content_sha256}.bin"
        if not path.exists():
            path.write_bytes(payload)
        return path

    def store_evidence(self, bundle: SettlementEvidenceBundle) -> Path:
        return self._write_record(
            self.evidence_dir / f"{bundle.snapshot_id}.json", _evidence_payload(bundle)
        )

    def store_request(self, request: ReviewRequest) -> Path:
        return self._write_record(
            self.requests_dir / f"{request.request_id}.json", _request_payload(request)
        )

    def store_decision(self, decision: ReviewDecision) -> Path:
        name = f"{decision.request_id}-{decision.evidence_fingerprint.short}.json"
        return self._write_record(self.decisions_dir / name, _decision_payload(decision))

    def store_certificate(
        self,
        *,
        certificate: SettlementCertificate,
        claim: CertificateClaim,
        snapshot_id: str,
        request_id: str,
        issued_at: datetime,
        policy_schema_version: str,
    ) -> CertificateRecord:
        record = CertificateRecord(
            certificate_id=_certificate_id(certificate, issued_at),
            claim=claim,
            market_ticker=certificate.market_ticker,
            snapshot_id=snapshot_id,
            request_id=request_id,
            certificate=certificate,
            issued_at=issued_at,
            policy_schema_version=policy_schema_version,
        )
        self._write_record(
            self.certificates_dir / f"{record.certificate_id}.json",
            _certificate_payload(record),
        )
        return record

    # -- reading -----------------------------------------------------------

    def list_certificates(self, *, market_ticker: str | None = None) -> list[CertificateRecord]:
        if not self.certificates_dir.exists():
            return []
        records = [
            _certificate_from_payload(json.loads(path.read_text(encoding="utf-8")))
            for path in sorted(self.certificates_dir.glob("*.json"))
        ]
        if market_ticker is not None:
            records = [r for r in records if r.market_ticker == market_ticker]
        return sorted(records, key=lambda r: r.issued_at)

    def get_certificate(self, certificate_id: str) -> CertificateRecord | None:
        path = self.certificates_dir / f"{certificate_id}.json"
        if not path.exists():
            matches = [
                p for p in self.certificates_dir.glob("*.json") if p.stem.startswith(certificate_id)
            ]
            if len(matches) != 1:
                return None
            path = matches[0]
        return _certificate_from_payload(json.loads(path.read_text(encoding="utf-8")))

    def active_at(
        self,
        *,
        market_ticker: str,
        claim: CertificateClaim,
        current_fingerprint: SettlementEvidenceFingerprint | None,
        at: datetime,
    ) -> CertificateRecord | None:
        """The certificate usable for ``market_ticker`` at ``at``, if any.

        Point-in-time in both directions. A certificate issued after ``at`` is
        invisible, so a replay cannot inherit knowledge nobody had yet; and one
        whose evidence has drifted is not returned, because it no longer
        describes the market being asked about.
        """
        moment = ensure_utc(at)
        candidates = [
            record
            for record in self.list_certificates(market_ticker=market_ticker)
            if record.claim is claim
            and record.issued_at <= moment
            and record.applicability(
                current_fingerprint=current_fingerprint, at=moment
            ).permits_live_use
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda r: r.issued_at)

    def history_for(self, market_ticker: str) -> list[CertificateRecord]:
        """Every certificate ever issued for a market, drifted ones included.

        Retained rather than deleted: a replay of a past instant needs the
        certificate that was actually in force then.
        """
        return self.list_certificates(market_ticker=market_ticker)

    def load_evidence(self, snapshot_id: str) -> dict[str, Any] | None:
        path = self.evidence_dir / f"{snapshot_id}.json"
        if not path.exists():
            matches = [
                p for p in self.evidence_dir.glob("*.json") if p.stem.startswith(snapshot_id)
            ]
            if len(matches) != 1:
                return None
            path = matches[0]
        payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return payload

    def list_requests(self) -> list[dict[str, Any]]:
        if not self.requests_dir.exists():
            return []
        return [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(self.requests_dir.glob("*.json"))
        ]

    def load_request(self, request_id: str) -> dict[str, Any] | None:
        matches = (
            [p for p in self.requests_dir.glob("*.json") if p.stem.startswith(request_id)]
            if self.requests_dir.exists()
            else []
        )
        if len(matches) != 1:
            return None
        payload: dict[str, Any] = json.loads(matches[0].read_text(encoding="utf-8"))
        return payload

    def load_decision_for(self, request_id: str) -> dict[str, Any] | None:
        if not self.decisions_dir.exists():
            return None
        matches = sorted(self.decisions_dir.glob(f"{request_id}-*.json"))
        if not matches:
            return None
        payload: dict[str, Any] = json.loads(matches[-1].read_text(encoding="utf-8"))
        return payload


# -- serialisation ---------------------------------------------------------


def _fingerprint_payload(fingerprint: SettlementEvidenceFingerprint) -> dict[str, Any]:
    return {
        "schema_version": fingerprint.schema_version,
        "digest": fingerprint.digest,
        "components": dict(fingerprint.components),
    }


def _fingerprint_from_payload(payload: dict[str, Any]) -> SettlementEvidenceFingerprint:
    return SettlementEvidenceFingerprint(
        schema_version=payload["schema_version"],
        components=payload["components"],
        digest=payload["digest"],
    )


def _evidence_payload(bundle: SettlementEvidenceBundle) -> dict[str, Any]:
    return {
        "snapshot_id": bundle.snapshot_id,
        "market_ticker": bundle.market_ticker,
        "event_ticker": bundle.event_ticker,
        "series_ticker": bundle.series_ticker,
        "captured_at": bundle.captured_at.isoformat(),
        "schema_version": bundle.schema_version,
        "market_fields": {k: _jsonable(v) for k, v in bundle.market_fields.items()},
        "event_fields": {k: _jsonable(v) for k, v in bundle.event_fields.items()},
        "series_fields": {k: _jsonable(v) for k, v in bundle.series_fields.items()},
        "documents": {
            name: {
                "url": doc.url,
                "retrieval": doc.retrieval.value,
                "retrieved_at": doc.retrieved_at.isoformat() if doc.retrieved_at else None,
                "http_status": doc.http_status,
                "content_type": doc.content_type,
                "content_sha256": doc.content_sha256,
                "content_bytes": doc.content_bytes,
                "extraction": doc.extraction.value,
                "note": doc.note,
            }
            for name, doc in bundle.documents.items()
        },
        "source_refs": dict(bundle.source_refs),
        "capture_errors": list(bundle.capture_errors),
        "fingerprint": _fingerprint_payload(bundle.fingerprint()),
    }


def _jsonable(value: Any) -> Any:
    """Render an evidence value for storage without losing its identity.

    A stored snapshot must re-fingerprint to the digest it had when captured,
    so every structure has to survive the round trip. Absent, null and empty
    stay distinguishable; nested mappings and sequences recurse rather than
    being flattened to a repr, which would turn a mapping into a string and
    silently change the digest on reload.
    """
    if isinstance(value, Absent):
        return {"__absent__": True}
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (str, int)):
        return value
    # Decimals and datetimes are captured as exact text upstream; anything else
    # reaching here is stringified deliberately and identically on both sides.
    return str(value)


def _request_payload(request: ReviewRequest) -> dict[str, Any]:
    return {
        "request_id": request.request_id,
        "claim": request.claim.value,
        "proposition": request.claim.proposition,
        "market_ticker": request.market_ticker,
        "snapshot_id": request.snapshot_id,
        "evidence_fingerprint": _fingerprint_payload(request.evidence_fingerprint),
        "rules_hash": request.rules_hash,
        "status": request.status.value,
        "completeness": {
            "completeness": request.completeness.completeness.value,
            "missing_required": list(request.completeness.missing_required),
            "unretrievable_documents": list(request.completeness.unretrievable_documents),
            "manual_viewing_required": dict(request.completeness.manual_viewing_required),
            "present_optional": list(request.completeness.present_optional),
            "document_requirements": dict(request.completeness.document_requirements),
        },
        "checklist": [
            {"key": q.key, "prompt": q.prompt, "why_it_matters": q.why_it_matters}
            for q in request.checklist
        ],
        "policy_schema_version": request.policy_schema_version,
        "generated_at": request.generated_at.isoformat(),
        "notes": list(request.notes),
    }


def _decision_payload(decision: ReviewDecision) -> dict[str, Any]:
    return {
        "request_id": decision.request_id,
        "claim": decision.claim.value,
        "market_ticker": decision.market_ticker,
        "snapshot_id": decision.snapshot_id,
        "evidence_fingerprint": _fingerprint_payload(decision.evidence_fingerprint),
        "decision": decision.decision.value,
        "reviewer": decision.reviewer,
        "reviewed_at": decision.reviewed_at.isoformat(),
        "checklist_answers": {k: v.value for k, v in decision.checklist_answers.items()},
        "notes": decision.notes,
        "external_evidence_acknowledged": dict(decision.external_evidence_acknowledged),
    }


def _certificate_payload(record: CertificateRecord) -> dict[str, Any]:
    certificate = record.certificate
    return {
        "certificate_id": record.certificate_id,
        "claim": record.claim.value,
        "market_ticker": record.market_ticker,
        "snapshot_id": record.snapshot_id,
        "request_id": record.request_id,
        "issued_at": record.issued_at.isoformat(),
        "policy_schema_version": record.policy_schema_version,
        "certificate": {
            "settlement_model": certificate.settlement_model.value,
            "status": certificate.status.value,
            "notional": certificate.notional.to_str(),
            "rules_hash": certificate.rules_hash,
            "evidence_fingerprint": _fingerprint_payload(certificate.evidence_fingerprint),
            "allowed_states": [s.name for s in certificate.allowed_states],
            "yes_payoff": {
                s.name: certificate.yes_payoff.payoff_per_contract(s).to_str()
                for s in certificate.allowed_states
            },
            "no_payoff": {
                s.name: certificate.no_payoff.payoff_per_contract(s).to_str()
                for s in certificate.allowed_states
            },
            "evidence": certificate.evidence,
            "verified_by": certificate.verified_by,
            "verification_method": certificate.verification_method,
            "verified_at": certificate.verified_at.isoformat(),
            "valid_from": certificate.valid_from.isoformat(),
            "valid_to": certificate.valid_to.isoformat() if certificate.valid_to else None,
        },
    }


def _certificate_from_payload(payload: dict[str, Any]) -> CertificateRecord:
    body = payload["certificate"]
    certificate = standard_binary_complement(
        market_ticker=payload["market_ticker"],
        evidence_fingerprint=_fingerprint_from_payload(body["evidence_fingerprint"]),
        rules_hash=body["rules_hash"],
        notional=Price.from_value(body["notional"]),
        evidence=body["evidence"],
        verified_by=body["verified_by"],
        verification_method=body["verification_method"],
        verified_at=datetime.fromisoformat(body["verified_at"]),
        valid_from=datetime.fromisoformat(body["valid_from"]),
        valid_to=datetime.fromisoformat(body["valid_to"]) if body["valid_to"] else None,
        status=CertificateStatus(body["status"]),
    )
    return CertificateRecord(
        certificate_id=payload["certificate_id"],
        claim=CertificateClaim(payload["claim"]),
        market_ticker=payload["market_ticker"],
        snapshot_id=payload["snapshot_id"],
        request_id=payload["request_id"],
        certificate=certificate,
        issued_at=datetime.fromisoformat(payload["issued_at"]),
        policy_schema_version=payload["policy_schema_version"],
    )


def drift_report(record: CertificateRecord, current: SettlementEvidenceFingerprint | None) -> str:
    """Human-readable account of what moved since a certificate was issued."""
    if current is None:
        return "current evidence unavailable; certificate cannot be shown to still apply"
    diff = record.certificate.evidence_fingerprint.diff(current)
    return diff.describe()


def unused_policy_check(claims: Iterable[CertificateClaim]) -> Sequence[str]:
    """Names of claims with no evidence policy. Used by tests to catch gaps."""
    missing = []
    for claim in claims:
        try:
            policy_for(claim)
        except ValueError:
            missing.append(claim.value)
    return missing
