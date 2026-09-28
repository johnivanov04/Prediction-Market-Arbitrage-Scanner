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

Incorporated documents
----------------------
Contract terms are not self-contained: Kalshi's hand payout determination for
undeterminable outcomes to the Exchange Rulebook. Those incorporated sources are
fetched too, and each citation is recorded as a
:class:`~predarb.semantics.dependency.GoverningDocumentDependency` so the policy
can refuse to call the evidence complete while a rule that governs the payout
sits in a document we do not hold.

The Rulebook is venue-level rather than per-market, so it is fetched once per
:class:`GoverningSourceCache` and shared across every market in a run.

Read-only and public throughout. No account, order, balance, position or fill
endpoint is touched, and credentials are never required.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from predarb.semantics.dependency import DependencySet, GoverningDocumentDependency
from predarb.semantics.evidence import (
    DocumentRetrieval,
    ExternalDocument,
    SettlementEvidenceBundle,
    TextExtraction,
    snapshot_id_for,
)
from predarb.semantics.fingerprint import ABSENT, SettlementEvidenceFingerprint
from predarb.semantics.incorporation import (
    DeclarationStore,
    dependency_set_for_document,
    dependency_set_for_text,
)
from predarb.semantics.lineage import lineages_by_citation
from predarb.semantics.materiality import DependencyMaterialityPolicy, ParentContext
from predarb.semantics.pdf_text import PdfExtraction, extract_pdf_text, looks_like_pdf
from predarb.semantics.product_terms import certification_date_from
from predarb.semantics.reference_resolution import headings_from_text, resolve_reference
from predarb.venues.kalshi.client import KalshiReadOnlyClient
from predarb.venues.kalshi.governing_sources import (
    EXCHANGE_RULEBOOK,
    GOVERNING_SOURCE_NAMES,
    GOVERNING_SOURCE_URLS,
    KALSHI_CITATION_GRAMMAR,
    MEMBER_AGREEMENT,
)
from predarb.venues.kalshi.materiality_declarations import KALSHI_MATERIALITY_POLICY
from predarb.venues.kalshi.models import (
    KalshiEvent,
    KalshiMarket,
    KalshiSeries,
    KalshiSettlementSource,
)
from predarb.venues.kalshi.normalize import rules_hash_for, settlement_kind_for
from predarb.venues.kalshi.product_filings import history_for_series
from predarb.venues.kalshi.rulebook_history import (
    EXCHANGE_RULEBOOK_HISTORY,
    headings_for_version,
    lineages_for_series,
)

