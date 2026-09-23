"""Do these markets' documented strike intervals tile the domain without gaps?

A research helper, and nothing more. It answers one narrow, mechanical question
about numbers, and the answer *supports* a human proof of AT_LEAST_ONE without
ever completing one.

What it reads
-------------
Only the documented structured strike fields (A-53):

    strike_type   greater | greater_or_equal | less | less_or_equal | between
    floor_strike  "Minimum expiration value that leads to a YES settlement"
    cap_strike    "Maximum expiration value that leads to a YES settlement"

Never the title. "Above 70 degrees" in a market title is a sentence written for
humans; deriving a settlement threshold from it would be inferring contract
semantics from marketing copy, and a single reworded title would silently change
what we thought the market paid on.

``functional`` and ``custom`` strikes are refused outright. Their documented
descriptions -- "mapping from expiration values to settlement values", and
"expiration value for each target" -- describe opaque structures with no
documented grammar, so nothing can be concluded from them mechanically.

What gap-free coverage does and does not prove
----------------------------------------------
It shows that *if* the underlying resolves to some real number in the domain,
at least one member's condition is satisfied. That is a real contribution.

It leaves untouched every other route to ALL-NO:

* the underlying may be undefined, unpublished or disputed;
* the event may be cancelled, postponed or voided;
* the domain itself comes from the contract, not from the strikes -- an
  unbounded tail is only "covered" if the contract says the value cannot go
  there;
* boundary conventions are the contract's, not arithmetic's. Whether
  ``X < 70`` and ``X >= 70`` genuinely meet at 70 depends on the rounding and
  tick rules that decide what the expiration value *is*.

So this module reports coverage, lists what it had to assume, and issues
nothing. A reviewer reads the result; the result never reads itself.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final

from predarb.clock import ensure_utc
from predarb.semantics.fingerprint import ABSENT, SettlementEvidenceFingerprint

PARTITION_SCHEMA_VERSION: Final = "strike-partition/1"
DOMAIN_CONSTRAINT_SCHEMA_VERSION: Final = "domain-constraint/1"

__all__ = [
    "DOMAIN_CONSTRAINT_SCHEMA_VERSION",
    "PARTITION_SCHEMA_VERSION",
    "CoverageStatus",
    "DomainBound",
    "DomainConstraintEvidence",
    "IntervalCoverage",
    "StrikeInterval",
    "UnsupportedStrikeError",
    "analyse_coverage",
    "interval_from_strike",
]


class UnsupportedStrikeError(ValueError):
    """The strike semantics are not documented well enough to be read."""


class CoverageStatus(StrEnum):
    """What the intervals do over the claimed domain."""

    GAP_FREE = "GAP_FREE"
    """Every value in the domain satisfies at least one member, with the domain
    established by evidence. Supports an AT_LEAST_ONE argument; does not
    complete one."""

    GAP_FREE_IF_DOMAIN_DISCRETE = "GAP_FREE_IF_DOMAIN_DISCRETE"
    """A **conditional** result, and deliberately not ``GAP_FREE``.

    The intervals tile the domain *if* the settlement value moves in steps of
    :attr:`IntervalCoverage.required_domain_step`, and leave real gaps
    otherwise. Kalshi temperature buckets are exactly this: ``66-67`` then
    ``68-69`` is a partition of whole degrees and has a hole at 67.5 over the
    reals.

    Which is true is a fact about the contract, not about arithmetic, so this
    status carries the condition rather than discharging it. Only
    :class:`DomainConstraintEvidence` -- authoritative source text, hashed,
    acknowledged by a reviewer -- turns it into ``GAP_FREE``."""

    GAP = "GAP"
    """At least one value satisfies no member -- a direct ALL-NO path."""

    UNBOUNDED_LOWER_TAIL_UNCOVERED = "UNBOUNDED_LOWER_TAIL_UNCOVERED"
    UNBOUNDED_UPPER_TAIL_UNCOVERED = "UNBOUNDED_UPPER_TAIL_UNCOVERED"
    """The members stop short of a domain edge the contract has not closed."""

    NOT_APPLICABLE = "NOT_APPLICABLE"
    """These members are not numeric intervals at all."""

    MALFORMED = "MALFORMED"
    """A strike is internally contradictory, e.g. a floor above its cap."""

    @property
    def supports_exhaustiveness_argument(self) -> bool:
        """Never "proves". The wording is the point.

        ``GAP_FREE_IF_DOMAIN_DISCRETE`` is excluded: its condition has not been
        discharged, and a conditional result that answered yes here would let a
        command-line flag stand in for contract evidence.
        """
        return self is CoverageStatus.GAP_FREE


@dataclass(frozen=True, slots=True)
class StrikeInterval:
    """One member's YES condition as a half-open-aware numeric interval.

    ``lower``/``upper`` of ``None`` mean unbounded on that side. The inclusivity
    flags are kept rather than normalised: whether ``X < 70`` and ``X >= 70``
    meet exactly at 70 is the difference between a partition and a gap of
    measure zero that the contract may or may not close.
    """

    ticker: str
    lower: Decimal | None
    upper: Decimal | None
    lower_inclusive: bool
    upper_inclusive: bool
    strike_type: str

    def __post_init__(self) -> None:
        if self.lower is not None and self.upper is not None and self.lower > self.upper:
            raise UnsupportedStrikeError(
                f"{self.ticker}: floor {self.lower} exceeds cap {self.upper}"
            )

    def contains(self, value: Decimal) -> bool:
        if self.lower is not None and (
            value < self.lower or (value == self.lower and not self.lower_inclusive)
        ):
            return False
        return not (
            self.upper is not None
            and (value > self.upper or (value == self.upper and not self.upper_inclusive))
        )

    def describe(self) -> str:
        left = (
            "(-inf" if self.lower is None else f"{'[' if self.lower_inclusive else '('}{self.lower}"
        )
        right = (
            "inf)" if self.upper is None else f"{self.upper}{']' if self.upper_inclusive else ')'}"
        )
        return f"{self.ticker} {left}, {right} [{self.strike_type}]"


def interval_from_strike(
    *,
    ticker: str,
    strike_type: str | None,
    floor_strike: Decimal | None,
    cap_strike: Decimal | None,
) -> StrikeInterval:
    """Read one market's documented strike fields as an interval.

    Boundary inclusivity comes from ``strike_type``, which documents it
    explicitly: ``greater`` is exclusive, ``greater_or_equal`` is inclusive. For
    ``between`` the documentation gives the endpoints as the minimum and maximum
    values "that lead to a YES settlement", so both ends are inclusive.
    """
    if not strike_type:
        raise UnsupportedStrikeError(f"{ticker}: no strike_type; nothing documented to read")

    kind = strike_type.strip().lower()
    if kind in {"functional", "custom", "structured"}:
        raise UnsupportedStrikeError(
            f"{ticker}: strike_type {kind!r} has no documented grammar, so its "
            "settlement condition cannot be read mechanically"
        )

    if kind in {"greater", "greater_or_equal"}:
        if floor_strike is None:
            raise UnsupportedStrikeError(f"{ticker}: {kind} strike without floor_strike")
        return StrikeInterval(
            ticker=ticker,
            lower=floor_strike,
            upper=None,
            lower_inclusive=kind == "greater_or_equal",
            upper_inclusive=False,
            strike_type=kind,
        )
    if kind in {"less", "less_or_equal"}:
        if cap_strike is None:
            raise UnsupportedStrikeError(f"{ticker}: {kind} strike without cap_strike")
        return StrikeInterval(
            ticker=ticker,
            lower=None,
            upper=cap_strike,
            lower_inclusive=False,
            upper_inclusive=kind == "less_or_equal",
            strike_type=kind,
        )
    if kind == "between":
        if floor_strike is None or cap_strike is None:
            raise UnsupportedStrikeError(
                f"{ticker}: between strike needs both floor_strike and cap_strike"
            )
        return StrikeInterval(
            ticker=ticker,
            lower=floor_strike,
            upper=cap_strike,
            lower_inclusive=True,
            upper_inclusive=True,
            strike_type=kind,
        )
    raise UnsupportedStrikeError(f"{ticker}: unrecognised strike_type {strike_type!r}")


@dataclass(frozen=True, slots=True)
class IntervalCoverage:
    """What the intervals do, and what had to be assumed to say so."""

    status: CoverageStatus
    intervals: tuple[StrikeInterval, ...]
    gaps: tuple[str, ...] = ()
    overlaps: tuple[str, ...] = ()
    """Recorded but never disqualifying. Overlap is irrelevant to AT_LEAST_ONE
    -- two members both settling YES is permitted -- and only matters to a
    separate AT_MOST_ONE review."""

    assumptions: tuple[str, ...] = ()
    unreadable: tuple[str, ...] = ()
    detail: str = ""
    required_domain_step: Decimal | None = None
    """The step the conditional result depends on, when the status is
    ``GAP_FREE_IF_DOMAIN_DISCRETE``. Carried so a reviewer sees the exact
    condition they would be discharging."""

    unconditional_gaps: tuple[str, ...] = ()
    """The gaps that exist over a continuous domain, preserved even when a step
    closes them. A conditional result must not erase the thing it is
    conditional on."""

    domain_evidence: DomainConstraintEvidence | None = None

    @property
    def supports_exhaustiveness_argument(self) -> bool:
        return self.status.supports_exhaustiveness_argument and not self.unreadable

    def fingerprint(self) -> SettlementEvidenceFingerprint:
        """Digest over the intervals **and the domain evidence they rely on**.

        Both halves matter. A certificate relying on a partition must go stale
        when a member's strike moves -- the same shape reached from different
        numbers is a different proof. It must equally go stale when the source
        that established the domain changes, because a partition over whole
        degrees stops being one the moment the contract admits half degrees.
        """
        return SettlementEvidenceFingerprint.over(
            {
                "partition.status": self.status.value,
                "partition.required_domain_step": (
                    str(self.required_domain_step)
                    if self.required_domain_step is not None
                    else ABSENT
                ),
                **(
                    self.domain_evidence.component_values()
                    if self.domain_evidence is not None
                    else {"domain.evidence": ABSENT}
                ),
                "partition.intervals": [
                    [
                        interval.ticker,
                        interval.strike_type,
                        str(interval.lower) if interval.lower is not None else ABSENT,
                        str(interval.upper) if interval.upper is not None else ABSENT,
                        interval.lower_inclusive,
                        interval.upper_inclusive,
                    ]
                    for interval in self.intervals
                ],
                "partition.unreadable": list(self.unreadable),
            },
            schema_version=PARTITION_SCHEMA_VERSION,
        )

    def describe(self) -> str:
        return (
            f"{self.status.value} over {len(self.intervals)} interval(s)"
            + (f"; gaps: {'; '.join(self.gaps)}" if self.gaps else "")
            + (f"; unreadable: {', '.join(self.unreadable)}" if self.unreadable else "")
        )


@dataclass(frozen=True, slots=True)
class DomainConstraintEvidence:
    """Authoritative evidence that the settlement domain is what it is claimed to be.

    The distinction this type exists to enforce:

        the system may show    "IF the domain is integer-valued, these tile it"
        only evidence shows    "the domain IS integer-valued"

    A CLI flag is not evidence. Neither is a market title saying "whole
    degrees", a strike rendered without decimals, or a history of integer
    settlements -- the first is marketing copy, the second is formatting, and
    the third is a sample. All three have been observed to look convincing.

    So an assertion carries its source: which document or rules component says
    it, that source's content hash, and a named human who read it. When the
    source moves, the hash moves, and any certificate that relied on the
    assertion goes stale.
    """

    variable: str
    """What is being constrained, e.g. "daily high temperature"."""

    units: str
    """The units the step is expressed in, e.g. "degrees Fahrenheit"."""

    step: Decimal | None = None
    """Smallest increment the settlement value can take. ``None`` leaves the
    domain continuous, which is the conservative reading."""

    lower: Decimal | None = None
    upper: Decimal | None = None

    source_component_ids: tuple[str, ...] = ()
    """Which evidence components carry the language, e.g.
    ``("market.rules_primary", "document.contract_terms")``."""

    source_hashes: Mapping[str, str] = field(default_factory=dict)
    """Component id -> content hash. What makes the assertion revocable."""

    quoted_language: str = ""
    """The sentence actually relied on, verbatim, so a later reader can judge it
    without re-fetching."""

    rationale: str = ""
    acknowledged_by: str = ""
    """The human who read the source and put their name to this reading."""

    acknowledged_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.acknowledged_at is not None:
            object.__setattr__(self, "acknowledged_at", ensure_utc(self.acknowledged_at))
        object.__setattr__(self, "source_hashes", dict(sorted(self.source_hashes.items())))

    @property
    def is_attested(self) -> bool:
        """Whether this is evidence rather than an assertion in evidence shape.

        Requires a source component, that source's hash, the language relied on,
        and a named reviewer. Any of those missing means somebody typed a number
        and nobody checked it.
        """
        return bool(
            self.source_component_ids
            and self.source_hashes
            and self.quoted_language
            and self.acknowledged_by
        )

    def blocking_reason(self) -> str | None:
        """Why this assertion cannot be relied on, or ``None`` if it can."""
        missing: list[str] = []
        if not self.source_component_ids:
            missing.append("no source component named")
        if not self.source_hashes:
            missing.append("no source content hash")
        if not self.quoted_language:
            missing.append("no quoted language")
        if not self.acknowledged_by:
            missing.append("no reviewer acknowledgement")
        if not missing:
            return None
        return (
            f"domain constraint on {self.variable!r} is asserted but not attested "
            f"({'; '.join(missing)}); a domain assertion without a source is an "
            "assumption, not evidence"
        )

    def component_values(self) -> dict[str, Any]:
        """The material inputs. Bound into any certificate that relies on this."""
        return {
            "domain.variable": self.variable,
            "domain.units": self.units,
            "domain.step": str(self.step) if self.step is not None else ABSENT,
            "domain.lower": str(self.lower) if self.lower is not None else ABSENT,
            "domain.upper": str(self.upper) if self.upper is not None else ABSENT,
            "domain.source_component_ids": sorted(self.source_component_ids),
            "domain.source_hashes": dict(self.source_hashes),
            "domain.quoted_language": self.quoted_language,
        }

    def fingerprint(self) -> SettlementEvidenceFingerprint:
        return SettlementEvidenceFingerprint.over(
            self.component_values(), schema_version=DOMAIN_CONSTRAINT_SCHEMA_VERSION
        )

    def describe(self) -> str:
        state = "attested" if self.is_attested else "UNATTESTED"
        return (
            f"{self.variable} in {self.units}, step {self.step} [{state}] "
            f"by {self.acknowledged_by or '(nobody)'} from "
            f"{sorted(self.source_component_ids)}"
        )


@dataclass(frozen=True, slots=True)
class DomainBound:
    """Limits and granularity the *contract* places on the underlying.

    Never discovered, never defaulted. ``evidence`` is what separates a
    conditional mathematical result from a discharged one: without an attested
    :class:`DomainConstraintEvidence`, a supplied step yields
    ``GAP_FREE_IF_DOMAIN_DISCRETE`` and the real-domain gaps are preserved in
    the result.
    """

    lower: Decimal | None = None
    upper: Decimal | None = None
    step: Decimal | None = None
    evidence: DomainConstraintEvidence | None = None
    justification: str = ""

    @property
    def effective_step(self) -> Decimal | None:
        """The step, from evidence when present and from the caller otherwise."""
        if self.evidence is not None and self.evidence.step is not None:
            return self.evidence.step
        return self.step

    @property
    def step_is_attested(self) -> bool:
        return self.evidence is not None and self.evidence.is_attested

    @property
    def bounds_are_attested(self) -> bool:
        return self.evidence is not None and self.evidence.is_attested


def _contiguous(
    covered_to: Decimal,
    covered_inclusive: bool,
    interval: StrikeInterval,
    step: Decimal | None,
) -> bool:
    """Whether ``interval`` starts where coverage already reaches.

    With a ``step``, two buckets are contiguous when no admissible value lies
    strictly between them -- ``[66, 67]`` then ``[68, 69]`` on whole degrees
    leaves nowhere for the underlying to land, so it is not a gap.
    """
    assert interval.lower is not None
    if interval.lower < covered_to:
        return True
    if interval.lower == covered_to:
        return covered_inclusive or interval.lower_inclusive
    if step is None or step <= 0:
        return False
    # The first admissible value above the covered region, and the last below
    # the next one. If they cross, nothing can fall in between.
    first_uncovered = covered_to if not covered_inclusive else covered_to + step
    last_before_next = interval.lower if not interval.lower_inclusive else interval.lower - step
    return first_uncovered > last_before_next


def _sweep(
    ordered: Sequence[StrikeInterval],
    step: Decimal | None = None,
) -> tuple[Decimal | None, bool, list[str], list[str]]:
    """Walk the sorted intervals, reporting where coverage breaks.

    Returns how far coverage reaches, whether that endpoint is included, and
    the gaps and overlaps found. ``None`` for the reach means coverage runs to
    positive infinity.
    """
    gaps: list[str] = []
    overlaps: list[str] = []
    first = ordered[0]
    covered_to: Decimal | None = first.upper
    covered_inclusive = first.upper_inclusive

    for index, interval in enumerate(ordered[1:], start=1):
        previous = ordered[index - 1]
        if covered_to is None:
            overlaps.append(f"{interval.ticker} lies inside an already-unbounded region")
            continue
        if interval.lower is None:
            overlaps.append(f"{interval.ticker} extends below the swept region")
            covered_to, covered_inclusive = interval.upper, interval.upper_inclusive
            continue
        touching = _contiguous(covered_to, covered_inclusive, interval, step)
        if not touching:
            gaps.append(
                f"({covered_to}, {interval.lower}) uncovered between "
                f"{previous.ticker} and {interval.ticker}"
            )
        elif interval.lower < covered_to:
            overlaps.append(f"{interval.ticker} overlaps below {covered_to}")
        if interval.upper is None:
            covered_to, covered_inclusive = None, True
        elif interval.upper > covered_to or (
            interval.upper == covered_to and interval.upper_inclusive
        ):
            covered_to, covered_inclusive = interval.upper, interval.upper_inclusive
    return covered_to, covered_inclusive, gaps, overlaps


def analyse_coverage(
    intervals: Sequence[StrikeInterval],
    *,
    domain: DomainBound | None = None,
    unreadable: Sequence[str] = (),
) -> IntervalCoverage:
    """Check whether these intervals leave any value uncovered.

    ``domain`` closes the tails. Without it, a set that stops short on either
    side is reported as an uncovered tail rather than as a partition: a reviewer
    must say why the underlying cannot go there.
    """
    if not intervals:
        return IntervalCoverage(
            status=CoverageStatus.NOT_APPLICABLE,
            intervals=(),
            unreadable=tuple(unreadable),
            detail="no readable numeric strike intervals",
        )

    ordered = sorted(
        intervals,
        key=lambda i: (
            i.lower is not None,
            i.lower if i.lower is not None else Decimal(0),
            not i.lower_inclusive,
        ),
    )
    assumptions: list[str] = []
    gaps: list[str] = []
    overlaps: list[str] = []
    step = domain.effective_step if domain else None
    step_attested = domain.step_is_attested if domain else False

    domain_lower = domain.lower if domain else None
    domain_upper = domain.upper if domain else None
    if domain and domain.justification:
        assumptions.append(f"domain bounds asserted by reviewer: {domain.justification}")
    if step is not None:
        assumptions.append(
            f"settlement value asserted to move in steps of {step}; without that "
            "assertion the boundaries between adjacent buckets are real gaps"
        )
    if domain is not None and domain.evidence is not None:
        assumptions.append(f"domain constraint evidence: {domain.evidence.describe()}")

    # Lower tail.
    first = ordered[0]
    if first.lower is not None and (domain_lower is None or first.lower > domain_lower):
        if domain_lower is None:
            return IntervalCoverage(
                status=CoverageStatus.UNBOUNDED_LOWER_TAIL_UNCOVERED,
                intervals=tuple(ordered),
                unreadable=tuple(unreadable),
                assumptions=tuple(assumptions),
                detail=(
                    f"nothing covers values below {first.lower}, and no contract "
                    "bound was supplied to say the underlying cannot go there"
                ),
            )
        gaps.append(f"[{domain_lower}, {first.lower}) uncovered")

    # Sweep: each interval must start where the covered region already reaches.
    covered_to, covered_inclusive, sweep_gaps, sweep_overlaps = _sweep(ordered, step)
    gaps.extend(sweep_gaps)
    overlaps.extend(sweep_overlaps)
    # The same sweep with no step, so the result can always state what the gaps
    # would be over a continuous domain -- a conditional answer must keep the
    # condition visible rather than replacing it.
    _, _, continuous_gaps, _ = _sweep(ordered, None) if step is not None else (None, None, [], None)

    # Upper tail.
    if covered_to is not None:
        if domain_upper is None:
            return IntervalCoverage(
                status=CoverageStatus.UNBOUNDED_UPPER_TAIL_UNCOVERED,
                intervals=tuple(ordered),
                gaps=tuple(gaps),
                overlaps=tuple(overlaps),
                unreadable=tuple(unreadable),
                assumptions=tuple(assumptions),
                detail=(
                    f"nothing covers values above {covered_to}, and no contract "
                    "bound was supplied to say the underlying cannot go there"
                ),
            )
        if covered_to < domain_upper or (covered_to == domain_upper and not covered_inclusive):
            gaps.append(f"({covered_to}, {domain_upper}] uncovered")

    if gaps:
        return IntervalCoverage(
            status=CoverageStatus.GAP,
            intervals=tuple(ordered),
            gaps=tuple(gaps),
            overlaps=tuple(overlaps),
            unreadable=tuple(unreadable),
            assumptions=tuple(assumptions),
            unconditional_gaps=tuple(gaps),
            domain_evidence=domain.evidence if domain else None,
            detail="at least one value satisfies no member, which is a direct ALL-NO path",
        )

    assumptions.append(
        "boundary meetings are read from strike_type inclusivity; whether the "
        "expiration value can actually land exactly on a boundary is a contract "
        "and tick-size question this helper does not answer"
    )

    # A step was needed to close the gaps. Whether that discharges them or
    # merely names a condition depends entirely on whether the step is attested.
    if step is not None and continuous_gaps and not step_attested:
        reason = (
            domain.evidence.blocking_reason()
            if domain is not None and domain.evidence is not None
            else "no domain constraint evidence was supplied at all"
        )
        return IntervalCoverage(
            status=CoverageStatus.GAP_FREE_IF_DOMAIN_DISCRETE,
            intervals=tuple(ordered),
            gaps=(),
            overlaps=tuple(overlaps),
            unreadable=tuple(unreadable),
            assumptions=tuple(assumptions),
            required_domain_step=step,
            unconditional_gaps=tuple(continuous_gaps),
            domain_evidence=domain.evidence if domain else None,
            detail=(
                f"these intervals tile the domain only if the settlement value moves "
                f"in steps of {step}; over a continuous domain they leave "
                f"{len(continuous_gaps)} gap(s). That condition is not discharged: "
                f"{reason}"
            ),
        )

    return IntervalCoverage(
        status=CoverageStatus.GAP_FREE,
        intervals=tuple(ordered),
        overlaps=tuple(overlaps),
        unreadable=tuple(unreadable),
        assumptions=tuple(assumptions),
        required_domain_step=step,
        unconditional_gaps=tuple(continuous_gaps),
        domain_evidence=domain.evidence if domain else None,
        detail=(
            "every value in the established domain satisfies at least one member; "
            "this supports an AT_LEAST_ONE argument and does not complete one, "
            "because cancellation, void and an undefined underlying remain open"
        ),
    )
