"""Whether an incorporated reference matters *to a particular claim*.

Two questions that are not the same
-----------------------------------
:mod:`predarb.semantics.reference_resolution` answers "does citation X point
unambiguously at source Y?". This module answers "does source Y matter to claim
C?". A reference can be perfectly resolved and irrelevant, or material and
unidentifiable, and the two states must never collapse into one another.

Why this exists
---------------
The Exchange Rulebook cites twenty-nine federal regulations. Not one of them
defines a payout, a Settlement Value, an Expiration Value, a void, or an outcome
review; they govern registration, minimum capital, recordkeeping, disciplinary
notice and self-certification procedure. Treating them as part of the proof that
YES + NO equals the notional blocked every market on the exchange for reasons
that had nothing to do with what any contract pays.

But the fix is emphatically **not** "federal regulations are procedural". A
future Part 38 or Part 40 rule about contract settlement would be squarely
material, and a domain-level whitelist would wave it through. So classification
is per claim, per citation, and per citing context.

The three guards
----------------
1. **Only a human-reviewed declaration can downgrade.** Nothing automated
   produces ``PROCEDURAL_FOR_CLAIM``. A declaration names its reviewer, the
   authority they read, and when.
2. **Context must match.** A declaration for "40.2 cited as the authority for
   filing a self-certification" does not apply to 40.2 cited in a paragraph
   about payouts. Payout language in the citing sentence vetoes the match
   outright, whatever else lines up.
3. **A declared payout impact cannot be downgraded at all.** If the scanner
   found the citing sentence talking about payouts, no declaration can quiet it.

Revocation and versioning
-------------------------
A declaration carries a policy version and can be revoked. Both participate in
the dependency fingerprint, so tightening the policy or withdrawing a
declaration is visible drift that puts affected evidence back to incomplete --
rather than a silent change in what a certificate meant.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Any

from predarb.clock import ensure_utc
from predarb.semantics.dependency import (
    DependencyMateriality,
    GoverningDocumentDependency,
    reference_slug,
)

__all__ = [
    "PAYOUT_VETO_PHRASES",
    "CitationScope",
    "ContextScope",
    "DependencyMaterialityPolicy",
    "MaterialityClass",
    "MaterialityDecision",
    "MaterialityDeclaration",
    "ParentContext",
]


class ParentContext(StrEnum):
    """What kind of document a citation appears in.

    Coarse on purpose: it exists so a declaration can say "in a certification
    cover letter" without having to name every product's document.
    """

    PRODUCT_CERTIFICATION_FILING = "PRODUCT_CERTIFICATION_FILING"
    """A CFTC self-certification letter -- the wrapper around Appendix A, not
    the contract terms themselves."""

    CONTRACT_TERMS = "CONTRACT_TERMS"
    """The binding terms and conditions."""

    EXCHANGE_RULEBOOK = "EXCHANGE_RULEBOOK"
    MEMBER_AGREEMENT = "MEMBER_AGREEMENT"
    MARKET_RULES_TEXT = "MARKET_RULES_TEXT"
    OTHER = "OTHER"


PAYOUT_VETO_PHRASES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bpayout", re.I),
    re.compile(r"\bsettlement value\b", re.I),
    re.compile(r"\bexpiration value\b", re.I),
    re.compile(r"\bpayout criterion\b", re.I),
    re.compile(r"\bmarket outcome\b", re.I),
    re.compile(r"\bvoid\b|\brefund\b|\bcancel", re.I),
    re.compile(r"\bfair allocation\b|\bproportion of the settlement\b", re.I),
    re.compile(r"\boutcome review\b", re.I),
    re.compile(r"\bnotional\b", re.I),
    re.compile(r"\bscalar contract\b", re.I),
)
"""Language that vetoes any procedural declaration, however well it matches.

This is the guard against a declaration written for one context quietly
covering a different one. If the sentence that does the incorporating talks
about what the contract pays, the reference is not procedural here, and no
amount of citation matching makes it so.
"""


class MaterialityClass(StrEnum):
    """A reference's standing *relative to one claim*."""

    MATERIAL = "MATERIAL"
    PROCEDURAL_FOR_CLAIM = "PROCEDURAL_FOR_CLAIM"
    UNCLASSIFIED = "UNCLASSIFIED"

    @property
    def blocks(self) -> bool:
        """Unclassified blocks exactly as material does. Only an explicit,
        context-matched human declaration clears a reference."""
        return self is not MaterialityClass.PROCEDURAL_FOR_CLAIM


