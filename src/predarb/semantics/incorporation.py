"""Finding incorporation-by-reference in governing text, and recording it.

Two ways a dependency becomes known, and no third
-------------------------------------------------
**Scanned.** When we can read the parent, a citation grammar extracts the
references it makes. The grammar is supplied by the venue layer, so nothing here
knows what a "Rulebook" is.

**Declared.** When we cannot read the parent -- a PDF whose text extraction
failed -- a human opens it at source and records what it incorporates. That
record is bound to the parent's exact content hash, so it cannot carry over to
an amended document. This is not a convenience: it is the only honest way an
unreadable document can ever become complete evidence, and it puts the human's
reading *into* the evidence chain instead of leaving it in their head.

What the scanner may and may not conclude
-----------------------------------------
It may raise concern and it may never lower it. A citation whose surrounding
sentence talks about payouts is classified ``MATERIAL``; every other citation is
``UNCLASSIFIED``, which blocks just the same. Nothing here can produce
``PROCEDURAL`` -- only a human, with a recorded rationale, can decide that a
referenced rule cannot reach the claim.

The asymmetry is deliberate. A scanner that could clear references would be a
scanner whose bugs silently approve certificates.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Self

from predarb.clock import ensure_utc
from predarb.semantics.dependency import (
    DependencyClosure,
    DependencyDiscovery,
    DependencyMateriality,
    DependencySet,
    GoverningDocumentDependency,
    PayoutImpact,
    VersionBinding,
)
from predarb.semantics.evidence import ExternalDocument

__all__ = [
    "SCANNER_VERSION",
    "CitationGrammar",
    "CitationPattern",
    "DeclarationStore",
    "DependencyDeclaration",
    "classify_citation",
    "declarations_from",
    "dependency_set_for_document",
    "dependency_set_for_text",
    "scan_incorporations",
]

_SHA256_HEX_LENGTH: Final = 64

SCANNER_VERSION: Final = "incorporation-scan/1"
"""Bumped whenever the grammar or classification changes.

Recorded on every ``ENUMERATED`` closure and fingerprinted, so improving the
scanner shows up as drift on existing certificates rather than silently
re-interpreting evidence that was approved under the old rules.
"""


# Sentence-ish boundaries, plus the start of a Rulebook definition. A
# definitions run carries no sentence punctuation, so without the last
# alternative a citation inherits whatever the neighbouring definition says.
_SENTENCE_SPLIT = re.compile(
    r"(?<=[.;:])\s+(?=[A-Z(\u201c])|\n{2,}|(?=\u201c[A-Z][^\u201d]{0,60}\u201d\s+means\b)"
)

# Cues are matched against the sentence that carries the citation, never against
# the referenced rule -- we have not read that yet. Each maps to the way the
# citing sentence says the reference can reach a payout.
_IMPACT_CUES: Final[tuple[tuple[re.Pattern[str], PayoutImpact], ...]] = (
    # "pay" alone is far too loose -- "unable to pay its obligations" is an
    # insolvency clause, not a payout rule -- so the payout cues require a
    # contract-side object. A cue that stops firing does not open a hole: the
    # reference becomes UNCLASSIFIED, which blocks exactly as MATERIAL does
    # until a human declaration covers it.
    (
        re.compile(
            r"\bpayout|\bpay\s+out\b|\bsettlement\s+value\b"
            r"|\bpays?\b[^.;]{0,60}\b(long|short|holder|position|contract)",
            re.I,
        ),
        PayoutImpact.PAYOUT_AMOUNT,
    ),
    (
        re.compile(r"\blong\b.*\bshort\b|\bshort\b.*\blong\b|\byes\b.*\bno\b", re.I),
        PayoutImpact.PAYOUT_SIDE,
    ),
    (
        re.compile(r"\bvoid\b|\bcancel|\brefund|\breturn(ed)?\s+(of\s+)?collateral\b", re.I),
        PayoutImpact.REFUND_OR_VOID,
    ),
    (
        re.compile(
            r"\bfair\s+allocation\b|\bproportion\b|\bdiscretion\b|\breview\s+committee\b",
            re.I,
        ),
        PayoutImpact.FAIR_ALLOCATION,
    ),
    (
        re.compile(
            r"\bexpiration\s+(value|date)\b|\bearly\s+expir|\bterminat"
            r"|\boutcome\s+review\b",
            re.I,
        ),
        PayoutImpact.TERMINAL_STATES,
    ),
    # Likewise scoped: an "amended schedule" of disciplinary offences is not a
    # contract modification.
    (
        re.compile(
            r"\b(modif\w*|amend\w*|chang\w*)\s+(the\s+|a\s+|any\s+)?"
            r"(contract|terms|specification|payout|underlying)",
            re.I,
        ),
        PayoutImpact.CONTRACT_MODIFICATION,
    ),
)


@dataclass(frozen=True, slots=True)
class CitationPattern:
    """One way a document names another governing document."""

    pattern: re.Pattern[str]
    """Must expose a group named ``ref`` holding the citation as written."""

    source_name: str
    source_url: str | None = None
    version_binding: VersionBinding = VersionBinding.UNKNOWN

    def __post_init__(self) -> None:
        if "ref" not in self.pattern.groupindex:
            raise ValueError(f"{self.source_name}: citation pattern needs a named group 'ref'")


@dataclass(frozen=True, slots=True)
class CitationGrammar:
    """The citation styles one venue's governing documents use."""

    name: str
    patterns: tuple[CitationPattern, ...]


