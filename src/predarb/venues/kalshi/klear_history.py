"""Filed version record for the Kalshi Klear DCO Rulebook.

Discovered through the CFTC clearing-organization filing chain rather than a
direct S3 guess. Two stale URLs returning 404 proved only that those URLs were
stale; the rulebook itself is on the public record, and v1.4 is identified.

The v1.4 problem, stated precisely
----------------------------------
v1.4 is **identified but its clean text is not publicly retrievable**. The
April 20, 2026 certification requests confidential treatment and encloses the
amendments "in temporarily confidential form"; only the cover letter is public.
So the text of v1.4 has not been read and is not claimed to have been.

What the public letter does establish, in its own words, is what changed and
what did not:

    "These changes do not alter the economics, risk, or operation of any
    currently cleared Contract. Rather, they create the definitional and
    structural framework necessary for the Clearing House to accommodate new
    products in future filings."

    "All existing full collateralization requirements for Fully Collateralized
    Contracts remain in place and unchanged."

That is an authoritative statement, by the filer, that the economics of
currently cleared contracts are unchanged from v1.3 -- whose full text *is*
public. So v1.3's settlement and collateral mechanics can be relied on for
contracts trading today, and the parts of v1.4 that remain unread are the parts
the letter says are new framework for future products: new definitions
distinguishing collateralization requirements, Rule 3.2C participant
eligibility, a Chapter 7 framework for products that are **not** fully cash
collateralized, and a Chapter 12 default-management framework.

That last pair is worth noticing rather than passing over: the exchange is
building the scaffolding for contracts that are not fully cash collateralized.
Any reasoning that leans on full collateralization is reasoning with a shelf
life.
"""

from __future__ import annotations

from datetime import date
from typing import Final

from predarb.semantics.lineage import SourceAmendment, SourceHistory, SourceVersion

__all__ = ["KLEAR_DCO_RULEBOOK", "KLEAR_RULEBOOK_HISTORY", "V14_UNREAD_CHAPTERS"]

KLEAR_DCO_RULEBOOK: Final = "klear_dco_rulebook"

_CFTC = "https://www.cftc.gov"

V100: Final = SourceVersion(
    source_name=KLEAR_DCO_RULEBOOK,
    version="1.00",
    effective_date=date(2024, 6, 4),
    filed_date=date(2024, 6, 4),
    document_sha256="e49d9a470ec7f2ff5a9661d52a9ab93fe963dc1d0aff4be136031a3f1a5fe0d5",
    url=f"{_CFTC}/media/10821/Kalshi%20Klear%20LLC%20DCO%20Application%20Exhibit%20A-2-Rulebook/download",
    note=(
        "Exhibit A-2 to the DCO registration application. 79 pages, pypdf CLEAN. "
        "The earliest public full text."
    ),
)

V12: Final = SourceVersion(
    source_name=KLEAR_DCO_RULEBOOK,
    version="1.2",
    effective_date=date(2025, 5, 16),
    filed_date=date(2025, 5, 2),
    document_sha256="71467487898d475b45d7aca39c49050481be6327d71dbcf81e3a28d96368297c",
    url=f"{_CFTC}/sites/default/files/filings/orgrules/25/05/rules05022520613.pdf",
    note=(
        "Joint KalshiEx/Kalshi Klear notice under CFTC Reg 40.6(a), carrying a "
        "working copy of v1.2 based on v1.1. 155 pages, pypdf CLEAN."
    ),
)

V13: Final = SourceVersion(
    source_name=KLEAR_DCO_RULEBOOK,
    version="1.3",
    effective_date=date(2025, 7, 4),
    filed_date=date(2025, 7, 4),
    document_sha256="59e85eb6c635b28a9f5baa2589f859e1e17cc7faa988fd0bb38248f6759832f9",
    url=f"{_CFTC}/sites/default/files/filings/orgrules/25/07/rules07042525566.pdf",
    note=(
        "162 pages, pypdf CLEAN. The latest publicly readable full text, and the "
        "one whose settlement and collateral mechanics current contracts run on."
    ),
)

V14_AMENDMENT: Final = SourceAmendment(
    source_name=KLEAR_DCO_RULEBOOK,
    amendment_id="rules0420261686",
    url=f"{_CFTC}/filings/orgrules/rules0420261686.pdf",
    document_sha256="6e5d6de9511643ee2b58641ebda005d86865eaf0307f779fed962109ca976420",
    filed_date=date(2026, 4, 20),
    effective_date=date(2026, 4, 27),
    version_before="1.3",
    version_after="1.4",
    carries_tracked_changes=False,
    affects=("Rule 3.2C", "Chapter 7", "Chapter 12", "Rule 2.4F", "Rule 2.8", "Rule 7.1D"),
    evidence=(
        "Kalshi Klear LLC \u2013 Rule Amendment and Conforming Changes, self-certified "
        "under CFTC Reg 40.6(a), effective after close-of-business April 27, 2026: "
        "“The amendments update the Rulebook from Version 1.3 to Version 1.4… "
        "These changes do not alter the economics, risk, or operation of any currently "
        "cleared Contract. Rather, they create the definitional and structural framework "
        "necessary for the Clearing House to accommodate new products in future "
        "filings.” Enclosures listed as “Confidential letter description of "
        "changes” and “Clean and redlined versions of Amended Rulebook”; "
        "the amended text itself is not public."
    ),
)

V14_IDENTIFIED_NOT_READ: Final = SourceVersion(
    source_name=KLEAR_DCO_RULEBOOK,
    version="1.4",
    effective_date=date(2026, 4, 27),
    filed_date=date(2026, 4, 20),
    document_sha256=None,
    url=f"{_CFTC}/filings/orgrules/rules0420261686.pdf",
    note=(
        "CURRENT VERSION IDENTIFIED, CURRENT TEXT NOT PUBLICLY RETRIEVABLE. Only the "
        "certification letter is public; the clean and redlined rulebook were filed "
        "confidentially. No hash is recorded because the document has not been read."
    ),
)

V14_UNREAD_CHAPTERS: Final[tuple[str, ...]] = (
    "new definitions distinguishing products with different collateralization requirements",
    "Rule 3.2C participant eligibility, including ECP status for certain contracts",
    "Chapter 7 framework for future products that are not fully cash collateralized",
    "Chapter 12 default-management framework",
    "Rule 2.4F / 2.8 Risk Management Committee scope",
)
"""What v1.4 changed, per its own public letter, and which we have not read.

Recorded so the gap is legible. None of it is said to touch the economics of a
currently cleared contract, and none of it has been verified against text.
"""

KLEAR_RULEBOOK_HISTORY: Final = SourceHistory(
    source_name=KLEAR_DCO_RULEBOOK,
    versions=(V100, V12, V13, V14_IDENTIFIED_NOT_READ),
    amendments=(V14_AMENDMENT,),
    versions_are_exhaustive=False,
    record_complete_from=None,
)
"""Deliberately not exhaustive and with no completeness date.

v1.1 was never located as a standalone filing, and the amendment record between
v1.00 and v1.3 was not enumerated. Under the same rule the Exchange history
follows, that means no negative verdict about Klear provisions may be stated
from this record.
"""
