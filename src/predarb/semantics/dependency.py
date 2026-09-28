"""Governing documents that incorporate *other* governing documents.

The gap this closes
-------------------
Step 8 treated a governing document as self-contained: fetch the contract terms,
hash them, and the contract is captured. Real contract terms are not
self-contained. Kalshi's terms say, verbatim:

    "If an Expiration Value cannot be determined on the Expiration Date, Kalshi
    has the right to determine payouts pursuant to Rule 6.3(b) in the Rulebook."

That sentence puts the payout rule for an entire class of terminal states in a
*different document*. A review conducted with the terms alone has not read the
rule that decides what happens when the ordinary outcome cannot be determined --
which is precisely the state a complement claim most needs to be sure about.

So a governing document is a node, not a leaf. Incorporation by reference
transfers governing force to the referenced text, and evidence completeness has
to follow that transfer.

The model is deliberately venue-neutral
---------------------------------------
Nothing here knows what "Rule 6.3(b)" is, or that Kalshi exists. A dependency is
(parent, reference, referenced source, materiality, retrieval, hash). The venue
layer supplies the citations; the policy layer decides which ones a given claim
cannot proceed without. Hardcoding one exchange's rule numbers would have to be
rewritten the moment that exchange renumbers its rulebook -- which, as it
happens, it already has: the terms cite 6.3(b) and 6.3(d) for provisions that
current Rulebook v1.29 numbers 6.3(c) and 6.3(f).

Failing closed
--------------
Two states fail closed, both because the alternative is a certificate that
*looks* well-founded:

* an **unclassified** dependency is treated as material. We cannot establish
  that a rule we have not read is harmless.
* an **unreadable parent** has an unknown dependency set, not an empty one. A
  PDF we could not extract may incorporate anything. Recording that as "no
  dependencies found" would convert a failure to read into a finding.

The second is what actually happened: both markets reviewed in the Phase-1
acceptance pass held contract-terms PDFs whose text extraction FAILED, and the
incorporation clauses were found by a human opening the PDF, not by us.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from predarb.semantics.evidence import DocumentRetrieval, ExternalDocument, TextExtraction
from predarb.semantics.fingerprint import ABSENT

__all__ = [
    "DependencyClosure",
    "DependencyDiscovery",
    "DependencyGraph",
    "DependencyMateriality",
    "DependencySet",
    "GoverningDocumentDependency",
    "PayoutImpact",
    "ReferenceResolution",
    "VersionBinding",
    "merge_dependency_sets",
    "reference_slug",
]


class PayoutImpact(StrEnum):
    """The ways an incorporated rule can reach a payout claim.

    A claim declares which of these it cannot tolerate being unread. This is the
    general form of the brief's list -- payout amount, payout side, refund/void
    treatment, scalar fair allocation, terminal settlement states -- expressed
    so that a future claim can declare a different set.
    """

    PAYOUT_AMOUNT = "PAYOUT_AMOUNT"
    """Changes how much is paid (e.g. payouts set by last traded price)."""

    PAYOUT_SIDE = "PAYOUT_SIDE"
    """Changes who is paid."""

    REFUND_OR_VOID = "REFUND_OR_VOID"
    """Permits voiding, cancellation, or return of collateral instead of payout."""

    FAIR_ALLOCATION = "FAIR_ALLOCATION"
    """Permits a discretionary split between long and short, rather than a
    binary award. A committee that may allocate "fairly" is not obviously
    constrained to sum to the notional; that is exactly the question a
    complement claim asks."""

    TERMINAL_STATES = "TERMINAL_STATES"
    """Adds, removes or redefines the reachable terminal settlement states
    (e.g. rules permitting early expiration or a changed expiration date)."""

    CONTRACT_MODIFICATION = "CONTRACT_MODIFICATION"
    """Permits the contract's own specifications to be changed after listing."""


