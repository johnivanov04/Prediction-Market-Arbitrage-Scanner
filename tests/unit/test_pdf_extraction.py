"""Extracting text from a governing PDF, and keeping the raw bytes authoritative.

The distinction every test here defends: the document's legal identity is the
SHA-256 of its raw bytes. Extracted text is our *reading* of it. Upgrading the
parser changes the reading and must never look like an amended contract.
"""

from __future__ import annotations

import dataclasses
import hashlib
from datetime import UTC, datetime

import pytest

from predarb.semantics.dependency import DependencyClosure
from predarb.semantics.evidence import (
    DocumentRetrieval,
    ExternalDocument,
    TextExtraction,
)
from predarb.semantics.fingerprint import SettlementEvidenceFingerprint
from predarb.semantics.incorporation import dependency_set_for_document, scan_incorporations
from predarb.semantics.pdf_text import (
    PARSER_NAME,
    PdfExtraction,
    extract_pdf_text,
    looks_like_pdf,
)
from predarb.venues.kalshi.governing_sources import KALSHI_CITATION_GRAMMAR

pytestmark = pytest.mark.unit

T0 = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)

CONTINGENCY_LINES = (
    "Contingencies: Before Settlement, Kalshi may initiate the Market",
    "Outcome Review Process pursuant to Rule 6.3(d) of the Rulebook.",
    "If an Expiration Value cannot be determined, Kalshi has the right",
    "to determine payouts pursuant to Rule 6.3(b) in the Rulebook.",
)


def make_pdf(lines: tuple[str, ...], *, blank_pages: int = 0) -> bytes:
    """A minimal single-stream PDF, built here rather than committed.

    A binary fixture in the repository would be one more governing-looking
    document nobody can diff. This one is generated from the text it contains,
    so a reader can see exactly what the parser is being asked to find.
    """
    escaped = "".join(
        f"({line.replace('(', chr(92) + '(').replace(')', chr(92) + ')')}) Tj T*\n"
        for line in lines
    )
    content = f"BT /F1 11 Tf 12 TL 40 750 Td\n{escaped}ET"
    page_ids = [3]
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        None,  # placeholder: /Pages, filled once page ids are known
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        "/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for index in range(blank_pages):
        page_number = 6 + index
        page_ids.append(page_number)
        objects.append("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << >> >>")
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects[1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>"

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)


TERMS_PDF = make_pdf(CONTINGENCY_LINES)


def document_from(payload: bytes) -> ExternalDocument:
    text, provenance = extract_pdf_text(payload, at=T0)
    extraction = TextExtraction.CLEAN if provenance.status == "CLEAN" else TextExtraction.PARTIAL
    if text is None:
        extraction = TextExtraction.FAILED
    return ExternalDocument.from_bytes(
        url="https://kalshi.test/terms.pdf",
        payload=payload,
        retrieved_at=T0,
        http_status=200,
        content_type="application/pdf",
        extraction=extraction,
        text=text,
        extraction_detail=provenance,
    )


class TestExtractionIsBoundToTheSource:
    def test_provenance_names_the_source_hash(self):
        _, provenance = extract_pdf_text(TERMS_PDF, at=T0)
        assert provenance.source_sha256 == hashlib.sha256(TERMS_PDF).hexdigest()

    def test_the_parser_identifies_itself(self):
        _, provenance = extract_pdf_text(TERMS_PDF, at=T0)
        assert provenance.parser_name == PARSER_NAME
        assert provenance.parser_version
        assert provenance.parser_identity.startswith(f"{PARSER_NAME}/")

    def test_the_extracted_text_hash_is_recorded(self):
        text, provenance = extract_pdf_text(TERMS_PDF, at=T0)
        assert text is not None
        assert provenance.text_sha256 == hashlib.sha256(text.encode()).hexdigest()

    def test_extraction_is_deterministic_for_a_fixture(self):
        first = extract_pdf_text(TERMS_PDF, at=T0)[1]
        second = extract_pdf_text(TERMS_PDF, at=T0)[1]
        assert first.text_sha256 == second.text_sha256
        assert first == second

    def test_page_and_character_counts_are_reported(self):
        _, provenance = extract_pdf_text(TERMS_PDF, at=T0)
        assert provenance.page_count == 1
        assert provenance.character_count > 0

    def test_it_records_when_it_was_extracted(self):
        _, provenance = extract_pdf_text(TERMS_PDF, at=T0)
        assert provenance.extracted_at == T0


