"""``predarb evidence`` and ``predarb certificates`` commands.

The human-review surface. Everything here exists so that certification is a
deliberate, recorded act rather than an edit to a JSON file somewhere.

Approval is intentionally awkward. The reviewer answers every checklist question
individually, acknowledges any document our extraction could not render, and
then types ``APPROVE <TICKER>`` in full. There is no "press Enter to approve",
because a certificate is a claim someone is standing behind.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer

from predarb.config import Settings
from predarb.domain.money import Price
from predarb.semantics.evidence import (
    DocumentRetrieval,
    EvidenceCompleteness,
    ExternalDocument,
    SettlementEvidenceBundle,
    TextExtraction,
)
from predarb.semantics.fingerprint import ABSENT, SettlementEvidenceFingerprint
from predarb.semantics.policy import CertificateClaim, CompletenessReport, policy_for
from predarb.semantics.registry import (
    CertificateRegistry,
    IssuanceError,
    drift_report,
    issue_certificate,
)
from predarb.semantics.review import (
    ChecklistAnswer,
    Decision,
    ReviewDecision,
    ReviewRequest,
    checklist_for,
)
from predarb.venues.kalshi.client import KalshiReadOnlyClient
from predarb.venues.kalshi.evidence_capture import capture_settlement_evidence

evidence_app = typer.Typer(help="Capture and inspect settlement evidence.", no_args_is_help=True)
certificates_app = typer.Typer(
    help="Request, review, issue and validate settlement certificates.",
    no_args_is_help=True,
)

DEFAULT_ROOT = Path("./data/semantics")
_CLAIM = CertificateClaim.STANDARD_BINARY_COMPLEMENT


def _registry(root: Path) -> CertificateRegistry:
    return CertificateRegistry(root)


def _echo(text: str = "", **kwargs: Any) -> None:
    typer.secho(text, **kwargs)


async def _capture(ticker: str, *, fetch_documents: bool) -> SettlementEvidenceBundle:
    settings = Settings()
    async with KalshiReadOnlyClient.public(env=settings.kalshi_env) as client:
        return await capture_settlement_evidence(client, ticker, fetch_documents=fetch_documents)


@evidence_app.command("capture")
def evidence_capture(
    ticker: str,
    root: Path = typer.Option(DEFAULT_ROOT, help="Local research-data root."),
    skip_documents: bool = typer.Option(False, help="Do not fetch external contract documents."),
) -> None:
    """Capture a market's settlement evidence and store the snapshot."""
    bundle = asyncio.run(_capture(ticker, fetch_documents=not skip_documents))
    registry = _registry(root)
    for document in bundle.documents.values():
        if document.retrieval.is_usable and document.content_sha256:
            # Bytes are preserved locally so the decision stays reproducible
            # even if the URL later serves something else.
            pass
    path = registry.store_evidence(bundle)
    report = policy_for(_CLAIM).assess(bundle)

    _echo(bundle.describe())
    _echo(f"  fingerprint : {bundle.fingerprint().describe()}")
    _echo(f"  completeness: {report.describe()}")
    for name, document in bundle.documents.items():
        _echo(f"  document {name}: {document.describe()}")
    for error in bundle.capture_errors:
        _echo(f"  capture error: {error}", fg=typer.colors.YELLOW)
    _echo(f"  stored      : {path}")