def classify_citation(context: str) -> tuple[DependencyMateriality, frozenset[PayoutImpact]]:
    """Read the citing sentence for evidence that the reference reaches a payout.

    Returns ``UNCLASSIFIED`` with no impacts when the sentence says nothing
    payout-shaped. That is not a clearance -- ``UNCLASSIFIED`` blocks -- it just
    records that the automated pass found no positive evidence either way.
    """
    impacts = frozenset(impact for cue, impact in _IMPACT_CUES if cue.search(context))
    if impacts:
        return DependencyMateriality.MATERIAL, impacts
    return DependencyMateriality.UNCLASSIFIED, frozenset()


_MAX_CONTEXT_CHARS: Final = 320


def _sentences(text: str) -> list[str]:
    collapsed = re.sub(r"[ \t]*\n[ \t]*", " ", text)
    return [part.strip() for part in _SENTENCE_SPLIT.split(collapsed) if part.strip()]


def _local_context(sentence: str, start: int, end: int) -> str:
    """The text immediately around a citation, not the whole block it sits in.

    A rulebook's definitions run and its table of contents contain no sentence
    punctuation at all, so the splitter hands back very large blocks. Reading
    materiality from a block like that attributes a neighbouring definition's
    language to this citation -- which is how a reference to the definition of
    "Person" came to look like it governed payouts. Narrowing to the text
    around the match makes the classification describe the citation, in both
    directions.
    """
    if len(sentence) <= _MAX_CONTEXT_CHARS:
        return sentence
    half = _MAX_CONTEXT_CHARS // 2
    left = max(0, start - half)
    right = min(len(sentence), end + half)
    return sentence[left:right].strip()


def scan_incorporations(
    text: str,
    *,
    parent: str,
    grammar: CitationGrammar,
    sources: Mapping[str, ExternalDocument] | None = None,
) -> tuple[GoverningDocumentDependency, ...]:
    """Extract every citation ``text`` makes, deduplicated by citation.

    ``sources`` supplies the referenced documents we already hold, so one
    retrieval of a rulebook resolves every citation into it.

    Self-references are skipped: a citation whose target is the parent itself
    is a cross-reference within one document, not an incorporation of another.
    """
    held = dict(sources or {})
    found: dict[str, GoverningDocumentDependency] = {}
    for sentence in _sentences(text):
        for pattern in grammar.patterns:
            if pattern.source_name == parent:
                # A document's references to its own sections are navigation,
                # not incorporation by reference. Without this the Rulebook's
                # own table of contents becomes seventy dependencies of the
                # Rulebook upon itself, burying the handful that matter.
                continue
            for match in pattern.pattern.finditer(sentence):
                reference = " ".join(match.group("ref").split())
                context = _local_context(sentence, match.start(), match.end())
                materiality, impacts = classify_citation(context)
                dependency = GoverningDocumentDependency(
                    parent=parent,
                    reference=reference,
                    source_name=pattern.source_name,
                    source=held.get(pattern.source_name),
                    payout_impacts=impacts,
                    materiality=materiality,
                    discovery=DependencyDiscovery.EXTRACTED_FROM_TEXT,
                    version_binding=pattern.version_binding,
                    citation_context=context,
                )
                existing = found.get(dependency.key)
                if existing is None or (
                    # Keep the strongest reading: one sentence may cite a rule
                    # neutrally and another may cite it for the payout.
                    len(dependency.payout_impacts) > len(existing.payout_impacts)
                ):
                    found[dependency.key] = dependency
    return tuple(found[key] for key in sorted(found))