class TestRawBytesRemainAuthoritative:
    def test_the_document_hash_is_of_the_raw_bytes_not_the_text(self):
        document = document_from(TERMS_PDF)
        assert document.content_sha256 == hashlib.sha256(TERMS_PDF).hexdigest()
        assert document.extraction_detail is not None
        assert document.content_sha256 != document.extraction_detail.text_sha256

    def test_a_parser_upgrade_does_not_change_document_identity(self):
        """The failure this prevents: bumping pypdf invalidating every
        certificate on the exchange while no contract changed a byte."""
        document = document_from(TERMS_PDF)
        assert document.extraction_detail is not None
        upgraded = dataclasses.replace(
            document,
            extraction_detail=dataclasses.replace(
                document.extraction_detail, parser_version="99.0.0"
            ),
        )
        assert upgraded.content_sha256 == document.content_sha256

    def test_extraction_provenance_is_not_fingerprinted(self):
        """Document identity in the evidence fingerprint is the raw hash alone."""
        document = document_from(TERMS_PDF)
        assert document.extraction_detail is not None
        values = {
            "document.contract_terms.sha256": document.content_sha256,
            "document.contract_terms.retrieval": document.retrieval.value,
        }
        before = SettlementEvidenceFingerprint.over(values, schema_version="t/1")
        # Nothing about the parser appears in the component set at all.
        assert not any("parser" in name for name in before.components)
        assert not any("text_sha256" in name for name in before.components)

    def test_changed_bytes_change_the_identity(self):
        other = make_pdf((*CONTINGENCY_LINES, "An added clause."))
        assert document_from(other).content_sha256 != document_from(TERMS_PDF).content_sha256

    def test_payload_round_trips_the_provenance(self):
        document = document_from(TERMS_PDF)
        revived = ExternalDocument.from_payload(document.payload())
        assert revived.extraction_detail == document.extraction_detail


class TestScanningExtractedText:
    def test_it_finds_the_citations_a_human_found_in_the_pdf(self):
        document = document_from(TERMS_PDF)
        assert document.text is not None
        found = {
            d.reference
            for d in scan_incorporations(
                document.text, parent="contract_terms", grammar=KALSHI_CITATION_GRAMMAR
            )
        }
        assert {"Rule 6.3(b)", "Rule 6.3(d)"} <= found

    def test_the_citation_is_preserved_exactly_as_the_pdf_wrote_it(self):
        document = document_from(TERMS_PDF)
        assert document.text is not None
        found = {
            d.reference
            for d in scan_incorporations(
                document.text, parent="contract_terms", grammar=KALSHI_CITATION_GRAMMAR
            )
        }
        assert "Rule 6.3(b)" in found
        assert "Rule 6.3(c)" not in found

    def test_a_readable_pdf_yields_an_enumerated_closure(self):
        derived = dependency_set_for_document(
            "contract_terms", document_from(TERMS_PDF), grammar=KALSHI_CITATION_GRAMMAR
        )
        assert derived.closure is DependencyClosure.ENUMERATED

    def test_a_document_never_incorporates_itself(self):
        """The Rulebook's own table of contents is navigation, not incorporation."""
        contents = make_pdf(("RULE 6.3 SETTLEMENT", "RULE 7.1 MARKET OUTCOME REVIEW"))
        derived = dependency_set_for_document(
            "exchange_rulebook", document_from(contents), grammar=KALSHI_CITATION_GRAMMAR
        )
        assert [d.reference for d in derived.flatten()] == []


