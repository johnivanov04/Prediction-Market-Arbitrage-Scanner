"""Filing history for one product's terms, and what it can resolve.

Why a history at all
--------------------
The document served at a venue's generic contract-terms URL is the document
served today. That is not the same as the version legally in force, and it is
certainly not the same as the version a given citation was written against. A
product's terms can be amended by a later filing that the generic URL may or
may not yet reflect.

So which terms govern is a question about *filings*, not about URLs, and this
module is the place that question is answered from.

What an amendment may resolve
-----------------------------
Exactly one thing: a citation in **its own product's** terms. Kalshi amends
products one at a time -- the 2026 filings that moved contingency references
from Rule 6.3 to Rule 7.1 each name a single contract under CFTC Regulation
40.6. An amendment to the rainfall contract says nothing about the James Bond
contract, and letting one stand in for the other would be inventing legal
history.

The check is therefore deliberately narrow, and refuses rather than guesses.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Self

from predarb.semantics.dependency import (
    GoverningDocumentDependency,
    ReferenceResolution,
    reference_slug,
)

__all__ = [
    "AmendmentSearch",
    "ProductTermsVersion",
    "ProductTermsVersionHistory",
    "certification_date_from",
]


@dataclass(frozen=True, slots=True)
class ProductTermsVersion:
    """One filed version of one product's terms and conditions."""

    product_key: str
    """Stable key for the product family, e.g. the series ticker ``KXBOND``."""

    product_name: str
    """The name as filed, e.g. ``"Will <actor> be announced as the next James
    Bond?"``. Kept verbatim: it is how a filing is identified."""

    filing_date: date
    regulation: str
    """The CFTC regulation the filing was made under, e.g. ``40.2(a)`` for an
    initial listing or ``40.6`` for an amendment."""

    source_url: str
    document_sha256: str
    """Hash of the filed document. The version's identity, as always."""

    effective_date: date | None = None
    supersedes_sha256: str | None = None
    """The version this one replaces, when the filing says so. Only recorded
    from an explicit statement -- inferring supersession from dates alone would
    manufacture a chain nobody filed."""

    changed_sections: tuple[str, ...] = ()
    contingency_citations: tuple[str, ...] = ()
    """The citations this version's contingency clause actually makes, verbatim
    (e.g. ``("Rule 6.3(b)", "Rule 6.3(d)")``)."""

    note: str | None = None

    @property
    def is_amendment(self) -> bool:
        return self.regulation.startswith("40.6")

    def cites(self, reference: str) -> bool:
        target = reference_slug(reference)
        return any(reference_slug(c) == target for c in self.contingency_citations)

    def payload(self) -> dict[str, Any]:
        return {
            "product_key": self.product_key,
            "product_name": self.product_name,
            "filing_date": self.filing_date.isoformat(),
            "regulation": self.regulation,
            "source_url": self.source_url,
            "document_sha256": self.document_sha256,
            "effective_date": self.effective_date.isoformat() if self.effective_date else None,
            "supersedes_sha256": self.supersedes_sha256,
            "changed_sections": list(self.changed_sections),
            "contingency_citations": list(self.contingency_citations),
            "note": self.note,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Self:
        effective = payload.get("effective_date")
        return cls(
            product_key=str(payload["product_key"]),
            product_name=str(payload["product_name"]),
            filing_date=date.fromisoformat(str(payload["filing_date"])),
            regulation=str(payload["regulation"]),
            source_url=str(payload["source_url"]),
            document_sha256=str(payload["document_sha256"]),
            effective_date=date.fromisoformat(effective) if effective else None,
            supersedes_sha256=payload.get("supersedes_sha256"),
            changed_sections=tuple(payload.get("changed_sections", ())),
            contingency_citations=tuple(payload.get("contingency_citations", ())),
            note=payload.get("note"),
        )

    def describe(self) -> str:
        kind = "amendment" if self.is_amendment else "initial listing"
        cites = ", ".join(self.contingency_citations) or "no contingency citation"
        return (
            f"{self.filing_date.isoformat()} {kind} (Reg {self.regulation}) "
            f"{self.document_sha256[:12]} cites {cites}"
        )


@dataclass(frozen=True, slots=True)
class AmendmentSearch:
    """What a search of the filing record turned up for one product.

    Recorded as evidence in its own right. "We looked and found nothing" is a
    different, weaker statement than "there is nothing", and a much stronger one
    than never having looked -- all three have to be distinguishable later.
    """

    PRODUCT_SPECIFIC_AMENDMENT_FOUND = "PRODUCT_SPECIFIC_AMENDMENT_FOUND"
    FAMILY_AMENDMENT_FOUND = "FAMILY_AMENDMENT_FOUND"
    NO_AMENDMENT_FOUND = "NO_AMENDMENT_FOUND"
    UNKNOWN = "UNKNOWN"

    product_key: str
    outcome: str
    searched_sources: tuple[str, ...] = ()
    searched_on: date | None = None
    found: tuple[ProductTermsVersion, ...] = ()
    note: str | None = None

    def __post_init__(self) -> None:
        allowed = {
            self.PRODUCT_SPECIFIC_AMENDMENT_FOUND,
            self.FAMILY_AMENDMENT_FOUND,
            self.NO_AMENDMENT_FOUND,
            self.UNKNOWN,
        }
        if self.outcome not in allowed:
            raise ValueError(f"{self.product_key}: unknown search outcome {self.outcome!r}")
        if self.outcome == self.NO_AMENDMENT_FOUND and not self.searched_sources:
            raise ValueError(
                f"{self.product_key}: NO_AMENDMENT_FOUND must name the sources searched; "
                "otherwise it is indistinguishable from not having looked"
            )


@dataclass(frozen=True, slots=True)
class ProductTermsVersionHistory:
    """Every filed version of one product's terms, newest last."""

    product_key: str
    versions: tuple[ProductTermsVersion, ...] = ()
    search: AmendmentSearch | None = None

    def __post_init__(self) -> None:
        for version in self.versions:
            if version.product_key != self.product_key:
                raise ValueError(
                    f"{self.product_key}: version names product {version.product_key!r}; "
                    "an amendment to one product is not history for another"
                )
        object.__setattr__(
            self, "versions", tuple(sorted(self.versions, key=lambda v: v.filing_date))
        )

    @property
    def latest_filed(self) -> ProductTermsVersion | None:
        return self.versions[-1] if self.versions else None

    def amendments(self) -> tuple[ProductTermsVersion, ...]:
        return tuple(v for v in self.versions if v.is_amendment)

    def version_for_hash(self, document_sha256: str) -> ProductTermsVersion | None:
        return next((v for v in self.versions if v.document_sha256 == document_sha256), None)

    def resolution_for(
        self, dependency: GoverningDocumentDependency, *, product_key: str
    ) -> tuple[ReferenceResolution, str | None, str | None]:
        """What this history establishes about one citation.

        Returns ``(resolution, authority, resolved_reference)``. The default is
        ``UNKNOWN`` with no authority, and every path out of it requires a
        filing that names *this* product.
        """
        if product_key != self.product_key:
            # The guard that matters: a rainfall amendment resolves nothing
            # about a James Bond contract.
            return ReferenceResolution.UNKNOWN, None, None

        for amendment in reversed(self.amendments()):
            if not amendment.cites(dependency.reference) and amendment.contingency_citations:
                # The amendment replaced the contingency clause, and the old
                # citation is not in the new text: the amendment governs.
                return (
                    ReferenceResolution.REFERENCE_AMENDED_BY_PRODUCT_FILING,
                    f"{amendment.product_name} amended {amendment.filing_date.isoformat()} "
                    f"under CFTC Reg {amendment.regulation} ({amendment.source_url})",
                    amendment.contingency_citations[0],
                )
        return ReferenceResolution.UNKNOWN, None, None

    def describe(self) -> str:
        if not self.versions:
            outcome = self.search.outcome if self.search else "UNKNOWN"
            return f"{self.product_key}: no filed version recorded ({outcome})"
        lines = [f"{self.product_key}: {len(self.versions)} filed version(s)"]
        lines += [f"  {v.describe()}" for v in self.versions]
        if self.search:
            lines.append(f"  amendment search: {self.search.outcome}")
        return "\n".join(lines)


def histories_by_product(
    histories: Iterable[ProductTermsVersionHistory],
) -> Mapping[str, ProductTermsVersionHistory]:
    merged: dict[str, ProductTermsVersionHistory] = {}
    for history in histories:
        if history.product_key in merged:
            raise ValueError(f"two histories for product {history.product_key!r}")
        merged[history.product_key] = history
    return dict(sorted(merged.items()))


def versions_from(records: Sequence[Mapping[str, Any]]) -> tuple[ProductTermsVersion, ...]:
    return tuple(ProductTermsVersion.from_payload(record) for record in records)


_FILING_DATE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(\d{1,2}),?\s+(20\d{2})",
    re.IGNORECASE,
)
_MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ],
        start=1,
    )
}


def certification_date_from(text: str | None) -> date | None:
    """The date a self-certification letter was submitted, read from the letter.

    Kalshi's product certifications open with the filing date above "SUBMITTED
    VIA CFTC PORTAL". Reading it makes the issuance date objective evidence
    from the document itself, rather than something a researcher has to look up
    per product before any citation in it can be resolved.

    Only the first date in the letterhead is taken: later dates in the body are
    listing dates, expiration examples and underlying event dates.
    """
    if not text:
        return None
    head = " ".join(text[:1200].split())
    match = _FILING_DATE.search(head)
    if match is None:
        return None
    month = _MONTHS.get(match.group(1).lower())
    if month is None:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(2)))
    except ValueError:
        return None
