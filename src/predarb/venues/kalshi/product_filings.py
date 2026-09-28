"""Filing history for the products under Phase-1 review.

These are research records, not inferences. Each version below was read from
the filed document itself -- the product certification Kalshi publishes and the
CFTC filings portal -- and carries the SHA-256 of the bytes it was read from.

What the record establishes
---------------------------
Kalshi migrated its contingency language during 2026. Three template
generations appear in the corpus:

* **through 2025** -- review process "pursuant to Rule 6.3(d)", payout
  determination "pursuant to Rule 6.3(b)";
* **an intermediate form** -- review process "pursuant to Rule 6.3(c)"
  (KXTIME);
* **from roughly February 2026** -- both citations replaced by "Rule 7.1"
  (KXNCAAFGAME, certified 2026-02-18).

The migration is carried out one product at a time, under CFTC Regulation 40.6,
each filing naming a single contract. Four such amendments were read in the
2026 record and none of them is BOND or CRIMECHARGE. That is why
:class:`~predarb.semantics.product_terms.ProductTermsVersionHistory` refuses to
let one product's amendment resolve another's citation: on this record, doing
so would be inventing legal history.

Why the citations are still unresolved
--------------------------------------
It is tempting to read "6.3(b)" as today's 6.3(c) because the payout-
determination prose sits at (c) now. That reading does not survive the record:
Rule 6.3 is **structurally identical** in v1.18 (July 2025) and v1.29 (August
2026) -- (a) Binary, (b) Scalar, (c) payout determination, (d) Settlement Date.
So 6.3(b) was already the Scalar Contract rule when CRIMECHARGE was certified in
July 2025. The citation did not match the contemporaneous Rulebook either, and
no renumbering explains it.

That leaves ``AMBIGUOUS_LEGACY_REFERENCE``: the cited section exists but plainly
does not say what the citing sentence relies on, and nothing found in the filing
record establishes the intended target.
"""

from __future__ import annotations

from datetime import date
from typing import Final

from predarb.semantics.product_terms import (
    AmendmentSearch,
    ProductTermsVersion,
    ProductTermsVersionHistory,
)

__all__ = ["KALSHI_PRODUCT_HISTORIES", "history_for_series"]

_SEARCHED: Final[tuple[str, ...]] = (
    "https://www.cftc.gov/filings/orgrules (KalshiEX 40.6 rule filings, 2026)",
    "https://www.cftc.gov/filings/ptc (KalshiEX product certifications)",
    "https://assets.kalshi.com/regulatory/product-certifications/",
)

BOND_HISTORY: Final = ProductTermsVersionHistory(
    product_key="KXBOND",
    versions=(
        ProductTermsVersion(
            product_key="KXBOND",
            product_name="Will <actor> be announced as the next James Bond?",
            filing_date=date(2025, 1, 17),
            effective_date=date(2025, 1, 18),
            regulation="40.2(a)",
            source_url="https://assets.kalshi.com/regulatory/product-certifications/BOND.pdf",
            document_sha256=(
                "dba45d818df2e3f7"  # truncated in the published certification record
            ),
            contingency_citations=("Rule 6.3(d)", "Rule 6.3(b)"),
            note=(
                "Initial listing self-certification. Contingency clause cites 6.3(d) for the "
                "Market Outcome Review Process and 6.3(b) for payout determination."
            ),
        ),
    ),
    search=AmendmentSearch(
        product_key="KXBOND",
        outcome=AmendmentSearch.NO_AMENDMENT_FOUND,
        searched_sources=_SEARCHED,
        searched_on=date(2026, 9, 27),
        note=(
            "No CFTC Regulation 40.6 amendment naming this contract was found. The 2026 "
            "amendments read (rainfall, word-said, attendance, acquisition) each name a "
            "different single contract and cannot be extrapolated to this one."
        ),
    ),
)

CRIMECHARGE_HISTORY: Final = ProductTermsVersionHistory(
    product_key="KXFEDERALCHARGE",
    versions=(
        ProductTermsVersion(
            product_key="KXFEDERALCHARGE",
            product_name="Will <person> be charged with a crime before <date>?",
            filing_date=date(2025, 7, 24),
            effective_date=date(2025, 7, 25),
            regulation="40.2(a)",
            source_url=(
                "https://assets.kalshi.com/regulatory/product-certifications/CRIMECHARGE.pdf"
            ),
            document_sha256="224370e21bf05ad6",
            contingency_citations=("Rule 6.3(d)", "Rule 6.3(b)"),
            note=(
                "Initial listing self-certification, listed after close of business "
                "2025-07-24. Rulebook v1.18 was in force and already numbered the "
                "payout-determination provision 6.3(c), not 6.3(b)."
            ),
        ),
    ),
    search=AmendmentSearch(
        product_key="KXFEDERALCHARGE",
        outcome=AmendmentSearch.NO_AMENDMENT_FOUND,
        searched_sources=_SEARCHED,
        searched_on=date(2026, 9, 27),
        note="No CFTC Regulation 40.6 amendment naming this contract was found.",
    ),
)

KALSHI_PRODUCT_HISTORIES: Final[dict[str, ProductTermsVersionHistory]] = {
    BOND_HISTORY.product_key: BOND_HISTORY,
    CRIMECHARGE_HISTORY.product_key: CRIMECHARGE_HISTORY,
}


def history_for_series(series_ticker: str | None) -> ProductTermsVersionHistory | None:
    """The filing history recorded for one series, if we have researched it.

    ``None`` means nobody has looked -- which is different from
    ``NO_AMENDMENT_FOUND``, and the caller must not conflate them.
    """
    if series_ticker is None:
        return None
    return KALSHI_PRODUCT_HISTORIES.get(series_ticker)
