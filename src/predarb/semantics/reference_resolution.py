"""Deciding, from objective evidence, what a citation points at.

Two kinds of citation, two kinds of evidence
--------------------------------------------
**A whole-rule citation** -- "Rule 7.1" -- is resolved by *heading stability*.
If the version in force when the document was written and the version in force
now both put the same heading at that number, and no recorded amendment
renumbered it, then the citation identifies the same provision it always did.
That is an objective, checkable fact, and a human should not have to declare
that Rule 7.1 means Rule 7.1.

**A subsection citation** -- "Rule 6.3(b)" -- is not. Rule 6.3 is headed
SETTLEMENT in every version ever filed, while its subsections have been shuffled
twice by amendments that inserted new ones above them. Heading stability says
nothing here, so a subsection citation is resolved only by an explicit
:class:`~predarb.semantics.lineage.ReferenceLineage` with an amendment behind
every hop.

Fail-closed is preserved
------------------------
Automatic resolution can only conclude ``EXACT_CURRENT_REFERENCE`` when the
evidence is genuinely exact, and ``BROKEN_REFERENCE`` when the number is simply
absent. Everything else returns ``UNKNOWN``, which blocks. Nothing here can
mark a reference procedural, or downgrade materiality; only a human declaration
does that.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

from predarb.semantics.dependency import ReferenceResolution, reference_slug
from predarb.semantics.lineage import ReferenceLineage, SourceHistory

__all__ = [
    "ResolutionOutcome",
    "heading_for_rule",
    "headings_from_text",
    "resolve_reference",
    "rule_number_of",
    "subsection_of",
]

_RULE_PATTERN = re.compile(r"rule\s*([\d]+(?:\.[\d]+)*)\s*(?:\(\s*([a-z0-9]+)\s*\))?", re.I)


def rule_number_of(reference: str) -> str | None:
    """``"Rule 6.3(b)"`` -> ``"6.3"``. ``None`` if it names no rule number."""
    match = _RULE_PATTERN.search(reference)
    return match.group(1) if match else None


def subsection_of(reference: str) -> str | None:
    """``"Rule 6.3(b)"`` -> ``"b"``; ``"Rule 7.1"`` -> ``None``."""
    match = _RULE_PATTERN.search(reference)
    return match.group(2).lower() if match and match.group(2) else None


def heading_for_rule(text: str, rule_number: str) -> str | None:
    """The heading printed at ``RULE <number>`` in a rulebook's text.

    Reads the last occurrence, because a filing typically carries a table of
    contents first and the body afterwards, and the body is the rule.
    """
    pattern = re.compile(rf"RULE\s+{re.escape(rule_number)}\b([^\n]*)")
    matches = pattern.findall(text)
    if not matches:
        return None
    for candidate in reversed(matches):
        # A table-of-contents line ends in a page number; the body heading does not.
        heading = re.sub(r"\s*\.*\s*\d+\s*$", "", candidate).strip()
        if heading:
            return " ".join(heading.split()).upper()
    return None


@dataclass(frozen=True, slots=True)
class ResolutionOutcome:
    """What the evidence supports for one citation, and why."""

    resolution: ReferenceResolution
    authority: str | None = None
    resolved_reference: str | None = None
    current_text_excerpt: str | None = None

    @property
    def is_resolved(self) -> bool:
        return self.resolution.is_resolved


def headings_from_text(text: str) -> dict[str, str]:
    """Every ``RULE <number> <HEADING>`` a rulebook's text carries."""
    numbers = {m.group(1) for m in re.finditer(r"RULE\s+(\d+\.\d+)\b", text)}
    found = {number: heading_for_rule(text, number) for number in numbers}
    return {number: heading for number, heading in found.items() if heading}


def resolve_reference(
    reference: str,
    *,
    source_name: str,
    issued_on: date | None = None,
    history: SourceHistory | None = None,
    issuance_headings: Mapping[str, str] | None = None,
    current_headings: Mapping[str, str] | None = None,
    source_held: bool = False,
    lineages: Mapping[str, ReferenceLineage] | None = None,
) -> ResolutionOutcome:
    """Resolve one citation from whatever objective evidence exists.

    An explicit lineage wins where one is recorded, because a subsection
    citation can only be established that way. Otherwise a whole-rule citation
    is checked for heading stability across the two bracketing versions.
    """
    if lineages:
        lineage = lineages.get(reference_slug(reference))
        if lineage is not None and history is not None:
            resolution, authority = lineage.resolution(history)
            return ResolutionOutcome(
                resolution=resolution,
                authority=authority,
                resolved_reference=(lineage.current_reference if resolution.is_resolved else None),
            )
    return _resolve_numbered(
        reference,
        source_name=source_name,
        issued_on=issued_on,
        history=history,
        issuance_headings=issuance_headings,
        current_headings=current_headings,
        source_held=source_held,
    )


