"""Logical relations between markets, and the certificates that prove them.

A :class:`~predarb.semantics.certificate.SettlementCertificate` says what one
market pays. A :class:`RelationCertificate` says which *joint* outcomes across
several markets are possible. They are separate objects because they are
separate proof obligations, established from different evidence, and a basket
needs both:

    AT_MOST_ONE says which joint YES combinations are allowed.
    It says nothing about what YES pays, what NO pays, or what a void does.

Overloading one certificate to mean both would let a market with proven payoff
semantics and an unreviewed relation look identical to one with both.

Two claims, with very different proof burdens
--------------------------------------------
``AT_MOST_ONE`` is cheap to hold. Its state space over a selected subset is:
none of the selected markets settles YES, or exactly one does. A winner
*outside* the subset -- including a historical market the API never returned --
is economically identical, from the basket's point of view, to "all selected
markets settle NO", which is already an enumerated state. Incomplete membership
cannot falsify it.

``AT_LEAST_ONE`` is the opposite. It asserts that some outcome **must** occur
among the selected set, so every outcome the set fails to cover is a
counterexample. Enumerating Kalshi's markets does not help: the venue may
simply never have listed a market for an outcome, and no query discovers an
absence the catalogue does not contain. ``mutually_exclusive`` does not help
either -- it is documented as an upper bound on YES resolutions and says
nothing about the minimum (A-52).

So AT_LEAST_ONE rests on the governing contract, read by a human, and its
checklist is written around the one thing that can falsify it:

    the ALL-NO terminal state.

For AT_MOST_ONE, ALL-NO is a perfectly valid outcome. For AT_LEAST_ONE it is
the forbidden state, and the certificate has to say why it cannot happen.

``EXACTLY_ONE`` is still absent, deliberately. It is the conjunction of the two
claims above, and the right way to reach it is to compose two independently
reviewed certificates over the same canonical member set -- not to invite a
third human review that could approve the conjunction without either half.

Selected subsets, not events
----------------------------
A certificate names an explicit, canonically ordered member set. "This event is
mutually exclusive" does not silently authorise markets added to it later: a new
member requires new evidence and a new certificate. The old certificate remains
valid for its own subset, because a market being added elsewhere does not make
the reviewed subset's relation false.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, Self

from predarb.clock import ensure_utc
from predarb.domain.payoff import SettlementState
from predarb.semantics.evidence import (
    EvidenceCompleteness,
    ExternalDocument,
)
from predarb.semantics.fingerprint import ABSENT, SettlementEvidenceFingerprint
from predarb.semantics.review import ChecklistAnswer, ChecklistQuestion, Decision

__all__ = [
    "ALL_SELECTED_MEMBERS_LOSE",
    "AT_LEAST_ONE_CHECKLIST",
    "AT_MOST_ONE_CHECKLIST",
    "MAX_ENUMERABLE_MEMBERS",
    "MIN_BASKET_MEMBERS",
    "NO_SELECTED_MEMBER_WINS",
    "RELATION_EVIDENCE_SCHEMA_VERSION",
    "JointStateRule",
    "MemberEvidence",
    "RelationCertificate",
    "RelationClaim",
    "RelationCompleteness",
    "RelationDecision",
    "RelationEvidenceBundle",
    "RelationReviewRequest",
    "RelationStatus",
    "at_most_one_states",
    "canonical_members",
    "checklist_for_claim",
    "enumerate_joint_assignments",
    "issue_relation_certificate",
    "relation_certificate_id",
]

RELATION_EVIDENCE_SCHEMA_VERSION: Final = "relation-evidence/1"

NO_SELECTED_MEMBER_WINS: Final = "NONE_OF_SELECTED"
"""The joint state where no selected market settles YES.

Named for what it asserts about the *selected subset*, not about the event. A
winner outside the subset lands here too, which is exactly why the claim
tolerates incomplete membership.
"""

ALL_SELECTED_MEMBERS_LOSE: Final = "ALL_SELECTED_MEMBERS_NO"
"""The joint state where every selected market settles NO.

The same physical outcome carries opposite weight under the two claims, which is
the clearest way to see that they are different propositions:

    AT_MOST_ONE   ALL-NO is permitted -- nothing was claimed to be certain
    AT_LEAST_ONE  ALL-NO is **forbidden** -- it is the only way the claim fails

An AT_LEAST_ONE certificate exists to document why this state cannot be reached.
"""

MIN_BASKET_MEMBERS: Final = 2
"""A one-member AT_MOST_ONE basket is economically meaningless.

