"""Deriving readable text from a PDF, with provenance for the derivation.

Raw bytes stay authoritative
----------------------------
The source document's identity is the SHA-256 of its raw bytes, and nothing
here changes that. Extracted text is **derived** evidence: it exists so a
scanner can find citations and a human can read the contract, and it is
recorded alongside the parser that produced it so a later parser upgrade is
visible as what it is -- a change in our reading -- rather than looking like an
amended legal document.

That distinction has a concrete consequence: ``extraction`` provenance is
recorded but deliberately **not** fingerprinted. If the parser version were part
of the evidence fingerprint, upgrading pypdf would invalidate every certificate
on the exchange while no contract had changed a byte.

What *is* fingerprinted is what the extraction let us discover: if a better
parser finds an incorporated rule the old one missed, the dependency set
changes, and that is real drift a reviewer should see.

No OCR
------
Deliberately absent. Kalshi's governing documents carry a real text layer, and
OCR is a far weaker evidence transformation -- it guesses glyphs. A scanned page
with no text layer is reported as a zero-text page and sent to manual review,
which is the honest answer.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Self

import pypdf

from predarb.clock import ensure_utc

__all__ = [
    "PARSER_NAME",
    "PARSER_VERSION",
    "PdfExtraction",
    "extract_pdf_text",
    "looks_like_pdf",
]

PARSER_NAME: Final = "pypdf"
PARSER_VERSION: Final = pypdf.__version__

_MIN_CHARS_PER_PAGE: Final = 8
"""Below this, a page is treated as carrying no text.

A handful of stray glyphs from a scanned page is not a text layer, and treating
it as one would hide the fact that nobody can actually read the page.
"""

_SUSPICIOUS_EMPTY_RATIO: Final = 0.2
"""Above this share of zero-text pages, extraction is reported as PARTIAL.

A governing document where a fifth of the pages came back blank has not been
read, whatever the other pages say.
"""

_MIN_EMPTY_PAGES_TO_SUSPECT: Final = 2
"""A single blank page never triggers the ratio test.

