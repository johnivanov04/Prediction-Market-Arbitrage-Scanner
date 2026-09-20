"""Venue-neutral acquisition-cost vocabulary.

A detector needs to know what a leg costs in fees without knowing which venue
computed it. These are the types that cross that boundary.

The unknown/zero distinction is carried across, deliberately
------------------------------------------------------------
:class:`FeesUnavailable` has **no monetary fields**. A venue that cannot
establish a leg's fee returns that, rather than a zero-valued interval, because
a genuinely zero fee is a real and reachable configuration. A caller cannot
accidentally add an unavailable fee into a total: there is nothing there to add.
"""

from __future__ import annotations

from dataclasses import dataclass

from predarb.domain.money import Money

__all__ = ["FeeBounds", "FeesUnavailable", "LegFees"]


@dataclass(frozen=True, slots=True)
class FeeBounds:
    """A proven interval containing a leg's true fee.

    ``lower == upper`` when the fee is exactly known, which only happens from
    real fill records. Pre-trade the interval has width, because the venue
    decides fill segmentation after the fact.
    """

    lower: Money
    upper: Money
    exact: bool
    supports_arbitrage_claim: bool
    """False when the figure rests on an unproven venue assumption. The number
    is still meaningful for triage; it just cannot carry a proof."""

    provenance: str
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.lower > self.upper:
            raise ValueError(f"fee lower bound {self.lower} exceeds upper bound {self.upper}")
        if self.lower.units < 0:
            raise ValueError(f"fees must not be negative, got {self.lower}")
        if self.exact and self.lower != self.upper:
            raise ValueError(f"fee claims to be exact but spans [{self.lower}, {self.upper}]")

    @property
    def width(self) -> Money:
        return self.upper - self.lower

    def describe(self) -> str:
        if self.exact:
            return f"{self.lower.to_str()} exact"
        return f"[{self.lower.to_str()}, {self.upper.to_str()}]"


@dataclass(frozen=True, slots=True)
class FeesUnavailable:
    """No fee figure could be established for this leg.

    Carries no amounts. The reason is the payload.
    """

    reason: str
    provenance: str
    warnings: tuple[str, ...] = ()

    @property
    def supports_arbitrage_claim(self) -> bool:
        return False

    def describe(self) -> str:
        return f"unavailable -- {self.reason}"


LegFees = FeeBounds | FeesUnavailable
"""One leg's fee result. Callers must narrow before reading any amount."""
