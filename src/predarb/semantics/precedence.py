"""Which settlement rule controls when a product cites one and the Rulebook another.

The question
------------
A modern Kalshi Binary Contract says, in its own terms: "If an Expiration Value
cannot be determined on the Expiration Date, Kalshi has the right to determine
payouts pursuant to Rule 7.1 in the Rulebook." Rule 7.1 contains no payout
methodology at all -- it provides that the Outcome Review Committee "will
determine the final Market Outcome". Meanwhile Rule 6.3(c) has its own trigger
for exactly this situation and routes to last-traded-price or a committee "fair
allocation".

So either:

**Reading A** -- the product rule controls, the Committee returns a Market
Outcome, which for a Binary Contract is YES or NO, and Rule 6.3(a) then pays the
whole Settlement Value to one side. Strictly two-state, and complementary.

**Reading B** -- Rule 6.3(c) fires on its own trigger regardless of what the
product cites, the fractional paths stay open, and neither strict two-state nor
complementarity is proven.

The difference decides whether a `STANDARD_BINARY_COMPLEMENT` certificate is
reachable on this venue at all, so it is worth adjudicating carefully and worth
refusing to adjudicate if the evidence will not bear it.

Three findings, tracked apart
-----------------------------
It is tempting to collapse these, and collapsing them is how one reading wins by
assumption:

``committee_output_is_binary``   does Rule 7.1 necessarily yield YES or NO?
``product_rule_controls``        does the product's citation displace 6.3(c)?
``general_rule_also_reachable``  does 6.3(c) fire anyway?

The first can be well supported while the second is open. That is in fact the
position the evidence leaves us in.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

__all__ = [
    "ControlFinding",
    "PrecedenceStatus",
    "SettlementPrecedenceProof",
    "SupportingAuthority",
]


class ControlFinding(StrEnum):
    """Whether a product-specific settlement rule displaces the general one."""

    PRODUCT_RULE_CONTROLS = "PRODUCT_RULE_CONTROLS"
    PRODUCT_RULE_DOES_NOT_CONTROL = "PRODUCT_RULE_DOES_NOT_CONTROL"
    UNRESOLVED = "UNRESOLVED"

    @property
    def is_settled(self) -> bool:
        return self is not ControlFinding.UNRESOLVED


class PrecedenceStatus(StrEnum):
    """The adjudication result."""

    RULE7_BINARY_PATH_PROVEN_EXCLUSIVE = "RULE7_BINARY_PATH_PROVEN_EXCLUSIVE"
    """Rule 7.1 controls, its output is binary, and 6.3(c) is not reachable."""

    RULE7_BINARY_PATH_PROVEN_BUT_63C_ALSO_REACHABLE = (
        "RULE7_BINARY_PATH_PROVEN_BUT_63C_ALSO_REACHABLE"
    )
    """Rule 7.1's output is binary, but the general fallback fires too."""

    RULE63C_PREVAILS = "RULE63C_PREVAILS"
    """The general fallback controls and the product citation does not displace it."""

    PRECEDENCE_UNRESOLVED = "PRECEDENCE_UNRESOLVED"
    """Authoritative evidence does not settle it. Blocks, and is the default."""

    @property
    def permits_strict_two_state(self) -> bool:
        return self is PrecedenceStatus.RULE7_BINARY_PATH_PROVEN_EXCLUSIVE


@dataclass(frozen=True, slots=True)
class SupportingAuthority:
    """One piece of governing text bearing on the question, and which way it cuts."""

    source: str
    reference: str
    quoted_text: str
    supports: str
    """``"A"``, ``"B"``, or ``"neither"`` -- which reading this text favours."""

    document_sha256: str | None = None
    reasoning: str = ""

    def __post_init__(self) -> None:
        if self.supports not in {"A", "B", "neither"}:
            raise ValueError(
                f"{self.reference}: `supports` must be 'A', 'B' or 'neither', got {self.supports!r}"
            )
        if not (self.quoted_text or "").strip():
            raise ValueError(f"{self.reference}: an authority must quote its text")

    def payload(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "reference": self.reference,
            "quoted_text": self.quoted_text,
            "supports": self.supports,
            "document_sha256": self.document_sha256,
            "reasoning": self.reasoning,
        }