@dataclass(frozen=True, slots=True)
class DependencyDeclaration:
    """A human's record of what an unreadable document incorporates.

    Bound to ``document_sha256``. An amended document has a different hash and
    therefore no declaration, which is the intended behaviour: nobody has read
    the new version.
    """

    parent: str
    document_sha256: str
    declared_by: str
    declared_at: datetime
    references: tuple[GoverningDocumentDependency, ...]
    note: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "declared_at", ensure_utc(self.declared_at))
        if len(self.document_sha256) != _SHA256_HEX_LENGTH:
            raise ValueError(
                f"{self.parent}: a declaration must name a full content hash, "
                f"got {self.document_sha256!r}"
            )
        if not self.declared_by.strip():
            raise ValueError(f"{self.parent}: a declaration must name who made it")
        for reference in self.references:
            if reference.parent != self.parent:
                raise ValueError(
                    f"{self.parent}: declared reference {reference.reference!r} names "
                    f"parent {reference.parent!r}"
                )
            if reference.discovery is not DependencyDiscovery.DECLARED_BY_REVIEWER:
                raise ValueError(
                    f"{self.parent}: declared reference {reference.reference!r} claims "
                    f"discovery {reference.discovery.value}; a human declaration must say so"
                )

    @property
    def declaration_id(self) -> str:
        payload = f"{self.parent}\x1e{self.document_sha256}"
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def applies_to(self, document: ExternalDocument | None) -> bool:
        return document is not None and document.content_sha256 == self.document_sha256

    def to_set(self, sources: Mapping[str, ExternalDocument] | None = None) -> DependencySet:
        """The enumerated closure this declaration establishes."""
        held = dict(sources or {})
        resolved = tuple(
            reference
            if reference.source is not None
            else GoverningDocumentDependency(
                parent=reference.parent,
                reference=reference.reference,
                source_name=reference.source_name,
                source=held.get(reference.source_name),
                payout_impacts=reference.payout_impacts,
                materiality=reference.materiality,
                discovery=reference.discovery,
                version_binding=reference.version_binding,
                source_version=reference.source_version,
                citation_context=reference.citation_context,
                rationale=reference.rationale,
                dependencies=reference.dependencies,
            )
            for reference in self.references
        )
        return DependencySet(
            parent=self.parent,
            closure=DependencyClosure.ENUMERATED,
            dependencies=resolved,
            note=(
                f"declared by {self.declared_by} at {self.declared_at.isoformat()} "
                f"against sha256 {self.document_sha256[:12]}"
                + (f"; {self.note}" if self.note else "")
            ),
            scanner_version=f"declaration/{self.declaration_id}",
        )

    def payload(self) -> dict[str, Any]:
        return {
            "parent": self.parent,
            "document_sha256": self.document_sha256,
            "declared_by": self.declared_by,
            "declared_at": self.declared_at.isoformat(),
            "note": self.note,
            "references": [
                {
                    "reference": reference.reference,
                    "source_name": reference.source_name,
                    "payout_impacts": sorted(i.value for i in reference.payout_impacts),
                    "materiality": reference.materiality.value,
                    "version_binding": reference.version_binding.value,
                    "source_version": reference.source_version,
                    "citation_context": reference.citation_context,
                    "rationale": reference.rationale,
                }
                for reference in self.references
            ],
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Self:
        references = tuple(
            GoverningDocumentDependency(
                parent=str(payload["parent"]),
                reference=str(item["reference"]),
                source_name=str(item["source_name"]),
                payout_impacts=frozenset(
                    PayoutImpact(value) for value in item.get("payout_impacts", ())
                ),
                materiality=DependencyMateriality(item["materiality"]),
                discovery=DependencyDiscovery.DECLARED_BY_REVIEWER,
                version_binding=VersionBinding(item.get("version_binding", "UNKNOWN")),
                source_version=item.get("source_version"),
                citation_context=item.get("citation_context"),
                rationale=item.get("rationale"),
            )
            for item in payload.get("references", ())
        )
        return cls(
            parent=str(payload["parent"]),
            document_sha256=str(payload["document_sha256"]),
            declared_by=str(payload["declared_by"]),
            declared_at=datetime.fromisoformat(str(payload["declared_at"])),
            references=references,
            note=payload.get("note"),
        )


@dataclass(frozen=True, slots=True)
class DeclarationStore:
    """Reviewer declarations, indexed by (parent, document hash)."""

    declarations: tuple[DependencyDeclaration, ...] = ()

    def lookup(
        self, parent: str, document: ExternalDocument | None
    ) -> DependencyDeclaration | None:
        for declaration in self.declarations:
            if declaration.parent == parent and declaration.applies_to(document):
                return declaration
        return None

    @classmethod
    def load(cls, path: Path) -> Self:
        if not path.exists():
            return cls()
        payload = json.loads(path.read_text())
        return cls(
            declarations=tuple(
                DependencyDeclaration.from_payload(item) for item in payload.get("declarations", ())
            )
        )

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"declarations": [d.payload() for d in self.declarations]}
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")

    def extended(self, declarations: Iterable[DependencyDeclaration]) -> Self:
        merged: dict[tuple[str, str], DependencyDeclaration] = {
            (d.parent, d.document_sha256): d for d in self.declarations
        }
        for declaration in declarations:
            merged[(declaration.parent, declaration.document_sha256)] = declaration
        return type(self)(declarations=tuple(merged[key] for key in sorted(merged)))