"At most one of {A} settles YES" is true of any binary market and proves
nothing; a single-leg NO position is a directional bet, not a basket. Refused
rather than silently allowed.
"""


class RelationClaim(StrEnum):
    """The logical proposition a relation certificate asserts.

    Two members. ``EXACTLY_ONE`` is deliberately absent: it is the conjunction
    of both, and composing two independently reviewed certificates is safer than
    inviting one review that could approve the conjunction without either half.
    """

    AT_MOST_ONE = "AT_MOST_ONE"
    AT_LEAST_ONE = "AT_LEAST_ONE"

    @property
    def proposition(self) -> str:
        return {
            RelationClaim.AT_MOST_ONE: (
                "Among these selected certified member markets, at most one may "
                "settle YES under the reviewed evidence. This asserts nothing "
                "about whether any of them must settle YES, and does not claim "
                "the selected set is exhaustive."
            ),
            RelationClaim.AT_LEAST_ONE: (
                "Among these selected certified member markets, at least one "
                "MUST settle YES under the reviewed evidence: there is no "
                "permitted terminal state in which every selected member settles "
                "NO. This asserts nothing about whether two of them could both "
                "settle YES, says nothing about notionals, liquidity or "
                "profitability, and is not a claim that the venue's market list "
                "is complete."
            ),
        }[self]

    @property
    def forbids_all_no(self) -> bool:
        """Whether every-member-NO is a counterexample to this claim."""
        return self is RelationClaim.AT_LEAST_ONE

    @property
    def permits_multiple_yes(self) -> bool:
        """Whether two members settling YES is compatible with this claim.

        ``True`` for AT_LEAST_ONE. Reading it as "exactly one" is the easiest
        mistake to make about this relation and the one that would silently
        import a mutual-exclusion guarantee nobody reviewed.
        """
        return self is RelationClaim.AT_LEAST_ONE


class RelationStatus(StrEnum):
    VERIFIED = "VERIFIED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    REJECTED = "REJECTED"
    INVALIDATED = "INVALIDATED"

    @property
    def permits_proof(self) -> bool:
        return self is RelationStatus.VERIFIED


class RelationCompleteness(StrEnum):
    COMPLETE = "COMPLETE"
    EVIDENCE_INCOMPLETE = "EVIDENCE_INCOMPLETE"

    @property
    def permits_approval(self) -> bool:
        return self is RelationCompleteness.COMPLETE


def canonical_members(tickers: Sequence[str]) -> tuple[str, ...]:
    """Sorted, de-duplicated member set.

    Canonical so that the same subset supplied in any order produces the same
    fingerprint and the same certificate identity.
    """
    unique = sorted(set(tickers))
    if len(unique) != len(tickers):
        raise ValueError(f"duplicate members supplied: {sorted(tickers)}")
    return tuple(unique)


def at_most_one_states(members: Sequence[str]) -> tuple[SettlementState, ...]:
    """The ``n + 1`` joint states of an AT_MOST_ONE relation.

    Not ``2 ** n`` combinations filtered down: the forbidden combinations are
    forbidden *by the certificate*, so enumerating and discarding them would
    build a state space the relation says cannot exist, then rely on filtering
    to remove it. With n members there are exactly n + 1 reachable states.

    The extra state is :data:`NO_SELECTED_MEMBER_WINS`, which covers both "no
    market in the event won" and "a market outside the selected subset won".
    """
    ordered = canonical_members(members)
    return (SettlementState(NO_SELECTED_MEMBER_WINS), *(SettlementState(t) for t in ordered))


@dataclass(frozen=True, slots=True)
class JointStateRule:
    """Which joint YES/NO assignments a relation permits, stated symbolically.

    Symbolic on purpose. ``AT_LEAST_ONE`` over n members permits ``2**n - 1``
    assignments, and materialising those for a 300-member event -- one was
    observed live -- is not a state space, it is a denial-of-service. The rule
    is a predicate plus a description of what it forbids, which is all any
    certification logic needs.

    The two claims are mirror images at exactly one point:

        AT_MOST_ONE    forbids two or more YES; ALL-NO is fine
        AT_LEAST_ONE   forbids ALL-NO; two or more YES is fine
    """

    claim: RelationClaim
    members: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "members", canonical_members(self.members))

    def permits(self, yes_members: Iterable[str]) -> bool:
        """Whether this joint assignment is compatible with the claim."""
        winners = set(yes_members)
        unknown = winners - set(self.members)
        if unknown:
            raise ValueError(
                f"{sorted(unknown)} are not members of this relation; a joint state "
                "must be expressed over the exact certified member set"
            )
        if self.claim is RelationClaim.AT_MOST_ONE:
            return len(winners) <= 1
        return len(winners) >= 1

    @property
    def forbidden_state(self) -> str | None:
        """The single named state this claim rules out, where there is one."""
        return ALL_SELECTED_MEMBERS_LOSE if self.claim.forbids_all_no else None

    def forbidden_description(self) -> str:
        if self.claim is RelationClaim.AT_LEAST_ONE:
            return (
                f"{ALL_SELECTED_MEMBERS_LOSE}: every one of "
                f"{list(self.members)} settles NO. This is the only terminal "
                "state that falsifies AT_LEAST_ONE, and the certificate must "
                "document why it cannot be reached."
            )
        return (
            f"two or more of {list(self.members)} settle YES simultaneously. "
            f"{ALL_SELECTED_MEMBERS_LOSE} is permitted under AT_MOST_ONE."
        )

    def describe(self) -> str:
        return (
            f"{self.claim.value} over {len(self.members)} member(s); "
            f"forbids {self.forbidden_description()}"
        )


MAX_ENUMERABLE_MEMBERS: Final = 12
"""Above this, joint assignments are never materialised.