__all__ = [
    "EVIDENCE_CAPTURE_SCHEMA_VERSION",
    "DocumentFetcher",
    "GoverningSourceCache",
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
        extraction, text, provenance = _extract_text(payload, content_type, at=at)
        return ExternalDocument.from_bytes(
            url=url,
            payload=payload,
            retrieved_at=at,
            http_status=response.status_code,
            content_type=content_type,
            extraction=extraction,
            text=text,
            note="truncated at the size cap" if truncated else None,
            extraction_detail=provenance,
        )

    def from_local_file(self, *, url: str, path: Path, at: datetime) -> ExternalDocument:
        """Record a governing document the operator supplied from disk.

        Needed because kalshi.com answers HTTP 429 to this client for the
        Member Agreement, the Rulebook page and the fee schedule. The
        alternative -- sending a browser User-Agent to get past that -- would be
        working around an access control the publisher put in place, so the
        human supplies the bytes instead and the record says so.
        """
        payload = path.read_bytes()
        extraction, text, provenance = _extract_text(payload, _content_type_for(path), at=at)
        return ExternalDocument.from_bytes(
            url=url,
            payload=payload,
            retrieved_at=at,
            http_status=200,
            content_type=_content_type_for(path),
            extraction=extraction,
            text=text,
            note=f"operator-supplied from {path.name}; this client receives HTTP 429 from the "
            "publisher for this URL",
            extraction_detail=provenance,
            retrieval=DocumentRetrieval.OPERATOR_SUPPLIED,
        )


class GoverningSourceCache:
    """Fetch each venue-level governing document at most once.

    The Rulebook is the same 800KB PDF for every market on the exchange, and
    re-fetching it per market would be both slow and rude. Cached per instance
    rather than per process so a test or a replay can hold its own view.

    ``retrieved_at`` stays the moment of the *actual* fetch, not of each reuse:
    that is when we really saw those bytes.
    """

    def __init__(self, fetcher: DocumentFetcher | None = None) -> None:
        self._fetcher = fetcher or DocumentFetcher()
        self._sources: dict[str, ExternalDocument] = {}

    async def sources(self, *, at: datetime) -> Mapping[str, ExternalDocument]:
        for name, url in GOVERNING_SOURCE_URLS.items():
            if name not in self._sources:
                self._sources[name] = await self._fetcher.fetch(url, at=at)
        return dict(self._sources)

    @staticmethod
    def not_attempted() -> Mapping[str, ExternalDocument]:
        """What to use when document fetching is switched off.

        Deliberately not an empty mapping: "we did not try" is different from
        "there is nothing to fetch", and only the first leaves the dependency
        unresolved rather than absent.
        """
        return {
            name: ExternalDocument.missing(
                url, DocumentRetrieval.NOT_ATTEMPTED, "document fetching disabled"
            )
            for name, url in GOVERNING_SOURCE_URLS.items()
        }


def _extract_pdf(
    payload: bytes, *, at: datetime | None
) -> tuple[TextExtraction, str | None, PdfExtraction | None]:
    """PDF text plus its provenance. Raw bytes stay the document's identity."""
    text, provenance = extract_pdf_text(payload, at=at)
    if text is None:
        return TextExtraction.FAILED, None, provenance
    extraction = TextExtraction.CLEAN if provenance.status == "CLEAN" else TextExtraction.PARTIAL
    return extraction, text, provenance


def _extract_text(
    payload: bytes, content_type: str | None, *, at: datetime | None = None
) -> tuple[TextExtraction, str | None, PdfExtraction | None]:
    """Best-effort readable text, with an honest verdict on how it went.

    PDFs go through pypdf and come back with provenance. Before that existed
    they were reported FAILED, which was honest but left every Kalshi governing
    document -- all of which are PDFs -- as an opaque leaf. That is what let two
    markets be marked evidence-complete while their terms handed payout
    determination to an unfetched rulebook.
    """
    kind = (content_type or "").lower()
    if looks_like_pdf(payload, content_type):
        return _extract_pdf(payload, at=at)
    if "html" in kind or b"<html" in payload[:2048].lower():
        try:
            raw = payload.decode("utf-8", errors="replace")
        except (UnicodeDecodeError, LookupError):
            return TextExtraction.FAILED, None, None
        stripped = _strip_markup(raw)
        if not stripped.strip():
            return TextExtraction.FAILED, None, None
        return TextExtraction.PARTIAL, stripped, None
    try:
        decoded = payload.decode("utf-8")
    except UnicodeDecodeError:
        return TextExtraction.FAILED, None, None
    return TextExtraction.CLEAN, decoded, None


def _content_type_for(path: Path) -> str:
    return {
        ".pdf": "application/pdf",
        ".html": "text/html",
        ".htm": "text/html",
        ".txt": "text/plain",
    }.get(path.suffix.lower(), "application/octet-stream")


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


_PARENT_CONTEXTS: dict[str, ParentContext] = {
    "contract": ParentContext.PRODUCT_CERTIFICATION_FILING,
    "contract_terms": ParentContext.CONTRACT_TERMS,
    EXCHANGE_RULEBOOK: ParentContext.EXCHANGE_RULEBOOK,
    MEMBER_AGREEMENT: ParentContext.MEMBER_AGREEMENT,
    "market.rules_primary": ParentContext.MARKET_RULES_TEXT,
    "market.rules_secondary": ParentContext.MARKET_RULES_TEXT,
}


def parent_context_for(parent: str) -> ParentContext:
    """Which kind of document a parent is, for materiality declarations.

    ``contract`` is the CFTC self-certification cover letter, not the binding
    terms -- the distinction matters, because a declaration written for
    "40.2 cited as filing authority" must not reach into the terms themselves.
    """
    return _PARENT_CONTEXTS.get(parent, ParentContext.OTHER)


def _apply_materiality(
    dependencies: dict[str, DependencySet],
    *,
    claim: str,
    policy: DependencyMaterialityPolicy,
) -> dict[str, DependencySet]:
    """Stamp each dependency with its claim-scoped materiality.

    Done at capture so the classification -- and the policy version behind it --
    is part of the evidence fingerprint. Revoking a declaration therefore shows
    up as drift and puts the affected evidence back to incomplete.
    """
    return {
        parent: replace(
            dependency_set,
            dependencies=tuple(
                policy.apply(d, claim=claim, parent=parent_context_for(parent))
                for d in dependency_set.dependencies
            ),
        )
        for parent, dependency_set in dependencies.items()
    }


def _resolve_citations(
    dependencies: dict[str, DependencySet],
    *,
    documents: Mapping[str, ExternalDocument],
    governing_sources: Mapping[str, ExternalDocument],
    series_ticker: str | None,
) -> dict[str, DependencySet]:
    """Decide what each Rulebook citation points at, from objective evidence.

    A whole-rule citation resolves on heading stability between the version in
    force when the citing document was written and the current one -- nobody
    should have to declare by hand that Rule 7.1 means Rule 7.1. A subsection
    citation resolves only from a recorded lineage with an amendment behind
    every hop, because Rule 6.3 has been headed SETTLEMENT throughout while its
    subsections moved twice.

    Everything else stays UNKNOWN and blocks.
    """
    rulebook = governing_sources.get(EXCHANGE_RULEBOOK)
    current_headings = headings_from_text(rulebook.text) if rulebook and rulebook.text else None
    lineages = lineages_by_citation(lineages_for_series(series_ticker))
    history = history_for_series(series_ticker)
    issued_on = None
    if history is not None and history.latest_filed is not None:
        issued_on = history.latest_filed.filing_date
    if issued_on is None:
        # Fall back to the date printed on the product certification itself.
        # It is the same fact, read from the document rather than from a
        # researcher's notes, and without it no citation in that document can
        # be checked for heading stability.
        certification = documents.get("contract")
        issued_on = certification_date_from(certification.text if certification else None)
    issuance_version = EXCHANGE_RULEBOOK_HISTORY.version_at(issued_on) if issued_on else None
    issuance_headings = headings_for_version(issuance_version.version if issuance_version else None)

    def resolved(dependency: GoverningDocumentDependency) -> GoverningDocumentDependency:
        into_rulebook = dependency.source_name == EXCHANGE_RULEBOOK
        outcome = resolve_reference(
            dependency.reference,
            source_name=dependency.source_name,
            issued_on=issued_on if into_rulebook else None,
            history=EXCHANGE_RULEBOOK_HISTORY if into_rulebook else None,
            issuance_headings=issuance_headings if into_rulebook else None,
            # Only the Rulebook's section structure is known to us. A citation
            # into any other source can still resolve when it names no section
            # at all -- incorporating a whole document is discharged by holding
            # that document.
            current_headings=current_headings if into_rulebook else None,
            source_held=dependency.source_is_held,
            lineages=lineages if into_rulebook else None,
        )
        return replace(
            dependency,
            reference_resolution=outcome.resolution,
            resolution_authority=outcome.authority,
            resolved_reference=outcome.resolved_reference,
            current_text_excerpt=outcome.current_text_excerpt,
            dependencies=tuple(resolved(nested) for nested in dependency.dependencies),
        )

    return {
        parent: replace(
            dependency_set,
            dependencies=tuple(resolved(d) for d in dependency_set.dependencies),
        )
        for parent, dependency_set in dependencies.items()
    }


async def capture_settlement_evidence(
    client: KalshiReadOnlyClient,
    market_ticker: str,
    *,
    at: datetime | None = None,
    fetcher: DocumentFetcher | None = None,
    fetch_documents: bool = True,
    governing_sources: Mapping[str, ExternalDocument] | None = None,
    declarations: DeclarationStore | None = None,
    claim: str = "STANDARD_BINARY_COMPLEMENT",
    materiality_policy: DependencyMaterialityPolicy | None = None,
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

    if governing_sources is None:
        governing_sources = (
            await GoverningSourceCache(fetcher).sources(at=moment)
            if fetch_documents
            else GoverningSourceCache.not_attempted()
        )

    market_fields = market_evidence_fields(market)
    event_fields = event_evidence_fields(event)
    series_fields = series_evidence_fields(series)

    # What each governing component itself incorporates. Scanned where we can
    # read the parent; for an unreadable PDF this yields an UNKNOWN closure
    # unless a reviewer has declared its references against that exact hash.
    dependencies = {
        name: dependency_set_for_document(
            name,
            documents.get(name),
            grammar=KALSHI_CITATION_GRAMMAR,
            sources=governing_sources,
            declarations=declarations,
        )
        for name in ("contract_terms", "contract")
    }
    # The venue-level governing documents are nodes too. The Member Agreement
    # in particular is what establishes that the Rulebook binds as amended from
    # time to time, so a change to it is drift in every certificate that relied
    # on that reading.
    for source_name in GOVERNING_SOURCE_NAMES:
        dependencies[source_name] = dependency_set_for_document(
            source_name,
            governing_sources.get(source_name),
            grammar=KALSHI_CITATION_GRAMMAR,
            sources=governing_sources,
            declarations=declarations,
        )
    for component in ("rules_primary", "rules_secondary"):
        value = market_fields.get(component)
        dependencies[f"market.{component}"] = dependency_set_for_text(
            f"market.{component}",
            value if isinstance(value, str) else None,
            grammar=KALSHI_CITATION_GRAMMAR,
            sources=governing_sources,
        )

    dependencies = _resolve_citations(
        dependencies,
        documents=documents,
        governing_sources=governing_sources,
        series_ticker=series_ticker,
    )
    dependencies = _apply_materiality(
        dependencies, claim=claim, policy=materiality_policy or KALSHI_MATERIALITY_POLICY
    )

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
        dependencies=dependencies,
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
        dependencies=provisional.dependencies,
        source_refs=provisional.source_refs,
        capture_errors=provisional.capture_errors,
    )