def _resolve_numbered(
    reference: str,
    *,
    source_name: str,
    issued_on: date | None,
    history: SourceHistory | None,
    issuance_headings: Mapping[str, str] | None,
    current_headings: Mapping[str, str] | None,
    source_held: bool,
) -> ResolutionOutcome:
    """The part that needs a rule number, split out to keep each branch legible."""
    number = rule_number_of(reference)
    if number is None:
        # A bare "the Rulebook" incorporates the whole document; there is no
        # section to resolve, and holding the document is the whole question.
        return ResolutionOutcome(
            resolution=(
                ReferenceResolution.EXACT_CURRENT_REFERENCE
                if source_held
                else ReferenceResolution.UNKNOWN
            ),
            authority=(
                f"whole-document incorporation of {source_name}; the current text is held"
                if source_held
                else None
            ),
        )

    if current_headings is None:
        return ResolutionOutcome(ReferenceResolution.UNKNOWN)

    current_heading = current_headings.get(number)
    if current_heading is None:
        return ResolutionOutcome(
            resolution=ReferenceResolution.BROKEN_REFERENCE,
            authority=f"no RULE {number} appears in the current {source_name}",
        )

    renumbering = (
        history.amendments_affecting(reference, after=issued_on)
        if history is not None and issued_on is not None
        else ()
    )
    # Three ways heading stability cannot settle this, all resolving to UNKNOWN
    # so an explicit lineage has to carry it:
    #   * a subsection citation -- Rule 6.3 is headed SETTLEMENT in every
    #     version ever filed while its subsections moved twice;
    #   * a recorded amendment touched the rule since issuance;
    #   * we do not hold the version in force at issuance to compare against.
    if subsection_of(reference) is not None or renumbering or issuance_headings is None:
        return ResolutionOutcome(
            resolution=ReferenceResolution.UNKNOWN,
            current_text_excerpt=f"RULE {number} {current_heading}",
        )

    return _compare_headings(
        number,
        source_name=source_name,
        issuance_heading=issuance_headings.get(number),
        current_heading=current_heading,
        record_is_exhaustive=(history.versions_are_exhaustive if history is not None else False),
    )


def _compare_headings(
    number: str,
    *,
    source_name: str,
    issuance_heading: str | None,
    current_heading: str,
    record_is_exhaustive: bool,
) -> ResolutionOutcome:
    """Heading stability across the bracketing official versions.

    A *negative* verdict -- the rule did not exist, or the number was reused --
    is only stated when the version record is exhaustive. With a sampled record
    the nearest held version can be a year stale, and asserting that a rule did
    not exist on that basis would be claiming to know something we do not.
    """
    disagrees = issuance_heading is None or issuance_heading != current_heading
    if disagrees and not record_is_exhaustive:
        return ResolutionOutcome(
            resolution=ReferenceResolution.UNKNOWN,
            authority=(
                f"RULE {number} reads {current_heading!r} now and differs in the nearest "
                f"recorded earlier {source_name}, but the version record is sampled rather "
                "than exhaustive, so the edition in force when the citing document was "
                "written was not compared"
            ),
            current_text_excerpt=f"RULE {number} {current_heading}",
        )
    if issuance_heading is None:
        return ResolutionOutcome(
            resolution=ReferenceResolution.BROKEN_REFERENCE_AT_ISSUANCE,
            authority=(
                f"no RULE {number} appeared in {source_name} as it stood when the "
                "citing document was written"
            ),
        )
    if issuance_heading != current_heading:
        return ResolutionOutcome(
            resolution=ReferenceResolution.AMBIGUOUS_LEGACY_REFERENCE,
            authority=(
                f"RULE {number} was {issuance_heading!r} at issuance and is "
                f"{current_heading!r} now; the number was reused for a different rule"
            ),
            current_text_excerpt=f"RULE {number} {current_heading}",
        )
    return ResolutionOutcome(
        resolution=ReferenceResolution.EXACT_CURRENT_REFERENCE,
        authority=(
            f"RULE {number} is headed {current_heading!r} both in the {source_name} in "
            "force when the citing document was written and in the current one, and no "
            "recorded amendment renumbered it"
        ),
        current_text_excerpt=f"RULE {number} {current_heading}",
    )
