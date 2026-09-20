"""Capture a Kalshi market's settlement evidence into a venue-neutral bundle.

The venue-specific half of Step 8: it knows which Kalshi fields carry settlement
meaning, fetches the market, its event, its series and any published contract
document, and hands back a :class:`SettlementEvidenceBundle` that nothing
downstream can tell came from Kalshi.

What is captured, and what is not
---------------------------------
Only fields that bear on what the contract can pay. Prices, sizes, volume, open
interest and liquidity are deliberately absent: a market whose price moved has
not changed its settlement semantics, and fingerprinting price would invalidate
every certificate on every tick.

Values are captured as **exact text** rather than parsed objects wherever a
parse could lose or normalise information. A ``Decimal`` keeps its trailing
zeros because how the venue reported a number is itself evidence.

External documents
------------------
Series metadata may publish ``contract_url`` and ``contract_terms_url``. Those
are fetched as raw bytes; the SHA-256 of those bytes is authoritative for change
detection, and any text extraction is for human readability only. A failure to
fetch is recorded as evidence in its own right rather than silently omitted --
"we could not read the terms" is something a reviewer must see.

Read-only and public throughout. No account, order, balance, position or fill
endpoint is touched, and credentials are never required.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from predarb.semantics.evidence import (
    DocumentRetrieval,
    ExternalDocument,
    SettlementEvidenceBundle,
    TextExtraction,
    snapshot_id_for,
)
from predarb.semantics.fingerprint import ABSENT, SettlementEvidenceFingerprint
from predarb.venues.kalshi.client import KalshiReadOnlyClient
from predarb.venues.kalshi.models import (
    KalshiEvent,
    KalshiMarket,
    KalshiSeries,
    KalshiSettlementSource,
)
from predarb.venues.kalshi.normalize import rules_hash_for, settlement_kind_for

__all__ = [
    "EVIDENCE_CAPTURE_SCHEMA_VERSION",
    "DocumentFetcher",
    "capture_settlement_evidence",
    "market_evidence_fields",
]

EVIDENCE_CAPTURE_SCHEMA_VERSION = "kalshi-settlement-evidence/1"
"""Version of *which fields we capture*, distinct from the encoding version.

Bumped when the inclusion list changes, so a bundle captured under an older
list cannot be mistaken for one captured under the current one.
"""

HTTP_ERROR_THRESHOLD = 400
"""Status at or above which a fetch is recorded as a failure."""

MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
"""Cap on a fetched contract document.

A contract PDF is a few hundred kilobytes; anything past this is either not a
contract or not something to hold in memory during a metadata sweep.
"""


def _text(value: object) -> object:
    """Exact text for anything whose parsed form could lose information."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _sources(sources: tuple[KalshiSettlementSource, ...]) -> object:
    """Settlement sources as an ordered list of name/url pairs.

    Order is preserved and significant: a venue that reorders its sources has
    changed which one is primary, which is a settlement change.
    """
    if not sources:
        return ABSENT
    return [[source.name or "", source.url or ""] for source in sources]


