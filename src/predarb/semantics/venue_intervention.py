"""Extraordinary venue intervention, tracked apart from settlement semantics.

Every CFTC-regulated venue reserves emergency authority: suspend trading
venue-wide, act on market disruption, modify or suspend its own rules under
extraordinary conditions. Phase 2D counted Rothera's Rule 1.11 as a settlement
mechanism and let it block a conservation proof. That was the wrong boundary,
and the reason is structural rather than convenient:

    A proof that no sovereign, regulator or exchange will ever intervene is not
    obtainable on any regulated venue. Requiring it makes contractual
    conservation unprovable everywhere, which means the requirement is not
    discriminating between venues -- it is refusing all of them for a property
    none of them can have.

That is categorically different from proving what a contract pays when it
resolves normally, which is a question about text and does have an answer.

So extraordinary authority leaves the semantic proof and becomes
:class:`ResidualVenueInterventionRisk`: recorded, carried on the proof, and
printed in every report. It is excluded from the proof, never from the
disclosure. A conclusion reached this way is
``CONTRACTUALLY_GUARANTEED_UNDER_NORMAL_GOVERNING_SETTLEMENT`` and is never
described as risk-free.

The exclusion is narrow, and this module enforces the narrowness
--------------------------------------------------------------
A settlement clause does not become extraordinary by being unlikely, or by
saying "sole discretion", or by living in a chapter with "Emergency" in its
title. :data:`IN_SCOPE_MECHANISMS` lists what stays inside the proof no matter
how it is labelled, and :func:`classify_intervention` refuses to exclude any of
it. The loophole this closes is real: Polymarket US Rule 2.8(d)(iii) is an
emergency-chapter power, while Rule 9.101(K) cancellation is a product clause
reachable on any rained-off game, and only the first may leave.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from predarb.semantics.settlement_census import SettlementMechanism

__all__ = [
    "IN_SCOPE_MECHANISMS",
    "ConservationScope",
    "InterventionScope",
    "ResidualVenueInterventionRisk",
    "classify_intervention",
]


class InterventionScope(StrEnum):
    """Whether a power belongs to the semantic proof or beside it."""

    ORDINARY_CONTRACT_RESOLUTION = "ORDINARY_CONTRACT_RESOLUTION"
    """Reachable in the normal life of a contract. Stays inside the proof."""

    EXTRAORDINARY_VENUE_INTERVENTION = "EXTRAORDINARY_VENUE_INTERVENTION"
    """Exchange-wide or regulatory action under extraordinary conditions.
    Leaves the proof and is disclosed as residual risk."""


class ConservationScope(StrEnum):
    """What a conservation conclusion is a conclusion about."""

    NORMAL_GOVERNING_SETTLEMENT = "CONTRACTUALLY_GUARANTEED_UNDER_NORMAL_GOVERNING_SETTLEMENT"
    """Proven over every ordinary settlement mechanism, with extraordinary venue
    intervention disclosed separately. The strongest claim this project makes,
    and deliberately not 'risk-free'."""

    NOT_ESTABLISHED = "NOT_ESTABLISHED"
    """Some ordinary mechanism is unresolved or missing. Blocks."""


IN_SCOPE_MECHANISMS: Final[frozenset[SettlementMechanism]] = frozenset(
    {
        SettlementMechanism.ORDINARY_BINARY,
        SettlementMechanism.LAST_FAIR_PRICE,
        SettlementMechanism.LAST_TRADED_PRICE,
        SettlementMechanism.FAIR_ALLOCATION,
        SettlementMechanism.FRACTIONAL_SHARE,
        SettlementMechanism.TIE_SPLIT,
        SettlementMechanism.CANCELLATION_LAST_RESULTS,
        SettlementMechanism.VOID_REFUND,
        SettlementMechanism.NATURAL_PERSON_SCALAR,
        SettlementMechanism.OUTCOME_REVIEW,
        SettlementMechanism.INDETERMINATE_FALLBACK,
        SettlementMechanism.OFFSET_NETTING,
    }
)
"""Mechanisms that stay inside the semantic proof whatever they are called.

This is the whole census, on purpose. Product cancellation, normal settlement
discretion, fair-market-price settlement, tie and push rules, source failure,
outcome review, scheduled contingencies and product-specific discretionary
valuation are all reachable in ordinary contract resolution, and a venue does
not get to reclassify one by filing it under an emergency heading.

Extraordinary authority is therefore recognised by what it *is* -- a venue-wide
or rule-level power -- and never by which settlement mechanism it resembles.
"""


@dataclass(frozen=True, slots=True)
class ResidualVenueInterventionRisk:
    """One extraordinary power, excluded from the proof and disclosed instead."""

    venue: str
    rule_reference: str
    quoted_text: str
    powers: tuple[str, ...]
    """What the authority permits, in the rule's own terms."""

    rationale: str
    """Why this is extraordinary rather than an ordinary settlement path."""

    approval_gate: str = ""
    """Any procedural constraint -- committee approval, regulator notice."""

    def __post_init__(self) -> None:
        if not (self.quoted_text or "").strip():
            raise ValueError(
                f"{self.rule_reference}: residual risk must quote the authority it "
                "excludes; an exclusion with no text is an assertion"
            )
        if not self.powers:
            raise ValueError(
                f"{self.rule_reference}: name the powers being excluded, so a reader "
                "can judge the exclusion rather than take it on trust"
            )

    def disclosure(self) -> str:
        """The paragraph a report must carry. Never omitted, never softened."""
        lines = [
            f"RESIDUAL_VENUE_INTERVENTION_RISK -- {self.venue}, {self.rule_reference}",
            f"  permits: {'; '.join(self.powers)}",
            f"  why excluded: {self.rationale}",
        ]
        if self.approval_gate:
            lines.append(f"  gated by: {self.approval_gate}")
        lines.append(
            "  This risk is disclosed, not eliminated. No conclusion resting on "
            "it may be described as risk-free."
        )
        return "\n".join(lines)


def classify_intervention(
    *,
    mechanism: SettlementMechanism | None,
    venue_wide: bool,
    reachable_in_ordinary_resolution: bool,
) -> InterventionScope:
    """Decide whether a power may leave the semantic proof.

    Three conditions, all required, and the first is the one that does the work:
    a power that matches any census mechanism is an ordinary settlement path
    however it is labelled. ``mechanism=None`` means the power is not a way of
    settling a contract at all -- it is authority over the venue or the rules --
    which is the only thing that can be extraordinary.
    """
    if mechanism is not None and mechanism in IN_SCOPE_MECHANISMS:
        return InterventionScope.ORDINARY_CONTRACT_RESOLUTION
    if reachable_in_ordinary_resolution:
        return InterventionScope.ORDINARY_CONTRACT_RESOLUTION
    if not venue_wide:
        return InterventionScope.ORDINARY_CONTRACT_RESOLUTION
    return InterventionScope.EXTRAORDINARY_VENUE_INTERVENTION
