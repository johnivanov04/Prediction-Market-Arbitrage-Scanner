"""The context a detector needs, resolved at one knowledge horizon.

The seam between "what did we know" and "what does the detector consume". A
venue adapter fills these in from recorded observations; the coordinator reads
only these types and never a venue schema.

Three outcomes, kept apart
--------------------------
``resolved``      we had it
``known_absent``  we checked an authoritative source and it held nothing
``missing``       we never checked, so we do not know

The middle one is the whole point. An explicitly empty certificate registry
settles the question -- there is no certificate, so the market is blocked -- and
that is a determinate decision. An *unchecked* registry settles nothing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from predarb.detectors.binary_complement import LegFeeQuoter
from predarb.domain.models import VenueInstrument
from predarb.opportunities.models import Classification
from predarb.replay.completeness import ReplayDataCompleteness
from predarb.replay.observation import KnowledgeHorizon
from predarb.replay.plan import BasketPlan, DetectorPlan
from predarb.semantics.certificate import SettlementCertificate
from predarb.semantics.fingerprint import SettlementEvidenceFingerprint
from predarb.semantics.relation import RelationCertificate

__all__ = [
    "BasketContext",
    "BinaryContext",
    "ContextProvider",
]


@dataclass(frozen=True, slots=True)
class BinaryContext:
    """Everything the same-market complement detector needs, or why it is absent."""

    instrument: VenueInstrument | None = None
    certificate: SettlementCertificate | None = None
    evidence_fingerprint: SettlementEvidenceFingerprint | None = None
    fee_quoter: LegFeeQuoter | None = None
    context_ids: Mapping[str, str | None] = field(default_factory=dict)
    missing: tuple[str, ...] = ()
    """Sources never observed. The detector cannot run and nothing is concluded."""

    known_absent: tuple[str, ...] = ()
    """Sources observed and found empty. Determinate, and not a gap."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "context_ids", dict(sorted(self.context_ids.items())))

    @property
    def can_run_detector(self) -> bool:
        return (
            not self.missing
            and self.instrument is not None
            and self.certificate is not None
            and self.fee_quoter is not None
        )

    @property
    def blocked_classification(self) -> str:
        """Which blocked verdict a known-absent context actually warrants.

        A series that publishes no fee type blocks on *fee* semantics, not on
        settlement semantics. Reporting every determinate block as a settlement
        block would attribute the refusal to the wrong proof obligation and send
        a reader to re-read a contract that was never the problem.
        """
        if self.certificate is None:
            return Classification.BLOCKED_SETTLEMENT_SEMANTICS.value
        return Classification.BLOCKED_FEE_SEMANTICS.value


@dataclass(frozen=True, slots=True)
class BasketContext:
    """Everything the AT_MOST_ONE basket detector needs, or why it is absent."""

    plan: BasketPlan
    relation_certificate: RelationCertificate | None = None
    relation_fingerprint: SettlementEvidenceFingerprint | None = None
    members: Mapping[str, BinaryContext] = field(default_factory=dict)
    fee_quoter: LegFeeQuoter | None = None
    context_ids: Mapping[str, str | None] = field(default_factory=dict)
    missing: tuple[str, ...] = ()
    known_absent: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "members", dict(sorted(self.members.items())))
        object.__setattr__(self, "context_ids", dict(sorted(self.context_ids.items())))

    @property
    def can_run_detector(self) -> bool:
        if self.missing or self.relation_certificate is None or self.fee_quoter is None:
            return False
        return all(
            member.instrument is not None and member.certificate is not None
            for member in self.members.values()
        ) and set(self.members) == set(self.plan.members)

    @property
    def blocked_classification(self) -> str:
        if self.relation_certificate is None or any(
            member.certificate is None for member in self.members.values()
        ):
            return Classification.BLOCKED_SETTLEMENT_SEMANTICS.value
        return Classification.BLOCKED_FEE_SEMANTICS.value


class ContextProvider(Protocol):
    """Resolves detector inputs from recorded knowledge. No I/O of any kind."""

    def binary_context(self, ticker: str, horizon: KnowledgeHorizon) -> BinaryContext:
        """Context for one market's complement evaluation."""
        ...

    def basket_context(self, plan: BasketPlan, horizon: KnowledgeHorizon) -> BasketContext:
        """Context for one AT_MOST_ONE group."""
        ...

    def completeness(
        self, horizon: KnowledgeHorizon, plan: DetectorPlan, subjects: Sequence[str]
    ) -> ReplayDataCompleteness:
        """Per-dimension knowledge status as of this horizon."""
        ...