def dependency_set_for_document(
    parent: str,
    document: ExternalDocument | None,
    *,
    grammar: CitationGrammar,
    sources: Mapping[str, ExternalDocument] | None = None,
    declarations: DeclarationStore | None = None,
) -> DependencySet:
    """Scan if we can read it, use a hash-bound declaration if we cannot.

    The order matters: a declaration is consulted only when the document is
    genuinely unreadable, so a human record can never quietly override what the
    text actually says.
    """
    if document is None or document.url is None:
        return DependencySet.not_applicable(parent)
    if not document.retrieval.is_usable:
        return DependencySet.for_document(parent, document)
    if document.extraction.is_readable and document.text:
        return DependencySet.for_document(
            parent,
            document,
            dependencies=scan_incorporations(
                document.text, parent=parent, grammar=grammar, sources=sources
            ),
            scanner_version=SCANNER_VERSION,
        )
    declaration = (declarations or DeclarationStore()).lookup(parent, document)
    if declaration is not None:
        return declaration.to_set(sources)
    return DependencySet.for_document(parent, document)


def dependency_set_for_text(
    parent: str,
    text: str | None,
    *,
    grammar: CitationGrammar,
    sources: Mapping[str, ExternalDocument] | None = None,
) -> DependencySet:
    """Closure for a governing component we hold as plain text (rules fields)."""
    if text is None or not text.strip():
        return DependencySet.not_applicable(parent, "component carries no text")
    return DependencySet(
        parent=parent,
        closure=DependencyClosure.ENUMERATED,
        dependencies=scan_incorporations(text, parent=parent, grammar=grammar, sources=sources),
        scanner_version=SCANNER_VERSION,
    )


def declarations_from(records: Sequence[Mapping[str, Any]]) -> DeclarationStore:
    return DeclarationStore(
        declarations=tuple(DependencyDeclaration.from_payload(record) for record in records)
    )
