"""Capture the evidence behind an AT_MOST_ONE relation claim.

The group analogue of :mod:`predarb.venues.kalshi.evidence_capture`. It gathers
the event's own semantic fields, per-member identity and settlement fingerprints,
the event's **currently observed** market list, and any governing series
document, then hands back a venue-neutral :class:`RelationEvidenceBundle`.

Observed membership is recorded, never trusted
----------------------------------------------
``GET /events`` omits markets that settled before the historical cutoff (A-46).
The captured membership list is therefore labelled -- in the field name, in the
bundle docstring and in the review packet -- as *current observed membership,
not proven exhaustive membership*. It is kept for audit; the AT_MOST_ONE claim
never depends on it being complete.

``mutually_exclusive`` is captured as strong structured evidence and nothing
more. It pre-populates a review request; it cannot certify one.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from predarb.semantics.evidence import DocumentRetrieval, ExternalDocument
from predarb.semantics.fingerprint import ABSENT
from predarb.semantics.relation import (
    RELATION_EVIDENCE_SCHEMA_VERSION,
    MemberEvidence,
    RelationEvidenceBundle,
)
from predarb.venues.kalshi.client import KalshiReadOnlyClient
from predarb.venues.kalshi.evidence_capture import (
    DocumentFetcher,
    _sources,
    _text,
    capture_settlement_evidence,
)
from predarb.venues.kalshi.models import KalshiEvent, KalshiSeries

__all__ = [
    "capture_relation_evidence",
    "event_relation_fields",
    "relation_snapshot_id",
]


def event_relation_fields(event: KalshiEvent, series: KalshiSeries | None) -> dict[str, Any]:
    """Event and series fields that bear on whether members can co-settle YES."""
    fields: dict[str, Any] = {
        "event_ticker": event.event_ticker,
        "series_ticker": event.series_ticker,
        "title": _text(event.title),
        "sub_title": _text(event.sub_title) if event.sub_title is not None else ABSENT,
        # Strong structured evidence for the claim. Never sufficient on its own:
        # a flag is not a reading of the rules.
        "mutually_exclusive": event.mutually_exclusive,
        "collateral_return_type": (
            _text(event.collateral_return_type)
            if event.collateral_return_type is not None
            else ABSENT
        ),
        "strike_date": _text(event.strike_date),
        "strike_period": _text(event.strike_period) if event.strike_period is not None else ABSENT,
        "settlement_sources": _sources(event.settlement_sources),
    }
    if series is not None:
        fields.update(
            {
                "series_title": _text(series.title),
                "series_settlement_sources": _sources(series.settlement_sources),
                "series_contract_url": (
                    _text(series.contract_url) if series.contract_url is not None else ABSENT
                ),
                "series_contract_terms_url": (
                    _text(series.contract_terms_url)
                    if series.contract_terms_url is not None
                    else ABSENT
                ),
                "series_additional_prohibitions": (
                    list(series.additional_prohibitions)
                    if series.additional_prohibitions
                    else ABSENT
                ),
            }
        )
    else:
        fields["series_captured"] = False
    return fields


def relation_snapshot_id(
    *, event_ticker: str, members: tuple[str, ...], digest: str, captured_at: datetime
) -> str:
    payload = f"{event_ticker}\x1e{','.join(members)}\x1e{digest}\x1e{captured_at.isoformat()}"
    return hashlib.sha256(payload.encode()).hexdigest()


async def capture_relation_evidence(
    client: KalshiReadOnlyClient,
    *,
    event_ticker: str,
    selected_members: list[str],
    member_certificate_ids: dict[str, str] | None = None,
    at: datetime | None = None,
    fetcher: DocumentFetcher | None = None,
    fetch_documents: bool = True,
) -> RelationEvidenceBundle:
    """Assemble the evidence for one AT_MOST_ONE claim over a selected subset.

    Each selected member's settlement evidence is captured too, so the relation
    fingerprint moves whenever any member's payoff semantics move -- the joint
    claim was reviewed against those tables.
    """
    moment = at or datetime.now(tz=UTC)
    fetcher = fetcher or DocumentFetcher()
    errors: list[str] = []

    envelope = await client.get_event(event_ticker, with_nested_markets=True)
    event = envelope.event
    observed: tuple[str, ...] = tuple(sorted(m.ticker for m in envelope.member_markets))

    series: KalshiSeries | None = None
    if event.series_ticker:
        try:
            series = await client.get_series(event.series_ticker)
        except Exception as exc:
            errors.append(f"series {event.series_ticker}: {type(exc).__name__}: {exc}")

    members: list[MemberEvidence] = []
    for ticker in sorted(set(selected_members)):
        settlement_digest: str | None = None
        try:
            bundle = await capture_settlement_evidence(
                client, ticker, at=moment, fetch_documents=False
            )
            settlement_digest = bundle.fingerprint().digest
            notional = bundle.market_fields.get("notional_value")
            rules_hash = bundle.rules_hash
            title = bundle.market_fields.get("title")
            yes_sub = bundle.market_fields.get("yes_sub_title")
            no_sub = bundle.market_fields.get("no_sub_title")
        except Exception as exc:
            errors.append(f"member {ticker}: {type(exc).__name__}: {exc}")
            notional = rules_hash = title = yes_sub = no_sub = None

        members.append(
            MemberEvidence(
                ticker=ticker,
                event_ticker=event_ticker,
                title=_str_or_none(title),
                yes_sub_title=_str_or_none(yes_sub),
                no_sub_title=_str_or_none(no_sub),
                rules_hash=rules_hash,
                notional=_str_or_none(notional),
                settlement_fingerprint=settlement_digest,
                settlement_certificate_id=(member_certificate_ids or {}).get(ticker),
            )
        )

    documents: dict[str, ExternalDocument] = {}
    if fetch_documents:
        documents["contract_terms"] = await fetcher.fetch(
            series.contract_terms_url if series else None, at=moment
        )
        documents["contract"] = await fetcher.fetch(
            series.contract_url if series else None, at=moment
        )
    else:
        for name in ("contract_terms", "contract"):
            documents[name] = ExternalDocument.missing(
                None, DocumentRetrieval.NOT_ATTEMPTED, "document fetching disabled"
            )

    provisional = RelationEvidenceBundle(
        snapshot_id="pending",
        event_ticker=event_ticker,
        series_ticker=event.series_ticker,
        selected_members=tuple(sorted(set(selected_members))),
        captured_at=moment,
        schema_version=RELATION_EVIDENCE_SCHEMA_VERSION,
        event_fields=event_relation_fields(event, series),
        member_evidence=tuple(members),
        observed_event_membership=observed,
        documents=documents,
        source_refs={
            "event": f"GET /events/{event_ticker}?with_nested_markets=true",
            "series": f"GET /series/{event.series_ticker}" if event.series_ticker else "(none)",
        },
        capture_errors=tuple(errors),
    )
    digest = provisional.fingerprint().digest
    return RelationEvidenceBundle(
        snapshot_id=relation_snapshot_id(
            event_ticker=event_ticker,
            members=provisional.selected_members,
            digest=digest,
            captured_at=moment,
        ),
        event_ticker=provisional.event_ticker,
        series_ticker=provisional.series_ticker,
        selected_members=provisional.selected_members,
        captured_at=provisional.captured_at,
        schema_version=provisional.schema_version,
        event_fields=provisional.event_fields,
        member_evidence=provisional.member_evidence,
        observed_event_membership=provisional.observed_event_membership,
        documents=provisional.documents,
        source_refs=provisional.source_refs,
        capture_errors=provisional.capture_errors,
    )


def _str_or_none(value: Any) -> str | None:
    if value is None or value is ABSENT:
        return None
    return str(value)