@dataclass(frozen=True, slots=True)
class CitationScope:
    """Which citations a declaration covers, with explicit bounds.

    A family is a list of exact members, never a prefix. "Everything starting
    40." would silently absorb a future 40.x about settlement; naming the
    members means a new regulation arrives unclassified, which is the whole
    point of failing closed.
    """

    source_name: str
    citations: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.citations:
            raise ValueError(
                f"{self.source_name}: a citation scope must name at least one citation; "
                "an empty scope would match everything"
            )
        for citation in self.citations:
            if "*" in citation or citation.endswith("."):
                raise ValueError(
                    f"{self.source_name}: {citation!r} looks like a prefix. Name the "
                    "exact citations covered, so a future regulation in the same part "
                    "arrives unclassified instead of pre-approved"
                )
        object.__setattr__(
            self, "citations", tuple(sorted({reference_slug(c) for c in self.citations}))
        )

    def covers(self, dependency: GoverningDocumentDependency) -> bool:
        return (
            dependency.source_name == self.source_name
            and reference_slug(dependency.reference) in self.citations
        )


@dataclass(frozen=True, slots=True)
class ContextScope:
    """Which citing contexts a declaration covers."""

    parents: tuple[ParentContext, ...]
    required_phrases: tuple[str, ...] = ()
    """Phrases the citing sentence must **all** contain. Empty means the parent
    kind alone is enough -- acceptable only where the parent kind is itself the
    context, as with a certification cover letter."""

    any_of_phrases: tuple[str, ...] = ()
    """At least one of these must appear in the citing sentence.

    For a declaration that turns on subject matter rather than document kind:
    a Rulebook rule cited *for fees* is a different question from the same rule
    cited for anything else, and the citing sentence is where that shows."""

    def matches(self, *, parent: ParentContext, citing_context: str | None) -> bool:
        if parent not in self.parents:
            return False
        haystack = (citing_context or "").lower()
        if self.required_phrases and not all(
            phrase.lower() in haystack for phrase in self.required_phrases
        ):
            return False
        return not self.any_of_phrases or any(
            phrase.lower() in haystack for phrase in self.any_of_phrases
        )


@dataclass(frozen=True, slots=True)
class MaterialityDeclaration:
    """A human's reviewed judgement that a reference cannot reach a claim.

    Reusable across markets, because the same federal regulation cited the same
    way in every product certification is one question, not one question per
    market. Reusable *only* where the citation and the context both match.
    """

    claim: str
    citation_scope: CitationScope
    context_scope: ContextScope
    classification: MaterialityClass
    rationale: str
    authority: str
    """The source the reviewer actually read -- a regulation title and where it
    came from, not a recollection."""

    reviewer: str
    reviewed_at: datetime
    policy_version: str
    source_hashes: Mapping[str, str] = None  # type: ignore[assignment]
    revoked_at: datetime | None = None
    revocation_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "reviewed_at", ensure_utc(self.reviewed_at))
        if self.revoked_at is not None:
            object.__setattr__(self, "revoked_at", ensure_utc(self.revoked_at))
        object.__setattr__(self, "source_hashes", dict(sorted((self.source_hashes or {}).items())))
        if self.classification is MaterialityClass.PROCEDURAL_FOR_CLAIM:
            for field_name in ("rationale", "authority", "reviewer"):
                if not (getattr(self, field_name) or "").strip():
                    raise ValueError(
                        f"{self.declaration_id}: a procedural declaration must record its "
                        f"{field_name}; an unexplained downgrade is indistinguishable from "
                        "an oversight"
                    )

    @property
    def declaration_id(self) -> str:
        return (
            f"{self.claim}:{self.citation_scope.source_name}:"
            f"{'+'.join(self.citation_scope.citations)}@{self.policy_version}"
        )

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None

    def applies_to(
        self,
        dependency: GoverningDocumentDependency,
        *,
        claim: str,
        parent: ParentContext,
    ) -> bool:
        """Whether this declaration speaks to this citation, here, for this claim."""
        if not self.is_active or claim != self.claim:
            return False
        if not self.citation_scope.covers(dependency):
            return False
        if not self.context_scope.matches(
            parent=parent, citing_context=dependency.citation_context
        ):
            return False
        if dependency.payout_impacts:
            # The scanner read the citing sentence and found payout language.
            # A declaration cannot quiet that.
            return False
        return not any(
            veto.search(dependency.citation_context or "") for veto in PAYOUT_VETO_PHRASES
        )

    def revoked(self, *, at: datetime, reason: str) -> MaterialityDeclaration:
        return replace(self, revoked_at=ensure_utc(at), revocation_reason=reason)

    def payload(self) -> dict[str, Any]:
        return {
            "claim": self.claim,
            "source_name": self.citation_scope.source_name,
            "citations": list(self.citation_scope.citations),
            "parents": [p.value for p in self.context_scope.parents],
            "required_phrases": list(self.context_scope.required_phrases),
            "any_of_phrases": list(self.context_scope.any_of_phrases),
            "classification": self.classification.value,
            "rationale": self.rationale,
            "authority": self.authority,
            "reviewer": self.reviewer,
            "reviewed_at": self.reviewed_at.isoformat(),
            "policy_version": self.policy_version,
            "source_hashes": dict(self.source_hashes),
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
            "revocation_reason": self.revocation_reason,
        }