class TestFailedExtractionRequiresManualReview:
    def _scanned_page_pdf(self) -> bytes:
        return make_pdf((), blank_pages=2)

    def test_a_pdf_with_no_text_layer_fails(self):
        text, provenance = extract_pdf_text(self._scanned_page_pdf(), at=T0)
        assert text is None
        assert provenance.status == "FAILED"
        assert provenance.warnings

    def test_a_failed_extraction_demands_manual_review(self):
        _, provenance = extract_pdf_text(self._scanned_page_pdf(), at=T0)
        assert provenance.requires_manual_review

    def test_corrupt_bytes_fail_rather_than_raise(self):
        text, provenance = extract_pdf_text(b"%PDF-1.4 not really a pdf", at=T0)
        assert text is None
        assert provenance.status == "FAILED"
        assert provenance.source_sha256 == hashlib.sha256(b"%PDF-1.4 not really a pdf").hexdigest()

    def test_an_unextractable_document_has_an_unknown_closure(self):
        document = document_from(self._scanned_page_pdf())
        derived = dependency_set_for_document(
            "contract_terms", document, grammar=KALSHI_CITATION_GRAMMAR
        )
        assert derived.closure is DependencyClosure.UNKNOWN

    def test_an_unextractable_document_requires_manual_viewing(self):
        assert document_from(self._scanned_page_pdf()).requires_manual_viewing

    def test_a_single_blank_page_is_not_suspicious(self):
        """Contract terms routinely have a blank verso; that is not a scan."""
        _, provenance = extract_pdf_text(make_pdf(CONTINGENCY_LINES, blank_pages=1), at=T0)
        assert provenance.status == "CLEAN"
        assert provenance.pages_with_no_text == (2,)

    def test_several_blank_pages_are(self):
        _, provenance = extract_pdf_text(make_pdf(CONTINGENCY_LINES, blank_pages=4), at=T0)
        assert provenance.status == "PARTIAL"
        assert provenance.requires_manual_review

    def test_zero_text_pages_are_reported_one_indexed(self):
        """So the numbers match what a reviewer sees in a PDF viewer."""
        _, provenance = extract_pdf_text(make_pdf(CONTINGENCY_LINES, blank_pages=1), at=T0)
        assert provenance.pages_with_no_text == (2,)


class TestOperatorSuppliedDocuments:
    """Kalshi answers HTTP 429 to this client for several governing documents."""

    def test_operator_supplied_bytes_are_usable_evidence(self):
        document = ExternalDocument.from_bytes(
            url="https://kalshi.com/docs/kalshi-member-agreement.pdf",
            payload=TERMS_PDF,
            retrieved_at=T0,
            http_status=200,
            content_type="application/pdf",
            extraction=TextExtraction.CLEAN,
            text="text",
            retrieval=DocumentRetrieval.OPERATOR_SUPPLIED,
        )
        assert document.retrieval.is_usable

    def test_it_is_distinguishable_from_something_we_fetched(self):
        """The evidence trail must never claim we retrieved what we did not."""
        document = ExternalDocument.from_bytes(
            url="https://kalshi.com/docs/kalshi-member-agreement.pdf",
            payload=TERMS_PDF,
            retrieved_at=T0,
            http_status=200,
            content_type="application/pdf",
            extraction=TextExtraction.CLEAN,
            text="text",
            retrieval=DocumentRetrieval.OPERATOR_SUPPLIED,
        )
        assert document.retrieval.value == "OPERATOR_SUPPLIED"
        assert document.retrieval.value != DocumentRetrieval.RETRIEVED.value

    def test_a_429_is_still_an_error_not_a_document(self):
        """A 429 body is an error page; hashing it would record a failure as
        evidence."""
        failed = ExternalDocument.missing(
            "https://kalshi.com/docs/kalshi-member-agreement.pdf",
            DocumentRetrieval.HTTP_ERROR,
            "HTTP 429",
        )
        assert not failed.retrieval.is_usable
        assert failed.content_sha256 is None


class TestLooksLikePdf:
    def test_it_recognises_the_magic_bytes(self):
        assert looks_like_pdf(b"%PDF-1.7 ...", None)

    def test_it_recognises_the_content_type(self):
        assert looks_like_pdf(b"anything", "application/pdf")

    def test_plain_text_is_not_a_pdf(self):
        assert not looks_like_pdf(b"just text", "text/plain")


class TestProvenanceRoundTrip:
    def test_it_survives_storage(self):
        _, provenance = extract_pdf_text(TERMS_PDF, at=T0)
        assert PdfExtraction.from_payload(provenance.payload()) == provenance
