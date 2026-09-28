"""What evidence each certificate claim requires, and what it may ignore.

There is deliberately **no universal evidence list**. Different claims need
different proof: a two-state complement needs the notional and every terminal
outcome enumerated; a future exhaustiveness claim over an event group will need
the full market list, which is irrelevant here. One global list would either
over-collect (blocking approvals on evidence the claim does not need) or
under-collect (approving on evidence that does not establish the claim).

Three categories
----------------
``REQUIRED``  absent or unretrievable => ``EVIDENCE_INCOMPLETE``, approval blocked
``OPTIONAL``  captured and fingerprinted when present; absence is not a failure
``IGNORED``   never captured, never fingerprinted -- volatile or irrelevant

``IGNORED`` is explicit rather than implied by omission so that a field's
exclusion is a recorded decision someone can argue with, not an oversight.

Documents are conditional, not optional
---------------------------------------
A governing contract document is a third thing again. Most Kalshi series publish
no ``contract_url`` or ``contract_terms_url`` at all, and requiring one would
make those markets permanently un-approvable -- a statement about the venue
rather than about the claim. But once the venue *does* reference a governing
document, that document governs, and a review conducted without it is not a
review of the contract.

So the requirement is conditional on the URL existing:

``ABSENT``                        no URL published; absence is not a failure
``PRESENT_BUT_OPTIONAL``          URL published, but not needed for this claim
``PRESENT_AND_REQUIRED``          URL published => its content must be retrieved

The failure mode this closes: "the field is globally OPTIONAL" quietly becoming
"the document may fail to load and the certificate can still be issued".

Incorporated documents govern too
---------------------------------
A governing document is a node, not a leaf. When contract terms say payouts may
be determined "pursuant to Rule 6.3(b) in the Rulebook", the rule that decides
those payouts lives in a document we have not fetched, and a review conducted
without it has not read the contract.

So completeness runs over the *closure*, not the document set:

* a parent whose incorporated references could not be enumerated -- because we
  could not read it -- is **not** complete. An unreadable PDF has an unknown
  dependency set, never an empty one.
* an incorporated reference that can reach a payout, whose source we do not
  hold, is missing required evidence.
* an incorporated reference classified ``PROCEDURAL`` by a human, with a
  recorded rationale, does not block. Unclassified ones do.

Why required-but-absent must block
----------------------------------
A reviewer shown a bundle with a missing contract document has no way to answer
"can this market void?". Letting them approve anyway would produce a certificate
that looks identical to a well-founded one. So incompleteness is computed, not
left to the reviewer's attention.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from predarb.semantics.dependency import (
    DependencyGraph,
    DependencySet,
    GoverningDocumentDependency,
    PayoutImpact,
)
from predarb.semantics.evidence import (
    EvidenceCompleteness,
    SettlementEvidenceBundle,
)
from predarb.semantics.fingerprint import Absent

__all__ = [
    "STANDARD_BINARY_COMPLEMENT_POLICY",
    "CertificateClaim",
    "CompletenessReport",
    "DocumentRequirement",
    "EvidencePolicy",
    "Requirement",
    "policy_for",
]


class CertificateClaim(StrEnum):
    """The specific proposition a certificate asserts.

    Narrow on purpose. ``STANDARD_BINARY_COMPLEMENT`` asserts only what the
    Step 7 detector needs: for every permitted terminal settlement state, YES
    payout + NO payout equals the explicit notional.

    It does **not** assert that the event is fair, that the market is correctly
    priced, that the outcome is likely, that the wording is good, that another
    market is equivalent, or that an event group is exhaustive.
    """

    STANDARD_BINARY_COMPLEMENT = "STANDARD_BINARY_COMPLEMENT"

    @property
    def proposition(self) -> str:
        return {
            CertificateClaim.STANDARD_BINARY_COMPLEMENT: (
                "For every permitted terminal settlement state of this market, "
                "YES payout + NO payout equals the explicit notional."
            )
        }[self]


class Requirement(StrEnum):
    REQUIRED = "REQUIRED"
    OPTIONAL = "OPTIONAL"
    IGNORED = "IGNORED"


class DocumentRequirement(StrEnum):
    """How a governing document bears on a claim, given whether it exists."""

    ABSENT = "ABSENT"
    """No URL was published. Nothing governs, so nothing is missing."""

    PRESENT_BUT_OPTIONAL = "PRESENT_BUT_OPTIONAL"
    """A URL exists but this claim does not depend on its content."""

    PRESENT_AND_REQUIRED = "PRESENT_AND_REQUIRED"
    """A URL exists and governs the claim. Its content must be retrieved, and a
    failed fetch is incomplete evidence -- not an absent document."""


@dataclass(frozen=True, slots=True)
class CompletenessReport:
    """Whether a bundle satisfies a policy, and precisely what is missing."""

    claim: CertificateClaim
    completeness: EvidenceCompleteness
    missing_required: tuple[str, ...]
    unretrievable_documents: tuple[str, ...]
    manual_viewing_required: Mapping[str, str]
    """Document name -> the exact content hash a reviewer must acknowledge.

    Bound to the hash, not just the name: an acknowledgement of version A must
    not satisfy version B. A contract that was amended between review and
    issuance has not been read."""

    present_optional: tuple[str, ...]
    document_requirements: Mapping[str, str] = field(default_factory=dict)
    """Per-document verdict, for audit: why each was or was not required."""

    unknown_dependency_closures: tuple[str, ...] = ()
    """Governing components we could not read well enough to list what they
    incorporate. Blocking: an unreadable document may incorporate anything."""

    unresolved_dependencies: tuple[str, ...] = ()
    """Incorporated references that can reach the claim and are not discharged,
    for whichever of the two reasons below."""

    unheld_dependency_sources: tuple[str, ...] = ()
    """Material references whose document we do not hold. Remedy: fetch it."""

    unresolved_citations: tuple[str, ...] = ()
    """Material references we hold, whose citation does not clearly identify a
    provision. Remedy: authoritative evidence of what the citation points at --
    never a guess from prose similarity."""

    dependency_closures: Mapping[str, str] = field(default_factory=dict)
    """Per-parent closure verdict, for audit."""

    dependency_cycles: tuple[str, ...] = ()
    """Cycles in the incorporation graph, each as ``a -> b -> a``.

    Reported, not treated as an error. The Member Agreement incorporates the
    Rulebook and the Rulebook incorporates the Member Agreement; that is how
    the documents are written. A cycle is only a problem when a material edge
    inside it is undischarged, and that blocks through the ordinary rule."""

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "manual_viewing_required", dict(sorted(self.manual_viewing_required.items()))
        )
        object.__setattr__(
            self, "document_requirements", dict(sorted(self.document_requirements.items()))
        )
        object.__setattr__(
            self, "dependency_closures", dict(sorted(self.dependency_closures.items()))
        )

    @property
    def permits_approval(self) -> bool:
        return self.completeness.permits_approval

    @property
    def documents_requiring_manual_viewing(self) -> tuple[str, ...]:
        return tuple(self.manual_viewing_required)

    def describe(self) -> str:
        if self.permits_approval:
            note = ""
            if self.manual_viewing_required:
                note = f"; manual viewing required for {', '.join(self.manual_viewing_required)}"
            return f"COMPLETE for {self.claim.value}{note}"
        parts = []
        if self.missing_required:
            parts.append(f"missing: {', '.join(self.missing_required)}")
        if self.unretrievable_documents:
            parts.append(
                "governing document referenced but not retrieved: "
                f"{', '.join(self.unretrievable_documents)}"
            )
        if self.unknown_dependency_closures:
            parts.append(
                "cannot enumerate what these governing documents incorporate: "
                f"{', '.join(self.unknown_dependency_closures)}"
            )
        if self.unheld_dependency_sources:
            parts.append(
                "incorporated rule governs the payout but its source was not retrieved: "
                f"{', '.join(self.unheld_dependency_sources)}"
            )
        if self.unresolved_citations:
            parts.append(
                "incorporated citation does not resolve to a known provision: "
                f"{', '.join(self.unresolved_citations)}"
            )
        return f"EVIDENCE_INCOMPLETE for {self.claim.value} ({'; '.join(parts)})"


@dataclass(frozen=True, slots=True)
class EvidencePolicy:
    """The evidence contract for one claim."""

    claim: CertificateClaim
    schema_version: str
    required: tuple[str, ...]
    optional: tuple[str, ...]
    ignored: tuple[str, ...]
    conditionally_required_documents: tuple[str, ...]
    """Documents that become required **once the venue publishes a URL** for
    them. Absence of the URL is fine; a published URL we could not read is not."""

    optional_documents: tuple[str, ...]
    rationale: Mapping[str, str]

    material_payout_impacts: frozenset[PayoutImpact] = frozenset()
    """Which kinds of incorporated rule this claim cannot proceed without having
    read. Declared per claim rather than globally: a claim about event
    exhaustiveness is not endangered by the same rules as a claim about a
    two-sided payout sum."""

    dependency_bearing_components: tuple[str, ...] = ()
    """Components whose own text can incorporate another governing document, and
    whose incorporation closure therefore has to be established. Documents that
    are ``PRESENT_AND_REQUIRED`` are always included; this names the non-document
    components (rules text) that govern in the same way."""

    def __post_init__(self) -> None:
        overlap = (set(self.required) & set(self.optional)) | (
            set(self.required) & set(self.ignored)
        )
        if overlap:
            raise ValueError(f"{self.claim.value}: components in two categories: {sorted(overlap)}")

    def requirement_for(self, component: str) -> Requirement:
        if component in self.required:
            return Requirement.REQUIRED
        if component in self.optional:
            return Requirement.OPTIONAL
        return Requirement.IGNORED

    def document_requirement(
        self, name: str, bundle: SettlementEvidenceBundle
    ) -> DocumentRequirement:
        """Whether this document is required *for this bundle*.

        Conditional on the venue having published a URL for it. A document with
        no URL governs nothing; one with a URL governs whether or not we managed
        to read it.
        """
        document = bundle.documents.get(name)
        published = document is not None and document.url is not None
        if not published:
            return DocumentRequirement.ABSENT
        if name in self.conditionally_required_documents:
            return DocumentRequirement.PRESENT_AND_REQUIRED
        return DocumentRequirement.PRESENT_BUT_OPTIONAL

    def dependency_set_for(self, parent: str, bundle: SettlementEvidenceBundle) -> DependencySet:
        """The recorded closure for ``parent``, or the honest default.

        The default is the load-bearing part. A bundle captured before
        incorporation was modelled records nothing, and reading that silence as
        "incorporates nothing" is precisely the error that let two markets be
        marked EVIDENCE COMPLETE while their terms handed the payout rule to an
        unfetched rulebook. So silence derives from the parent's own
        readability, which for an unreadable PDF is ``UNKNOWN``.
        """
        recorded = bundle.dependencies.get(parent)
        if recorded is not None:
            return recorded
        if parent in bundle.documents:
            return DependencySet.for_document(parent, bundle.documents[parent])
        value = bundle.component_values().get(parent)
        if value is None or isinstance(value, Absent):
            return DependencySet.not_applicable(parent, "component carries no text")
        return DependencySet.unknown(
            parent,
            "no incorporation scan was recorded for this component, so what it "
            "incorporates is unknown rather than nothing",
        )

    def dependency_parents(self, bundle: SettlementEvidenceBundle) -> tuple[str, ...]:
        """Every component whose incorporation closure this claim depends on."""
        documents = {
            name
            for name in set(self.conditionally_required_documents) | set(bundle.documents)
            if self.document_requirement(name, bundle) is DocumentRequirement.PRESENT_AND_REQUIRED
        }
        return tuple(sorted(documents | set(self.dependency_bearing_components)))

    def blocking_dependencies(
        self, bundle: SettlementEvidenceBundle
    ) -> tuple[GoverningDocumentDependency, ...]:
        """Incorporated references this claim needs and does not hold."""
        return tuple(
            dependency
            for parent in self.dependency_parents(bundle)
            for dependency in self.dependency_set_for(parent, bundle).blocking(
                self.material_payout_impacts
            )
        )

    def assess(self, bundle: SettlementEvidenceBundle) -> CompletenessReport:
        """Decide whether this bundle can support the claim at all."""
        values = bundle.component_values()
        missing = tuple(
            name
            for name in self.required
            if name not in values or isinstance(values[name], Absent) or values[name] is None
        )

        requirements: dict[str, str] = {}
        unretrievable: list[str] = []
        manual: dict[str, str] = {}
        for name in sorted(set(self.conditionally_required_documents) | set(bundle.documents)):
            requirement = self.document_requirement(name, bundle)
            requirements[name] = requirement.value
            document = bundle.documents.get(name)
            if requirement is not DocumentRequirement.PRESENT_AND_REQUIRED:
                continue
            if document is None or not document.retrieval.is_usable:
                # A governing document we could not read is missing evidence,
                # not an absent document.
                unretrievable.append(name)
                continue
            if document.requires_manual_viewing and document.content_sha256:
                manual[name] = document.content_sha256

        closures: dict[str, str] = {}
        unknown_closures: list[str] = []
        unresolved: list[str] = []
        unheld: list[str] = []
        unresolved_citations: list[str] = []
        dependency_parents = self.dependency_parents(bundle)
        for parent in dependency_parents:
            dependency_set = self.dependency_set_for(parent, bundle)
            closures[parent] = dependency_set.closure.value
            if not dependency_set.closure.is_established:
                unknown_closures.append(parent)
            impacts = self.material_payout_impacts
            unresolved.extend(d.key for d in dependency_set.blocking(impacts))
            unheld.extend(d.key for d in dependency_set.unheld_sources(impacts))
            unresolved_citations.extend(d.key for d in dependency_set.unresolved_citations(impacts))
            # Keyed as "dependency:<parent>/<source>/<ref>", so acknowledging the
            # terms can never stand in for acknowledging the rulebook they
            # incorporate.
            manual.update(dependency_set.manual_viewing(self.material_payout_impacts))

        graph = DependencyGraph(
            {parent: self.dependency_set_for(parent, bundle) for parent in dependency_parents}
        )

        complete = (
            EvidenceCompleteness.COMPLETE
            if not missing and not unretrievable and not unknown_closures and not unresolved
            else EvidenceCompleteness.EVIDENCE_INCOMPLETE
        )
        return CompletenessReport(
            claim=self.claim,
            completeness=complete,
            missing_required=missing,
            unretrievable_documents=tuple(unretrievable),
            manual_viewing_required=manual,
            present_optional=tuple(
                name
                for name in self.optional
                if name in values and not isinstance(values[name], Absent)
            ),
            document_requirements=requirements,
            unknown_dependency_closures=tuple(unknown_closures),
            unresolved_dependencies=tuple(dict.fromkeys(unresolved)),
            unheld_dependency_sources=tuple(dict.fromkeys(unheld)),
            unresolved_citations=tuple(dict.fromkeys(unresolved_citations)),
            dependency_closures=closures,
            dependency_cycles=tuple(" -> ".join((*cycle, cycle[0])) for cycle in graph.cycles()),
        )


_BINARY_RATIONALE: Final[dict[str, str]] = {
    "market.rules_primary": "states the resolution condition; the core of the claim",
    "market.rules_secondary": "commonly carries void, DNP and postponement clauses",
    "market.notional_value": "the claim is about summing to this exact number",
    "market.market_type": "recorded so a later type change is visible as drift",
    "market.settlement_kind": "our own normalisation of the above; drift is meaningful",
    "market.can_close_early": "an early close can change which states are reachable",
    "market.yes_sub_title": "defines what YES means; a reworded side is a different claim",
    "market.no_sub_title": "defines what NO means",
    "event.mutually_exclusive": "event structure can imply cross-market settlement rules",
    "event.collateral_return_type": "affects what is returned on settlement",
    "series.settlement_sources": "who decides the outcome; a changed source is a changed contract",
    "document.contract_terms.sha256": "the authoritative terms; hashed by content, not URL",
}

STANDARD_BINARY_COMPLEMENT_POLICY: Final = EvidencePolicy(
    claim=CertificateClaim.STANDARD_BINARY_COMPLEMENT,
    schema_version="policy/standard-binary-complement/1",
    required=(
        "market.rules_primary",
        "market.notional_value",
        "market.market_type",
        "market.settlement_kind",
        "market.yes_sub_title",
        "market.no_sub_title",
    ),
    optional=(
        "market.rules_secondary",
        "market.can_close_early",
        "market.settlement_timer_seconds",
        "market.expected_expiration_time",
        "market.latest_expiration_time",
        "market.custom_strike",
        "market.strike_type",
        "market.floor_strike",
        "market.cap_strike",
        "event.mutually_exclusive",
        "event.collateral_return_type",
        "event.title",
        "series.settlement_sources",
        "series.contract_url",
        "series.contract_terms_url",
    ),
    ignored=(
        "market.last_price",
        "market.yes_bid",
        "market.yes_ask",
        "market.no_bid",
        "market.no_ask",
        "market.volume",
        "market.open_interest",
        "market.liquidity",
    ),
    # Conditional, not optional. Most Kalshi series publish no governing
    # document, and requiring one would make those markets permanently
    # un-approvable. But once a URL is published, that document governs the
    # contract, and a review conducted without it is not a review.
    conditionally_required_documents=("contract_terms", "contract"),
    optional_documents=(),
    # Every way an incorporated rule can reach "YES + NO == notional". A rule
    # that can set payouts by last traded price, void the contract, split the
    # settlement value at a committee's discretion, or change which terminal
    # states exist is a rule this claim cannot be proved without reading.
    material_payout_impacts=frozenset(PayoutImpact),
    # Rules text governs in the same way a filed document does, and can cite the
    # rulebook just as the terms do.
    dependency_bearing_components=(
        "market.rules_primary",
        "market.rules_secondary",
        # Venue-level governing documents. The Member Agreement establishes how
        # the Rulebook binds, so a claim that relies on reading the Rulebook
        # relies on it too.
        "exchange_rulebook",
        "member_agreement",
    ),
    rationale=_BINARY_RATIONALE,
)

_POLICIES: Final[dict[CertificateClaim, EvidencePolicy]] = {
    CertificateClaim.STANDARD_BINARY_COMPLEMENT: STANDARD_BINARY_COMPLEMENT_POLICY,
}


def policy_for(claim: CertificateClaim) -> EvidencePolicy:
    try:
        return _POLICIES[claim]
    except KeyError:
        raise ValueError(
            f"no evidence policy defined for claim {claim!r}; a claim without a "
            "policy has no defined proof obligation"
        ) from None