def market_evidence_fields(market: KalshiMarket) -> dict[str, Any]:
    """The settlement-relevant fields of a market, and nothing else.

    Every entry here is justified in ``STANDARD_BINARY_COMPLEMENT_POLICY``'s
    rationale table. Omissions are as deliberate as inclusions.
    """
    return {
        "ticker": market.ticker,
        "market_type": market.market_type,
        # Our own normalisation of market_type. Captured alongside the raw value
        # so that a change in either -- the venue's vocabulary or our reading of
        # it -- shows up as drift.
        "settlement_kind": settlement_kind_for(market.market_type).value,
        "title": _text(market.title),
        "yes_sub_title": _text(market.yes_sub_title)
        if market.yes_sub_title is not None
        else ABSENT,
        "no_sub_title": _text(market.no_sub_title) if market.no_sub_title is not None else ABSENT,
        "notional_value": (
            _text(market.notional_value_dollars)
            if market.notional_value_dollars is not None
            else ABSENT
        ),
        "rules_primary": _text(market.rules_primary)
        if market.rules_primary is not None
        else ABSENT,
        "rules_secondary": (
            _text(market.rules_secondary) if market.rules_secondary is not None else ABSENT
        ),
        "rules_hash": rules_hash_for(market.rules_primary, market.rules_secondary) or ABSENT,
        "strike_type": _text(market.strike_type) if market.strike_type is not None else ABSENT,
        "floor_strike": _text(market.floor_strike) if market.floor_strike is not None else ABSENT,
        "cap_strike": _text(market.cap_strike) if market.cap_strike is not None else ABSENT,
        "custom_strike": (
            {k: _text(v) for k, v in sorted(market.custom_strike.items())}
            if market.custom_strike is not None
            else ABSENT
        ),
        "can_close_early": market.can_close_early,
        "early_close_condition": (
            _text(market.early_close_condition)
            if market.early_close_condition is not None
            else ABSENT
        ),
        "settlement_timer_seconds": market.settlement_timer_seconds,
        "expected_expiration_time": _text(market.expected_expiration_time),
        "latest_expiration_time": _text(market.latest_expiration_time),
        "expiration_value": (
            _text(market.expiration_value) if market.expiration_value is not None else ABSENT
        ),
        "price_level_structure": (
            _text(market.price_level_structure)
            if market.price_level_structure is not None
            else ABSENT
        ),
    }