@dataclass(frozen=True, slots=True)
class MaterialityDecision:
    """The classification applied to one dependency, and what produced it."""

    classification: MaterialityClass
    declaration_id: str | None = None
    policy_version: str | None = None
    rationale: str | None = None

    @property
    def blocks(self) -> bool:
        return self.classification.blocks


@dataclass(frozen=True, slots=True)
class DependencyMaterialityPolicy:
    """Claim-scoped materiality, from a versioned set of human declarations."""

    version: str
    declarations: tuple[MaterialityDeclaration, ...] = ()

    def active(self) -> tuple[MaterialityDeclaration, ...]:
        return tuple(d for d in self.declarations if d.is_active)

    def classify(
        self,
        dependency: GoverningDocumentDependency,
        *,
        claim: str,
        parent: ParentContext,
    ) -> MaterialityDecision:
        """Classify one dependency for one claim in one context.

        Declarations are consulted in a fixed order -- sorted by their own id --
        so two policies holding the same declarations in different order reach
        the same decision.
        """
        if dependency.payout_impacts:
            return MaterialityDecision(
                MaterialityClass.MATERIAL,
                rationale="the citing sentence declares payout impacts",
            )
        for declaration in sorted(self.active(), key=lambda d: d.declaration_id):
            if declaration.applies_to(dependency, claim=claim, parent=parent):
                return MaterialityDecision(
                    classification=declaration.classification,
                    declaration_id=declaration.declaration_id,
                    policy_version=declaration.policy_version,
                    rationale=declaration.rationale,
                )
        if dependency.materiality is DependencyMateriality.MATERIAL:
            return MaterialityDecision(MaterialityClass.MATERIAL)
        return MaterialityDecision(MaterialityClass.UNCLASSIFIED)

    def apply(
        self,
        dependency: GoverningDocumentDependency,
        *,
        claim: str,
        parent: ParentContext,
    ) -> GoverningDocumentDependency:
        """Stamp a dependency with its claim-scoped classification.

        Applied at capture so the decision -- and the policy version behind it --
        lands in the evidence fingerprint. Revoking a declaration then shows up
        as drift rather than as a silent change in what a certificate meant.
        """
        decision = self.classify(dependency, claim=claim, parent=parent)
        materiality = dependency.materiality
        if decision.classification is MaterialityClass.PROCEDURAL_FOR_CLAIM:
            materiality = DependencyMateriality.PROCEDURAL
        return replace(
            dependency,
            materiality=materiality,
            rationale=decision.rationale or dependency.rationale,
            materiality_declaration_id=decision.declaration_id,
            materiality_policy_version=decision.policy_version or self.version,
            dependencies=tuple(
                self.apply(nested, claim=claim, parent=parent) for nested in dependency.dependencies
            ),
        )

    def extended(
        self, declarations: Iterable[MaterialityDeclaration]
    ) -> DependencyMaterialityPolicy:
        return DependencyMaterialityPolicy(
            version=self.version, declarations=(*self.declarations, *declarations)
        )

    def without(
        self, declaration_id: str, *, at: datetime, reason: str
    ) -> DependencyMaterialityPolicy:
        """Revoke one declaration, keeping it on the record as revoked."""
        return DependencyMaterialityPolicy(
            version=self.version,
            declarations=tuple(
                d.revoked(at=at, reason=reason) if d.declaration_id == declaration_id else d
                for d in self.declarations
            ),
        )

    def describe(self) -> str:
        lines = [f"materiality policy {self.version}: {len(self.active())} active declaration(s)"]
        for declaration in sorted(self.active(), key=lambda d: d.declaration_id):
            lines.append(
                f"  {declaration.classification.value} "
                f"{declaration.citation_scope.source_name} "
                f"{', '.join(declaration.citation_scope.citations)} "
                f"in {', '.join(p.value for p in declaration.context_scope.parents)}"
            )
        return "\n".join(lines)


def declarations_from(records: Sequence[Mapping[str, Any]]) -> tuple[MaterialityDeclaration, ...]:
    return tuple(
        MaterialityDeclaration(
            claim=str(r["claim"]),
            citation_scope=CitationScope(
                source_name=str(r["source_name"]), citations=tuple(r["citations"])
            ),
            context_scope=ContextScope(
                parents=tuple(ParentContext(p) for p in r["parents"]),
                required_phrases=tuple(r.get("required_phrases", ())),
                any_of_phrases=tuple(r.get("any_of_phrases", ())),
            ),
            classification=MaterialityClass(r["classification"]),
            rationale=str(r["rationale"]),
            authority=str(r["authority"]),
            reviewer=str(r["reviewer"]),
            reviewed_at=datetime.fromisoformat(str(r["reviewed_at"])),
            policy_version=str(r["policy_version"]),
            source_hashes=dict(r.get("source_hashes", {})),
            revoked_at=(
                datetime.fromisoformat(str(r["revoked_at"])) if r.get("revoked_at") else None
            ),
            revocation_reason=r.get("revocation_reason"),
        )
        for r in records
    )
