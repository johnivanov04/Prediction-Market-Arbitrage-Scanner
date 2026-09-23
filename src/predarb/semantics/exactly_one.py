"""EXACTLY_ONE, derived from two certificates rather than reviewed as a third.

    EXACTLY_ONE(S)  ==  AT_MOST_ONE(S)  AND  AT_LEAST_ONE(S)

The composition is the whole design. Introducing EXACTLY_ONE as a primitive
would mean a human review that could approve the conjunction without either half
being independently established -- and the two halves have genuinely different
evidence policies. AT_MOST_ONE asks whether two members can both win, answered
from the rules in front of the reviewer. AT_LEAST_ONE asks whether every member
can lose, which needs the contract's cancellation and void provisions and, where
a partition is relied on, evidence about the settlement domain. One review
covering both would be satisfied by one reviewer's judgement about two
different questions.

Derived at evaluation time, not stored
---------------------------------------
This object is computed when a decision needs it and then discarded. Persisting
it would create a third record with its own lifecycle, its own staleness rules
and -- inevitably -- its own way of surviving a parent it no longer matches. A
derivation that is recomputed every time cannot outlive its parents.

That also gives the replay semantics for free: at a horizon where either parent
was not yet known, the derivation simply does not happen, so a future-issued
certificate cannot make an earlier decision look better than it was.

The fingerprints are deliberately not compared
-----------------------------------------------
The two parents' aggregate evidence fingerprints will differ, because their
evidence policies differ. Requiring them to match would make derivation
impossible. What must match is the canonical member set and each member's
*settlement* evidence -- the per-member semantics both proofs were reviewed
against. Both parent fingerprints are recorded so the derivation can be audited
back to what was actually approved.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from predarb.clock import ensure_utc
from predarb.semantics.relation import (
    RelationCertificate,
    RelationClaim,
    canonical_members,
)

__all__ = [
    "DerivationRefusal",
    "DerivedExactlyOneProof",
    "derive_exactly_one",
    "exactly_one_states",
]


class DerivationRefusal(StrEnum):
    """Why two certificates do not compose into EXACTLY_ONE."""

    MISSING_AT_MOST_ONE = "MISSING_AT_MOST_ONE"
    MISSING_AT_LEAST_ONE = "MISSING_AT_LEAST_ONE"
    WRONG_CLAIM = "WRONG_CLAIM"
    MEMBER_SETS_DIFFER = "MEMBER_SETS_DIFFER"
    PARENT_STALE = "PARENT_STALE"
    PARENT_NOT_YET_VALID = "PARENT_NOT_YET_VALID"
    MEMBER_EVIDENCE_DIVERGED = "MEMBER_EVIDENCE_DIVERGED"
    """The two parents were reviewed against different settlement evidence for
    the same member, so they are not talking about the same contract."""

    NO_VALIDITY_OVERLAP = "NO_VALIDITY_OVERLAP"


def exactly_one_states(members: Sequence[str]) -> tuple[str, ...]:
    """The **n** joint states EXACTLY_ONE permits: one winner, in turn.

    Not ``n + 1`` -- the all-NO state AT_MOST_ONE allows is removed by
    AT_LEAST_ONE. Not ``2 ** n - 1`` either -- the multi-winner states
    AT_LEAST_ONE allows are removed by AT_MOST_ONE. The conjunction is exactly
    the singletons, and it is small enough to name.
    """
    return canonical_members(members)


@dataclass(frozen=True, slots=True)
class DerivedExactlyOneProof:
    """EXACTLY_ONE over a member set, and the two proofs it was composed from."""

    event_ticker: str
    members: tuple[str, ...]
    derived_at: datetime
    """The decision instant this derivation was computed for. Not an issuance
    time: the object is recomputed per decision and never stored."""

    at_most_one_certificate_id: str
    at_most_one_evidence_digest: str
    at_least_one_certificate_id: str
    at_least_one_evidence_digest: str
    member_settlement_fingerprints: Mapping[str, str]
    """The per-member semantics **both** parents were reviewed against."""

    valid_from: datetime
    valid_to: datetime | None
    """The intersection of the parents' validity windows. A derived proof
    cannot outlive the narrower of the two."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "derived_at", ensure_utc(self.derived_at))
        object.__setattr__(self, "valid_from", ensure_utc(self.valid_from))
        if self.valid_to is not None:
            object.__setattr__(self, "valid_to", ensure_utc(self.valid_to))
        object.__setattr__(
            self,
            "member_settlement_fingerprints",
            dict(sorted(self.member_settlement_fingerprints.items())),
        )

    @property
    def derivation_id(self) -> str:
        """Deterministic identity: same parents, same members, same id.

        Recomputing the derivation twice for the same inputs must produce the
        same id, so an audit trail can point at one derivation rather than at a
        sequence of indistinguishable ones.
        """
        payload = "\x1e".join(
            (
                self.event_ticker,
                ",".join(self.members),
                self.at_most_one_certificate_id,
                self.at_most_one_evidence_digest,
                self.at_least_one_certificate_id,
                self.at_least_one_evidence_digest,
            )
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    @property
    def permitted_states(self) -> tuple[str, ...]:
        return exactly_one_states(self.members)

    def permits(self, yes_members: Sequence[str]) -> bool:
        """Exactly one winner, and it must be a member."""
        winners = set(yes_members)
        unknown = winners - set(self.members)
        if unknown:
            raise ValueError(f"{sorted(unknown)} are not members of this relation")
        return len(winners) == 1

    def describe(self) -> str:
        return (
            f"EXACTLY_ONE over {list(self.members)} derived from "
            f"{self.at_most_one_certificate_id[:12]} (AT_MOST_ONE) and "
            f"{self.at_least_one_certificate_id[:12]} (AT_LEAST_ONE); "
            f"{len(self.permitted_states)} permitted state(s)"
        )


def _validity_problem(certificate: RelationCertificate, at: datetime) -> DerivationRefusal | None:
    if at < certificate.valid_from:
        return DerivationRefusal.PARENT_NOT_YET_VALID
    if certificate.valid_to is not None and at > certificate.valid_to:
        return DerivationRefusal.PARENT_STALE
    if not certificate.status.permits_proof:
        return DerivationRefusal.PARENT_STALE
    return None


def _evidence_refusals(
    at_most_one: RelationCertificate,
    at_least_one: RelationCertificate,
    current: Mapping[str, str] | None,
) -> list[str]:
    """Whether both parents describe the same contracts, still.

    Both were reviewed against the members' settlement semantics. If they saw
    different ones, they are not proofs about the same contracts and their
    conjunction is about neither; if current evidence has moved past what either
    saw, the parent is stale regardless of its own status field.
    """
    refusals: list[str] = []
    reviewed_most = dict(at_most_one.member_settlement_fingerprints)
    reviewed_least = dict(at_least_one.member_settlement_fingerprints)

    diverged = sorted(
        ticker
        for ticker in set(reviewed_most) | set(reviewed_least)
        if reviewed_most.get(ticker) != reviewed_least.get(ticker)
    )
    if diverged:
        refusals.append(
            f"{DerivationRefusal.MEMBER_EVIDENCE_DIVERGED.value}: the two parents "
            f"were reviewed against different settlement evidence for {diverged[:5]}"
        )

    if current is None:
        refusals.append(
            f"{DerivationRefusal.MEMBER_EVIDENCE_DIVERGED.value}: current member "
            "settlement evidence is unavailable, so neither parent can be shown to "
            "still apply"
        )
        return refusals

    drifted = sorted(
        ticker for ticker, reviewed in reviewed_least.items() if current.get(ticker) != reviewed
    )
    if drifted:
        refusals.append(
            f"{DerivationRefusal.PARENT_STALE.value}: settlement evidence has moved "
            f"since review for {drifted[:5]}"
        )
    return refusals


def derive_exactly_one(
    *,
    at_most_one: RelationCertificate | None,
    at_least_one: RelationCertificate | None,
    current_member_fingerprints: Mapping[str, str] | None,
    at: datetime,
) -> tuple[DerivedExactlyOneProof | None, tuple[str, ...]]:
    """Compose the two proofs, or say precisely why they do not compose.

    Returns ``(proof, refusals)``. A non-empty refusal list always comes with a
    ``None`` proof: there is no partial derivation, because half of
    EXACTLY_ONE is one of the two claims we already have and calling it the
    conjunction would overstate it.
    """
    moment = ensure_utc(at)
    refusals: list[str] = []

    if at_most_one is None:
        refusals.append(
            f"{DerivationRefusal.MISSING_AT_MOST_ONE.value}: no AT_MOST_ONE "
            "certificate was available at this instant"
        )
    elif at_most_one.claim is not RelationClaim.AT_MOST_ONE:
        refusals.append(
            f"{DerivationRefusal.WRONG_CLAIM.value}: the certificate offered as "
            f"AT_MOST_ONE asserts {at_most_one.claim.value}"
        )

    if at_least_one is None:
        refusals.append(
            f"{DerivationRefusal.MISSING_AT_LEAST_ONE.value}: no AT_LEAST_ONE "
            "certificate was available at this instant"
        )
    elif at_least_one.claim is not RelationClaim.AT_LEAST_ONE:
        refusals.append(
            f"{DerivationRefusal.WRONG_CLAIM.value}: the certificate offered as "
            f"AT_LEAST_ONE asserts {at_least_one.claim.value}"
        )

    if refusals or at_most_one is None or at_least_one is None:
        return None, tuple(refusals)

    if at_most_one.selected_members != at_least_one.selected_members:
        return None, (
            f"{DerivationRefusal.MEMBER_SETS_DIFFER.value}: AT_MOST_ONE covers "
            f"{list(at_most_one.selected_members)} and AT_LEAST_ONE covers "
            f"{list(at_least_one.selected_members)}; a conjunction over different "
            "sets is not a claim about either",
        )

    for label, certificate in (("AT_MOST_ONE", at_most_one), ("AT_LEAST_ONE", at_least_one)):
        problem = _validity_problem(certificate, moment)
        if problem is not None:
            refusals.append(
                f"{problem.value}: the {label} certificate is not usable at "
                f"{moment.isoformat()} (status {certificate.status.value})"
            )

    refusals.extend(_evidence_refusals(at_most_one, at_least_one, current_member_fingerprints))

    valid_from = max(at_most_one.valid_from, at_least_one.valid_from)
    ends = [c.valid_to for c in (at_most_one, at_least_one) if c.valid_to is not None]
    valid_to = min(ends) if ends else None
    if valid_to is not None and valid_to < valid_from:
        refusals.append(
            f"{DerivationRefusal.NO_VALIDITY_OVERLAP.value}: the parents' validity "
            f"windows do not intersect ({valid_from.isoformat()} > "
            f"{valid_to.isoformat()})"
        )

    if refusals:
        return None, tuple(refusals)

    return (
        DerivedExactlyOneProof(
            event_ticker=at_least_one.event_ticker,
            members=at_least_one.selected_members,
            derived_at=moment,
            at_most_one_certificate_id=at_most_one.certificate_id,
            at_most_one_evidence_digest=at_most_one.evidence_fingerprint.digest,
            at_least_one_certificate_id=at_least_one.certificate_id,
            at_least_one_evidence_digest=at_least_one.evidence_fingerprint.digest,
            member_settlement_fingerprints=dict(at_least_one.member_settlement_fingerprints),
            valid_from=valid_from,
            valid_to=valid_to,
        ),
        (),
    )
