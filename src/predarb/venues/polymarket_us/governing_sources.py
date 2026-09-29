"""Filed governing documents for QCX LLC d/b/a Polymarket US, captured by hash.

Polymarket US is a CFTC-designated contract market whose clearing house, QC
Clearing LLC, is a registered DCO with its own rulebook -- the Kalshi split,
not the ForecastEx single document.

The split matters more here than it did at either earlier venue, because of
what the DCM rulebook turns out *not* to contain. It defines the Contract
Outcome as $1.00 or $0.00 and stops; every number a holder is actually paid
lives in a per-product Part 40 certification, and Rule 1.5 makes those
certifications govern *notwithstanding any provision of the Rules to the
contrary*. A screen run against the rulebook alone therefore reads as clean for
a reason that has nothing to do with the venue being clean. See
:mod:`predarb.venues.polymarket_us.settlement_findings`.

Currency caveat, recorded rather than resolved: the clearing rulebook retrieved
is dated 2025-12-31, four months older than the DCM rulebook, and the athletic
terms are the 2025-09-30 certification, the most recent athletic filing located.
A later amendment to either may exist and was not found.
"""

from __future__ import annotations

from datetime import date
from typing import Final

from predarb.semantics.lineage import SourceHistory, SourceVersion

__all__ = [
    "AEC_TERMS",
    "AEC_TERMS_SHA256",
    "CAOC_TERMS",
    "CAOC_TERMS_SHA256",
    "PMUS_CLEARING_RULEBOOK",
    "PMUS_CLEARING_RULEBOOK_HISTORY",
    "PMUS_RULEBOOK",
    "PMUS_RULEBOOK_HISTORY",
]

PMUS_RULEBOOK: Final = "polymarket_us_rulebook"
PMUS_CLEARING_RULEBOOK: Final = "polymarket_us_clearing_rulebook"
AEC_TERMS: Final = "polymarket_us_athletic_event_terms"
CAOC_TERMS: Final = "polymarket_us_combinatoric_athletic_terms"

_CFTC: Final = "https://www.cftc.gov"

AEC_TERMS_SHA256: Final = "50014b0f643953c8b1851783245cadd4adcbd1c08ebf2f0baa7807a4d5c33cf3"
"""Athletic Event Contracts, Rule 9.101, self-certified 2025-09-30.

The document that decides the venue. Section D adds a $0.50/$0.50 tie state to
what the rulebook calls a binary contract, and Section K permits settlement at
last-traded prices or "other fair and equitable valuation" on cancellation.
"""

CAOC_TERMS_SHA256: Final = "94a0b293e5e57359db042b50c0b5326d868b4799cc25cf59a2d2bd86a4856faa"
"""Combinatoric Athletic Outcome Contract, self-certified 2026-05-20.

States its Payout Condition as an explicit biconditional over its legs -- the
only unambiguous logical AND found at any venue in phases 1 and 2 -- while
modelling those same legs as settling "$1.00/$0.00".
"""

RB_2026_08: Final = SourceVersion(
    source_name=PMUS_RULEBOOK,
    version="2026-08-05",
    effective_date=date(2026, 8, 5),
    filed_date=None,
    document_sha256="5e3ba3880e63ffb1cfb649fbe2fb7ad305b85ee382ff194721689f2bec4d696d",
    url="https://polymarketexchange.com/files/legal/",
    note="84 pages, extracted clean. Defines Contract Outcome, Settlement Amount, "
    "Rule 1.5 precedence, Rule 10.3 modification and Rule 10.4 Contract Outcome "
    "Review. Contains no payout arithmetic for any product.",
)

CLEARING_2025_12: Final = SourceVersion(
    source_name=PMUS_CLEARING_RULEBOOK,
    version="2025-12-31",
    effective_date=date(2025, 12, 31),
    filed_date=date(2025, 12, 31),
    document_sha256="a5a91041b08f8e1e06c662a62c25fb06ccb6d4eee3c5bf2f3f58aca927ad1fe6",
    url=f"{_CFTC}/filings/orgrules/rules12312536031.pdf",
    note="60 pages, extracted clean. Novation, full collateralisation, and "
    "offset restricted to Contracts with the same terms and conditions. "
    "Defines no payout of its own; Rule 5.3(d) pays per the Contract Rules.",
)

PMUS_RULEBOOK_HISTORY: Final = SourceHistory(
    source_name=PMUS_RULEBOOK,
    versions=(RB_2026_08,),
    versions_are_exhaustive=False,
)
"""One retrieved version. Not exhaustive: no prior DCM rulebook was collected,
so this history may not be used to support a negative dating verdict."""

PMUS_CLEARING_RULEBOOK_HISTORY: Final = SourceHistory(
    source_name=PMUS_CLEARING_RULEBOOK,
    versions=(CLEARING_2025_12,),
    versions_are_exhaustive=False,
)
"""Likewise one version, and it predates the DCM rulebook by four months."""
