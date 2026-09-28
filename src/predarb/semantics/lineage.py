"""Tracing a written citation to the provision that governs it now.

The problem this solves
-----------------------
A contract cites "Rule 6.3(b)". Today Rule 6.3(b) is the Scalar Contract rule.
Two very different things could be true:

* the citation was **exact when written**, and the provision it named has since
  been pushed along by amendments that inserted subsections above it; or
* the citation was **already wrong** on the day the product was certified.

The first is resolvable and the second is not, and they must never be conflated.
Both of those are real cases in this corpus: BOND (certified 2025-01-17, under
Rulebook v1.14, where 6.3(b) *was* the indeterminate-outcome payout provision)
and CRIMECHARGE (certified 2025-07-24, months after the scalar amendment had
already made 6.3(b) the Scalar Contract rule).

What a lineage is, and is not
-----------------------------
It is **not** auto-renumbering. Every hop names the amendment that moved the
provision, with that amendment's own hash, effective date, and the language
relied on. A lineage with a hop nobody can evidence is not a lineage; it
resolves to ``UNKNOWN`` and blocks.

Nor is it prose matching. "The payout-determination wording now appears at (c),
so (b) must have become (c)" is exactly the reasoning this module refuses.
Movement is established by the amendment that performed it.

Gaps are the hard part
----------------------
A chain of hops can be individually sound and still wrong, if an amendment
nobody recorded moved the provision again. So a lineage is complete only when
the amendment record it was checked against covers the whole window from
issuance to today. :class:`SourceHistory` holds that record, and
:meth:`ReferenceLineage.validate` refuses a chain with a window it cannot
account for.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any, Self

from predarb.semantics.dependency import ReferenceResolution, reference_slug

__all__ = [
    "HopBasis",
    "LineageHop",
    "ReferenceLineage",
    "SourceAmendment",
    "SourceHistory",
    "SourceVersion",
]


class HopBasis(StrEnum):
    """What authority establishes one hop.

    Ordered from strongest to weakest. Nothing weaker than ``VERSION_BRACKET``
    is admissible, and in particular there is no member for "the prose looks
    like it moved".
    """

    TRACKED_AMENDMENT = "TRACKED_AMENDMENT"
    """A filed amendment carrying tracked changes that perform the movement."""

    AMENDMENT_STATEMENT = "AMENDMENT_STATEMENT"
    """A filed amendment stating in words what it changes and leaves alone
    (e.g. "[Existing subsections (a) through (d) unchanged.]")."""

    VERSION_BRACKET = "VERSION_BRACKET"
    """Two official, filed versions of the source bracket the window, and the
    provision's text is identical in both at the stated subsections.

    Admissible only for establishing that a provision did **not** move. It can
    close a window; it can never be the authority for a movement, because a
    movement has an amendment and that amendment is what governs.
    """


@dataclass(frozen=True, slots=True)
class SourceVersion:
    """One filed, official version of a governing source."""

    source_name: str
    version: str
    effective_date: date
    document_sha256: str
    url: str
    filed_date: date | None = None
    note: str | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "source_name": self.source_name,
            "version": self.version,
            "effective_date": self.effective_date.isoformat(),
            "document_sha256": self.document_sha256,
            "url": self.url,
            "filed_date": self.filed_date.isoformat() if self.filed_date else None,
            "note": self.note,
        }

    def describe(self) -> str:
        return (
            f"v{self.version} effective {self.effective_date.isoformat()} "
            f"{self.document_sha256[:12]}"
        )


@dataclass(frozen=True, slots=True)
class SourceAmendment:
    """One filed amendment to a governing source."""

    source_name: str
    amendment_id: str
    url: str
    document_sha256: str
    effective_date: date
    affects: tuple[str, ...]
    """Rule numbers this amendment touches, e.g. ``("Rule 6.3",)``. Read from
    the amendment itself, not guessed from a diff."""

    filed_date: date | None = None
    version_before: str | None = None
    version_after: str | None = None
    carries_tracked_changes: bool = False
    evidence: str | None = None
    """The amendment's own words, quoted, for the change being relied on."""

    def affects_rule(self, reference: str) -> bool:
        """Whether this amendment touches the rule a citation names.

        Matched on the rule number, ignoring the subsection: an amendment to
        Rule 6.3 can renumber 6.3(b) without ever naming "6.3(b)".
        """
        target = _rule_number(reference)
        return any(_rule_number(affected) == target for affected in self.affects)

    def payload(self) -> dict[str, Any]:
        return {
            "source_name": self.source_name,
            "amendment_id": self.amendment_id,
            "url": self.url,
            "document_sha256": self.document_sha256,
            "effective_date": self.effective_date.isoformat(),
            "affects": list(self.affects),
            "filed_date": self.filed_date.isoformat() if self.filed_date else None,
            "version_before": self.version_before,
            "version_after": self.version_after,
            "carries_tracked_changes": self.carries_tracked_changes,
            "evidence": self.evidence,
        }

    def describe(self) -> str:
        span = (
            f" v{self.version_before}->v{self.version_after}"
            if self.version_before and self.version_after
            else ""
        )
        return (
            f"{self.amendment_id}{span} effective {self.effective_date.isoformat()} "
            f"{self.document_sha256[:12]} affects {', '.join(self.affects)}"
        )


