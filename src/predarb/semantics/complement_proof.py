"""Research model for the complement-conservation invariant.

The claim under investigation, for one matched contract unit with notional ``N``:

    for every permitted terminal settlement path there exists p, 0 <= p <= N,
    such that the long/YES payout is p and the short/NO payout is N - p

which gives ``YES + NO == N`` without requiring either leg to be restricted to
``{0, N}``.

Three independent axes, deliberately never collapsed
----------------------------------------------------
``STRICT_TWO_STATE``            p is confined to {0, N}
``FRACTIONAL_ALLOWED``          p may lie strictly between 0 and N
``COMPLEMENT_CONSERVATION``     YES + NO == N in every permitted state

A contract can have strict-two-state false and complement-conservation true.
That is the case this module exists to test, and
:mod:`predarb.semantics.settlement_census` already holds the first two axes.

What counts as a proof here
---------------------------
Per-mechanism, and every reachable mechanism must be proven. One unresolved
path blocks the whole thing; one non-complementary path disproves it. Nothing
is inferred from full collateralization, clearing solvency, zero-sum intuition,
or the shape of a worked example -- a complementary example establishes that the
rule *permits* complementary settlement, not that it *requires* it.

Rounding is a first-class part of the proof, not a footnote. p + (N - p) == N is
arithmetic; whether the two payouts a venue actually pays sum to N depends on
where rounding happens and whether the second side is a residual or an
independent computation. An unspecified rounding model blocks an exact theorem
however clean the conceptual allocation looks.

This module issues nothing. It is a way of recording what has and has not been
established, so that the gaps stay visible.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from predarb.domain.money import Price
from predarb.semantics.settlement_census import SettlementMechanism
from predarb.semantics.venue_intervention import (
    ConservationScope,
    ResidualVenueInterventionRisk,
)

__all__ = [
    "ComplementConservationProof",
    "MechanismProof",
    "MechanismStatus",
    "ProofStatus",
    "RoundingModel",
    "SourceComponent",
]


class MechanismStatus(StrEnum):
    """Whether one settlement path is proven to conserve the notional."""

    PROVEN_COMPLEMENTARY = "PROVEN_COMPLEMENTARY"
    """Governing text establishes the second side as the residual of the first."""

    NOT_COMPLEMENTARY = "NOT_COMPLEMENTARY"
    """Governing text permits the two payouts to miss the notional."""

    UNRESOLVED = "UNRESOLVED"
    """Not established either way. Blocks, and is the honest default."""

    @property
    def blocks(self) -> bool:
        return self is not MechanismStatus.PROVEN_COMPLEMENTARY


class RoundingModel(StrEnum):
    """How the two sides' payouts are reduced to payable amounts."""

    RESIDUAL = "RESIDUAL"
    """One side is computed, the other is ``N`` minus it. Conserves exactly."""

    INDEPENDENT = "INDEPENDENT"
    """Each side is computed and rounded on its own. Can lose the residue."""

    EXACT_NO_ROUNDING = "EXACT_NO_ROUNDING"
    """Payouts land on the payable grid with nothing to round."""

    UNSPECIFIED = "UNSPECIFIED"
    """No governing text states where rounding happens. Blocks an exact theorem."""

    @property
    def conserves_exactly(self) -> bool:
        return self in {RoundingModel.RESIDUAL, RoundingModel.EXACT_NO_ROUNDING}


class ProofStatus(StrEnum):
    """The overall research verdict."""

    PROVEN_SYSTEMICALLY = "COMPLEMENT_CONSERVATION_PROVEN_SYSTEMICALLY"
    PROVEN_FOR_SUBSET = "COMPLEMENT_CONSERVATION_PROVEN_FOR_SUBSET"
    NOT_PROVEN = "COMPLEMENT_CONSERVATION_NOT_PROVEN"
    DISPROVEN = "COMPLEMENT_CONSERVATION_DISPROVEN"
    EVIDENCE_INCOMPLETE = "APPLICABLE_GOVERNING_EVIDENCE_INCOMPLETE"


@dataclass(frozen=True, slots=True)
class SourceComponent:
    """One governing document a proof rests on, pinned by hash and version."""

    name: str
    version: str | None
    document_sha256: str | None
    url: str | None = None
    retrieved_note: str | None = None

    @property
    def is_pinned(self) -> bool:
        """Whether this source is nailed down enough to rest a proof on.

        A source with no hash is a source we cannot show we read, and a proof
        resting on one is a proof about an unidentified document.
        """
        return bool(self.document_sha256 and self.version)

    def payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "document_sha256": self.document_sha256,
            "url": self.url,
            "retrieved_note": self.retrieved_note,
        }