``2 ** 12`` is 4,096 -- large enough for any synthetic test and small enough
that an accidental enumeration is a slow test rather than an outage.
"""


def enumerate_joint_assignments(
    rule: JointStateRule, *, max_members: int = MAX_ENUMERABLE_MEMBERS
) -> tuple[tuple[tuple[str, ...], bool], ...]:
    """Every YES subset and whether the rule permits it. **Tests only.**

    Exists so the symbolic predicate can be checked exhaustively against a real
    enumeration on small sets. Nothing in certification or detection calls it,
    and it refuses to run on a set large enough for that to matter.
    """
    if len(rule.members) > max_members:
        raise ValueError(
            f"refusing to enumerate 2**{len(rule.members)} assignments; the relation "
            "is represented symbolically and does not need them"
        )
    results: list[tuple[tuple[str, ...], bool]] = []
    for mask in range(1 << len(rule.members)):
        winners = tuple(ticker for index, ticker in enumerate(rule.members) if mask & (1 << index))
        results.append((winners, rule.permits(winners)))
    return tuple(results)


@dataclass(frozen=True, slots=True)
class MemberEvidence:
    """One selected market's identity and semantic state within the group.

    Carries the member's own settlement fingerprint and active certificate id,
    so that a change to any member's settlement evidence invalidates the group
    relation too -- the relation was reviewed against those payoff semantics.
    """

    ticker: str
    event_ticker: str
    title: str | None
    yes_sub_title: str | None
    no_sub_title: str | None
    rules_hash: str | None
    notional: str | None
    settlement_fingerprint: str | None
    settlement_certificate_id: str | None

    def component_values(self) -> dict[str, Any]:
        return {
            f"member.{self.ticker}.event_ticker": self.event_ticker,
            f"member.{self.ticker}.title": self.title if self.title is not None else ABSENT,
            f"member.{self.ticker}.yes_sub_title": (
                self.yes_sub_title if self.yes_sub_title is not None else ABSENT
            ),
            f"member.{self.ticker}.no_sub_title": (
                self.no_sub_title if self.no_sub_title is not None else ABSENT
            ),
            f"member.{self.ticker}.rules_hash": (
                self.rules_hash if self.rules_hash is not None else ABSENT
            ),
            f"member.{self.ticker}.notional": (
                self.notional if self.notional is not None else ABSENT
            ),
            f"member.{self.ticker}.settlement_fingerprint": (
                self.settlement_fingerprint if self.settlement_fingerprint is not None else ABSENT
            ),
        }


@dataclass(frozen=True, slots=True)
class RelationEvidenceBundle:
    """Immutable snapshot of the evidence behind a group relation claim."""

    snapshot_id: str
    event_ticker: str
    series_ticker: str | None
    selected_members: tuple[str, ...]
    captured_at: datetime
    schema_version: str

    event_fields: Mapping[str, Any]
    member_evidence: tuple[MemberEvidence, ...]
    observed_event_membership: tuple[str, ...]
    """Every market ticker the event currently returns.

    **Current observed membership, not proven exhaustive membership.** Markets
    settled before the historical cutoff are omitted by the API (A-46). Captured
    for audit; the claim never depends on it being complete.
    """

    documents: Mapping[str, ExternalDocument] = field(default_factory=dict)
    source_refs: Mapping[str, str] = field(default_factory=dict)
    capture_errors: tuple[str, ...] = ()

    exhaustiveness_basis: str | None = None
    """What an AT_LEAST_ONE proof **logically rests on**. ``None`` for
    AT_MOST_ONE, which has no exhaustiveness burden. Only a
    :class:`~predarb.semantics.exhaustiveness.ProofBasis` value is admissible
    here -- venue membership coverage is not one of them."""

    supporting_evidence: tuple[str, ...] = ()
    """Corroboration the proof cites without resting on. Recorded separately
    from the basis so nobody can read "we enumerated the catalogue" as the
    reason the claim is true."""

    membership_fingerprint: str | None = None
    """Digest of the venue membership evidence, when the claim relies on it.

    Whether this is material depends on the basis, which is why the basis is
    recorded. A proof from contract language must not have its certificate
    invalidated because the venue listed an unrelated market; a proof from
    catalogue coverage must be invalidated exactly then."""

    membership_is_material: bool = False
    partition_fingerprint: str | None = None
    partition_is_material: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "captured_at", ensure_utc(self.captured_at))
        object.__setattr__(self, "selected_members", canonical_members(self.selected_members))
        object.__setattr__(self, "event_fields", dict(sorted(self.event_fields.items())))
        object.__setattr__(self, "documents", dict(sorted(self.documents.items())))
        object.__setattr__(self, "source_refs", dict(sorted(self.source_refs.items())))
        object.__setattr__(
            self, "member_evidence", tuple(sorted(self.member_evidence, key=lambda m: m.ticker))
        )
        known = {member.ticker for member in self.member_evidence}
        missing = set(self.selected_members) - known
        if missing:
            raise ValueError(
                f"selected members without evidence: {sorted(missing)}; a relation "
                "cannot be reviewed over markets whose semantics were not captured"
            )

    def component_values(self) -> dict[str, Any]:
        """The **material** evidence: what the claim actually depends on.

        ``observed_event_membership`` is deliberately excluded. The claim is
        scoped to an exact selected subset, so a market appearing or vanishing
        *outside* that subset cannot make "at most one of {A, B, C} settles
        YES" false. Fingerprinting it would expire certificates every time the
        venue added an unrelated market -- churn that teaches reviewers to
        ignore drift, which is exactly what drift detection must not become.

        It is retained on the bundle and in storage for audit and discovery;
        :meth:`audit_values` exposes it for diffing.
        """
        values: dict[str, Any] = {
            "relation.selected_members": list(self.selected_members),
            "relation.exhaustiveness_basis": (
                self.exhaustiveness_basis if self.exhaustiveness_basis else ABSENT
            ),
            "relation.supporting_evidence": sorted(self.supporting_evidence),
        }
        # Relied-upon evidence is material; the same evidence unrelied-upon is
        # audit-only. Binding it unconditionally would expire certificates on
        # changes their proofs never consulted, and teaching reviewers to ignore
        # drift is exactly what drift detection must not do.
        if self.membership_is_material:
            values["relation.membership_evidence"] = (
                self.membership_fingerprint if self.membership_fingerprint else ABSENT
            )
        if self.partition_is_material:
            values["relation.partition_evidence"] = (
                self.partition_fingerprint if self.partition_fingerprint else ABSENT
            )
        for name, value in self.event_fields.items():
            values[f"event.{name}"] = value
        for member in self.member_evidence:
            if member.ticker in self.selected_members:
                values.update(member.component_values())
        for name, document in self.documents.items():
            values[f"document.{name}.sha256"] = (
                document.content_sha256 if document.retrieval.is_usable else ABSENT
            )
            values[f"document.{name}.retrieval"] = document.retrieval.value
            values[f"document.{name}.url"] = document.url if document.url else ABSENT
        return values

    def fingerprint(self) -> SettlementEvidenceFingerprint:
        """Fingerprint over material evidence only."""
        return SettlementEvidenceFingerprint.over(
            self.component_values(), schema_version=self.schema_version
        )

    def audit_values(self) -> dict[str, Any]:
        """Observations kept for audit that are *not* part of the claim.

        Diffable, so a reviewer can see the event's membership move over time,
        without any of it invalidating a selected-subset certificate.
        """
        return {
            "relation.observed_event_membership_NOT_PROVEN_EXHAUSTIVE": sorted(
                self.observed_event_membership
            ),
            "relation.membership_evidence_audit_only": (
                None if self.membership_is_material else self.membership_fingerprint
            ),
            "relation.partition_evidence_audit_only": (
                None if self.partition_is_material else self.partition_fingerprint
            ),
        }

    def member(self, ticker: str) -> MemberEvidence | None:
        return next((m for m in self.member_evidence if m.ticker == ticker), None)

    @property
    def members_without_semantic_evidence(self) -> tuple[str, ...]:
        """Selected members whose settlement evidence was not captured.

        This is what blocks a relation review: a reviewer cannot judge whether
        two markets can both settle YES without their rules in front of them.
        Whether those markets hold *payout* certificates is a different
        obligation, checked by the detector rather than here.
        """
        return tuple(
            member.ticker
            for member in self.member_evidence
            if member.ticker in self.selected_members
            and (member.settlement_fingerprint is None or member.rules_hash is None)
        )

    @property
    def members_without_settlement_certificate(self) -> tuple[str, ...]:
        """Audit only. Recorded so a discovery queue can show readiness, and
        deliberately **not** a completeness condition -- see
        :meth:`RelationReviewRequest.create`."""
        return tuple(
            member.ticker
            for member in self.member_evidence
            if member.ticker in self.selected_members and member.settlement_certificate_id is None
        )

    def describe(self) -> str:
        return (
            f"{self.event_ticker} AT_MOST_ONE over {len(self.selected_members)} selected "
            f"members (event currently returns {len(self.observed_event_membership)}) "
            f"snapshot {self.snapshot_id[:12]}"
        )


AT_LEAST_ONE_CHECKLIST: Final[tuple[ChecklistQuestion, ...]] = (
    ChecklistQuestion(
        key="outcome_universe_identified",
        prompt=(
            "What real-world outcome universe does this event cover, and is it "
            "stated precisely enough in the governing evidence to be closed?"
        ),
        safe_answers=(ChecklistAnswer.YES,),
        why_it_matters=(
            "AT_LEAST_ONE is a statement about a universe of outcomes. Until the "
            "universe is pinned down there is nothing for the member set to be "
            "exhaustive *of*."
        ),
    ),
    ChecklistQuestion(
        key="rules_guarantee_one_occurs",
        prompt=(
            "Does the governing evidence explicitly guarantee that one of the "
            "selected propositions must occur?"
        ),
        safe_answers=(ChecklistAnswer.YES,),
        why_it_matters=(
            "This is the claim itself. It must come from contract language, not "
            "from the member list looking complete."
        ),
    ),
    ChecklistQuestion(
        key="all_outcomes_represented",
        prompt=(
            "Is every possible outcome -- named, unnamed, write-in, 'other' or "
            "field -- represented by a selected member?"
        ),
        safe_answers=(ChecklistAnswer.YES,),
        why_it_matters=(
            "One unrepresented outcome is one path to ALL-NO, which is the only "
            "state that falsifies the claim."
        ),
    ),
    ChecklistQuestion(
        key="can_end_with_none_true",
        prompt=(
            "Could the event conclude with no selected proposition being true -- "
            "including the event never occurring?"
        ),
        safe_answers=(ChecklistAnswer.NO,),
        why_it_matters="This asks for ALL-NO directly, in the plainest terms available.",
    ),
    ChecklistQuestion(
        key="cancellation_settles_all_no",
        prompt=(
            "Could cancellation, postponement, abandonment or DNP cause every "
            "selected market to settle NO?"
        ),
        safe_answers=(ChecklistAnswer.NO,),
        why_it_matters=(
            "The most common real route to ALL-NO. A contest that is never held "
            "usually has no winner, and every 'X wins' market settles NO."
        ),
    ),
    ChecklistQuestion(
        key="void_or_refund_possible",
        prompt="Could void or refund semantics apply to some but not all members?",
        safe_answers=(ChecklistAnswer.NO,),
        why_it_matters=(
            "A void member is neither YES nor NO, so the joint state leaves the "
            "model the certificate was reviewed against."
        ),
    ),
    ChecklistQuestion(
        key="scalar_prevents_full_yes",
        prompt=(
            "Could a scalar or fair-price mechanism mean no member is a full YES "
            "winner at the notional?"
        ),
        safe_answers=(ChecklistAnswer.NO,),
        why_it_matters=(
            "AT_LEAST_ONE is defined over a *full* winning payoff. If members can "
            "settle fractionally, 'at least one settled YES' stops meaning 'at "
            "least one paid the notional', and the claim no longer supports the "
            "economics it exists for."
        ),
    ),
    ChecklistQuestion(
        key="ties_or_co_winners_understood",
        prompt="Are ties or co-winners possible, and are their settlement rules captured?",
        safe_answers=(ChecklistAnswer.YES, ChecklistAnswer.NOT_APPLICABLE),
        why_it_matters=(
            "Co-winners do not break AT_LEAST_ONE -- multiple YES is permitted -- "
            "but a tie rule that voids the contest instead does, so the rule has "
            "to be read either way."
        ),
    ),
    ChecklistQuestion(
        key="member_set_covers_universe",
        prompt=(
            "Is the selected member set complete for the claimed outcome universe, "
            "rather than merely complete for what the venue happens to list?"
        ),
        safe_answers=(ChecklistAnswer.YES,),
        why_it_matters=(
            "Kalshi may simply never have created a market for an outcome. No "
            "query discovers an absence the catalogue does not contain."
        ),
    ),
    ChecklistQuestion(
        key="membership_enumeration_exhausted",
        prompt=(
            "If this claim relies on covering all of the event's Kalshi markets, "
            "was live + historical enumeration exhausted with the cutoff stable?"
        ),
        safe_answers=(ChecklistAnswer.YES, ChecklistAnswer.NOT_APPLICABLE),
        why_it_matters=(
            "Reading the nested event view alone omits archived members on 46% of "
            "events (A-50). NOT_APPLICABLE is the honest answer when the proof "
            "rests on contract language rather than on covering the catalogue."
        ),
    ),
    ChecklistQuestion(
        key="governing_documents_reviewed",
        prompt="Are all governing documents captured, current and actually read?",
        safe_answers=(ChecklistAnswer.YES,),
        why_it_matters=(
            "A referenced but unretrieved contract is the single most likely place "
            "for the clause that creates an ALL-NO path."
        ),
    ),
    ChecklistQuestion(
        key="claim_is_not_mutual_exclusion",
        prompt=(
            "Is this claim strictly AT_LEAST_ONE, and NOT being used to assert "
            "mutual exclusion or exactly-one?"
        ),
        safe_answers=(ChecklistAnswer.YES,),
        why_it_matters=(
            "AT_LEAST_ONE permits two, three or all members settling YES. Reading "
            "it as exactly-one would import a mutual-exclusion guarantee nobody "
            "reviewed."
        ),
    ),
)
"""The AT_LEAST_ONE review checklist.

