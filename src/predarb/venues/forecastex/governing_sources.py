"""Filed governing documents for ForecastEx LLC, captured by hash.

ForecastEx is a CFTC-designated contract market and registered DCO, affiliated
with Interactive Brokers. Its Rulebook covers both the Exchange and the
Clearinghouse in one document, so the Kalshi split between a DCM rulebook and a
separate clearing rulebook does not arise here.

Version discovery followed the CFTC filing chain rather than a guessed URL. The
current version is identified as September 1, 2026 from the public regulatory
index, and its direct URL returned HTTP 404 at retrieval time -- the same
identified-but-unread state as Kalshi Klear v1.4, and recorded the same way,
with no hash.
"""

from __future__ import annotations

from datetime import date
from typing import Final

from predarb.semantics.lineage import SourceHistory, SourceVersion

__all__ = [
    "FORECASTEX_RULEBOOK",
    "FORECASTEX_RULEBOOK_HISTORY",
    "SUCCESS_TERMS",
    "SUCCESS_TERMS_SHA256",
]

FORECASTEX_RULEBOOK: Final = "forecastex_rulebook"
SUCCESS_TERMS: Final = "forecastex_success_terms"

_CFTC: Final = "https://www.cftc.gov"

SUCCESS_TERMS_SHA256: Final = "e4315e182ef453f3eaf4846f433e7306d19ac73674a3ae4e566f805f0e5dfc7b"
"""Success Contract Terms and Conditions, filed 2025-09-16 (ptc09162530506).

The product family carrying the Participant Split and Fair Price rules, and the
one document in the corpus that states the conservation invariant expressly.
"""

RB_2025_09: Final = SourceVersion(
    source_name=FORECASTEX_RULEBOOK,
    version="2025-09-16",
    effective_date=date(2025, 9, 16),
    filed_date=date(2025, 9, 2),
    document_sha256="b8a88063e92596643e306391b54ebfe4a9071460a6e78b55457a95cf24fcab43",
    url=f"{_CFTC}/filings/orgrules/rules09022529879.pdf",
    note="70 pages, pypdf CLEAN.",
)

RB_2026_02: Final = SourceVersion(
    source_name=FORECASTEX_RULEBOOK,
    version="2026-02-17",
    effective_date=date(2026, 2, 17),
    document_sha256="b9115d921ef45d4b8d5abcae974b3788408532c42f212df45ac2a1fece863b1a",
    url=f"{_CFTC}/filings/orgrules/rules02022638613.pdf",
    note="73 pages, pypdf CLEAN.",
)

RB_2026_03: Final = SourceVersion(
    source_name=FORECASTEX_RULEBOOK,
    version="2026-03-16",
    effective_date=date(2026, 3, 16),
    document_sha256="fedeba0a1c75786a83089882e3e1502f126d3d932513e00c03697eab6b0ebf45",
    url=f"{_CFTC}/sites/default/files/filings/orgrules/26/03/rules03022640208.pdf",
    note=(
        "74 pages, pypdf CLEAN. Redline from the 2026-02-17 version; the latest "
        "version whose text was actually retrieved and hashed. Every quotation in "
        "the complement findings is read from this document."
    ),
)

RB_2026_09_IDENTIFIED: Final = SourceVersion(
    source_name=FORECASTEX_RULEBOOK,
    version="2026-09-01",
    effective_date=date(2026, 9, 1),
    document_sha256=None,
    url="https://data.forecastex.com/regulatory/ForecastEx_LLC_Rulebook.pdf",
    note=(
        "CURRENT VERSION IDENTIFIED, TEXT NOT RETRIEVED. The public regulatory index "
        "names a September 1, 2026 version; the URL returned HTTP 404 on retrieval, "
        "as did seven filename variants. No hash is recorded because the document "
        "has not been read."
    ),
)

FORECASTEX_RULEBOOK_HISTORY: Final = SourceHistory(
    source_name=FORECASTEX_RULEBOOK,
    versions=(RB_2025_09, RB_2026_02, RB_2026_03, RB_2026_09_IDENTIFIED),
    versions_are_exhaustive=False,
    record_complete_from=None,
)
"""Not exhaustive, and no completeness date.

ForecastEx files rulebook amendments frequently and the record between
2025-09 and 2026-03 was not enumerated. Under the same rule the Kalshi
histories follow, no negative verdict about a ForecastEx provision may be
stated from this record.
"""