Contract terms are routinely two pages with a blank verso, and calling that a
suspicious extraction would send every short document to manual review for a
page that has nothing on it. Two or more blank pages is a different signal.
Either way the page numbers are always reported, so a reviewer can look.
"""


@dataclass(frozen=True, slots=True)
class PdfExtraction:
    """Provenance for one text derivation. Never a substitute for the source."""

    parser_name: str
    parser_version: str
    source_sha256: str
    """The raw bytes this text was derived from. The document's real identity."""

    status: str
    """``CLEAN``, ``PARTIAL`` or ``FAILED`` -- mirrors ``TextExtraction``."""

    text_sha256: str | None = None
    """Hash of the derived text, so an identical re-extraction is provable."""

    page_count: int = 0
    pages_with_no_text: tuple[int, ...] = ()
    """1-indexed, so they match what a reviewer sees in a PDF viewer."""

    character_count: int = 0
    extracted_at: datetime | None = None
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.extracted_at is not None:
            object.__setattr__(self, "extracted_at", ensure_utc(self.extracted_at))

    @property
    def requires_manual_review(self) -> bool:
        """Whether a human must open the source rather than trust this text.

        True for anything short of a clean full extraction. Incomplete, corrupt
        or suspicious extraction is not a weaker form of reading -- it is a
        reason to go to the source.
        """
        return self.status != "CLEAN"

    @property
    def parser_identity(self) -> str:
        return f"{self.parser_name}/{self.parser_version}"

    @classmethod
    def failed(cls, source_sha256: str, reason: str, *, at: datetime | None = None) -> Self:
        return cls(
            parser_name=PARSER_NAME,
            parser_version=PARSER_VERSION,
            source_sha256=source_sha256,
            status="FAILED",
            extracted_at=at,
            warnings=(reason,),
        )

    def payload(self) -> dict[str, Any]:
        return {
            "parser_name": self.parser_name,
            "parser_version": self.parser_version,
            "source_sha256": self.source_sha256,
            "status": self.status,
            "text_sha256": self.text_sha256,
            "page_count": self.page_count,
            "pages_with_no_text": list(self.pages_with_no_text),
            "character_count": self.character_count,
            "extracted_at": self.extracted_at.isoformat() if self.extracted_at else None,
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Self:
        extracted_at = payload.get("extracted_at")
        return cls(
            parser_name=str(payload["parser_name"]),
            parser_version=str(payload["parser_version"]),
            source_sha256=str(payload["source_sha256"]),
            status=str(payload["status"]),
            text_sha256=payload.get("text_sha256"),
            page_count=int(payload.get("page_count", 0)),
            pages_with_no_text=tuple(payload.get("pages_with_no_text", ())),
            character_count=int(payload.get("character_count", 0)),
            extracted_at=datetime.fromisoformat(extracted_at) if extracted_at else None,
            warnings=tuple(payload.get("warnings", ())),
        )

    def describe(self) -> str:
        empty = (
            f", {len(self.pages_with_no_text)} zero-text page(s)" if self.pages_with_no_text else ""
        )
        return (
            f"{self.parser_identity} {self.status} "
            f"{self.page_count} page(s), {self.character_count} char(s){empty}"
        )


def looks_like_pdf(payload: bytes, content_type: str | None) -> bool:
    return "pdf" in (content_type or "").lower() or payload[:5] == b"%PDF-"


def extract_pdf_text(
    payload: bytes, *, at: datetime | None = None
) -> tuple[str | None, PdfExtraction]:
    """Derive text from PDF bytes, reporting honestly on how it went.

    Returns ``(text, provenance)``. ``text`` is ``None`` when nothing usable
    came out -- a reviewer shown garbled output might read it, which is worse
    than showing them nothing.
    """
    source_sha256 = hashlib.sha256(payload).hexdigest()
    warnings: list[str] = []

    try:
        reader = pypdf.PdfReader(io.BytesIO(payload), strict=False)
    except Exception as exc:  # pypdf raises a wide family on malformed input
        return None, PdfExtraction.failed(source_sha256, f"{type(exc).__name__}: {exc}", at=at)

    if getattr(reader, "is_encrypted", False):
        # An encrypted document we cannot open is not a document we have read.
        try:
            reader.decrypt("")
        except Exception as exc:
            return None, PdfExtraction.failed(
                source_sha256, f"encrypted and undecryptable: {type(exc).__name__}", at=at
            )
        warnings.append("document was encrypted; opened with an empty password")

    pages: list[str] = []
    empty_pages: list[int] = []
    try:
        page_count = len(reader.pages)
    except Exception as exc:
        return None, PdfExtraction.failed(
            source_sha256, f"page count unreadable: {type(exc).__name__}: {exc}", at=at
        )

    for index in range(page_count):
        try:
            text = reader.pages[index].extract_text() or ""
        except Exception as exc:
            warnings.append(f"page {index + 1}: {type(exc).__name__}: {exc}")
            text = ""
        if len(text.strip()) < _MIN_CHARS_PER_PAGE:
            empty_pages.append(index + 1)
        pages.append(text)

    combined = "\n".join(pages).strip()
    if not combined:
        return None, PdfExtraction(
            parser_name=PARSER_NAME,
            parser_version=PARSER_VERSION,
            source_sha256=source_sha256,
            status="FAILED",
            page_count=page_count,
            pages_with_no_text=tuple(empty_pages),
            extracted_at=at,
            warnings=(*warnings, "no page yielded text; the document may be a scan"),
        )

    empty_ratio = len(empty_pages) / page_count if page_count else 1.0
    suspicious = (
        len(empty_pages) >= _MIN_EMPTY_PAGES_TO_SUSPECT and empty_ratio > _SUSPICIOUS_EMPTY_RATIO
    )
    status = "CLEAN"
    if warnings or suspicious:
        status = "PARTIAL"
        if suspicious:
            warnings.append(
                f"{len(empty_pages)} of {page_count} pages yielded no text; "
                "treat this extraction as incomplete"
            )

    return combined, PdfExtraction(
        parser_name=PARSER_NAME,
        parser_version=PARSER_VERSION,
        source_sha256=source_sha256,
        status=status,
        text_sha256=hashlib.sha256(combined.encode("utf-8")).hexdigest(),
        page_count=page_count,
        pages_with_no_text=tuple(empty_pages),
        character_count=len(combined),
        extracted_at=at,
        warnings=tuple(warnings),
    )