Every question is aimed at one target: is there a terminal state in which every
selected member settles NO? Any answer outside ``safe_answers`` -- ``UNCERTAIN``
included -- blocks approval, because the claim is a conjunction of these
propositions and an uncertain conjunct makes the conjunction uncertain.
"""


def checklist_for_claim(claim: RelationClaim) -> tuple[ChecklistQuestion, ...]:
    """The review questions this claim must survive.

    Routed by claim rather than shared, because the two ask opposite things of
    the same facts. "Could cancellation produce multiple YES outcomes?" must be
    NO for AT_MOST_ONE; "could cancellation make every member settle NO?" must
    be NO for AT_LEAST_ONE. One checklist covering both would have to be
    satisfied by contradictory answers.
    """
    return {
        RelationClaim.AT_MOST_ONE: AT_MOST_ONE_CHECKLIST,
        RelationClaim.AT_LEAST_ONE: AT_LEAST_ONE_CHECKLIST,
    }[claim]


AT_MOST_ONE_CHECKLIST: Final[tuple[ChecklistQuestion, ...]] = (
    ChecklistQuestion(
        key="same_relation",
        prompt="Do all selected markets belong to the same intended event and relation?",
        safe_answers=(ChecklistAnswer.YES,),
        why_it_matters="A basket across unrelated markets proves nothing jointly.",
    ),
    ChecklistQuestion(
        key="metadata_indicates_exclusivity",
        prompt="Does authoritative event metadata indicate mutual exclusivity?",
        safe_answers=(ChecklistAnswer.YES,),
        why_it_matters="Supporting evidence, though never sufficient on its own.",
    ),
    ChecklistQuestion(
        key="rules_forbid_two_yes",
        prompt="Do the governing rules support that no two selected markets can both settle YES?",
        safe_answers=(ChecklistAnswer.YES,),
        why_it_matters="This is the claim itself, asked of the rules rather than a flag.",
    ),
    ChecklistQuestion(
        key="ties_or_co_winners",
        prompt="Could ties or co-winners produce multiple YES settlements?",
        safe_answers=(ChecklistAnswer.NO,),
        why_it_matters="A tie rule that pays two winners breaks the n+1 state space.",
    ),
    ChecklistQuestion(
        key="cancellation_multiplies_yes",
        prompt="Could cancellation, postponement or DNP rules create multiple YES outcomes?",
        safe_answers=(ChecklistAnswer.NO,),
        why_it_matters="Abandonment provisions sometimes settle several markets YES.",
    ),
    ChecklistQuestion(
        key="scalar_breaks_joint_model",
        prompt="Could scalar or fractional settlement break the binary joint-state model?",
        safe_answers=(ChecklistAnswer.NO,),
        why_it_matters="A partial YES is neither of the two per-member states.",
    ),
    ChecklistQuestion(
        key="member_semantic_evidence_complete",
        prompt=(
            "Is complete, current semantic evidence captured for every selected "
            "member -- rules text, rules hash and settlement metadata?"
        ),
        safe_answers=(ChecklistAnswer.YES,),
        why_it_matters=(
            "You cannot judge whether two markets can both settle YES without "
            "their rules in front of you."
        ),
    ),
    ChecklistQuestion(
        key="members_describe_distinct_outcomes",
        prompt=(
            "Do the selected members describe genuinely distinct outcomes, with no "
            "two that could be satisfied by the same real-world event?"
        ),
        safe_answers=(ChecklistAnswer.YES,),
        why_it_matters=(
            "Overlapping propositions are the ordinary way an at-most-one claim "
            "fails; payout certification would not catch it."
        ),
    ),
    ChecklistQuestion(
        key="documents_captured_and_read",
        prompt="Were the required event and series documents captured and read?",
        safe_answers=(ChecklistAnswer.YES, ChecklistAnswer.NOT_APPLICABLE),
        why_it_matters="Approving on evidence you did not read is not a review.",
    ),
    ChecklistQuestion(
        key="claim_is_only_at_most_one",
        prompt=(
            "Is the claim ONLY at-most-one, with no assertion that the selected set "
            "is exhaustive or that one member must win?"
        ),
        safe_answers=(ChecklistAnswer.YES,),
        why_it_matters=(
            "Event membership cannot be shown exhaustive (A-46); an exhaustiveness "
            "claim would be unprovable from this evidence."
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class RelationReviewRequest:
    """A packet asking a human to decide one relation claim."""

    request_id: str
    claim: RelationClaim
    event_ticker: str
    selected_members: tuple[str, ...]
    snapshot_id: str
    evidence_fingerprint: SettlementEvidenceFingerprint
    completeness: RelationCompleteness
    incompleteness_reasons: tuple[str, ...]
    observed_event_membership: tuple[str, ...]
    generated_at: datetime
    checklist: tuple[ChecklistQuestion, ...]
    policy_schema_version: str
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "generated_at", ensure_utc(self.generated_at))

    @property
    def permits_approval(self) -> bool:
        return self.completeness.permits_approval

    @classmethod
    def create(
        cls,
        *,
        bundle: RelationEvidenceBundle,
        claim: RelationClaim,
        generated_at: datetime,
        notes: Sequence[str] = (),
        extra_incompleteness: Sequence[str] = (),
    ) -> Self:
        reasons: list[str] = []
        if len(bundle.selected_members) < MIN_BASKET_MEMBERS:
            reasons.append(
                f"only {len(bundle.selected_members)} selected member(s); "
                f"at least {MIN_BASKET_MEMBERS} are needed for the claim to say anything"
            )
        # AT_LEAST_ONE carries its own, stricter requirements. They are supplied
        # by the caller rather than derived here, because judging them needs the
        # membership and partition evidence, which lives outside this module.
        reasons.extend(extra_incompleteness)
        # Deliberately NOT a condition: whether members already hold payout
        # certificates. A relation review answers "can two of these both settle
        # YES?", which is judged from the rules, not from whether someone has
        # separately certified the payoff tables. Requiring certificates first
        # would impose an ordering the proofs do not have -- and the detector
        # still demands both before any economics, so nothing is lost.
        unevidenced = bundle.members_without_semantic_evidence
        if unevidenced:
            reasons.append(
                f"selected members without complete semantic evidence: {', '.join(unevidenced)}"
            )
        for name, document in bundle.documents.items():
            if document.url is not None and not document.retrieval.is_usable:
                reasons.append(f"governing document {name} referenced but not retrieved")

        completeness = (
            RelationCompleteness.COMPLETE
            if not reasons
            else RelationCompleteness.EVIDENCE_INCOMPLETE
        )
        fingerprint = bundle.fingerprint()
        payload = f"{claim.value}\x1e{bundle.snapshot_id}\x1e{fingerprint.digest}"
        return cls(
            request_id=hashlib.sha256(payload.encode()).hexdigest()[:32],
            claim=claim,
            event_ticker=bundle.event_ticker,
            selected_members=bundle.selected_members,
            snapshot_id=bundle.snapshot_id,
            evidence_fingerprint=fingerprint,
            completeness=completeness,
            incompleteness_reasons=tuple(reasons),
            observed_event_membership=bundle.observed_event_membership,
            generated_at=generated_at,
            checklist=checklist_for_claim(claim),
            policy_schema_version=RELATION_EVIDENCE_SCHEMA_VERSION,
            notes=tuple(notes),
        )

    def describe(self) -> str:
        state = "AWAITING_REVIEW" if self.permits_approval else "EVIDENCE_INCOMPLETE"
        return (
            f"{self.request_id[:12]} {self.event_ticker} {self.claim.value} "
            f"over {len(self.selected_members)} selected members [{state}]"
        )


@dataclass(frozen=True, slots=True)
class RelationDecision:
    """A recorded human verdict on a relation claim."""

    request_id: str
    claim: RelationClaim
    event_ticker: str
    selected_members: tuple[str, ...]
    snapshot_id: str
    evidence_fingerprint: SettlementEvidenceFingerprint
    decision: Decision
    reviewer: str
    reviewed_at: datetime
    checklist_answers: Mapping[str, ChecklistAnswer]
    notes: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "reviewed_at", ensure_utc(self.reviewed_at))
        object.__setattr__(self, "selected_members", canonical_members(self.selected_members))
        object.__setattr__(self, "checklist_answers", dict(sorted(self.checklist_answers.items())))
        if not self.reviewer.strip():
            raise ValueError("a relation decision must record who made it")

    def blocking_reason(  # noqa: PLR0911 - one guard clause per rule, each with its own reason
        self, request: RelationReviewRequest
    ) -> str | None:
        if self.decision is not Decision.APPROVED:
            return f"decision is {self.decision.value}, not APPROVED"
        if self.request_id != request.request_id:
            return f"decision is for request {self.request_id[:12]}"
        if self.selected_members != request.selected_members:
            return (
                "decision covers a different member set "
                f"({list(self.selected_members)} vs {list(request.selected_members)})"
            )
        if not self.evidence_fingerprint.matches(request.evidence_fingerprint):
            return (
                "decision was made against different relation evidence "
                f"({self.evidence_fingerprint.short} vs "
                f"{request.evidence_fingerprint.short})"
            )
        if not request.permits_approval:
            return f"evidence is incomplete: {'; '.join(request.incompleteness_reasons)}"
        missing = tuple(q.key for q in request.checklist if q.key not in self.checklist_answers)
        if missing:
            return f"checklist not fully answered: {', '.join(missing)}"
        unsafe = tuple(
            q.key for q in request.checklist if not q.is_satisfied_by(self.checklist_answers[q.key])
        )
        if unsafe:
            return f"checklist answers block this claim: {', '.join(unsafe)}"
        return None

    def describe(self) -> str:
        return (
            f"{self.decision.value} by {self.reviewer} at {self.reviewed_at.isoformat()} "
            f"on relation evidence {self.evidence_fingerprint.short}"
        )


@dataclass(frozen=True, slots=True)
class RelationCertificate:
    """A human-approved claim about which joint outcomes are possible.

    Proves **only** that at most one of the named selected members may settle
    YES under the reviewed evidence. It does not prove that one must settle YES,
    that the selected set is exhaustive, that the members share a notional, that
    fee semantics are resolved, or anything about liquidity or price. Those are
    separate obligations held elsewhere.
    """

    certificate_id: str
    claim: RelationClaim
    event_ticker: str
    selected_members: tuple[str, ...]
    snapshot_id: str
    evidence_fingerprint: SettlementEvidenceFingerprint
    member_settlement_fingerprints: Mapping[str, str]
    """Each member's settlement fingerprint at review time.

    Held separately so a member's payoff semantics drifting invalidates the
    relation too: the joint claim was reviewed against those tables."""

    status: RelationStatus
    reviewer: str
    reviewed_at: datetime
    issued_at: datetime
    valid_from: datetime
    valid_to: datetime | None
    policy_schema_version: str
    evidence: str
    notes: str = ""

    def __post_init__(self) -> None:
        for name in ("reviewed_at", "issued_at", "valid_from"):
            object.__setattr__(self, name, ensure_utc(getattr(self, name)))
        if self.valid_to is not None:
            object.__setattr__(self, "valid_to", ensure_utc(self.valid_to))
        object.__setattr__(self, "selected_members", canonical_members(self.selected_members))
        object.__setattr__(
            self,
            "member_settlement_fingerprints",
            dict(sorted(self.member_settlement_fingerprints.items())),
        )
        if len(self.selected_members) < MIN_BASKET_MEMBERS:
            raise ValueError(
                f"{self.event_ticker}: AT_MOST_ONE over "
                f"{len(self.selected_members)} member(s) asserts nothing"
            )
        if self.status is RelationStatus.VERIFIED and not self.evidence.strip():
            raise ValueError(
                f"{self.event_ticker}: a VERIFIED relation certificate must carry evidence"
            )

    def states(self) -> tuple[SettlementState, ...]:
        """The joint state space this certificate licenses."""
        return at_most_one_states(self.selected_members)

    def covers(self, members: Sequence[str]) -> bool:
        """Whether this certificate's claim covers exactly this member set.

        Exact, not superset: a certificate reviewed over {A, B, C} is not
        automatically a certificate about {A, B}, because the reviewer answered
        questions about the set as presented.
        """
        return canonical_members(members) == self.selected_members

    def blocking_reason(  # noqa: PLR0911 - one guard clause per rule, each with its own reason
        self,
        *,
        current_fingerprint: SettlementEvidenceFingerprint | None,
        member_settlement_fingerprints: Mapping[str, str] | None,
        at: datetime,
    ) -> str | None:
        """Why this relation certificate cannot be relied on now."""
        moment = ensure_utc(at)
        if not self.status.permits_proof:
            return f"{self.event_ticker}: relation certificate is {self.status.value}"
        if current_fingerprint is None:
            return (
                f"{self.event_ticker}: current relation evidence is unavailable, so the "
                f"certificate for {self.evidence_fingerprint.short} cannot be shown to apply"
            )
        if not self.evidence_fingerprint.matches(current_fingerprint):
            drift = self.evidence_fingerprint.diff(current_fingerprint)
            return (
                f"{self.event_ticker}: relation evidence has changed since verification "
                f"({self.evidence_fingerprint.short} -> {current_fingerprint.short}); "
                f"{drift.describe()}"
            )
        if member_settlement_fingerprints is None:
            return f"{self.event_ticker}: member settlement evidence is unavailable"
        for ticker, reviewed in self.member_settlement_fingerprints.items():
            current = member_settlement_fingerprints.get(ticker)
            if current is None:
                return f"{self.event_ticker}: no current settlement evidence for {ticker}"
            if current != reviewed:
                return (
                    f"{self.event_ticker}: settlement evidence for {ticker} has changed "
                    f"since the relation was reviewed ({reviewed[:12]} -> {current[:12]})"
                )
        if moment < self.valid_from:
            return f"{self.event_ticker}: relation certificate is not yet valid"
        if self.valid_to is not None and moment > self.valid_to:
            return f"{self.event_ticker}: relation certificate has expired"
        return None

    @property
    def identity(self) -> str:
        return f"{self.event_ticker}@{self.evidence_fingerprint.short}"

    def describe(self) -> str:
        return (
            f"{self.certificate_id[:12]} {self.event_ticker} {self.claim.value} over "
            f"selected certified mutually-exclusive subset "
            f"{list(self.selected_members)} by {self.reviewer}"
        )


def relation_certificate_id(
    *, event_ticker: str, fingerprint: SettlementEvidenceFingerprint, issued_at: datetime
) -> str:
    payload = f"{event_ticker}\x1e{fingerprint.digest}\x1e{ensure_utc(issued_at).isoformat()}"
    return hashlib.sha256(payload.encode()).hexdigest()


def issue_relation_certificate(
    *,
    bundle: RelationEvidenceBundle,
    request: RelationReviewRequest,
    decision: RelationDecision,
    issued_at: datetime,
    valid_to: datetime | None = None,
) -> RelationCertificate:
    """Issue a relation certificate from an approved review, or refuse and say why."""
    blocking = decision.blocking_reason(request)
    if blocking is not None:
        raise ValueError(f"review does not support issuance: {blocking}")
    fingerprint = bundle.fingerprint()
    if not fingerprint.matches(request.evidence_fingerprint):
        raise ValueError(
            f"the bundle supplied ({fingerprint.short}) is not the one reviewed "
            f"({request.evidence_fingerprint.short})"
        )
    if bundle.selected_members != request.selected_members:
        raise ValueError("bundle member set differs from the reviewed member set")

    member_fingerprints = {
        member.ticker: member.settlement_fingerprint
        for member in bundle.member_evidence
        if member.ticker in bundle.selected_members and member.settlement_fingerprint
    }
    missing = set(bundle.selected_members) - set(member_fingerprints)
    if missing:
        raise ValueError(f"selected members without a settlement fingerprint: {sorted(missing)}")

    return RelationCertificate(
        certificate_id=relation_certificate_id(
            event_ticker=bundle.event_ticker, fingerprint=fingerprint, issued_at=issued_at
        ),
        claim=request.claim,
        event_ticker=bundle.event_ticker,
        selected_members=bundle.selected_members,
        snapshot_id=bundle.snapshot_id,
        evidence_fingerprint=fingerprint,
        member_settlement_fingerprints=member_fingerprints,
        status=RelationStatus.VERIFIED,
        reviewer=decision.reviewer,
        reviewed_at=decision.reviewed_at,
        issued_at=issued_at,
        valid_from=issued_at,
        valid_to=valid_to,
        policy_schema_version=request.policy_schema_version,
        evidence=(
            f"Relation snapshot {bundle.snapshot_id[:16]} captured "
            f"{bundle.captured_at.isoformat()}; selected certified mutually-exclusive "
            f"subset of {len(bundle.observed_event_membership)} currently observed event "
            f"members; checklist answered in full by {decision.reviewer}."
        ),
        notes=decision.notes,
    )


def completeness_of(bundle: RelationEvidenceBundle) -> EvidenceCompleteness:
    """Convenience bridge to the settlement-evidence completeness vocabulary."""
    request = RelationReviewRequest.create(
        bundle=bundle, claim=RelationClaim.AT_MOST_ONE, generated_at=bundle.captured_at
    )
    return (
        EvidenceCompleteness.COMPLETE
        if request.permits_approval
        else EvidenceCompleteness.EVIDENCE_INCOMPLETE
    )