def _rule_number(reference: str) -> str:
    """``"Rule 6.3(b)"`` and ``"RULE 6.3"`` both reduce to ``"6-3"``."""
    slug = reference_slug(reference).removeprefix("rule-")
    # Drop a trailing single-letter subsection: "6-3-b" -> "6-3".
    parts = slug.split("-")
    if len(parts) > 1 and len(parts[-1]) == 1 and parts[-1].isalpha():
        parts = parts[:-1]
    return "-".join(parts)


@dataclass(frozen=True, slots=True)
class SourceHistory:
    """The filed version and amendment record for one governing source."""

    source_name: str
    versions: tuple[SourceVersion, ...] = ()
    amendments: tuple[SourceAmendment, ...] = ()
    versions_are_exhaustive: bool = False
    """Whether every filed version is recorded here, or only samples.

    Load-bearing for *negative* verdicts. Saying "Rule 3.13 did not exist when
    this was written" requires holding the version that was actually in force;
    with a sampled record the nearest held version may be a year stale, and the
    honest answer is that we do not know. Positive verdicts survive sampling,
    because a heading identical across every recorded version bracketing the
    window is evidence in its own right.
    """

    record_complete_from: date | None = None
    """The date from which the amendment record is believed exhaustive.

    Recorded honestly: a lineage starting before this date cannot claim to have
    accounted for every amendment, however sound each of its hops looks.
    """

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "versions", tuple(sorted(self.versions, key=lambda v: v.effective_date))
        )
        object.__setattr__(
            self, "amendments", tuple(sorted(self.amendments, key=lambda a: a.effective_date))
        )

    @property
    def current_version(self) -> SourceVersion | None:
        return self.versions[-1] if self.versions else None

    def version_at(self, moment: date) -> SourceVersion | None:
        """The latest *recorded* version effective at or before ``moment``."""
        applicable = [v for v in self.versions if v.effective_date <= moment]
        return applicable[-1] if applicable else None

    def version_at_is_certain(self, moment: date) -> bool:
        """Whether :meth:`version_at` is the version that was really in force.

        Certain when the record is exhaustive, or when the recorded version
        effective at ``moment`` is immediately followed by a recorded version
        effective after it -- leaving no room for an unrecorded edition in
        between only if the record is exhaustive. With a sampled record we can
        never be sure, so this is simply the exhaustiveness flag.
        """
        return self.versions_are_exhaustive and self.version_at(moment) is not None

    def versions_spanning(self, start: date) -> tuple[SourceVersion, ...]:
        """Every recorded version from the one in force at ``start`` onward."""
        at_start = self.version_at(start)
        if at_start is None:
            return self.versions
        return tuple(v for v in self.versions if v.effective_date >= at_start.effective_date)

    def amendments_affecting(
        self, reference: str, *, after: date, upto: date | None = None
    ) -> tuple[SourceAmendment, ...]:
        """Amendments touching ``reference``'s rule, effective in the window."""
        return tuple(
            amendment
            for amendment in self.amendments
            if amendment.affects_rule(reference)
            and amendment.effective_date > after
            and (upto is None or amendment.effective_date <= upto)
        )

    def describe(self) -> str:
        lines = [f"{self.source_name}: {len(self.versions)} version(s)"]
        lines += [f"  {v.describe()}" for v in self.versions]
        lines += [f"  amendment {a.describe()}" for a in self.amendments]
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class LineageHop:
    """One evidenced movement -- or evidenced non-movement -- of a provision."""

    from_reference: str
    to_reference: str
    basis: HopBasis
    effective_date: date
    amendment_id: str | None = None
    amendment_url: str | None = None
    amendment_sha256: str | None = None
    version_before: str | None = None
    version_after: str | None = None
    evidence: str | None = None

    def __post_init__(self) -> None:
        amendment_based = self.basis in {
            HopBasis.TRACKED_AMENDMENT,
            HopBasis.AMENDMENT_STATEMENT,
        }
        if amendment_based and not (self.amendment_id and self.amendment_sha256):
            raise ValueError(
                f"{self.from_reference} -> {self.to_reference}: an amendment-based hop "
                "must name the amendment and its hash"
            )
        if self.basis is HopBasis.VERSION_BRACKET and self.moved:
            raise ValueError(
                f"{self.from_reference} -> {self.to_reference}: a version bracket cannot "
                "establish a movement; the amendment that performed it is the authority"
            )
        if not (self.evidence or "").strip():
            raise ValueError(
                f"{self.from_reference} -> {self.to_reference}: every hop must quote the "
                "language it relies on"
            )

    @property
    def moved(self) -> bool:
        return reference_slug(self.from_reference) != reference_slug(self.to_reference)

    def payload(self) -> dict[str, Any]:
        return {
            "from_reference": self.from_reference,
            "to_reference": self.to_reference,
            "basis": self.basis.value,
            "effective_date": self.effective_date.isoformat(),
            "amendment_id": self.amendment_id,
            "amendment_url": self.amendment_url,
            "amendment_sha256": self.amendment_sha256,
            "version_before": self.version_before,
            "version_after": self.version_after,
            "evidence": self.evidence,
        }

    def describe(self) -> str:
        arrow = "->" if self.moved else "=="
        return (
            f"{self.from_reference} {arrow} {self.to_reference} "
            f"[{self.basis.value} {self.amendment_id or 'version bracket'} "
            f"eff {self.effective_date.isoformat()}]"
        )