class DependencyMateriality(StrEnum):
    """Whether an incorporated reference can reach the claim.

    ``UNCLASSIFIED`` is not a third outcome between the other two -- it is
    treated as ``MATERIAL`` everywhere it matters. It exists so the audit trail
    can distinguish "a human read this and found it procedural" from "nobody has
    looked", which are very different grounds for an approval.
    """

    MATERIAL = "MATERIAL"
    PROCEDURAL = "PROCEDURAL"
    UNCLASSIFIED = "UNCLASSIFIED"

    @property
    def blocks_until_read(self) -> bool:
        return self is not DependencyMateriality.PROCEDURAL


class DependencyDiscovery(StrEnum):
    """How we came to know a dependency exists -- or that we cannot know."""

    EXTRACTED_FROM_TEXT = "EXTRACTED_FROM_TEXT"
    """Found by scanning readable text of the parent document."""

    DECLARED_BY_REVIEWER = "DECLARED_BY_REVIEWER"
    """Supplied by a human who opened the parent at source."""

    DECLARED_BY_POLICY = "DECLARED_BY_POLICY"
    """Known to govern every document of this kind, independent of its text."""


class DependencyClosure(StrEnum):
    """Whether the *set* of a parent's dependencies is known to be complete."""

    ENUMERATED = "ENUMERATED"
    """The parent was read and its references listed. May legitimately be empty."""

    UNKNOWN = "UNKNOWN"
    """The parent could not be read, so it may incorporate anything. Fails
    closed: an unknown closure is never a complete one."""

    NOT_APPLICABLE = "NOT_APPLICABLE"
    """No parent document is published, so nothing can be incorporated."""

    @property
    def is_established(self) -> bool:
        return self is not DependencyClosure.UNKNOWN


class VersionBinding(StrEnum):
    """*Which edition* of a referenced source governs the relationship.

    Strictly separate from :class:`ReferenceResolution`, which asks *what a
    given citation points at*. Conflating the two is tempting and wrong: a
    Rulebook can bind dynamically while a specific section number written in
    2025 still fails to identify anything in today's text.
    """

    AS_AMENDED_FROM_TIME_TO_TIME = "AS_AMENDED_FROM_TIME_TO_TIME"
    """The current text governs, and an amendment changes what governs.

    This is Kalshi's position for the Member relationship, in the Member
    Agreement's own words: bound by "the Kalshi rules (as supplemented or
    amended from time to time, the 'Kalshi Rulebook')" and to "the Kalshi
    Rulebook, as now existing and as hereafter duly amended from time to time"."""

    AS_OF_ISSUANCE = "AS_OF_ISSUANCE"
    """The text in force when the parent was certified governs; later
    amendments do not reach back."""

    UNKNOWN = "UNKNOWN"
    """Not established from authoritative documentation. Treated conservatively:
    a change in the referenced source requires human re-review."""

    @property
    def amendment_requires_review(self) -> bool:
        """Whether a change in the source obliges a fresh human reading.

        True for dynamic binding because the governing text genuinely changed,
        and true for UNKNOWN because we cannot show it did not.
        """
        return self is not VersionBinding.AS_OF_ISSUANCE