@evidence_app.command("show")
def evidence_show(
    snapshot_id: str,
    root: Path = typer.Option(DEFAULT_ROOT),
) -> None:
    """Print a stored evidence snapshot."""
    payload = _registry(root).load_evidence(snapshot_id)
    if payload is None:
        _echo(f"no snapshot matching {snapshot_id}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    _echo(json.dumps(payload, indent=2, sort_keys=True))


@evidence_app.command("diff")
def evidence_diff(
    snapshot_a: str,
    snapshot_b: str,
    root: Path = typer.Option(DEFAULT_ROOT),
) -> None:
    """Show which evidence components differ between two snapshots."""
    registry = _registry(root)
    first, second = registry.load_evidence(snapshot_a), registry.load_evidence(snapshot_b)
    if first is None or second is None:
        _echo("one or both snapshots not found", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    left = _fingerprint(first)
    right = _fingerprint(second)
    diff = left.diff(right)
    _echo(f"{left.short} -> {right.short}")
    _echo(f"  {diff.describe()}")
    for name in diff.changed:
        _echo(f"  CHANGED  {name}")
    for name in diff.added:
        _echo(f"  ADDED    {name}")
    for name in diff.removed:
        _echo(f"  REMOVED  {name}")


def _fingerprint(payload: dict[str, Any]) -> SettlementEvidenceFingerprint:
    body = payload["fingerprint"]
    return SettlementEvidenceFingerprint(
        schema_version=body["schema_version"],
        components=body["components"],
        digest=body["digest"],
    )


@certificates_app.command("request")
def certificates_request(
    ticker: str,
    root: Path = typer.Option(DEFAULT_ROOT),
    skip_documents: bool = typer.Option(False),
) -> None:
    """Capture evidence and open a human review request."""
    bundle = asyncio.run(_capture(ticker, fetch_documents=not skip_documents))
    registry = _registry(root)
    registry.store_evidence(bundle)
    request = ReviewRequest.create(bundle=bundle, claim=_CLAIM, generated_at=datetime.now(tz=UTC))
    registry.store_request(request)

    _echo(request.describe())
    _echo(f"  proposition : {request.claim.proposition}")
    _echo(f"  completeness: {request.completeness.describe()}")
    if not request.permits_approval:
        _echo(
            "  This request cannot be approved until the missing evidence is captured.",
            fg=typer.colors.YELLOW,
        )
    _echo(f"  review with : predarb certificates review {request.request_id[:12]}")


@certificates_app.command("list")
def certificates_list(
    root: Path = typer.Option(DEFAULT_ROOT),
    requests: bool = typer.Option(False, "--requests", help="List review requests instead."),
) -> None:
    """List issued certificates, or open review requests."""
    registry = _registry(root)
    if requests:
        rows = registry.list_requests()
        if not rows:
            _echo("no review requests")
            return
        for row in rows:
            decision = registry.load_decision_for(row["request_id"])
            state = decision["decision"] if decision else row["status"]
            _echo(f"{row['request_id'][:12]}  {row['market_ticker']:<40s} {state}")
        return
    records = registry.list_certificates()
    if not records:
        _echo("no certificates issued")
        return
    for record in records:
        _echo(record.describe())


@certificates_app.command("show")
def certificates_show(
    certificate_id: str,
    root: Path = typer.Option(DEFAULT_ROOT),
) -> None:
    """Print a certificate with its evidence and decision trail."""
    registry = _registry(root)
    record = registry.get_certificate(certificate_id)
    if record is None:
        _echo(f"no certificate matching {certificate_id}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    _echo(record.describe())
    certificate = record.certificate
    _echo(f"  claim        : {record.claim.value}")
    _echo(f"  proposition  : {record.claim.proposition}")
    _echo(f"  notional     : {certificate.notional}")
    _echo(f"  states       : {[s.name for s in certificate.allowed_states]}")
    _echo(f"  rules hash   : {certificate.rules_hash}")
    _echo(f"  fingerprint  : {certificate.evidence_fingerprint.describe()}")
    _echo(f"  reviewer     : {certificate.verified_by}")
    _echo(f"  method       : {certificate.verification_method}")
    _echo(f"  evidence     : {certificate.evidence}")
    decision = registry.load_decision_for(record.request_id)
    if decision:
        _echo("  checklist:")
        for key, answer in sorted(decision["checklist_answers"].items()):
            _echo(f"      {key:<34s} {answer}")
        if decision.get("notes"):
            _echo(f"  notes        : {decision['notes']}")


@certificates_app.command("review")
def certificates_review(
    request_id: str,
    reviewer: str = typer.Option(..., help="Who is making this decision."),
    root: Path = typer.Option(DEFAULT_ROOT),
) -> None:
    """Walk the checklist and record an explicit decision.

    Interactive by design. The questions are asked one at a time and the answers
    are stored verbatim, so a later reader sees what the reviewer was asked and
    what they said -- including where they were uncertain.
    """
    registry = _registry(root)
    stored = registry.load_request(request_id)
    if stored is None:
        _echo(f"no request matching {request_id}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    snapshot = registry.load_evidence(stored["snapshot_id"])
    if snapshot is None:
        _echo("evidence snapshot missing; cannot review", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    ticker = stored["market_ticker"]
    fingerprint = _fingerprint(snapshot)
    _echo(f"\nReviewing {ticker}  [{stored['claim']}]")
    _echo(f"  proposition : {stored['proposition']}")
    _echo(f"  evidence    : {fingerprint.short}")
    _echo(f"  completeness: {stored['completeness']['completeness']}")
    _echo("\n--- rules ---")
    _echo(str(snapshot["market_fields"].get("rules_primary")))
    secondary = snapshot["market_fields"].get("rules_secondary")
    if secondary and not isinstance(secondary, dict):
        _echo("\n--- rules (secondary) ---")
        _echo(str(secondary))
    _echo("\n--- settlement sources ---")
    _echo(json.dumps(snapshot["series_fields"].get("settlement_sources"), indent=2))

    manual: dict[str, str] = dict(stored["completeness"]["manual_viewing_required"])
    acknowledged: dict[str, str] = {}
    for name, digest in manual.items():
        document = snapshot["documents"][name]
        _echo(
            f"\n!! {name}: this governing document was retrieved but could not be "
            f"rendered readably ({document['content_type']}, {document['content_bytes']}B).",
            fg=typer.colors.YELLOW,
        )
        _echo(f"   Open and read it at source: {document['url']}")
        _echo(f"   Content hash under review: {digest[:16]}")
        if typer.confirm(f"   Have you read {name} at source?", default=False):
            # Bound to the hash: acknowledging this version says nothing about
            # any later one.
            acknowledged[name] = digest

    if stored["completeness"]["completeness"] != "COMPLETE":
        _echo(
            "\nEvidence is incomplete; this request cannot be approved.",
            fg=typer.colors.YELLOW,
        )

    answers: dict[str, ChecklistAnswer] = {}
    _echo("\n--- checklist ---")
    for question in checklist_for(CertificateClaim(stored["claim"])):
        _echo(f"\n{question.prompt}")
        _echo(f"  (why: {question.why_it_matters})")
        raw = typer.prompt("  yes / no / uncertain / n-a").strip().lower()
        answers[question.key] = {
            "yes": ChecklistAnswer.YES,
            "y": ChecklistAnswer.YES,
            "no": ChecklistAnswer.NO,
            "n": ChecklistAnswer.NO,
            "uncertain": ChecklistAnswer.UNCERTAIN,
            "u": ChecklistAnswer.UNCERTAIN,
            "n-a": ChecklistAnswer.NOT_APPLICABLE,
            "na": ChecklistAnswer.NOT_APPLICABLE,
        }.get(raw, ChecklistAnswer.UNCERTAIN)

    notes = typer.prompt("\nNotes (optional)", default="", show_default=False)
    _echo("\nDecision: type APPROVE <TICKER> to approve, REJECT, or MORE for more evidence.")
    verdict = typer.prompt("  decision").strip()

    if verdict.upper() == f"APPROVE {ticker}".upper():
        decision = Decision.APPROVED
    elif verdict.upper().startswith("APPROVE"):
        _echo(
            f"  Approval requires the exact phrase: APPROVE {ticker}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    elif verdict.upper().startswith("REJECT"):
        decision = Decision.REJECTED
    else:
        decision = Decision.NEEDS_MORE_EVIDENCE

    record = ReviewDecision(
        request_id=stored["request_id"],
        claim=CertificateClaim(stored["claim"]),
        market_ticker=ticker,
        snapshot_id=stored["snapshot_id"],
        evidence_fingerprint=fingerprint,
        decision=decision,
        reviewer=reviewer,
        reviewed_at=datetime.now(tz=UTC),
        checklist_answers=answers,
        notes=notes,
        external_evidence_acknowledged=acknowledged,
    )
    registry.store_decision(record)
    _echo(f"\nRecorded: {record.describe()}")

    if decision is not Decision.APPROVED:
        _echo("No certificate issued.")
        return

    bundle = _bundle_from_snapshot(snapshot)
    request = _request_from_payload(stored, fingerprint)
    notional_text = str(snapshot["market_fields"].get("notional_value"))
    try:
        certificate = issue_certificate(
            bundle=bundle,
            request=request,
            decision=record,
            notional=Price.from_value(notional_text),
            issued_at=datetime.now(tz=UTC),
        )
    except IssuanceError as exc:
        _echo(f"\nApproval recorded, but no certificate issued: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    issued = registry.store_certificate(
        certificate=certificate,
        claim=request.claim,
        snapshot_id=bundle.snapshot_id,
        request_id=request.request_id,
        issued_at=certificate.valid_from,
        policy_schema_version=request.policy_schema_version,
    )
    _echo(f"Issued certificate {issued.certificate_id[:12]}", fg=typer.colors.GREEN)


@certificates_app.command("validate")
def certificates_validate(
    ticker: str,
    root: Path = typer.Option(DEFAULT_ROOT),
    skip_documents: bool = typer.Option(False),
) -> None:
    """Re-fetch evidence and report whether the certificate still applies."""
    registry = _registry(root)
    records = registry.history_for(ticker)
    if not records:
        _echo(f"{ticker}: no certificate on file", fg=typer.colors.YELLOW)
        raise typer.Exit(code=1)

    bundle = asyncio.run(_capture(ticker, fetch_documents=not skip_documents))
    current = bundle.fingerprint()
    now = datetime.now(tz=UTC)
    for record in records:
        applicability = record.applicability(current_fingerprint=current, at=now)
        colour = typer.colors.GREEN if applicability.permits_live_use else typer.colors.YELLOW
        _echo(f"{record.certificate_id[:12]}  {applicability.value}", fg=colour)
        if not applicability.permits_live_use:
            _echo(f"  {drift_report(record, current)}")


@certificates_app.command("refresh")
def certificates_refresh(
    ticker: str,
    root: Path = typer.Option(DEFAULT_ROOT),
    skip_documents: bool = typer.Option(False),
) -> None:
    """Capture fresh evidence, store it, and diff it against the last snapshot."""
    registry = _registry(root)
    bundle = asyncio.run(_capture(ticker, fetch_documents=not skip_documents))
    registry.store_evidence(bundle)
    current = bundle.fingerprint()

    previous = [
        payload
        for payload in (
            registry.load_evidence(path.stem)
            for path in sorted(registry.evidence_dir.glob("*.json"))
        )
        if payload
        and payload["market_ticker"] == ticker
        and payload["snapshot_id"] != bundle.snapshot_id
    ]
    _echo(f"{ticker}: captured {bundle.snapshot_id[:12]} ({current.short})")
    if not previous:
        _echo("  no earlier snapshot to compare against")
        return
    last = max(previous, key=lambda p: p["captured_at"])
    diff = _fingerprint(last).diff(current)
    _echo(f"  vs {last['snapshot_id'][:12]}: {diff.describe()}")
    for name in diff.changed:
        _echo(f"  CHANGED  {name}")
    for name in diff.added:
        _echo(f"  ADDED    {name}")
    for name in diff.removed:
        _echo(f"  REMOVED  {name}")


def _bundle_from_snapshot(payload: dict[str, Any]) -> SettlementEvidenceBundle:
    """Rebuild a bundle from its stored form, preserving absent/null/empty."""

    def revive(value: Any) -> Any:
        """Inverse of the registry's storage encoding.

        Must mirror ``_jsonable`` exactly: if the two disagree, a reloaded
        snapshot re-fingerprints differently and issuance fails against the
        very evidence that was reviewed.
        """
        if isinstance(value, dict) and value.get("__absent__") is True:
            return ABSENT
        if isinstance(value, list):
            return [revive(item) for item in value]
        if isinstance(value, dict):
            return {key: revive(item) for key, item in value.items()}
        return value

    documents = {
        name: ExternalDocument(
            url=body["url"],
            retrieval=DocumentRetrieval(body["retrieval"]),
            retrieved_at=(
                datetime.fromisoformat(body["retrieved_at"]) if body["retrieved_at"] else None
            ),
            http_status=body["http_status"],
            content_type=body["content_type"],
            content_sha256=body["content_sha256"],
            content_bytes=body["content_bytes"],
            extraction=TextExtraction(body["extraction"]),
            note=body["note"],
        )
        for name, body in payload["documents"].items()
    }
    return SettlementEvidenceBundle(
        snapshot_id=payload["snapshot_id"],
        market_ticker=payload["market_ticker"],
        event_ticker=payload["event_ticker"],
        series_ticker=payload["series_ticker"],
        captured_at=datetime.fromisoformat(payload["captured_at"]),
        schema_version=payload["schema_version"],
        market_fields=revive(payload["market_fields"]),
        event_fields=revive(payload["event_fields"]),
        series_fields=revive(payload["series_fields"]),
        documents=documents,
        source_refs=payload["source_refs"],
        capture_errors=tuple(payload["capture_errors"]),
    )


def _request_from_payload(
    payload: dict[str, Any], fingerprint: SettlementEvidenceFingerprint
) -> ReviewRequest:
    body = payload["completeness"]
    claim = CertificateClaim(payload["claim"])
    return ReviewRequest(
        request_id=payload["request_id"],
        claim=claim,
        market_ticker=payload["market_ticker"],
        snapshot_id=payload["snapshot_id"],
        evidence_fingerprint=fingerprint,
        completeness=CompletenessReport(
            claim=claim,
            completeness=EvidenceCompleteness(body["completeness"]),
            missing_required=tuple(body["missing_required"]),
            unretrievable_documents=tuple(body["unretrievable_documents"]),
            manual_viewing_required=dict(body["manual_viewing_required"]),
            present_optional=tuple(body["present_optional"]),
            document_requirements=dict(body.get("document_requirements", {})),
        ),
        rules_hash=payload["rules_hash"],
        generated_at=datetime.fromisoformat(payload["generated_at"]),
        checklist=checklist_for(claim),
        policy_schema_version=payload["policy_schema_version"],
        notes=tuple(payload["notes"]),
    )
