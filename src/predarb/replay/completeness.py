"""Whether a replay's inputs were complete enough to draw a conclusion.

Two distinctions carry the weight here.

**Absence is not ignorance.** "No certificate observation exists in the bundle"
does not mean "the system knew no certificate existed". The first is a gap in
capture; the second is a fact the system held. Only an explicit snapshot -- "we
queried this authoritative source at ordinal N and it was empty" -- licenses the
second reading. Conflating them lets an under-captured bundle masquerade as a
confident negative result.

**Completeness is per detector.** The binary-complement detector does not care
whether relation knowledge was captured; an AT_MOST_ONE basket does. A missing
dimension that a detector never consults must not poison its conclusions, or
every absence claim degrades to the weakest dimension in the bundle.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

__all__ = [
    "BASKET_DIMENSIONS",
    "BINARY_COMPLEMENT_DIMENSIONS",
    "CompletenessDimension",
    "DimensionStatus",
    "ReplayDataCompleteness",
]


class DimensionStatus(StrEnum):
    """Why a knowledge dimension can or cannot support a conclusion."""

    COMPLETE = "COMPLETE"
    """Observed, current, and usable -- including an explicitly empty source."""

    INCOMPLETE_MISSING_OBSERVATION = "INCOMPLETE_MISSING_OBSERVATION"
    """The source was never observed. We do not know what it held."""

    INCOMPLETE_STALE_CONTEXT = "INCOMPLETE_STALE_CONTEXT"
    """Observed, but longer ago than the freshness policy allows. The venue is
    not contractually barred from changing these fields mid-session, so silence
    past the policy window is an assumption, not knowledge."""

    CORRUPT = "CORRUPT"
    """Observed but unusable -- malformed, or failing its own integrity check."""

    NOT_REQUIRED = "NOT_REQUIRED"
    """Irrelevant to the detector being judged."""

    @property
    def permits_conclusion(self) -> bool:
        return self in {DimensionStatus.COMPLETE, DimensionStatus.NOT_REQUIRED}


class CompletenessDimension(StrEnum):
    BOOK_STREAM = "BOOK_STREAM"
    LIFECYCLE = "LIFECYCLE"
    MARKET_METADATA = "MARKET_METADATA"
    FEE_KNOWLEDGE = "FEE_KNOWLEDGE"
    SETTLEMENT_KNOWLEDGE = "SETTLEMENT_KNOWLEDGE"
    RELATION_KNOWLEDGE = "RELATION_KNOWLEDGE"


BINARY_COMPLEMENT_DIMENSIONS: tuple[CompletenessDimension, ...] = (
    CompletenessDimension.BOOK_STREAM,
    CompletenessDimension.LIFECYCLE,
    CompletenessDimension.MARKET_METADATA,
    CompletenessDimension.FEE_KNOWLEDGE,
    CompletenessDimension.SETTLEMENT_KNOWLEDGE,
)
"""A same-market complement never consults relation knowledge."""

BASKET_DIMENSIONS: tuple[CompletenessDimension, ...] = (
    *BINARY_COMPLEMENT_DIMENSIONS,
    CompletenessDimension.RELATION_KNOWLEDGE,
)
"""A basket needs everything the complement needs, plus the relation."""


@dataclass(frozen=True, slots=True)
class ReplayDataCompleteness:
    """Per-dimension verdict, judged per detector."""

    dimensions: Mapping[CompletenessDimension, DimensionStatus] = field(default_factory=dict)
    reasons: Mapping[CompletenessDimension, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "dimensions", dict(sorted(self.dimensions.items())))
        object.__setattr__(self, "reasons", dict(sorted(self.reasons.items())))

    def status(self, dimension: CompletenessDimension) -> DimensionStatus:
        return self.dimensions.get(dimension, DimensionStatus.INCOMPLETE_MISSING_OBSERVATION)

    def permits_absence_claim_for(self, required: Sequence[CompletenessDimension]) -> bool:
        """Whether "no candidate existed" may be said for *this* detector."""
        return all(self.status(dimension).permits_conclusion for dimension in required)

    def blocking_dimensions_for(
        self, required: Sequence[CompletenessDimension]
    ) -> tuple[CompletenessDimension, ...]:
        return tuple(
            dimension for dimension in required if not self.status(dimension).permits_conclusion
        )

    def absence_statement(self, scope: str, required: Sequence[CompletenessDimension]) -> str:
        """The strongest sentence a caller is entitled to print."""
        blocking = self.blocking_dimensions_for(required)
        if not blocking:
            return f"No proven candidate existed within {scope}."
        detail = ", ".join(
            f"{dimension.value}={self.status(dimension).value}" for dimension in blocking
        )
        return (
            f"No candidate observed within {scope}, but replay inputs are incomplete "
            f"for definitive evaluation ({detail})."
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "dimensions": {d.value: s.value for d, s in self.dimensions.items()},
            "reasons": {d.value: list(r) for d, r in self.reasons.items() if r},
            "binary_complement_permits_absence_claim": self.permits_absence_claim_for(
                BINARY_COMPLEMENT_DIMENSIONS
            ),
            "basket_permits_absence_claim": self.permits_absence_claim_for(BASKET_DIMENSIONS),
        }

    def describe(self) -> str:
        return "; ".join(f"{d.value}={s.value}" for d, s in self.dimensions.items())