@dataclass(frozen=True, slots=True)
class SettlementPrecedenceProof:
    """The adjudication record for one product's contingency clause."""

    subject: str
    product_terms_sha256: str
    rulebook_version: str
    rulebook_sha256: str
    product_contingency_text: str
    referenced_rule: str
    general_fallback_rule: str
    market_outcome_definition: str
    committee_output_semantics: str

    authorities: tuple[SupportingAuthority, ...]
    committee_output_is_binary: bool
    product_rule_controls: ControlFinding
    general_rule_also_reachable: bool | None
    """``None`` means not established either way -- not 'false'."""

    guidelines_located: bool = False
    guidelines_note: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def status(self) -> PrecedenceStatus:
        """The verdict, failing closed on anything unsettled.

        Exclusivity is the only status that unlocks a strict two-state claim, and
        it demands all three findings: a binary committee output, a product rule
        that controls, and a general fallback shown *not* to be reachable.
        Anything short of that is reported as what it is.
        """
        if not self.product_rule_controls.is_settled:
            return PrecedenceStatus.PRECEDENCE_UNRESOLVED
        if self.product_rule_controls is ControlFinding.PRODUCT_RULE_DOES_NOT_CONTROL:
            return PrecedenceStatus.RULE63C_PREVAILS
        if not self.committee_output_is_binary:
            return PrecedenceStatus.PRECEDENCE_UNRESOLVED
        if self.general_rule_also_reachable is None:
            return PrecedenceStatus.PRECEDENCE_UNRESOLVED
        if self.general_rule_also_reachable:
            return PrecedenceStatus.RULE7_BINARY_PATH_PROVEN_BUT_63C_ALSO_REACHABLE
        return PrecedenceStatus.RULE7_BINARY_PATH_PROVEN_EXCLUSIVE

    @property
    def unlocks_strict_two_state(self) -> bool:
        return self.status.permits_strict_two_state

    def authorities_for(self, reading: str) -> tuple[SupportingAuthority, ...]:
        return tuple(a for a in self.authorities if a.supports == reading)

    def source_digest(self) -> str:
        payload = (
            f"{self.product_terms_sha256}|{self.rulebook_version}|{self.rulebook_sha256}|"
            + "|".join(
                f"{a.reference}:{a.document_sha256}"
                for a in sorted(self.authorities, key=lambda a: a.reference)
            )
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:32]

    def still_applies_to(
        self, *, product_terms_sha256: str, rulebook_sha256: str
    ) -> tuple[bool, tuple[str, ...]]:
        reasons: list[str] = []
        if product_terms_sha256 != self.product_terms_sha256:
            reasons.append("product terms content hash changed")
        if rulebook_sha256 != self.rulebook_sha256:
            reasons.append("Rulebook content hash changed")
        return (not reasons, tuple(reasons))

    def payload(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "status": self.status.value,
            "product_terms_sha256": self.product_terms_sha256,
            "rulebook_version": self.rulebook_version,
            "rulebook_sha256": self.rulebook_sha256,
            "product_contingency_text": self.product_contingency_text,
            "referenced_rule": self.referenced_rule,
            "general_fallback_rule": self.general_fallback_rule,
            "market_outcome_definition": self.market_outcome_definition,
            "committee_output_semantics": self.committee_output_semantics,
            "committee_output_is_binary": self.committee_output_is_binary,
            "product_rule_controls": self.product_rule_controls.value,
            "general_rule_also_reachable": self.general_rule_also_reachable,
            "guidelines_located": self.guidelines_located,
            "guidelines_note": self.guidelines_note,
            "authorities": [a.payload() for a in self.authorities],
            "source_digest": self.source_digest(),
            "notes": list(self.notes),
        }

    def describe(self) -> str:
        lines = [
            f"{self.subject}: {self.status.value}",
            f"  committee output binary : {self.committee_output_is_binary}",
            f"  product rule controls   : {self.product_rule_controls.value}",
            f"  6.3(c) also reachable   : {self.general_rule_also_reachable}",
            f"  guidelines located      : {self.guidelines_located}",
            f"  authorities for A       : {len(self.authorities_for('A'))}",
            f"  authorities for B       : {len(self.authorities_for('B'))}",
            f"  unlocks strict two-state: {self.unlocks_strict_two_state}",
        ]
        return "\n".join(lines)


def proofs_by_subject(
    proofs: Iterable[SettlementPrecedenceProof],
) -> dict[str, SettlementPrecedenceProof]:
    merged: dict[str, SettlementPrecedenceProof] = {}
    for proof in proofs:
        if proof.subject in merged:
            raise ValueError(f"two precedence proofs for {proof.subject!r}")
        merged[proof.subject] = proof
    return dict(sorted(merged.items()))