@dataclass(frozen=True, slots=True)
class MechanismProof:
    """The finding for one reachable settlement mechanism."""

    mechanism: SettlementMechanism
    status: MechanismStatus
    rule_reference: str
    quoted_text: str
    reasoning: str
    reachable: bool = True
    """Whether this path can actually occur for the contract under study.

    An unreachable path is recorded rather than omitted, together with why it
    cannot occur -- proving a path unreachable is itself work, and silently
    dropping it would look identical to never having considered it."""

    unreachable_because: str | None = None

    both_branches_explicit: bool = False
    """Whether the governing text states **both** sides' payouts.

    Required for ``PROVEN_COMPLEMENTARY``. A payoff invariant must not rest on
    "nothing else says the losing side gets paid": silence is not a zero, and a
    proof that leans on it is a proof about the absence of text rather than
    about the text. Where a rule says the Settlement Value goes to one side, the
    other side's zero has to be stated somewhere -- and for Kalshi it usually
    is, in the product certification's own payout description."""

    def __post_init__(self) -> None:
        if self.status is MechanismStatus.PROVEN_COMPLEMENTARY and not self.both_branches_explicit:
            raise ValueError(
                f"{self.mechanism.value}: cannot be PROVEN_COMPLEMENTARY without governing "
                "text stating both sides' payouts; the losing side's zero must be "
                "established, not inferred from silence"
            )
        if not self.reachable and not (self.unreachable_because or "").strip():
            raise ValueError(
                f"{self.mechanism.value}: a path called unreachable must say why; "
                "otherwise it is indistinguishable from one that was overlooked"
            )
        if not (self.quoted_text or "").strip():
            raise ValueError(
                f"{self.mechanism.value}: every mechanism finding must quote the "
                "governing text it rests on"
            )

    @property
    def blocks(self) -> bool:
        return self.reachable and self.status.blocks

    def payload(self) -> dict[str, Any]:
        return {
            "mechanism": self.mechanism.value,
            "status": self.status.value,
            "rule_reference": self.rule_reference,
            "quoted_text": self.quoted_text,
            "reasoning": self.reasoning,
            "reachable": self.reachable,
            "unreachable_because": self.unreachable_because,
            "both_branches_explicit": self.both_branches_explicit,
        }

    def describe(self) -> str:
        reach = "" if self.reachable else " (unreachable)"
        return f"{self.mechanism.value}{reach}: {self.status.value} [{self.rule_reference}]"


