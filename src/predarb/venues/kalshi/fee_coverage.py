"""Census of what the fee engine can price, with denominators that add up.

Two separate questions, deliberately not merged into one percentage:

1. **Can we compute a fee at all?** Only ``quadratic`` has an established taker
   rate here, so everything else is uncomputable.
2. **Can that fee support a contractual-arbitrage claim?** A computable fee
   still cannot, if it depends on the unresolved ``fee_multiplier`` mapping
   (A-14). "Priceable" and "safe to build a riskless claim on" are different
   properties, and collapsing them overstates coverage.

The other trap is the denominator. ``/series`` is **not** exhaustive -- 23
perpetual-futures series are reachable by ticker but never listed (A-42) -- so a
census must say which universe it counted. :class:`CoverageCensus` refuses to
hold counts that do not sum to its own total, which is what stops an
inconsistent summary being printed.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from predarb.domain.enums import FeeType
from predarb.venues.kalshi.fee_model import SUPPORTED_TAKER_FEE_TYPES

__all__ = [
    "ArbEligibility",
    "CombinedCoverage",
    "CoverageCensus",
    "classify_fee_configuration",
]


class ArbEligibility(StrEnum):
    """What a series' fee metadata permits."""

    BOUNDABLE_FOR_ARB = "BOUNDABLE_FOR_ARB"
    """Computable, and bounded through no unproven assumption. ``quadratic``
    with ``fee_multiplier = 1``."""

    CALCULABLE_ONLY = "CALCULABLE_ONLY"
    """A number can be produced, but it rests on the unresolved maker/taker
    column mapping for ``fee_multiplier`` (A-14). Reportable as a hypothetical;
    never load-bearing for a riskless claim."""

    BLOCKED_UNSUPPORTED_TYPE = "BLOCKED_UNSUPPORTED_TYPE"
    """No established taker formula. Nothing is computed at all."""

    @property
    def is_calculable(self) -> bool:
        return self is not ArbEligibility.BLOCKED_UNSUPPORTED_TYPE

    @property
    def supports_arbitrage_claim(self) -> bool:
        return self is ArbEligibility.BOUNDABLE_FOR_ARB


def classify_fee_configuration(
    fee_type_raw: str | None, multiplier: Decimal | None
) -> ArbEligibility:
    """Classify one series' fee metadata. Fails closed on anything unknown."""
    if fee_type_raw is None or multiplier is None:
        return ArbEligibility.BLOCKED_UNSUPPORTED_TYPE
    try:
        fee_type = FeeType(fee_type_raw)
    except ValueError:
        return ArbEligibility.BLOCKED_UNSUPPORTED_TYPE
    if fee_type not in SUPPORTED_TAKER_FEE_TYPES:
        return ArbEligibility.BLOCKED_UNSUPPORTED_TYPE
    if multiplier != 1:
        return ArbEligibility.CALCULABLE_ONLY
    return ArbEligibility.BOUNDABLE_FOR_ARB


@dataclass(frozen=True, slots=True)
class CoverageCensus:
    """Counts over one clearly named universe of series.

    ``universe`` is part of the data, not a caption. A census of the ``/series``
    listing and a census of everything addressable are different populations,
    and a reader who cannot tell which they are looking at will misread the
    percentage.
    """

    universe: str
    total: int
    by_state: dict[ArbEligibility, int]
    by_fee_type: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.total < 0:
            raise ValueError(f"{self.universe}: total must not be negative, got {self.total}")
        state_sum = sum(self.by_state.values())
        if state_sum != self.total:
            raise ValueError(
                f"{self.universe}: eligibility counts sum to {state_sum} but total is "
                f"{self.total}; a census whose parts do not add up cannot be reported"
            )
        if self.by_fee_type:
            type_sum = sum(self.by_fee_type.values())
            if type_sum != self.total:
                raise ValueError(
                    f"{self.universe}: fee-type counts sum to {type_sum} but total is {self.total}"
                )

    @classmethod
    def from_rows(
        cls, universe: str, rows: Sequence[tuple[str | None, Decimal | None]]
    ) -> CoverageCensus:
        """Build from ``(fee_type, multiplier)`` pairs, one per series."""
        states: Counter[ArbEligibility] = Counter()
        types: Counter[str] = Counter()
        for fee_type, multiplier in rows:
            states[classify_fee_configuration(fee_type, multiplier)] += 1
            types[fee_type or "<missing>"] += 1
        return cls(
            universe=universe,
            total=len(rows),
            by_state={state: states.get(state, 0) for state in ArbEligibility},
            by_fee_type=dict(types.most_common()),
        )

    @property
    def calculable(self) -> int:
        return sum(count for state, count in self.by_state.items() if state.is_calculable)

    @property
    def boundable_for_arb(self) -> int:
        return self.by_state.get(ArbEligibility.BOUNDABLE_FOR_ARB, 0)

    @property
    def blocked(self) -> int:
        return self.total - self.boundable_for_arb

    def fraction(self, count: int) -> Decimal | None:
        """A share of *this* census's total, or ``None`` when it is empty."""
        if self.total == 0:
            return None
        return (Decimal(count) / Decimal(self.total)).quantize(Decimal("0.0001"))


@dataclass(frozen=True, slots=True)
class CombinedCoverage:
    """Several censuses plus an explicit account of how they were assembled.

    Combining is allowed only alongside ``assembly``, which must say how the
    universe was built and that it may still be incomplete. A bare combined
    percentage is the thing this type exists to prevent.
    """

    censuses: tuple[CoverageCensus, ...]
    assembly: str

    def __post_init__(self) -> None:
        if not self.assembly.strip():
            raise ValueError(
                "a combined coverage figure requires an explicit description of how "
                "the universe was assembled; the listing is known to be non-exhaustive"
            )
        names = [census.universe for census in self.censuses]
        if len(set(names)) != len(names):
            raise ValueError(f"census universes must be distinct, got {names}")

    @property
    def total(self) -> int:
        return sum(census.total for census in self.censuses)

    @property
    def boundable_for_arb(self) -> int:
        return sum(census.boundable_for_arb for census in self.censuses)

    @property
    def calculable(self) -> int:
        return sum(census.calculable for census in self.censuses)

    @property
    def blocked(self) -> int:
        return self.total - self.boundable_for_arb

    def fraction_boundable(self) -> Decimal | None:
        if self.total == 0:
            return None
        return (Decimal(self.boundable_for_arb) / Decimal(self.total)).quantize(Decimal("0.0001"))

    def describe(self) -> str:
        parts = [f"{c.universe}: {c.boundable_for_arb}/{c.total} boundable" for c in self.censuses]
        return (
            f"{'; '.join(parts)}; combined {self.boundable_for_arb}/{self.total}. {self.assembly}"
        )
