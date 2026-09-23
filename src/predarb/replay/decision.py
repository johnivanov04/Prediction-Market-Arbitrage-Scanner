"""One immutable envelope around every evaluation outcome.

The distinction the envelope exists to preserve:

    detector_did_run = False        we lacked the context to ask
    detector ran -> BLOCKED_...     the detector asked and said no

Both are legitimate system decisions and both belong in the record, but they are
not the same claim. Collapsing them would let an under-captured replay report
that a detector examined a market and rejected it, when in truth the detector
never ran.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from predarb.clock import ensure_utc
from predarb.replay.completeness import (
    BASKET_DIMENSIONS,
    BINARY_COMPLEMENT_DIMENSIONS,
    CompletenessDimension,
    ReplayDataCompleteness,
)
from predarb.replay.fingerprint import EconomicDecisionFingerprint

__all__ = [
    "MISSING_KNOWLEDGE",
    "DecisionRecord",
    "DetectorKind",
    "compare_decisions",
]

MISSING_KNOWLEDGE = "MISSING_POINT_IN_TIME_KNOWLEDGE"
"""Not a detector verdict. The detector never ran."""


class DetectorKind(StrEnum):
    BINARY_COMPLEMENT = "BINARY_COMPLEMENT"
    AT_MOST_ONE_BASKET = "AT_MOST_ONE_BASKET"
    AT_LEAST_ONE_BASKET = "AT_LEAST_ONE_BASKET"

    @property
    def required_dimensions(self) -> tuple[CompletenessDimension, ...]:
        return {
            DetectorKind.BINARY_COMPLEMENT: BINARY_COMPLEMENT_DIMENSIONS,
            DetectorKind.AT_MOST_ONE_BASKET: BASKET_DIMENSIONS,
            DetectorKind.AT_LEAST_ONE_BASKET: BASKET_DIMENSIONS,
        }[self]


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    """What the system decided, about what, knowing what, and on whose authority."""

    detector: DetectorKind
    trigger_ordinal: int
    trigger_reason: str
    decision_ordinal: int
    decision_time: datetime
    subjects: tuple[str, ...]
    """The market, or the basket's member set."""

    detector_did_run: bool
    classification: str
    fingerprint: EconomicDecisionFingerprint

    quantity: str | None = None
    context_ids: Mapping[str, str | None] = field(default_factory=dict)
    """Resolved context versions: metadata, fee config, certificates, evidence.

    Held so a live and a replay record can be compared component by component
    rather than only by final verdict."""

    completeness: ReplayDataCompleteness = field(default_factory=ReplayDataCompleteness)
    blocking_reason: str | None = None
    missing_knowledge: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "decision_time", ensure_utc(self.decision_time))
        object.__setattr__(self, "context_ids", dict(sorted(self.context_ids.items())))
        if self.detector_did_run and self.classification == MISSING_KNOWLEDGE:
            raise ValueError(
                f"{MISSING_KNOWLEDGE} means the detector never ran, so it cannot be "
                "the result of a detector that did"
            )
        # The converse is deliberately not an error. A decision can be
        # determinate without the detector running: knowing from an explicitly
        # empty registry that no certificate exists settles the question by
        # itself. That is knowledge, not a gap, so reporting it as
        # MISSING_POINT_IN_TIME_KNOWLEDGE would understate what the system knew.

    @property
    def permits_absence_claim(self) -> bool:
        return self.completeness.permits_absence_claim_for(self.detector.required_dimensions)

    def comparable(self) -> dict[str, object]:
        """The fields a live-vs-replay comparison is entitled to require equal.

        Excludes anything that legitimately differs between two faithful runs of
        the same history -- run ids, wall-clock duration, log text.
        """
        return {
            "detector": self.detector.value,
            "trigger_ordinal": self.trigger_ordinal,
            "trigger_reason": self.trigger_reason,
            "decision_ordinal": self.decision_ordinal,
            "decision_time": self.decision_time.isoformat(),
            "subjects": list(self.subjects),
            "quantity": self.quantity,
            "detector_did_run": self.detector_did_run,
            "classification": self.classification,
            "blocking_reason": self.blocking_reason,
            "missing_knowledge": list(self.missing_knowledge),
            "context_ids": dict(self.context_ids),
            "completeness": self.completeness.as_dict()["dimensions"],
            "fingerprint": self.fingerprint.digest,
        }

    def describe(self) -> str:
        ran = "ran" if self.detector_did_run else "not run"
        return (
            f"#{self.trigger_ordinal} {self.detector.value} {list(self.subjects)} "
            f"[{ran}] {self.classification} {self.fingerprint.short}"
        )


def compare_decisions(
    live: Sequence[DecisionRecord], replay: Sequence[DecisionRecord]
) -> tuple[str, ...]:
    """Field-level mismatches between two decision sequences.

    Reports *which field* differs rather than only that two records differ,
    because "these decisions are not equal" is not something anyone can debug.
    """
    problems: list[str] = []
    if len(live) != len(replay):
        problems.append(f"decision count differs: live {len(live)} vs replay {len(replay)}")
    for index, (a, b) in enumerate(zip(live, replay, strict=False)):
        left, right = a.comparable(), b.comparable()
        differing = sorted(k for k in set(left) | set(right) if left.get(k) != right.get(k))
        if differing:
            problems.append(
                f"decision {index} ({a.detector.value} {list(a.subjects)}): {', '.join(differing)}"
            )
    return tuple(problems)