@dataclass(frozen=True, slots=True)
class ComplementConservationProof:
    """What has and has not been established for one contract or family."""

    subject: str
    notional: Price
    contract_type: str
    mechanisms: tuple[MechanismProof, ...]
    sources: tuple[SourceComponent, ...]
    rounding: RoundingModel
    rounding_note: str = ""
    mechanism_closure_established: bool = False
    """Whether the mechanism list is known to be complete.

    An unknown closure blocks: an unenumerated path is exactly the kind of thing
    a proof is supposed to rule out."""

    residual_interventions: tuple[ResidualVenueInterventionRisk, ...] = field(default_factory=tuple)
    """Extraordinary venue authority excluded from the proof and disclosed beside it.

    Excluded from the *proof*, never from the *report*: :meth:`describe` and
    :meth:`payload` both carry it, and :attr:`conservation_scope` refuses to say
    anything stronger than "under normal governing settlement" while any is
    recorded. See :mod:`predarb.semantics.venue_intervention` for why this
    boundary exists and how narrow it is."""

    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def conservation_scope(self) -> ConservationScope:
        """What a positive verdict here is a verdict about.

        Never unqualified. A proven family with residual interventions recorded
        is guaranteed *under normal governing settlement* and is not risk-free;
        one with none recorded is still only a claim about the governing text.
        """
        if self.status in {ProofStatus.PROVEN_SYSTEMICALLY, ProofStatus.PROVEN_FOR_SUBSET}:
            return ConservationScope.NORMAL_GOVERNING_SETTLEMENT
        return ConservationScope.NOT_ESTABLISHED

    @property
    def reachable(self) -> tuple[MechanismProof, ...]:
        return tuple(m for m in self.mechanisms if m.reachable)

    @property
    def blocking(self) -> tuple[MechanismProof, ...]:
        return tuple(m for m in self.mechanisms if m.blocks)

    @property
    def unpinned_sources(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.sources if not s.is_pinned)

    @property
    def status(self) -> ProofStatus:
        """The verdict. Every gate fails closed, and the order matters.

        A disproven path is reported as disproven even when other things are
        also missing, because that is the strongest thing known: no amount of
        further evidence will repair a rule that permits a shortfall. Nothing
        reachable and nothing proven is likewise not a proof of anything, so an
        empty reachable set counts as missing evidence rather than success.
        """
        if any(
            m.status is MechanismStatus.NOT_COMPLEMENTARY and m.reachable for m in self.mechanisms
        ):
            return ProofStatus.DISPROVEN
        evidence_missing = (
            not self.sources
            or self.unpinned_sources
            or not self.mechanism_closure_established
            or not self.reachable
        )
        if evidence_missing:
            return ProofStatus.EVIDENCE_INCOMPLETE
        if self.blocking:
            return ProofStatus.NOT_PROVEN
        if not self.rounding.conserves_exactly:
            return ProofStatus.NOT_PROVEN
        return ProofStatus.PROVEN_FOR_SUBSET

    def source_digest(self) -> str:
        """Content-derived identity of the documents this proof rests on.

        A proof is a statement about specific texts. If one of them is now a
        different document, the proof has not become false -- it has stopped
        being about anything current, which is a different and more dangerous
        condition, because it still reads as a proof.
        """
        payload = "\x1e".join(
            f"{s.name}|{s.version}|{s.document_sha256}"
            for s in sorted(self.sources, key=lambda s: s.name)
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:32]

    def still_applies_to(self, current: Iterable[SourceComponent]) -> tuple[bool, tuple[str, ...]]:
        """Whether this proof still describes the documents now in force.

        Returns ``(applies, reasons)``. A version or hash that moved is named,
        and a source that has gone missing is named too.
        """
        by_name = {s.name: s for s in current}
        reasons: list[str] = []
        for source in sorted(self.sources, key=lambda s: s.name):
            now = by_name.get(source.name)
            if now is None:
                reasons.append(f"{source.name}: no longer present in the source set")
                continue
            if now.version != source.version:
                reasons.append(f"{source.name}: version {source.version} -> {now.version}")
            if now.document_sha256 != source.document_sha256:
                reasons.append(f"{source.name}: content hash changed")
        return (not reasons, tuple(reasons))

    def exact_bound(self) -> str:
        """The conservation bound this proof supports, stated honestly."""
        if self.status is ProofStatus.PROVEN_FOR_SUBSET:
            bound = f"YES + NO == {self.notional} exactly, in every ordinary settlement state"
            if self.residual_interventions:
                bound += (
                    f"; {len(self.residual_interventions)} extraordinary venue "
                    "intervention power(s) disclosed separately and not proven against"
                )
            return bound
        if not self.rounding.conserves_exactly:
            return (
                f"no bound: the rounding model is {self.rounding.value}, so the "
                "residue between the two payouts is not constrained by any governing "
                "text examined"
            )
        return "no bound: " + "; ".join(
            f"{m.mechanism.value} is {m.status.value}" for m in self.blocking
        )

    def payload(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "notional": str(self.notional),
            "contract_type": self.contract_type,
            "status": self.status.value,
            "rounding": self.rounding.value,
            "rounding_note": self.rounding_note,
            "mechanism_closure_established": self.mechanism_closure_established,
            "mechanisms": [m.payload() for m in self.mechanisms],
            "sources": [s.payload() for s in self.sources],
            "conservation_scope": self.conservation_scope.value,
            "residual_interventions": [
                {
                    "venue": r.venue,
                    "rule_reference": r.rule_reference,
                    "powers": list(r.powers),
                    "rationale": r.rationale,
                }
                for r in self.residual_interventions
            ],
            "exact_bound": self.exact_bound(),
            "source_digest": self.source_digest(),
            "notes": list(self.notes),
        }

    def describe(self) -> str:
        lines = [f"{self.subject}: {self.status.value}", f"  notional {self.notional}"]
        lines += [f"  {m.describe()}" for m in self.mechanisms]
        lines.append(f"  rounding: {self.rounding.value}")
        lines.append(f"  scope: {self.conservation_scope.value}")
        lines.append(f"  bound: {self.exact_bound()}")
        lines += [
            "  " + line
            for risk in self.residual_interventions
            for line in risk.disclosure().splitlines()
        ]
        return "\n".join(lines)


def proofs_by_subject(
    proofs: Iterable[ComplementConservationProof],
) -> Mapping[str, ComplementConservationProof]:
    merged: dict[str, ComplementConservationProof] = {}
    for proof in proofs:
        if proof.subject in merged:
            raise ValueError(f"two proofs for subject {proof.subject!r}")
        merged[proof.subject] = proof
    return dict(sorted(merged.items()))