class ReferenceResolution(StrEnum):
    """What a *written* citation actually points at today.

    A separate axis from :class:`VersionBinding` on purpose. Kalshi's Rulebook
    binds dynamically, and yet the BOND contract terms cite "Rule 6.3(b)" for
    payout determination while Rule 6.3(b) is the Scalar Contract rule -- in the
    current Rulebook *and* in v1.18, the edition in force when the neighbouring
    CRIMECHARGE product was certified. Dynamic binding does not repair a
    citation; it just means the target keeps moving.

    Nothing in this system ever advances a citation along this axis on its own.
    Prose similarity between an old section and a new one is research evidence,
    not authority: a binding citation may be re-pointed only by a product-
    specific amendment, a Rulebook redline proving the movement, an official
    filing updating the reference, or an equivalent authoritative source.
    """

    EXACT_CURRENT_REFERENCE = "EXACT_CURRENT_REFERENCE"
    """The cited section exists in the current source and is the provision the
    citing sentence relies on. Requires evidence, not a number match."""

    HISTORICAL_REFERENCE_RESOLVED = "HISTORICAL_REFERENCE_RESOLVED"
    """The citation named an earlier edition, and an authoritative source
    establishes what it maps to now."""

    REFERENCE_AMENDED_BY_PRODUCT_FILING = "REFERENCE_AMENDED_BY_PRODUCT_FILING"
    """A product-specific amendment replaced the citation. The amendment, not
    the superseded terms document, is what governs."""

    AMBIGUOUS_LEGACY_REFERENCE = "AMBIGUOUS_LEGACY_REFERENCE"
    """The cited section exists but plainly does not say what the citing
    sentence relies on, and no authority establishes the intended target."""

    BROKEN_REFERENCE = "BROKEN_REFERENCE"
    """The cited section does not exist in the applicable source at all."""

    BROKEN_REFERENCE_AT_ISSUANCE = "BROKEN_REFERENCE_AT_ISSUANCE"
    """The citation did not identify the relied-on provision on the day the
    product was certified.

    Strictly worse than a stale reference, and never repairable by lineage: a
    citation that was wrong when written does not acquire a valid historical
    target merely because an older template once used that number.
    """

    UNKNOWN = "UNKNOWN"
    """Not yet examined. The default, and it blocks."""

    @property
    def is_resolved(self) -> bool:
        """Whether the citation identifies a provision we can actually read."""
        return self in {
            ReferenceResolution.EXACT_CURRENT_REFERENCE,
            ReferenceResolution.HISTORICAL_REFERENCE_RESOLVED,
            ReferenceResolution.REFERENCE_AMENDED_BY_PRODUCT_FILING,
        }


_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def reference_slug(reference: str) -> str:
    """A stable, filesystem- and fingerprint-safe key for a citation.

    ``"Rule 6.3(b)"`` and ``"rule 6.3 (b)"`` are the same citation written two
    ways, and must not produce two dependencies.
    """
    slug = _SLUG_STRIP.sub("-", reference.strip().lower()).strip("-")
    # A leading article is punctuation, not identity: "the Rulebook" and
    # "Rulebook" are one incorporation written two ways, and counting them twice
    # would make a reviewer acknowledge the same document under two names.
    slug = slug.removeprefix("the-")
    if not slug:
        raise ValueError(f"citation {reference!r} has no usable characters")
    return slug


