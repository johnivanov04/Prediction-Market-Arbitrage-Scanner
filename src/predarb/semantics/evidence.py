"""Captured settlement evidence, and the snapshot a human actually reviewed.

A certificate asserts something about a *version* of the evidence, so the
evidence has to be a first-class, immutable, reproducible object. If the API
later changes a field, a URL starts serving different bytes, or a contract page
disappears, the recorded decision must still be explicable.

What belongs here
-----------------
Only fields that bear on **what the contract can pay**. Prices, volume, open
interest and order-book state are explicitly not settlement semantics: a market
whose price moved has not changed what it pays. Including volatile fields would
invalidate every certificate on every tick, which trains reviewers to ignore
drift -- the opposite of the intent.

The inclusion list is therefore explicit and per-claim
(:mod:`predarb.semantics.policy`), never "every field on the market object".

External documents
------------------
Series metadata can point at contract terms or filings. For those, **raw bytes
and their SHA-256 are authoritative** for change detection. Text extraction is
for human readability only and is allowed to be imperfect -- but when it is
imperfect that is recorded, so nobody can mistake garbled extraction for a
careful read.

A URL is never treated as evidence of its own content. The same URL serving
different bytes is a change, and is detected as one.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Self

from predarb.clock import ensure_utc
from predarb.semantics.fingerprint import (
    ABSENT,
    SettlementEvidenceFingerprint,
)

__all__ = [
    "DocumentRetrieval",
    "EvidenceCompleteness",
    "ExternalDocument",
    "SettlementEvidenceBundle",
    "TextExtraction",
    "snapshot_id_for",
]


class DocumentRetrieval(StrEnum):
    """How an attempt to fetch an external document ended."""

    RETRIEVED = "RETRIEVED"
    HTTP_ERROR = "HTTP_ERROR"
    NETWORK_ERROR = "NETWORK_ERROR"
    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    NO_URL_PUBLISHED = "NO_URL_PUBLISHED"

    @property
    def is_usable(self) -> bool:
        return self is DocumentRetrieval.RETRIEVED


class TextExtraction(StrEnum):
    """How readable the fetched document is for a human reviewer.

    Separate from retrieval on purpose: bytes can arrive perfectly while
    remaining unreadable, and a reviewer must never be shown garbled text as
    though it were the contract.
    """

    CLEAN = "CLEAN"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    NOT_APPLICABLE = "NOT_APPLICABLE"

    @property
    def is_readable(self) -> bool:
        return self is TextExtraction.CLEAN


class EvidenceCompleteness(StrEnum):
    """Whether a bundle carries everything a claim requires."""

    COMPLETE = "COMPLETE"
    EVIDENCE_INCOMPLETE = "EVIDENCE_INCOMPLETE"

    @property
    def permits_approval(self) -> bool:
        return self is EvidenceCompleteness.COMPLETE


@dataclass(frozen=True, slots=True)
class ExternalDocument:
    """One fetched contract document, or a record of why it is missing.

    ``content_sha256`` is the authority for change detection. ``text`` exists
    only so a reviewer can read something, and ``extraction`` says how much to
    trust it.
    """

    url: str | None
    retrieval: DocumentRetrieval
    retrieved_at: datetime | None = None
    http_status: int | None = None
    content_type: str | None = None
    content_sha256: str | None = None
    content_bytes: int | None = None
    extraction: TextExtraction = TextExtraction.NOT_APPLICABLE
    text: str | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        if self.retrieved_at is not None:
            object.__setattr__(self, "retrieved_at", ensure_utc(self.retrieved_at))
        if self.retrieval.is_usable and not self.content_sha256:
            raise ValueError(
                f"{self.url}: a RETRIEVED document must carry a content hash, or "
                "change detection would rest on the URL alone"
            )

    @classmethod
    def missing(cls, url: str | None, reason: DocumentRetrieval, note: str = "") -> Self:
        return cls(url=url, retrieval=reason, note=note or None)

    @classmethod
    def from_bytes(
        cls,
        *,
        url: str,
        payload: bytes,
        retrieved_at: datetime,
        http_status: int,
        content_type: str | None,
        extraction: TextExtraction,
        text: str | None,
        note: str | None = None,
    ) -> Self:
        return cls(
            url=url,
            retrieval=DocumentRetrieval.RETRIEVED,
            retrieved_at=retrieved_at,
            http_status=http_status,
            content_type=content_type,
            content_sha256=hashlib.sha256(payload).hexdigest(),
            content_bytes=len(payload),
            extraction=extraction,
            text=text,
            note=note,
        )

    @property
    def requires_manual_viewing(self) -> bool:
        """Whether the reviewer must open the source URL themselves.

        True whenever we hold bytes we could not render readably. Approving on
        the strength of garbled text would be approving nothing.
        """
        return self.retrieval.is_usable and not self.extraction.is_readable

    def describe(self) -> str:
        if not self.retrieval.is_usable:
            return f"{self.retrieval.value}: {self.url or '(no url published)'}"
        return (
            f"{self.url} [{self.http_status}] {self.content_bytes}B "
            f"sha256={(self.content_sha256 or '')[:12]}... text={self.extraction.value}"
        )


@dataclass(frozen=True, slots=True)
class SettlementEvidenceBundle:
    """An immutable snapshot of one market's settlement evidence.

    Field groups are kept separate (market / event / series / documents) because
    a drift report is far more useful when it can say *which layer* moved.
    """

    snapshot_id: str
    market_ticker: str
    event_ticker: str | None
    series_ticker: str | None
    captured_at: datetime
    schema_version: str

    market_fields: Mapping[str, Any]
    event_fields: Mapping[str, Any]
    series_fields: Mapping[str, Any]
    documents: Mapping[str, ExternalDocument]

    source_refs: Mapping[str, str] = field(default_factory=dict)
    """Raw-payload identifiers, so the exact API responses can be found again."""

    capture_errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "captured_at", ensure_utc(self.captured_at))
        for name in ("market_fields", "event_fields", "series_fields"):
            object.__setattr__(self, name, dict(sorted(getattr(self, name).items())))
        object.__setattr__(self, "documents", dict(sorted(self.documents.items())))
        object.__setattr__(self, "source_refs", dict(sorted(self.source_refs.items())))

    def component_values(self) -> dict[str, Any]:
        """The flat, namespaced mapping that gets fingerprinted.

        Documents contribute their **content hash**, not their bytes or their
        URL: the bytes live in the evidence store, and a URL is not evidence of
        its own content. A document that could not be retrieved contributes its
        failure reason, so "we could not fetch the terms" is itself part of the
        fingerprint rather than silently absent.
        """
        values: dict[str, Any] = {}
        for prefix, fields in (
            ("market", self.market_fields),
            ("event", self.event_fields),
            ("series", self.series_fields),
        ):
            for name, value in fields.items():
                values[f"{prefix}.{name}"] = value
        for name, document in self.documents.items():
            if document.retrieval.is_usable:
                values[f"document.{name}.sha256"] = document.content_sha256
            else:
                values[f"document.{name}.sha256"] = ABSENT
            values[f"document.{name}.retrieval"] = document.retrieval.value
            values[f"document.{name}.url"] = document.url if document.url else ABSENT
        return values

    def fingerprint(self) -> SettlementEvidenceFingerprint:
        return SettlementEvidenceFingerprint.over(
            self.component_values(), schema_version=self.schema_version
        )

    @property
    def rules_hash(self) -> str | None:
        value = self.market_fields.get("rules_hash")
        return value if isinstance(value, str) else None

    @property
    def documents_requiring_manual_viewing(self) -> tuple[str, ...]:
        return tuple(name for name, doc in self.documents.items() if doc.requires_manual_viewing)

    def describe(self) -> str:
        return (
            f"{self.market_ticker} snapshot {self.snapshot_id[:12]} "
            f"captured {self.captured_at.isoformat()} "
            f"({len(self.component_values())} components, {len(self.documents)} documents)"
        )


def snapshot_id_for(
    *, market_ticker: str, fingerprint: SettlementEvidenceFingerprint, captured_at: datetime
) -> str:
    """Content-addressed id: same evidence at the same instant is the same id.

    The capture time is included so two captures of identical evidence remain
    distinguishable records -- useful for showing that nothing drifted between
    two points in time, which is itself a finding.
    """
    payload = f"{market_ticker}\x1e{fingerprint.digest}\x1e{ensure_utc(captured_at).isoformat()}"
    return hashlib.sha256(payload.encode()).hexdigest()
