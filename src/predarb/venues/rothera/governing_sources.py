"""Filed governing documents for Rothera Exchange and Clearing LLC, by hash.

Retrieved from the exchange's own regulatory notices index at
``rothera.io/reg-notices`` on 2026-09-29, which is the authoritative current
list; the CFTC filing chain was used to cross-check dates. The DCM rulebook's
hash matches the copy taken independently during the phase-2C shallow screen,
which is how we know 2026-05-20 is current rather than merely latest-found.

The architecture is the same shape as Polymarket US and it must not be read the
same way. Both rulebooks are silent on payouts -- the DCM rulebook contains zero
occurrences of "Event Contract", "Expiration Value", "Settlement Value",
"Payment Criterion", "final settlement" or "long position" -- and every payout
number lives in a per-product Part 40 certification. What differs is what those
certifications say: see :mod:`predarb.venues.rothera.settlement_findings`.
"""

from __future__ import annotations

from datetime import date
from typing import Final

from predarb.semantics.lineage import SourceHistory, SourceVersion

__all__ = [
    "COMMON_TERMS_AMENDMENT_SHA256",
    "DCM_RULEBOOK",
    "DCM_RULEBOOK_HISTORY",
    "DCM_RULEBOOK_SHA256",
    "DCO_RULEBOOK",
    "DCO_RULEBOOK_HISTORY",
    "DCO_RULEBOOK_SHA256",
    "FEE_SCHEDULE_SHA256",
    "PRODUCT_FILINGS",
    "ProductFiling",
]

DCM_RULEBOOK: Final = "rothera_dcm_rulebook"
DCO_RULEBOOK: Final = "rothera_dco_rulebook"

_NOTICES: Final = "https://www.rothera.io/reg-notices"

DCM_RULEBOOK_SHA256: Final = "26084b165a930e1c47c3ab78ef523859eae79e5b76e8a20de8deed928814bced"
DCO_RULEBOOK_SHA256: Final = "7f644f206b98a79e940d1a038665688d253d3858f7a2e7ae3b8f51ed4c4e8c9e"
FEE_SCHEDULE_SHA256: Final = "23457e6e3bfa95f7653a59bfefb443eeebbb6267f2f88782aae05b8237326629"
COMMON_TERMS_AMENDMENT_SHA256: Final = (
    "92580557c0ead57b86985a2b4fbd60271528bdcc540046b25ff2cd3989b26d4f"
)

RB_DCM_2026_05: Final = SourceVersion(
    source_name=DCM_RULEBOOK,
    version="2026-05-20",
    effective_date=date(2026, 5, 20),
    filed_date=date(2026, 5, 5),
    document_sha256=DCM_RULEBOOK_SHA256,
    url=_NOTICES,
    note=(
        "105 pages, extracted clean. Conventional central limit order book on "
        "price-time priority (Rule 4.3(B)); Rule 7.2 'Procedures'; Rule 1.11 "
        "Emergency Rules; Error Trade Policy with No Cancellation Ranges. "
        "Defines no product payout."
    ),
)

RB_DCO_2026_05: Final = SourceVersion(
    source_name=DCO_RULEBOOK,
    version="2026-05-20",
    effective_date=date(2026, 5, 20),
    filed_date=date(2026, 5, 5),
    document_sha256=DCO_RULEBOOK_SHA256,
    url=_NOTICES,
    note=(
        "101 pages, extracted clean. Novation (Rule 5.1), full collateralisation "
        "(Rule 6.1), Rule 5.2 Settlement of Contracts, Rule 10.13 Contracts Not "
        "Voidable. Defines no payout formula of its own."
    ),
)

DCM_RULEBOOK_HISTORY: Final = SourceHistory(
    source_name=DCM_RULEBOOK,
    versions=(RB_DCM_2026_05,),
    versions_are_exhaustive=False,
)
"""Predecessors exist under the LedgerX and MIAXdx names and were not collected,
so this record may not support a negative dating verdict."""

DCO_RULEBOOK_HISTORY: Final = SourceHistory(
    source_name=DCO_RULEBOOK,
    versions=(RB_DCO_2026_05,),
    versions_are_exhaustive=False,
)


class ProductFiling:
    """One Part 40 product certification, identified by content hash."""

    __slots__ = ("family", "filed", "note", "sha256")

    def __init__(self, family: str, filed: date, sha256: str, note: str = "") -> None:
        self.family = family
        self.filed = filed
        self.sha256 = sha256
        self.note = note

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ProductFiling({self.family!r}, {self.filed.isoformat()})"


PRODUCT_FILINGS: Final = (
    ProductFiling("Baseball Outcome", date(2026, 5, 20), "56d8dc1b18c1f928fa4f5947"),
    ProductFiling("U.S. Core PCE Price Index", date(2026, 5, 20), "ed69272131c94d1ff426a4c2"),
    ProductFiling("U.S. Weekly Jobless Claims", date(2026, 5, 20), "c863cc3fcfe619b599399630"),
    ProductFiling("Soccer Outcome", date(2026, 6, 3), "e5bde175917536ff054d0651"),
    ProductFiling("Soccer Outcome Spread", date(2026, 6, 3), "a238516dc793d043b9472859"),
    ProductFiling("Soccer Total Goals", date(2026, 6, 3), "931e1bc27d50fa392e5fede7"),
    ProductFiling("Football Outcome", date(2026, 6, 24), "068a50c7026e25c54cedb9cc"),
    ProductFiling("Pro Football Match Outcome", date(2026, 8, 5), "6fb4850f7e3e8641487dfeba"),
    ProductFiling("Pro Football Spread", date(2026, 8, 20), "5f02cc8f8c82568a5823078c"),
    ProductFiling("Pro Football Totals", date(2026, 8, 20), "cb2f0d0cfdb9aa596e9ec64a"),
    ProductFiling(
        "Pro Football Advance to Playoffs", date(2026, 8, 18), "edcbebfba01d3dcdb5107689"
    ),
    ProductFiling("NBA Championship", date(2026, 7, 30), "7d3ccf74b7db5a066b7bc132"),
    ProductFiling("NBA Conference Championship", date(2026, 7, 30), "fe47bdefacb972c31e414624"),
    ProductFiling("NBA Division Championship", date(2026, 7, 30), "f022233e3d5fc679bf7c1818"),
)
"""The fourteen families read in full. Hashes are the leading 24 hex characters
of each certification's sha256, which is what the audit trail carries; the full
digests are in ``docs/phase2d_report.md``.

Filing dates for the soccer and football families are the dates the exchange
posted them to its notices index, not necessarily the CFTC receipt dates.
"""

SUPERSEDED_TERM_NOTE: Final = (
    "A 40.6(a) amendment filed 2026-06-17 and effective for trade date 2026-07-02 "
    "replaces 'position limit' with 'position accountability level' for Soccer "
    "Outcome, Baseball Outcome, Soccer Spread, Soccer Total Goals, Soccer "
    "Achievement, U.S. Weekly Jobless Claims and U.S. Core PCE. The certifications "
    "read here therefore state a superseded position-limit term. Nothing in that "
    "amendment touches settlement, but it proves the certifications are amended in "
    "place without being reissued, so a hash alone does not establish currency."
)