@dataclass(frozen=True, slots=True)
class GoverningDocumentDependency:
    """One incorporation-by-reference, and the state of the source it points at.

    Recursive on purpose: the referenced source may itself incorporate a third
    document, and a nested dependency that stopped being counted would be a
    silent hole in exactly the place this class exists to close.
    """

    parent: str
    """The component that does the incorporating, e.g. ``"contract_terms"``."""

    reference: str
    """The citation as written in the parent, e.g. ``"Rule 6.3(b)"``."""

    source_name: str
    """Canonical name of the referenced governing source, e.g.
    ``"exchange_rulebook"``. Shared by every citation into the same document, so
    one retrieval satisfies them all."""

    source: ExternalDocument | None = None
    """The referenced document itself, when we hold it."""

    payout_impacts: frozenset[PayoutImpact] = frozenset()
    """How this reference can reach a payout, if it can."""

    materiality: DependencyMateriality = DependencyMateriality.UNCLASSIFIED
    discovery: DependencyDiscovery = DependencyDiscovery.EXTRACTED_FROM_TEXT
    version_binding: VersionBinding = VersionBinding.UNKNOWN

    source_version: str | None = None
    """Version label of the retrieved source, e.g. ``"1.29"``, when it states
    one. Kept beside the hash because a renumbering is legible to a human in a
    way a changed digest is not."""

    reference_resolution: ReferenceResolution = ReferenceResolution.UNKNOWN
    """What the citation, as written, actually points at. Never advanced
    automatically -- see :class:`ReferenceResolution`."""

    resolution_authority: str | None = None
    """The authoritative source that established a resolved status: a filing
    identifier, an amendment, a redline. Required for any resolved value, so a
    resolution can be checked rather than believed."""

    resolved_reference: str | None = None
    """What the citation resolves *to*, when that differs from what was
    written, e.g. ``"Rule 6.3(c)"``. The written ``reference`` is never
    overwritten -- both are kept, side by side."""

    current_text_excerpt: str | None = None
    """What the cited section number actually says in the source we hold.
    Research evidence for a reviewer, never a resolution by itself."""

    materiality_declaration_id: str | None = None
    """The human declaration that classified this reference for a claim, if
    one did. Fingerprinted, so revoking it is visible drift."""

    materiality_policy_version: str | None = None
    """The materiality policy version in force when this was classified."""

    citation_context: str | None = None
    """The sentence that does the incorporating, quoted from the parent. This is
    the evidence for the materiality classification, so a reviewer can check the
    classification rather than trust it."""

    rationale: str | None = None
    """Why this was classified as it was -- required for ``PROCEDURAL``, since
    downgrading a reference is the only way to stop it blocking."""

    dependencies: tuple[GoverningDocumentDependency, ...] = ()
    """References the *referenced source* itself incorporates."""

    def __post_init__(self) -> None:
        if self.materiality is DependencyMateriality.PROCEDURAL:
            if self.payout_impacts:
                raise ValueError(
                    f"{self.parent}/{self.reference}: classified PROCEDURAL while declaring "
                    f"payout impacts {sorted(i.value for i in self.payout_impacts)}; a reference "
                    "that can reach a payout is not procedural"
                )
            if not (self.rationale or "").strip():
                raise ValueError(
                    f"{self.parent}/{self.reference}: PROCEDURAL requires a rationale; "
                    "an unexplained downgrade is indistinguishable from an oversight"
                )
        if self.payout_impacts and self.materiality is not DependencyMateriality.MATERIAL:
            raise ValueError(
                f"{self.parent}/{self.reference}: declares payout impacts but is "
                f"{self.materiality.value}; declared impact implies materiality"
            )
        if self.reference_resolution.is_resolved and not (self.resolution_authority or "").strip():
            raise ValueError(
                f"{self.parent}/{self.reference}: resolved as "
                f"{self.reference_resolution.value} without naming the authority that "
                "established it; prose similarity is research evidence, not authority"
            )
        if self.resolved_reference and not self.reference_resolution.is_resolved:
            raise ValueError(
                f"{self.parent}/{self.reference}: names a resolved target "
                f"{self.resolved_reference!r} while unresolved "
                f"({self.reference_resolution.value}); a citation is never silently "
                "re-pointed"
            )

    @property
    def dependency_id(self) -> str:
        """Content-derived identity: same citation into the same source, same id."""
        payload = f"{self.parent}\x1e{self.source_name}\x1e{reference_slug(self.reference)}"
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    @property
    def key(self) -> str:
        """Human-readable, stable key used in fingerprints and reports."""
        return f"{self.parent}/{self.source_name}/{reference_slug(self.reference)}"

    @property
    def retrieval(self) -> DocumentRetrieval:
        return self.source.retrieval if self.source is not None else DocumentRetrieval.NOT_ATTEMPTED

    @property
    def source_sha256(self) -> str | None:
        return self.source.content_sha256 if self.source is not None else None

    @property
    def extraction(self) -> TextExtraction:
        return self.source.extraction if self.source is not None else TextExtraction.NOT_APPLICABLE

    @property
    def is_resolved(self) -> bool:
        """Whether this dependency is discharged: source held *and* citation
        understood.

        Both halves are needed. Holding the Rulebook does not help if the
        citation into it points at the wrong provision, and knowing which
        provision is meant does not help if we do not have the text.
        """
        return self.source_is_held and self.reference_resolution.is_resolved

    @property
    def source_is_held(self) -> bool:
        """Whether we hold the referenced document's bytes."""
        return self.source is not None and self.source.retrieval.is_usable

    @property
    def requires_manual_viewing(self) -> bool:
        """Retrieved, but not rendered readably -- a human must open it."""
        return self.source is not None and self.source.requires_manual_viewing

    def is_material_to(self, impacts: Iterable[PayoutImpact]) -> bool:
        """Whether this reference can reach a claim sensitive to ``impacts``.

        Unclassified references answer ``True`` regardless: the absence of a
        classification is not evidence of irrelevance.
        """
        if self.materiality is DependencyMateriality.PROCEDURAL:
            return False
        if self.materiality is DependencyMateriality.UNCLASSIFIED:
            return True
        return bool(self.payout_impacts & set(impacts))

    def flatten(self, _seen: set[str] | None = None) -> Iterator[GoverningDocumentDependency]:
        """This dependency and every nested one, depth-first and cycle-safe.

        Visited keys are tracked even though an object cycle cannot be built
        from immutable values: the graph these describe genuinely does contain
        cycles (the Member Agreement incorporates the Rulebook, and the
        Rulebook incorporates the Member Agreement), and a traversal that
        assumed otherwise would be one refactor away from hanging.

        Children are walked in key order, so the sequence does not depend on
        the order a caller happened to build the tuple in.
        """
        seen = _seen if _seen is not None else set()
        if self.key in seen:
            return
        seen.add(self.key)
        yield self
        for nested in sorted(self.dependencies, key=lambda d: d.key):
            yield from nested.flatten(seen)

    def fingerprint_values(self) -> dict[str, object]:
        """This dependency's contribution to the evidence fingerprint.

        The hash of the referenced source is included, so an amendment to an
        incorporated rulebook is drift in every certificate that relied on it.
        The materiality and version binding are included too: reclassifying a
        rule as procedural, or discovering its version semantics, changes what
        was approved even when no document byte moved.
        """
        prefix = f"dependency.{self.key}"
        return {
            f"{prefix}.sha256": self.source_sha256 or ABSENT,
            f"{prefix}.retrieval": self.retrieval.value,
            f"{prefix}.materiality": self.materiality.value,
            f"{prefix}.impacts": tuple(sorted(i.value for i in self.payout_impacts)),
            f"{prefix}.version_binding": self.version_binding.value,
            f"{prefix}.source_version": self.source_version or ABSENT,
            f"{prefix}.reference_resolution": self.reference_resolution.value,
            f"{prefix}.resolved_reference": self.resolved_reference or ABSENT,
            # Both are fingerprinted so that revoking a declaration, or
            # tightening the policy, invalidates the certificates that relied
            # on it rather than silently changing what they meant.
            f"{prefix}.materiality_declaration": self.materiality_declaration_id or ABSENT,
            f"{prefix}.materiality_policy": self.materiality_policy_version or ABSENT,
        }

    def payload(self) -> dict[str, Any]:
        return {
            "parent": self.parent,
            "reference": self.reference,
            "source_name": self.source_name,
            "source": self.source.payload() if self.source is not None else None,
            "payout_impacts": sorted(impact.value for impact in self.payout_impacts),
            "materiality": self.materiality.value,
            "discovery": self.discovery.value,
            "version_binding": self.version_binding.value,
            "source_version": self.source_version,
            "reference_resolution": self.reference_resolution.value,
            "resolution_authority": self.resolution_authority,
            "resolved_reference": self.resolved_reference,
            "current_text_excerpt": self.current_text_excerpt,
            "materiality_declaration_id": self.materiality_declaration_id,
            "materiality_policy_version": self.materiality_policy_version,
            "citation_context": self.citation_context,
            "rationale": self.rationale,
            "dependencies": [nested.payload() for nested in self.dependencies],
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> GoverningDocumentDependency:
        source = payload.get("source")
        return cls(
            parent=str(payload["parent"]),
            reference=str(payload["reference"]),
            source_name=str(payload["source_name"]),
            source=ExternalDocument.from_payload(source) if source else None,
            payout_impacts=frozenset(
                PayoutImpact(value) for value in payload.get("payout_impacts", ())
            ),
            materiality=DependencyMateriality(payload["materiality"]),
            discovery=DependencyDiscovery(payload["discovery"]),
            version_binding=VersionBinding(payload.get("version_binding", "UNKNOWN")),
            source_version=payload.get("source_version"),
            reference_resolution=ReferenceResolution(
                payload.get("reference_resolution", "UNKNOWN")
            ),
            resolution_authority=payload.get("resolution_authority"),
            resolved_reference=payload.get("resolved_reference"),
            current_text_excerpt=payload.get("current_text_excerpt"),
            materiality_declaration_id=payload.get("materiality_declaration_id"),
            materiality_policy_version=payload.get("materiality_policy_version"),
            citation_context=payload.get("citation_context"),
            rationale=payload.get("rationale"),
            dependencies=tuple(
                cls.from_payload(nested) for nested in payload.get("dependencies", ())
            ),
        )

    def describe(self) -> str:
        held = "held" if self.source_is_held else f"NOT-HELD({self.retrieval.value})"
        impacts = ",".join(sorted(i.value for i in self.payout_impacts)) or "-"
        version = f" v{self.source_version}" if self.source_version else ""
        target = f" -> {self.resolved_reference}" if self.resolved_reference else ""
        return (
            f"{self.reference}{target} -> {self.source_name}{version} "
            f"[{self.materiality.value}/{impacts}] source={held} "
            f"citation={self.reference_resolution.value}"
        )


@dataclass(frozen=True, slots=True)
class DependencySet:
    """Everything one parent document incorporates, and whether that list is complete.

    The closure is the load-bearing part. A set of zero dependencies means two
    completely different things depending on whether we read the parent, and
    conflating them is how an unreadable PDF becomes an approved certificate.
    """

    parent: str
    closure: DependencyClosure
    dependencies: tuple[GoverningDocumentDependency, ...] = ()
    note: str | None = None
    scanner_version: str | None = None
    """Which extraction pass produced an ``ENUMERATED`` closure, so a later
    improvement to the scanner is visible as drift rather than invisible."""

    def __post_init__(self) -> None:
        # An UNKNOWN closure carrying dependencies is meaningful and allowed: a
        # human may have found some references in a document we still cannot
        # fully read. What it must never do is upgrade the closure.
        if self.closure is DependencyClosure.NOT_APPLICABLE and self.dependencies:
            raise ValueError(
                f"{self.parent}: NOT_APPLICABLE closure cannot carry dependencies; "
                "a document that does not exist incorporates nothing"
            )
        seen: dict[str, str] = {}
        for dependency in self.dependencies:
            if dependency.parent != self.parent:
                raise ValueError(
                    f"{self.parent}: dependency {dependency.reference!r} names parent "
                    f"{dependency.parent!r}"
                )
            if dependency.key in seen:
                raise ValueError(f"{self.parent}: duplicate citation {dependency.key}")
            seen[dependency.key] = dependency.reference

    @classmethod
    def unknown(cls, parent: str, note: str) -> DependencySet:
        return cls(parent=parent, closure=DependencyClosure.UNKNOWN, note=note)

    @classmethod
    def not_applicable(cls, parent: str, note: str = "no document published") -> DependencySet:
        return cls(parent=parent, closure=DependencyClosure.NOT_APPLICABLE, note=note)

    @classmethod
    def for_document(
        cls,
        parent: str,
        document: ExternalDocument | None,
        *,
        dependencies: Iterable[GoverningDocumentDependency] = (),
        scanner_version: str | None = None,
    ) -> DependencySet:
        """Derive the closure from the parent document's own readability.

        This is the default every capture path should use, because it makes the
        honest answer the easy one: we can only claim to have enumerated what a
        document incorporates if we could read the document.
        """
        if document is None or document.url is None:
            return cls.not_applicable(parent)
        if not document.retrieval.is_usable:
            return cls.unknown(
                parent,
                f"parent not retrieved ({document.retrieval.value}); "
                "incorporated references cannot be enumerated",
            )
        if not document.extraction.is_readable:
            return cls(
                parent=parent,
                closure=DependencyClosure.UNKNOWN,
                dependencies=tuple(dependencies),
                note=(
                    f"parent text extraction {document.extraction.value}; the document may "
                    "incorporate references we cannot see"
                ),
                scanner_version=scanner_version,
            )
        return cls(
            parent=parent,
            closure=DependencyClosure.ENUMERATED,
            dependencies=tuple(dependencies),
            scanner_version=scanner_version,
        )

    def flatten(self) -> tuple[GoverningDocumentDependency, ...]:
        """Every dependency in this set, deduplicated and deterministically ordered.

        Deduplication is by citation key, so a diamond -- two citations into a
        document that itself cites a third -- expands once, not twice. The
        result is sorted so two sets built in different insertion orders
        flatten, fingerprint and assess identically.
        """
        seen: set[str] = set()
        collected: list[GoverningDocumentDependency] = []
        for dependency in sorted(self.dependencies, key=lambda d: d.key):
            collected.extend(dependency.flatten(seen))
        return tuple(sorted(collected, key=lambda d: d.key))

    def blocking(self, impacts: Iterable[PayoutImpact]) -> tuple[GoverningDocumentDependency, ...]:
        """Material dependencies not yet discharged, nested ones included."""
        wanted = tuple(impacts)
        return tuple(
            dependency
            for dependency in self.flatten()
            if dependency.is_material_to(wanted) and not dependency.is_resolved
        )

    def unheld_sources(
        self, impacts: Iterable[PayoutImpact]
    ) -> tuple[GoverningDocumentDependency, ...]:
        """Material dependencies whose referenced document we do not have."""
        wanted = tuple(impacts)
        return tuple(
            dependency
            for dependency in self.flatten()
            if dependency.is_material_to(wanted) and not dependency.source_is_held
        )

    def unresolved_citations(
        self, impacts: Iterable[PayoutImpact]
    ) -> tuple[GoverningDocumentDependency, ...]:
        """Material dependencies we hold, whose citation points somewhere unclear.

        Reported apart from an unheld source because the remedy is different:
        one is a fetch, the other is a question about which provision the
        contract actually invoked.
        """
        wanted = tuple(impacts)
        return tuple(
            dependency
            for dependency in self.flatten()
            if dependency.is_material_to(wanted)
            and dependency.source_is_held
            and not dependency.reference_resolution.is_resolved
        )

    def manual_viewing(self, impacts: Iterable[PayoutImpact]) -> dict[str, str]:
        """Material dependencies held but unreadable, mapped to the exact hash.

        Keyed distinctly from documents so an acknowledgement of the contract
        terms can never be mistaken for an acknowledgement of the rulebook the
        terms incorporate.
        """
        wanted = tuple(impacts)
        return {
            f"dependency:{dependency.key}": sha
            for dependency in self.flatten()
            if dependency.is_material_to(wanted)
            and dependency.requires_manual_viewing
            and (sha := dependency.source_sha256) is not None
        }

    def fingerprint_values(self) -> dict[str, object]:
        values: dict[str, object] = {
            f"dependency_closure.{self.parent}": self.closure.value,
        }
        for dependency in self.flatten():
            values.update(dependency.fingerprint_values())
        return values

    def payload(self) -> dict[str, Any]:
        return {
            "parent": self.parent,
            "closure": self.closure.value,
            "note": self.note,
            "scanner_version": self.scanner_version,
            "dependencies": [dependency.payload() for dependency in self.dependencies],
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> DependencySet:
        return cls(
            parent=str(payload["parent"]),
            closure=DependencyClosure(payload["closure"]),
            dependencies=tuple(
                GoverningDocumentDependency.from_payload(item)
                for item in payload.get("dependencies", ())
            ),
            note=payload.get("note"),
            scanner_version=payload.get("scanner_version"),
        )

    def describe(self) -> str:
        if self.closure is DependencyClosure.NOT_APPLICABLE:
            return f"{self.parent}: nothing incorporated (no document)"
        listed = ", ".join(d.describe() for d in self.flatten()) or "none listed"
        return f"{self.parent}: closure={self.closure.value}; {listed}"


def merge_dependency_sets(sets: Iterable[DependencySet]) -> Mapping[str, DependencySet]:
    """Index sets by parent, refusing two sets for the same parent."""
    merged: dict[str, DependencySet] = {}
    for item in sets:
        if item.parent in merged:
            raise ValueError(f"two dependency sets for parent {item.parent!r}")
        merged[item.parent] = item
    return dict(sorted(merged.items()))


@dataclass(frozen=True, slots=True)
class DependencyGraph:
    """The whole incorporation graph for one bundle, cycles and all.

    Cycles are real here and are not errors. The Member Agreement incorporates
    the Rulebook, and the Rulebook's definition of the Member relationship
    points back at the Member Agreement; that is how the documents are written.
    What matters is that traversal terminates, that it is deterministic, and
    that an unresolved material edge inside a cycle still blocks -- a loop is
    not a loophole.
    """

    sets: Mapping[str, DependencySet]

    def __post_init__(self) -> None:
        for parent, dependency_set in self.sets.items():
            if not isinstance(dependency_set, DependencySet):
                raise TypeError(
                    f"{parent}: a graph holds DependencySet values, got "
                    f"{type(dependency_set).__name__}"
                )
            if dependency_set.parent != parent:
                raise ValueError(
                    f"{parent}: set is keyed here but names parent {dependency_set.parent!r}"
                )
        object.__setattr__(self, "sets", dict(sorted(self.sets.items())))

    @property
    def parents(self) -> tuple[str, ...]:
        return tuple(self.sets)

    def edges(self) -> tuple[tuple[str, str, str], ...]:
        """``(parent, source_name, citation)`` for every edge, sorted."""
        return tuple(
            sorted(
                (dependency.parent, dependency.source_name, dependency.reference)
                for dependency_set in self.sets.values()
                for dependency in dependency_set.flatten()
            )
        )

    def adjacency(self) -> Mapping[str, tuple[str, ...]]:
        """Parent -> the sources it incorporates, deduplicated and sorted."""
        out: dict[str, tuple[str, ...]] = {}
        for parent, dependency_set in self.sets.items():
            targets = {d.source_name for d in dependency_set.flatten()}
            out[parent] = tuple(sorted(targets))
        return out

    def cycles(self) -> tuple[tuple[str, ...], ...]:
        """Every distinct cycle among document nodes, in canonical form.

        Each cycle is rotated to start at its smallest node and reported once,
        so the same loop discovered from two different entry points is one
        finding rather than two.
        """
        adjacency = self.adjacency()
        found: set[tuple[str, ...]] = set()

        def walk(node: str, path: list[str], on_path: set[str]) -> None:
            for target in adjacency.get(node, ()):
                if target in on_path:
                    cycle = path[path.index(target) :]
                    pivot = cycle.index(min(cycle))
                    found.add(tuple(cycle[pivot:] + cycle[:pivot]))
                    continue
                if target not in adjacency:
                    # A source nobody recorded a set for is a leaf, not a loop.
                    continue
                path.append(target)
                on_path.add(target)
                walk(target, path, on_path)
                path.pop()
                on_path.discard(target)

        for start in sorted(adjacency):
            walk(start, [start], {start})
        return tuple(sorted(found))

    def has_cycle(self) -> bool:
        return bool(self.cycles())

    def all_dependencies(self) -> tuple[GoverningDocumentDependency, ...]:
        """Every dependency in the graph, deduplicated by key and sorted."""
        by_key: dict[str, GoverningDocumentDependency] = {}
        for dependency_set in self.sets.values():
            for dependency in dependency_set.flatten():
                by_key.setdefault(dependency.key, dependency)
        return tuple(by_key[key] for key in sorted(by_key))

    def blocking(self, impacts: Iterable[PayoutImpact]) -> tuple[GoverningDocumentDependency, ...]:
        """Material, undischarged dependencies anywhere in the graph."""
        wanted = tuple(impacts)
        return tuple(
            dependency
            for dependency in self.all_dependencies()
            if dependency.is_material_to(wanted) and not dependency.is_resolved
        )

    def fingerprint_values(self) -> dict[str, object]:
        """Deterministic regardless of how the graph was assembled."""
        values: dict[str, object] = {}
        for dependency_set in self.sets.values():
            values.update(dependency_set.fingerprint_values())
        values["dependency_graph.cycles"] = tuple("->".join(c) for c in self.cycles())
        return dict(sorted(values.items()))

    def describe(self) -> str:
        lines = [f"{len(self.sets)} governing component(s)"]
        for parent, targets in self.adjacency().items():
            lines.append(f"  {parent} -> {', '.join(targets) or '(nothing)'}")
        for cycle in self.cycles():
            lines.append(f"  cycle: {' -> '.join((*cycle, cycle[0]))}")
        return "\n".join(lines)
