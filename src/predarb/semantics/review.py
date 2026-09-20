"""Human review of settlement evidence: requests, checklists, decisions.

A certificate does not mean "this ticker looks like a normal binary market". It
means:

    a human reviewed this exact version of the relevant settlement evidence and
    approved this specific payoff claim.

Everything here exists to keep that sentence true.

No automatic approval
---------------------
Nothing in this module can produce an approval. Text tooling may later summarise
or flag clauses, and any such helper is advisory: text similarity is not
evidence of settlement behaviour, and a model's confidence is not a person's
judgement. Approval requires an explicit recorded human decision, with the
checklist answered *before* the verdict rather than rationalised after it.

Answers are recorded, not inferred
----------------------------------
The checklist is stored with the decision. A later reader can see not just that
someone approved, but what they were asked and what they said -- including the
questions they marked as uncertain.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Final, Self

from predarb.clock import ensure_utc
from predarb.semantics.evidence import EvidenceCompleteness, SettlementEvidenceBundle
from predarb.semantics.fingerprint import SettlementEvidenceFingerprint
from predarb.semantics.policy import CertificateClaim, CompletenessReport, policy_for

__all__ = [
    "STANDARD_BINARY_COMPLEMENT_CHECKLIST",
    "ChecklistAnswer",
    "ChecklistQuestion",
    "Decision",
    "ReviewDecision",
    "ReviewRequest",
    "ReviewStatus",
    "checklist_for",
]


class ReviewStatus(StrEnum):
    AWAITING_REVIEW = "AWAITING_REVIEW"
    EVIDENCE_INCOMPLETE = "EVIDENCE_INCOMPLETE"
    DECIDED = "DECIDED"


class Decision(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    NEEDS_MORE_EVIDENCE = "NEEDS_MORE_EVIDENCE"

    @property
    def permits_issuance(self) -> bool:
        return self is Decision.APPROVED


class ChecklistAnswer(StrEnum):
    """A reviewer's answer.

    ``UNCERTAIN`` exists so that honest doubt has somewhere to go. Without it,
    a reviewer facing an ambiguous clause must either overclaim or abandon the
    review, and the first is far more likely.
    """

    YES = "YES"
    NO = "NO"
    UNCERTAIN = "UNCERTAIN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(frozen=True, slots=True)
class ChecklistQuestion:
    """One question a reviewer must answer, and the answer that permits approval.

    ``safe_answers`` encodes what the claim needs. A question answered outside
    that set does not merely warn -- it blocks approval, because the claim is a
    conjunction of these propositions.
    """

    key: str
    prompt: str
    safe_answers: tuple[ChecklistAnswer, ...]
    why_it_matters: str

    def is_satisfied_by(self, answer: ChecklistAnswer) -> bool:
        return answer in self.safe_answers


STANDARD_BINARY_COMPLEMENT_CHECKLIST: Final[tuple[ChecklistQuestion, ...]] = (
    ChecklistQuestion(
        key="explicit_notional",
        prompt="Is the explicit per-contract notional stated and unambiguous?",
        safe_answers=(ChecklistAnswer.YES,),
        why_it_matters="The claim is that YES+NO sums to this exact number.",
    ),
    ChecklistQuestion(
        key="all_outcomes_enumerated",
        prompt="Do the rules enumerate every possible settlement outcome?",
        safe_answers=(ChecklistAnswer.YES,),
        why_it_matters="A worst case over states you did not list is not a worst case.",
    ),
    ChecklistQuestion(
        key="yes_can_settle_elsewhere",
        prompt="Can YES settle to any value other than 0 or the full notional?",
        safe_answers=(ChecklistAnswer.NO,),
        why_it_matters="A fractional or fair-market resolution breaks the two-state table.",
    ),
    ChecklistQuestion(
        key="fractional_complement_proven",
        prompt=(
            "If YES can settle fractionally, do the rules prove NO settles at "
            "exactly notional minus YES?"
        ),
        safe_answers=(ChecklistAnswer.YES, ChecklistAnswer.NOT_APPLICABLE),
        why_it_matters="Fractional settlement is survivable only if complementarity is explicit.",
    ),
    ChecklistQuestion(
        key="void_or_refund",
        prompt="Can this market void, cancel or refund rather than settling?",
        safe_answers=(ChecklistAnswer.NO,),
        why_it_matters="A refund state is a third terminal state the payoff table omits.",
    ),
    ChecklistQuestion(
        key="dnp_or_postponement",
        prompt=(
            "Do did-not-play, cancellation or postponement provisions create another payout state?"
        ),
        safe_answers=(ChecklistAnswer.NO,),
        why_it_matters=(
            "Kalshi rules admit fair-market and DNP resolutions that no two-state "
            "table describes (A-45)."
        ),
    ),
    ChecklistQuestion(
        key="early_close_affects_settlement",
        prompt="Is there an early-close rule that changes how or when this settles?",
        safe_answers=(ChecklistAnswer.NO, ChecklistAnswer.NOT_APPLICABLE),
        why_it_matters="Early close can change which terminal states are reachable.",
    ),
    ChecklistQuestion(
        key="special_settlement_mechanism",
        prompt="Do the contract terms introduce any special settlement mechanism?",
        safe_answers=(ChecklistAnswer.NO, ChecklistAnswer.NOT_APPLICABLE),
        why_it_matters="Series-level terms can override what the market blurb implies.",
    ),
    ChecklistQuestion(
        key="sources_captured",
        prompt="Were all required sources captured, and did you read the ones that matter?",
        safe_answers=(ChecklistAnswer.YES,),
        why_it_matters="Approving on evidence you did not read is not a review.",
    ),
    ChecklistQuestion(
        key="table_covers_every_state",
        prompt="Does the proposed payoff table cover EVERY permitted terminal state?",
        safe_answers=(ChecklistAnswer.YES,),
        why_it_matters="This is the claim itself, asked directly.",
    ),
)

_CHECKLISTS: Final[dict[CertificateClaim, tuple[ChecklistQuestion, ...]]] = {
    CertificateClaim.STANDARD_BINARY_COMPLEMENT: STANDARD_BINARY_COMPLEMENT_CHECKLIST,
}


def checklist_for(claim: CertificateClaim) -> tuple[ChecklistQuestion, ...]:
    try:
        return _CHECKLISTS[claim]
    except KeyError:
        raise ValueError(f"no review checklist defined for {claim!r}") from None


@dataclass(frozen=True, slots=True)
class ReviewRequest:
    """A packet asking a human to decide one claim about one evidence snapshot."""

    request_id: str
    claim: CertificateClaim
    market_ticker: str
    snapshot_id: str
    evidence_fingerprint: SettlementEvidenceFingerprint
    completeness: CompletenessReport
    rules_hash: str | None
    generated_at: datetime
    checklist: tuple[ChecklistQuestion, ...]
    policy_schema_version: str
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "generated_at", ensure_utc(self.generated_at))

    @property
    def status(self) -> ReviewStatus:
        if self.completeness.completeness is EvidenceCompleteness.EVIDENCE_INCOMPLETE:
            return ReviewStatus.EVIDENCE_INCOMPLETE
        return ReviewStatus.AWAITING_REVIEW

    @property
    def permits_approval(self) -> bool:
        """Incomplete evidence cannot be approved, however willing the reviewer."""
        return self.completeness.permits_approval

    @classmethod
    def create(
        cls,
        *,
        bundle: SettlementEvidenceBundle,
        claim: CertificateClaim,
        generated_at: datetime,
        notes: Sequence[str] = (),
    ) -> Self:
        policy = policy_for(claim)
        completeness = policy.assess(bundle)
        fingerprint = bundle.fingerprint()
        payload = f"{claim.value}\x1e{bundle.snapshot_id}\x1e{fingerprint.digest}"
        return cls(
            request_id=hashlib.sha256(payload.encode()).hexdigest()[:32],
            claim=claim,
            market_ticker=bundle.market_ticker,
            snapshot_id=bundle.snapshot_id,
            evidence_fingerprint=fingerprint,
            completeness=completeness,
            rules_hash=bundle.rules_hash,
            generated_at=generated_at,
            checklist=checklist_for(claim),
            policy_schema_version=policy.schema_version,
            notes=tuple(notes),
        )

    def describe(self) -> str:
        return (
            f"{self.request_id[:12]} {self.market_ticker} {self.claim.value} "
            f"[{self.status.value}] evidence={self.evidence_fingerprint.short}"
        )


@dataclass(frozen=True, slots=True)
class ReviewDecision:
    """A recorded human verdict, bound to the exact evidence it was made on."""

    request_id: str
    claim: CertificateClaim
    market_ticker: str
    snapshot_id: str
    evidence_fingerprint: SettlementEvidenceFingerprint
    decision: Decision
    reviewer: str
    reviewed_at: datetime
    checklist_answers: Mapping[str, ChecklistAnswer]
    notes: str = ""
    external_evidence_acknowledged: Mapping[str, str] = field(default_factory=dict)
    """Document name -> the exact content hash the reviewer confirms reading.

    Bound to the hash rather than the name. An acknowledgement of version A must
    not satisfy version B: a contract amended between review and issuance has
    not been read, however recently the reviewer looked at the old one."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "reviewed_at", ensure_utc(self.reviewed_at))
        object.__setattr__(self, "checklist_answers", dict(sorted(self.checklist_answers.items())))
        object.__setattr__(
            self,
            "external_evidence_acknowledged",
            dict(sorted(self.external_evidence_acknowledged.items())),
        )
        if not self.reviewer.strip():
            raise ValueError("a decision must record who made it")

    def unanswered(self, checklist: Sequence[ChecklistQuestion]) -> tuple[str, ...]:
        return tuple(q.key for q in checklist if q.key not in self.checklist_answers)

    def unsafe_answers(self, checklist: Sequence[ChecklistQuestion]) -> tuple[str, ...]:
        """Questions answered in a way the claim cannot survive."""
        return tuple(
            q.key
            for q in checklist
            if q.key in self.checklist_answers
            and not q.is_satisfied_by(self.checklist_answers[q.key])
        )

    def blocking_reason(  # noqa: PLR0911 - one guard clause per rule, each with its own reason
        self, request: ReviewRequest
    ) -> str | None:
        """Why this decision cannot issue a certificate, or ``None`` if it can.

        Checked against the request rather than trusted on its own: a decision
        recorded against different evidence, or a different claim, is not a
        decision about this request.
        """
        if self.decision is not Decision.APPROVED:
            return f"decision is {self.decision.value}, not APPROVED"
        if self.request_id != request.request_id:
            return f"decision is for request {self.request_id[:12]}, not {request.request_id[:12]}"
        if self.claim is not request.claim:
            # Vacuous today: CertificateClaim has one member, so mypy proves this
            # unreachable. Kept because it becomes load-bearing the moment a
            # second claim exists -- approving a complement must never issue an
            # exhaustiveness certificate.
            return (  # type: ignore[unreachable]
                f"decision approved {self.claim.value}, not {request.claim.value}"
            )
        if not self.evidence_fingerprint.matches(request.evidence_fingerprint):
            return (
                "decision was made against different evidence "
                f"({self.evidence_fingerprint.short} vs {request.evidence_fingerprint.short})"
            )
        if not request.permits_approval:
            return f"evidence is incomplete: {request.completeness.describe()}"
        missing = self.unanswered(request.checklist)
        if missing:
            return f"checklist not fully answered: {', '.join(missing)}"
        unsafe = self.unsafe_answers(request.checklist)
        if unsafe:
            return f"checklist answers block this claim: {', '.join(unsafe)}"
        required_viewing = request.completeness.manual_viewing_required
        unacknowledged = tuple(
            name for name in required_viewing if name not in self.external_evidence_acknowledged
        )
        if unacknowledged:
            return (
                "these governing documents could not be rendered readably and were "
                f"not acknowledged as viewed at source: {', '.join(unacknowledged)}"
            )
        stale = tuple(
            name
            for name, digest in required_viewing.items()
            if self.external_evidence_acknowledged[name] != digest
        )
        if stale:
            return (
                "the document version acknowledged is not the one under review "
                f"({', '.join(stale)}); an acknowledgement of an earlier version "
                "does not carry over"
            )
        return None

    def describe(self) -> str:
        return (
            f"{self.decision.value} by {self.reviewer} at "
            f"{self.reviewed_at.isoformat()} on evidence "
            f"{self.evidence_fingerprint.short}"
        )
