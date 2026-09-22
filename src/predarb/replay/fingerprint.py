"""Deterministic fingerprint over an economic decision.

Comparing live and replay output by equality would fail on incidental runtime
detail: a run id, a wall-clock execution timestamp, log formatting. Those differ
between two runs of the *same* inputs and say nothing about whether the
economics matched.

So the comparison is over a digest of the **material** inputs and results:
what was decided, about what, from which book state, under which fee and
certificate context, and with what numbers. Anything that can differ between two
faithful evaluations of identical history is excluded by construction -- not
filtered out afterwards, but never fed in.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from predarb.clock import ensure_utc
from predarb.detectors.binary_complement import BinaryComplementResult
from predarb.detectors.no_basket import BasketResult
from predarb.semantics.fingerprint import canonical_encoding

__all__ = [
    "DECISION_FINGERPRINT_VERSION",
    "EconomicDecisionFingerprint",
    "fingerprint_basket",
    "fingerprint_binary_complement",
    "fingerprint_known_absent",
    "fingerprint_missing_knowledge",
]

DECISION_FINGERPRINT_VERSION = "economic-decision/1"


@dataclass(frozen=True, slots=True)
class EconomicDecisionFingerprint:
    """A digest of one detector evaluation, plus the components behind it."""

    version: str
    detector: str
    digest: str
    components: Mapping[str, str]

    @property
    def short(self) -> str:
        return self.digest[:16]

    def differing_components(self, other: EconomicDecisionFingerprint) -> tuple[str, ...]:
        """Which material components differ -- so a mismatch can be explained."""
        names = set(self.components) | set(other.components)
        return tuple(
            sorted(
                name for name in names if self.components.get(name) != other.components.get(name)
            )
        )

    def describe(self) -> str:
        return f"{self.detector}:{self.short}"


def _digest(detector: str, components: Mapping[str, Any]) -> EconomicDecisionFingerprint:
    encoded = {
        name: hashlib.sha256(f"{name}\x1e{canonical_encoding(value)}".encode()).hexdigest()
        for name, value in components.items()
    }
    payload = f"{DECISION_FINGERPRINT_VERSION}\x1d{detector}\x1d" + "\x1d".join(
        f"{name}\x1e{value}" for name, value in sorted(encoded.items())
    )
    return EconomicDecisionFingerprint(
        version=DECISION_FINGERPRINT_VERSION,
        detector=detector,
        digest=hashlib.sha256(payload.encode()).hexdigest(),
        components=dict(sorted(encoded.items())),
    )


def _quote_components(prefix: str, quote: Any) -> dict[str, Any]:
    """Book provenance and the exact liquidity a quote would consume."""
    if quote is None:
        return {f"{prefix}.quote": None}
    return {
        f"{prefix}.filled": quote.filled_quantity.to_str(),
        f"{prefix}.gross_cost": quote.gross_cost.to_str(),
        f"{prefix}.epoch": quote.connection_epoch,
        f"{prefix}.sid": quote.sid,
        f"{prefix}.book_seq": quote.book_seq,
        f"{prefix}.liquidity": [i.describe() for i in quote.liquidity_ids],
    }


def _payoff_components(prefix: str, payoff: Any) -> dict[str, Any]:
    if payoff is None:
        return {f"{prefix}.payoff": None}
    return {
        f"{prefix}.states": [s.name for s in payoff.states],
        f"{prefix}.per_state": [
            [entry.state.name, entry.total.to_str()] for entry in payoff.per_state
        ],
        f"{prefix}.worst_case": payoff.worst_case.to_str(),
    }


def _profit_components(prefix: str, profit: Any) -> dict[str, Any]:
    if profit is None:
        return {f"{prefix}.profit": None}
    return {
        f"{prefix}.gross_cost": profit.gross_cost.to_str(),
        f"{prefix}.fee_lower": profit.fee_lower.to_str(),
        f"{prefix}.fee_upper": profit.fee_upper.to_str(),
        f"{prefix}.profit_lower": profit.profit_lower_bound.to_str(),
        f"{prefix}.profit_upper": profit.profit_upper_bound.to_str(),
    }


def fingerprint_missing_knowledge(
    *,
    detector: str,
    market_ticker: str,
    quantity: str,
    decision_time: datetime,
    missing: Sequence[str],
) -> EconomicDecisionFingerprint:
    """Fingerprint a decision the replay could not make.

    Recorded as an evaluation rather than skipped. A skipped decision is
    invisible in the report, which reads exactly like a decision that was made
    and found nothing -- the same failure mode as silently empty results.
    """
    return _digest(
        detector,
        {
            "decision_time": ensure_utc(decision_time).isoformat(),
            "market": market_ticker,
            "quantity": quantity,
            "classification": "MISSING_POINT_IN_TIME_KNOWLEDGE",
            "missing": sorted(missing),
        },
    )


def fingerprint_known_absent(
    *,
    detector: str,
    market_ticker: str,
    quantity: str,
    decision_time: datetime,
    classification: str,
    known_absent: Sequence[str],
) -> EconomicDecisionFingerprint:
    """Fingerprint a decision that was determinate *without* running a detector.

    Distinct from :func:`fingerprint_missing_knowledge`: this is a conclusion,
    not a gap. The specific absences are material, because "we checked and there
    is no certificate for A" and "...none for A, B and C" are different states of
    knowledge that happen to lead to the same verdict today.
    """
    return _digest(
        detector,
        {
            "decision_time": ensure_utc(decision_time).isoformat(),
            "market": market_ticker,
            "quantity": quantity,
            "classification": classification,
            "detector_did_run": False,
            "known_absent": sorted(known_absent),
        },
    )


def fingerprint_binary_complement(
    result: BinaryComplementResult, *, decision_time: datetime
) -> EconomicDecisionFingerprint:
    """Fingerprint one same-market complement evaluation."""
    components: dict[str, Any] = {
        "decision_time": ensure_utc(decision_time).isoformat(),
        "market": result.identity.market_ticker,
        "quantity": result.quantity.to_str(),
        "classification": result.classification.value,
        "semantic_status": result.semantic_status.value,
        "payoff_status": result.payoff_status.value,
        "cost_status": result.cost_status.value,
        "execution_status": result.execution_status.value,
        "book.epoch": result.identity.connection_epoch,
        "book.sid": result.identity.sid,
        "book.seq": result.identity.book_seq,
        "certificate": result.certificate_identity,
        "certificate_evidence": result.certificate_evidence_fingerprint,
        "current_evidence": result.current_evidence_fingerprint,
        "allowed_states": [s.name for s in result.allowed_states],
        "blocking_reason": result.blocking_reason,
    }
    components.update(_quote_components("yes", result.yes_quote))
    components.update(_quote_components("no", result.no_quote))
    components.update(_payoff_components("payoff", result.portfolio_payoff))
    components.update(_profit_components("cost", result.profit))
    for side, fees in (("yes", result.yes_fees), ("no", result.no_fees)):
        components[f"{side}.fees"] = None if fees is None else fees.describe()
    return _digest("binary_complement", components)


def fingerprint_basket(
    result: BasketResult, *, decision_time: datetime
) -> EconomicDecisionFingerprint:
    """Fingerprint one AT_MOST_ONE basket evaluation."""
    components: dict[str, Any] = {
        "decision_time": ensure_utc(decision_time).isoformat(),
        "event": result.event_ticker,
        "members": list(result.members),
        "quantity": result.quantity.to_str(),
        "classification": result.classification.value,
        "semantic_status": result.semantic_status.value,
        "payoff_status": result.payoff_status.value,
        "cost_status": result.cost_status.value,
        "execution_status": result.execution_status.value,
        "relation_certificate": result.relation_certificate_id,
        "relation_evidence": result.relation_evidence_fingerprint,
        "member_certificates": dict(sorted(result.member_certificate_ids.items())),
        "joint_states": [s.name for s in result.joint_states],
        "blocking_reason": result.blocking_reason,
        "blocking_legs": list(result.blocking_legs),
        "total_gross_cost": (result.total_gross_cost.to_str() if result.total_gross_cost else None),
    }
    for ticker in result.members:
        components.update(_quote_components(f"leg.{ticker}", result.quotes.get(ticker)))
        leg_fees = result.fees_by_leg.get(ticker)
        components[f"leg.{ticker}.fees"] = None if leg_fees is None else leg_fees.describe()
    components.update(_payoff_components("payoff", result.portfolio_payoff))
    components.update(_profit_components("cost", result.profit))
    return _digest("no_basket", components)


def compare_fingerprints(
    left: Sequence[EconomicDecisionFingerprint],
    right: Sequence[EconomicDecisionFingerprint],
) -> tuple[str, ...]:
    """Mismatches between two evaluation sequences, described.

    Returns an empty tuple when the two runs agree economically. A mismatch is
    reported with the differing component names rather than two opaque digests,
    because "these hashes differ" is not something anyone can act on.
    """
    problems: list[str] = []
    if len(left) != len(right):
        problems.append(f"evaluation count differs: {len(left)} vs {len(right)}")
    for index, (a, b) in enumerate(zip(left, right, strict=False)):
        if a.digest == b.digest:
            continue
        differing = a.differing_components(b)
        problems.append(f"evaluation {index} ({a.detector}) differs in: {', '.join(differing)}")
    return tuple(problems)
