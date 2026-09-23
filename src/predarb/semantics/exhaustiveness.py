"""The evidence policy for AT_LEAST_ONE, which is stricter than AT_MOST_ONE.

The two claims fail in opposite directions, and that asymmetry sets the burden.

``AT_MOST_ONE`` is falsified by finding **two** members that can both settle
YES. Every member is in front of the reviewer, so the search space is bounded by
what has been captured, and evidence about markets outside the subset is
irrelevant -- a winner elsewhere is economically identical to "none of ours won".

``AT_LEAST_ONE`` is falsified by finding **one** outcome that no member covers.
That outcome need not be a market, need not be listed by the venue, and need not
have been imagined by anyone. It is a claim about the world, checked against a
contract, and no amount of API enumeration closes it.

So this policy asks for more, and asks it about the one thing that can go wrong:

    Is there a terminal state in which every selected member settles NO?

Two proof bases, and one thing that is not a basis at all
----------------------------------------------------------
``CONTRACT_LANGUAGE``
    The governing rules say outright that one of these outcomes must occur.
    Strongest, and needs the documents actually retrieved -- a referenced but
    unfetched contract is exactly where the exception clause lives.

``STRUCTURED_PARTITION``
    The members are documented numeric intervals that provably tile the whole
    domain (A-53). The helper in :mod:`predarb.semantics.partition` can show
    gap-freeness *conditional on a domain*; establishing the domain itself takes
    evidence, and the contract still governs cancellation and void.

``VENUE_MEMBERSHIP_COVERAGE`` is **not** a basis. It is
:class:`SupportingEvidence`, and the type system enforces that. Step 11's own
research is the reason: membership evidence refuses to claim completeness,
``is_provisional`` lets a market vanish from both tiers, the documented
live/historical partition was observed overlapping, and no query can reveal an
outcome the venue never listed. A reviewer must never be able to approve

    "we queried every endpoint we know about"

as though it meant

    "one of these propositions must necessarily be true".

Membership may *corroborate* a proof whose logical basis is one of the two
above. When it is cited, it is fingerprinted, so a change to it invalidates the
certificate. When it is not cited, it stays audit-only.

Nothing here approves anything. The policy decides whether a request is
*complete enough to put in front of a human*, which is a lower bar than being
true, and deliberately so: a reviewer should never be handed a packet whose
gaps they would have to notice themselves.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from predarb.semantics.evidence import ExternalDocument
from predarb.semantics.membership import CombinedVenueMembershipEvidence
from predarb.semantics.relation import MIN_BASKET_MEMBERS

__all__ = [
    "AT_LEAST_ONE_POLICY_VERSION",
    "MembershipReliance",
    "ProofBasis",
    "SupportingEvidence",
    "exhaustiveness_incompleteness",
    "membership_reliance_reasons",
]

AT_LEAST_ONE_POLICY_VERSION: Final = "at-least-one-evidence/1"


class ProofBasis(StrEnum):
    """What an AT_LEAST_ONE proof **logically rests on**.

    Deliberately two members. Each is a statement about the *outcome universe*,
    which is what the claim is about. Enumerating a venue's catalogue is not on
    this list, and cannot be added to it -- see :class:`SupportingEvidence`.
    """

    CONTRACT_LANGUAGE = "CONTRACT_LANGUAGE"
    """The governing rules say outright that one of these outcomes must occur.
    Strongest, and needs the documents actually retrieved -- a referenced but
    unfetched contract is exactly where the exception clause lives."""

    STRUCTURED_PARTITION = "STRUCTURED_PARTITION"
    """The members are documented numeric intervals that provably tile the whole
    domain (A-53), with the domain itself established by evidence."""

    @property
    def relies_on_partition(self) -> bool:
        return self is ProofBasis.STRUCTURED_PARTITION

    @property
    def describe(self) -> str:
        return {
            ProofBasis.CONTRACT_LANGUAGE: (
                "the governing contract states that one of these outcomes must occur"
            ),
            ProofBasis.STRUCTURED_PARTITION: (
                "the members are documented strike intervals that tile an "
                "evidence-established domain without gaps"
            ),
        }[self]


class SupportingEvidence(StrEnum):
    """Evidence that *corroborates* a proof without ever being one.

    Venue membership coverage lives here, and cannot be promoted. Step 11's own
    research is the reason:

    * ``CombinedVenueMembershipEvidence`` refuses to claim completeness;
    * ``is_provisional`` means a market can be removed, so membership shrinks
      and a removed market is in neither tier (A-51);
    * the documented live/historical partition was observed overlapping (A-48),
      so even the division it rests on is not exactly as described;
    * and most fundamentally, no query reveals an outcome the venue never
      created a market for.

    "We queried every endpoint we know about" is not "one of these propositions
    must be true". The first is a fact about a catalogue; the second is a fact
    about the world. Treating them as equivalent is the single largest
    exhaustiveness error available, so the type system refuses to express it.
    """

    VENUE_MEMBERSHIP_COVERAGE = "VENUE_MEMBERSHIP_COVERAGE"

    @property
    def describe(self) -> str:
        return {
            SupportingEvidence.VENUE_MEMBERSHIP_COVERAGE: (
                "the selected set covers every market the venue currently lists; "
                "corroborating only, never a proof of the outcome universe"
            )
        }[self]


class MembershipReliance(StrEnum):
    """Whether venue membership evidence is load-bearing for this claim.

    The AT_MOST_ONE decision in Step 9 was that observed membership is
    audit-only: a market appearing outside the subset cannot make "at most one
    of {A, B, C} settles YES" false. That does not carry over unchanged. An
    AT_LEAST_ONE proof may *cite* membership as corroboration, and when it does,
    a membership change should invalidate it -- so the evidence is fingerprinted
    exactly when it was relied upon, and not otherwise.

    ``MATERIAL`` here never means "this is the proof". It means "this
    corroboration was cited, so its change matters".
    """

    AUDIT_ONLY = "AUDIT_ONLY"
    MATERIAL = "MATERIAL"


@dataclass(frozen=True, slots=True)
class _Requirement:
    key: str
    why: str


REQUIRED_EVIDENCE: Final[tuple[_Requirement, ...]] = (
    _Requirement(
        "exact_selected_member_set",
        "the claim is about one canonical set; a different set is a different claim",
    ),
    _Requirement(
        "event_and_series_rules",
        "the outcome universe is defined in the rules, not in the market titles",
    ),
    _Requirement(
        "governing_documents_retrieved",
        "a referenced but unfetched contract is where the exception clause lives",
    ),
    _Requirement(
        "settlement_sources",
        "an unavailable source is itself a route to void or to no determination",
    ),
    _Requirement(
        "member_semantic_evidence",
        "AT_LEAST_ONE is defined over a full winning payoff, so what YES pays matters",
    ),
    _Requirement(
        "membership_evidence_when_relied_upon",
        "a proof that leans on the catalogue must be bound to the catalogue it saw",
    ),
)


def membership_reliance_reasons(
    cited: bool,
    membership: CombinedVenueMembershipEvidence | None,
    selected_members: Sequence[str],
) -> tuple[str, ...]:
    """Why cited membership evidence is too weak to corroborate anything.

    Empty means it is fit to cite. Only called when a proof actually cites it --
    demanding exhausted enumeration from a proof that rests on contract language
    and never mentions the catalogue would block good claims for irrelevant
    reasons.

    Note what this function does **not** do: it never returns "and therefore the
    claim is proven". Corroboration that survives these checks is still only
    corroboration.
    """
    if not cited:
        return ()
    if membership is None:
        return ("venue membership evidence is cited as corroboration but was never captured",)

    reasons: list[str] = []
    if not membership.both_paths_exhausted:
        reasons.append(
            f"cited membership enumeration did not exhaust both paths "
            f"(live {membership.live_trace.status.value}, "
            f"historical {membership.historical_trace.status.value})"
        )
    if not membership.cutoff_stable:
        reasons.append(
            "the historical cutoff moved during the cited enumeration, so the two "
            "tiers were read against different boundaries"
        )
    uncovered = sorted(set(membership.member_tickers) - set(selected_members))
    if uncovered:
        reasons.append(
            f"the venue lists {len(uncovered)} market(s) the selected set does not "
            f"cover: {', '.join(uncovered[:5])}"
        )
    if membership.is_event_open:
        reasons.append(
            "the event still has unsettled markets, so the venue may add more; "
            "cited membership is a moving target while that is true"
        )
    provisional = [m.ticker for m in membership.members if m.is_provisional]
    if provisional:
        reasons.append(
            f"{len(provisional)} cited member(s) are provisional and may be removed, "
            "so the catalogue being cited is not stable"
        )
    return tuple(reasons)


def exhaustiveness_incompleteness(
    *,
    basis: ProofBasis | None,
    selected_members: Sequence[str],
    documents: dict[str, ExternalDocument],
    settlement_sources: Sequence[object],
    membership: CombinedVenueMembershipEvidence | None,
    cites_membership: bool = False,
    partition_supported: bool | None = None,
) -> tuple[str, ...]:
    """Every reason this evidence is not yet fit to put in front of a reviewer.

    Empty means the packet is complete. It does **not** mean the claim is true;
    that is the reviewer's judgement, and nothing here substitutes for it.

    ``basis`` is a :class:`ProofBasis`, so there is no value it can take that
    means "the catalogue looked complete". That option does not exist at the
    type level and therefore cannot be selected by mistake.
    """
    reasons: list[str] = []

    if basis is None:
        reasons.append(
            "no proof basis declared; a reviewer cannot check a proof without "
            "being told what it rests on"
        )
    if len(set(selected_members)) < MIN_BASKET_MEMBERS:
        reasons.append(
            "AT_LEAST_ONE over fewer than two members is a certainty claim about a "
            "single market and belongs in a settlement certificate, not a relation"
        )

    # Documents are required outright, not merely if referenced. AT_MOST_ONE can
    # often be judged from rules text alone; the ALL-NO question usually cannot,
    # because cancellation and void provisions live in the contract.
    usable = {name for name, document in documents.items() if document.retrieval.is_usable}
    referenced_but_missing = sorted(
        name
        for name, document in documents.items()
        if document.url is not None and not document.retrieval.is_usable
    )
    if referenced_but_missing:
        reasons.append(
            f"governing document(s) referenced but not retrieved: "
            f"{', '.join(referenced_but_missing)}"
        )
    if not usable:
        reasons.append(
            "no governing document was retrieved; the ALL-NO question is normally "
            "answered by cancellation and void provisions in the contract"
        )
    if not settlement_sources:
        reasons.append(
            "no settlement source recorded; an unavailable source is itself a route "
            "to no determination"
        )

    reasons.extend(membership_reliance_reasons(cites_membership, membership, selected_members))
    if basis is not None and basis.relies_on_partition and not partition_supported:
        reasons.append(
            "the claim rests on a structured strike partition, but the members' "
            "documented strike fields do not demonstrate gap-free coverage over an "
            "evidence-established domain"
        )

    return tuple(reasons)