@dataclass(frozen=True, slots=True)
class ReferenceLineage:
    """A citation, what it pointed at when written, and what it points at now."""

    source_document: str
    citation_as_written: str
    source_name: str
    issuance_date: date

    issuance_version: str | None = None
    issuance_version_sha256: str | None = None
    issuance_target: str | None = None
    """What the cited subsection actually said in the version in force at
    issuance, quoted or summarised from that version."""

    issuance_reference_valid: bool = False
    """Whether the citation identified the provision the citing sentence relies
    on, *on the day the product was certified*. This is the fork in the road."""

    hops: tuple[LineageHop, ...] = ()
    current_reference: str | None = None
    current_version: str | None = None
    note: str | None = None

    def chain_is_continuous(self) -> tuple[bool, str | None]:
        """Whether the hops form an unbroken chain from the written citation."""
        expected = reference_slug(self.citation_as_written)
        for hop in self.hops:
            if reference_slug(hop.from_reference) != expected:
                return False, (f"hop starts at {hop.from_reference!r}, expected {expected!r}")
            expected = reference_slug(hop.to_reference)
        if self.current_reference and reference_slug(self.current_reference) != expected:
            return False, (
                f"chain ends at {expected!r}, but the current reference is "
                f"{self.current_reference!r}"
            )
        return True, None

    def missing_windows(self, history: SourceHistory) -> tuple[str, ...]:
        """Amendments that touched this rule and are not accounted for by a hop.

        This is the check that makes a lineage trustworthy rather than merely
        plausible. Each amendment affecting the rule since issuance must appear
        as a hop -- as a movement, or as an explicit non-movement.
        """
        problems: list[str] = []
        if history.record_complete_from is None:
            problems.append("the amendment record has no stated completeness date")
        elif history.record_complete_from > self.issuance_date:
            problems.append(
                f"the amendment record is only complete from "
                f"{history.record_complete_from.isoformat()}, after issuance on "
                f"{self.issuance_date.isoformat()}"
            )
        accounted = {hop.amendment_id for hop in self.hops if hop.amendment_id}
        for amendment in history.amendments_affecting(
            self.citation_as_written, after=self.issuance_date
        ):
            if amendment.amendment_id not in accounted:
                problems.append(
                    f"amendment {amendment.amendment_id} (effective "
                    f"{amendment.effective_date.isoformat()}) touched "
                    f"{', '.join(amendment.affects)} and is unaccounted for"
                )
        return tuple(problems)

    def resolution(self, history: SourceHistory) -> tuple[ReferenceResolution, str | None]:
        """The resolution this lineage supports, and the authority for it.

        Fails closed at every step: a citation that was wrong at issuance is
        never repaired, a broken chain resolves to nothing, and an
        unaccounted-for amendment window resolves to nothing.
        """
        if not self.issuance_reference_valid:
            return (
                ReferenceResolution.BROKEN_REFERENCE_AT_ISSUANCE,
                (
                    f"{self.citation_as_written} did not identify the relied-on provision "
                    f"in {self.source_name} v{self.issuance_version}, the version in force "
                    f"on {self.issuance_date.isoformat()}"
                    + (f": {self.issuance_target}" if self.issuance_target else "")
                ),
            )
        continuous, problem = self.chain_is_continuous()
        if not continuous:
            return ReferenceResolution.UNKNOWN, f"lineage chain is broken: {problem}"
        gaps = self.missing_windows(history)
        if gaps:
            return ReferenceResolution.UNKNOWN, "lineage has gaps: " + "; ".join(gaps)
        if not self.hops:
            return (
                ReferenceResolution.EXACT_CURRENT_REFERENCE,
                (
                    f"{self.citation_as_written} was exact at issuance in "
                    f"{self.source_name} v{self.issuance_version} and no amendment "
                    "affecting that rule has taken effect since"
                ),
            )
        authority = "; ".join(hop.describe() for hop in self.hops)
        if not any(hop.moved for hop in self.hops):
            return (
                ReferenceResolution.EXACT_CURRENT_REFERENCE,
                f"exact at issuance and unmoved since: {authority}",
            )
        return (
            ReferenceResolution.HISTORICAL_REFERENCE_RESOLVED,
            f"lineage from {self.citation_as_written} to {self.current_reference}: {authority}",
        )

    def payload(self) -> dict[str, Any]:
        return {
            "source_document": self.source_document,
            "citation_as_written": self.citation_as_written,
            "source_name": self.source_name,
            "issuance_date": self.issuance_date.isoformat(),
            "issuance_version": self.issuance_version,
            "issuance_version_sha256": self.issuance_version_sha256,
            "issuance_target": self.issuance_target,
            "issuance_reference_valid": self.issuance_reference_valid,
            "hops": [hop.payload() for hop in self.hops],
            "current_reference": self.current_reference,
            "current_version": self.current_version,
            "note": self.note,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Self:
        return cls(
            source_document=str(payload["source_document"]),
            citation_as_written=str(payload["citation_as_written"]),
            source_name=str(payload["source_name"]),
            issuance_date=date.fromisoformat(str(payload["issuance_date"])),
            issuance_version=payload.get("issuance_version"),
            issuance_version_sha256=payload.get("issuance_version_sha256"),
            issuance_target=payload.get("issuance_target"),
            issuance_reference_valid=bool(payload.get("issuance_reference_valid", False)),
            hops=tuple(
                LineageHop(
                    from_reference=str(h["from_reference"]),
                    to_reference=str(h["to_reference"]),
                    basis=HopBasis(h["basis"]),
                    effective_date=date.fromisoformat(str(h["effective_date"])),
                    amendment_id=h.get("amendment_id"),
                    amendment_url=h.get("amendment_url"),
                    amendment_sha256=h.get("amendment_sha256"),
                    version_before=h.get("version_before"),
                    version_after=h.get("version_after"),
                    evidence=h.get("evidence"),
                )
                for h in payload.get("hops", ())
            ),
            current_reference=payload.get("current_reference"),
            current_version=payload.get("current_version"),
            note=payload.get("note"),
        )

    def describe(self) -> str:
        head = (
            f"{self.source_document}: {self.citation_as_written} @ "
            f"{self.issuance_date.isoformat()} (v{self.issuance_version}, "
            f"{'valid' if self.issuance_reference_valid else 'NOT VALID'} at issuance)"
        )
        return "\n".join([head, *(f"    {hop.describe()}" for hop in self.hops)])


def lineages_by_citation(
    lineages: Iterable[ReferenceLineage],
) -> Mapping[str, ReferenceLineage]:
    merged: dict[str, ReferenceLineage] = {}
    for lineage in lineages:
        key = reference_slug(lineage.citation_as_written)
        if key in merged:
            raise ValueError(f"two lineages for citation {lineage.citation_as_written!r}")
        merged[key] = lineage
    return dict(sorted(merged.items()))


def histories_by_source(histories: Sequence[SourceHistory]) -> Mapping[str, SourceHistory]:
    return {history.source_name: history for history in histories}