def event_evidence_fields(event: KalshiEvent | None) -> dict[str, Any]:
    if event is None:
        return {"captured": False}
    return {
        "captured": True,
        "event_ticker": event.event_ticker,
        "series_ticker": event.series_ticker,
        "title": _text(event.title),
        "sub_title": _text(event.sub_title),
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


def series_evidence_fields(series: KalshiSeries | None) -> dict[str, Any]:
    if series is None:
        return {"captured": False}
    return {
        "captured": True,
        "ticker": series.ticker,
        "title": _text(series.title),
        "frequency": _text(series.frequency) if series.frequency is not None else ABSENT,
        "contract_url": _text(series.contract_url) if series.contract_url is not None else ABSENT,
        "contract_terms_url": (
            _text(series.contract_terms_url) if series.contract_terms_url is not None else ABSENT
        ),
        "settlement_sources": _sources(series.settlement_sources),
        "additional_prohibitions": (
            list(series.additional_prohibitions) if series.additional_prohibitions else ABSENT
        ),
    }


class DocumentFetcher:
    """Fetches public contract documents, preserving raw bytes.

    Kept separate from the API client because these are arbitrary public web
    URLs published by the venue, not Kalshi API endpoints: they need no
    authentication, obey no rate-limit policy of ours, and must never be sent
    credentials.
    """

    def __init__(self, timeout_s: float = 20.0) -> None:
        self._timeout = timeout_s

    async def fetch(self, url: str | None, *, at: datetime) -> ExternalDocument:
        if not url:
            return ExternalDocument.missing(None, DocumentRetrieval.NO_URL_PUBLISHED)
        try:
            # No credentials, no cookies, no auth headers: this is public
            # material and must never receive any of ours.
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
                response = await client.get(url, headers={"accept": "*/*"})
        except httpx.HTTPError as exc:
            return ExternalDocument.missing(
                url, DocumentRetrieval.NETWORK_ERROR, f"{type(exc).__name__}: {exc}"
            )

        if response.status_code >= HTTP_ERROR_THRESHOLD:
            return ExternalDocument(
                url=url,
                retrieval=DocumentRetrieval.HTTP_ERROR,
                retrieved_at=at,
                http_status=response.status_code,
                note=f"HTTP {response.status_code}",
            )

        payload = response.content[:MAX_DOCUMENT_BYTES]
        truncated = len(response.content) > MAX_DOCUMENT_BYTES
        content_type = response.headers.get("content-type")
        extraction, text = _extract_text(payload, content_type)
        return ExternalDocument.from_bytes(
            url=url,
            payload=payload,
            retrieved_at=at,
            http_status=response.status_code,
            content_type=content_type,
            extraction=extraction,
            text=text,
            note="truncated at the size cap" if truncated else None,
        )


def _extract_text(payload: bytes, content_type: str | None) -> tuple[TextExtraction, str | None]:
    """Best-effort readable text, with an honest verdict on how it went.

    A PDF is reported as FAILED rather than guessed at: showing a reviewer
    mangled binary as though it were contract text would be worse than showing
    them nothing, because they might read it.
    """
    kind = (content_type or "").lower()
    if "pdf" in kind or payload[:5] == b"%PDF-":
        return (
            TextExtraction.FAILED,
            None,
        )
    if "html" in kind or b"<html" in payload[:2048].lower():
        try:
            raw = payload.decode("utf-8", errors="replace")
        except (UnicodeDecodeError, LookupError):
            return TextExtraction.FAILED, None
        stripped = _strip_markup(raw)
        if not stripped.strip():
            return TextExtraction.FAILED, None
        return TextExtraction.PARTIAL, stripped
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return TextExtraction.FAILED, None
    return TextExtraction.CLEAN, text


def _strip_markup(raw: str) -> str:
    """Crude tag removal, labelled PARTIAL because that is what it is."""
    out: list[str] = []
    depth = 0
    for char in raw:
        if char == "<":
            depth += 1
        elif char == ">":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(char)
    return " ".join("".join(out).split())


async def capture_settlement_evidence(
    client: KalshiReadOnlyClient,
    market_ticker: str,
    *,
    at: datetime | None = None,
    fetcher: DocumentFetcher | None = None,
    fetch_documents: bool = True,
) -> SettlementEvidenceBundle:
    """Fetch and assemble one market's settlement evidence.

    Failures to fetch the event, series or documents are recorded in the bundle
    rather than raised: an incomplete bundle is a real, reviewable state, and
    the policy decides whether it is good enough for the claim in question.
    """
    moment = at or datetime.now(tz=UTC)
    fetcher = fetcher or DocumentFetcher()
    errors: list[str] = []

    market = await client.get_market(market_ticker)

    event: KalshiEvent | None = None
    try:
        event = (await client.get_event(market.event_ticker)).event
    except Exception as exc:
        errors.append(f"event {market.event_ticker}: {type(exc).__name__}: {exc}")

    series: KalshiSeries | None = None
    series_ticker = event.series_ticker if event is not None else None
    if series_ticker:
        try:
            series = await client.get_series(series_ticker)
        except Exception as exc:
            errors.append(f"series {series_ticker}: {type(exc).__name__}: {exc}")

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

    market_fields = market_evidence_fields(market)
    event_fields = event_evidence_fields(event)
    series_fields = series_evidence_fields(series)

    provisional = SettlementEvidenceBundle(
        snapshot_id="pending",
        market_ticker=market.ticker,
        event_ticker=market.event_ticker,
        series_ticker=series_ticker,
        captured_at=moment,
        schema_version=EVIDENCE_CAPTURE_SCHEMA_VERSION,
        market_fields=market_fields,
        event_fields=event_fields,
        series_fields=series_fields,
        documents=documents,
        source_refs={
            "market": f"GET /markets/{market.ticker}",
            "event": f"GET /events/{market.event_ticker}",
            "series": f"GET /series/{series_ticker}" if series_ticker else "(not fetched)",
        },
        capture_errors=tuple(errors),
    )
    fingerprint: SettlementEvidenceFingerprint = provisional.fingerprint()
    return SettlementEvidenceBundle(
        snapshot_id=snapshot_id_for(
            market_ticker=market.ticker, fingerprint=fingerprint, captured_at=moment
        ),
        market_ticker=provisional.market_ticker,
        event_ticker=provisional.event_ticker,
        series_ticker=provisional.series_ticker,
        captured_at=provisional.captured_at,
        schema_version=provisional.schema_version,
        market_fields=provisional.market_fields,
        event_fields=provisional.event_fields,
        series_fields=provisional.series_fields,
        documents=provisional.documents,
        source_refs=provisional.source_refs,
        capture_errors=provisional.capture_errors,
    )
