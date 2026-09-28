"""The filed version and amendment record for the Kalshi Exchange Rulebook.

Every hash here is of bytes actually fetched and parsed. Every quoted line was
read out of the filing it is attributed to.

The two amendments that matter to Rule 6.3
-------------------------------------------
**Scalar contracts** (filed 2025-02-18, effective 2025-03-03, v1.15 -> v1.16).
The filing carries "a copy of the Rulebook showing changes, as well a clean
copy", and the tracked copy inserts an entirely new Scalar Contract paragraph
as Rule 6.3(b), pushing every later subsection down one letter. Before it,
6.3(b) was the indeterminate-outcome payout provision and 6.3(d) was the Market
Outcome Review provision.

**Settlement of Contracts** (filed 2026-03-02, effective 2026-03-17). Its
Appendix A says, in as many words, "[Existing subsections (a) through (d)
unchanged.]" and then adds a new (e) for the death of a natural person who is a
contract's primary subject. That insertion pushes Market Outcome Review from
(e) to (f), which is where v1.29 has it.

On the completeness of this record
----------------------------------
``record_complete_from`` is set to the effective date of v1.14, and the basis is
stated rather than assumed. Three official versions -- v1.14, v1.18 and v1.29 --
bracket the window, and their Rule 6.3 text differs by exactly the two
amendments above. Intermediate version filings (v1.15, v1.17, v1.19 through
v1.28) were not individually enumerated, so this record establishes the *net*
mapping of each subsection across the window rather than proving no provision
ever moved and moved back. For tracing where a citation points today, the net
mapping is the question.
"""

from __future__ import annotations

from datetime import date
from typing import Final

from predarb.semantics.lineage import (
    HopBasis,
    LineageHop,
    ReferenceLineage,
    SourceAmendment,
    SourceHistory,
    SourceVersion,
)
from predarb.venues.kalshi.governing_sources import EXCHANGE_RULEBOOK

__all__ = [
    "BOND_LINEAGES",
    "CRIMECHARGE_LINEAGES",
    "EXCHANGE_RULEBOOK_HISTORY",
    "RULE_HEADINGS",
    "headings_for_version",
    "lineages_for_series",
]

# Rule-number -> heading, extracted with pypdf from each filed version at the
# hash recorded above. This is what makes automatic resolution of a whole-rule
# citation possible without re-fetching an 80-page PDF: if a number carries the
# same heading in the version in force when a document was written and in the
# current one, the citation still identifies the same rule.
#
# It also shows why that check has teeth. Twenty-one rule numbers changed
# meaning between v1.14 and v1.29 -- Rule 3.6 went from "DUES, FEES, AND
# EXPENSES PAYABLE BY MEMBERS" to "OBLIGATIONS APPLICABLE TO ALL PARTICIPANTS",
# and 5.12 from "HOURS FOR TRADING CONTRACTS" to "INVALIDATION OF ORDERS AND
# TRADES UPON SUSPENSION". Citations to those numbers in a 2025 document are
# stale in a way no amount of number-matching would reveal.
RULE_HEADINGS: Final[dict[str, dict[str, str]]] = {
    "1.14": {
        "2.1": "OWNERSHIP",
        "2.2": "BOARD OF DIRECTORS",
        "2.3": "OFFICERS",
        "2.4": "RESTRICTIONS ON WHO MAY BE MEMBERS OF THE BOARD,",
        "2.5": "COMMITTEES AND SUBCOMMITTEES",
        "2.6": "REGULATORY OVERSIGHT COMMITTEE",
        "2.7": "DISCIPLINARY PANEL, APPEALS COMMITTEE, AND OUTCOME",
        "2.8": "EMERGENCY RULES",
        "2.9": "VOTING BY INTERESTED BOARD MEMBERS",
        "2.10": "INDEMNIFICATION OF DIRECTORS, OFFICERS, AND OTHERS",
        "2.11": "PROHIBITION ON USE OF MATERIAL, NON-PUBLIC",
        "2.12": "LIMITATION ON TRADING BY AFFILIATES",
        "2.13": "CONSENT TO JURISDICTION",
        "2.14": "RECORDKEEPING",
        "2.15": "INFORMATION-SHARING AGREEMENTS",
        "2.16": "RECORDKEEPING AND REPORTING REQUIREMENTS",
        "2.17": "PUBLIC INFORMATION",
        "3.1": "MEMBERS - APPLICATIONS, AGREEMENTS, ELIGIBILITY CRITERIA,",
        "3.2": "MEMBER OBLIGATIONS",
        "3.3": "REJECTION OF APPLICANT AND LIMITATIONS OF TRADING",
        "3.4": "COMMUNICATIONS BETWEEN KALSHI AND MEMBERS",
        "3.5": "MEMBER FUNDS MAINTAINED WITH THE COMPANY",
        "3.6": "DUES, FEES, AND EXPENSES PAYABLE BY MEMBERS",
        "4.1": "ELIGIBILITY TO BE DESIGNATED AS A MARKET MAKER",
        "4.2": "DESIGNATION AS A MARKET MAKER",
        "4.3": "MARKET MAKER BENEFITS",
        "4.4": "MARKET MAKER OBLIGATIONS",
        "4.5": "MARKET MAKER POSITION ACCOUNTABILITY LEVELS",
        "5.1": "PRIOR REVIEW OF THESE RULES AND ACCEPTANCE OF TERMS",
        "5.2": "MEMBER ACCESS TO KALSHI",
        "5.3": "TRADING CONTRACTS \u2013 MEMBERS",
        "5.4": "ORDER ENTRY",
        "5.5": "HANDLING OF CUSTOMER ORDERS",
        "5.6": "DISPUTED ORDERS",
        "5.7": "PRIORITY OF ORDERS",
        "5.8": "FILLING ORDERS TO TRADE CONTRACTS",
        "5.9": "CANCELLATION OF ORDERS",
        "5.10": "TRADE CANCELLATIONS",
        "5.11": "VIEWING THE MARKET AND EXECUTED ORDERS",
        "5.12": "HOURS FOR TRADING CONTRACTS",
        "5.13": "PROHIBITED TRANSACTIONS AND ACTIVITIES",
        "5.14": "POSITION ACCOUNTABILITY",
        "5.15": "POSITION LIMITS",
        "6.1": "CLEARANCE",
        "6.2": "SETTLING CONTRACT TRADES",
        "6.3": "SETTLEMENT",
        "6.4": "SETTLING MEMBER WITHDRAWAL REQUESTS",
        "7.1": "THE MARKET OUTCOME REVIEW PROCESS",
        "7.2": "CONTRACT MODIFICATIONS",
        "8.1": "INVESTMENT OF MEMBER ACCOUNT FUNDS",
        "9.1": "MONITORING THE MARKET",
        "9.2": "INVESTIGATIONS, HEARINGS, AND APPEALS",
        "9.3": "SETTLEMENT OF INVESTIGATIONS",
        "9.4": "NOTICE AND PUBLICATION OF DISCIPLINARY ACTION",
        "9.5": "PENALTIES",
        "9.6": "SUMMARY SUSPENSION",
        "9.7": "REPRESENTATION BY COUNSEL",
        "9.8": "REPORTING VIOLATIONS TO THE COMMISSION",
        "10.1": "GENERAL",
        "10.2": "FAIR AND EQUITABLE ARBITRATION PROCEDURES",
        "10.3": "WITHDRAWAL OF ARBITRATION CLAIM",
        "10.4": "PENALTIES",
        "10.5": "ARBITRATION PANEL",
        "11.1": "PROPERTY RIGHTS",
        "11.2": "SIGNATURES",
        "11.3": "LIMITATION OF LIABILITY",
        "12.1": "ACTIVITIES OF SELF-REGULATORY ORGANIZATION EMPLOYEES",
        "12.2": "SERVICE ON SELF-REGULATORY ORGANIZATION GOVERNING",
        "12.3": "VOTING BY INTERESTED MEMBERS OF SELF-REGULATORY",
        "13.1": "TERMS THAT ARE UNIFORM ACROSS CONTRACTS",
    },
    "1.18": {
        "2.1": "OWNERSHIP",
        "2.2": "BOARD OF DIRECTORS",
        "2.3": "OFFICERS",
        "2.4": "RESTRICTIONS ON WHO MAY BE MEMBERS OF THE BOARD,",
        "2.5": "COMMITTEES AND SUBCOMMITTEES",
        "2.6": "REGULATORY OVERSIGHT COMMITTEE",
        "2.7": "DISCIPLINARY PANEL, APPEALS COMMITTEE, AND OUTCOME",
        "2.8": "EMERGENCY RULES",
        "2.9": "VOTING BY INTERESTED BOARD MEMBERS",
        "2.10": "INDEMNIFICATION OF DIRECTORS, OFFICERS, AND OTHERS",
        "2.11": "PROHIBITION ON USE OF MATERIAL, NON-PUBLIC",
        "2.12": "LIMITATION ON TRADING BY AFFILIATES",
        "2.13": "CONSENT TO JURISDICTION",
        "2.14": "RECORDKEEPING",
        "2.15": "INFORMATION-SHARING AGREEMENTS",
        "2.16": "RECORDKEEPING AND REPORTING REQUIREMENTS",
        "2.17": "PUBLIC INFORMATION",
        "3.1": "SELF-CLEARING MEMBERS - APPLICATIONS, AGREEMENTS,",
        "3.2": "FCMS",
        "3.3": "FCM CUSTOMERS",
        "3.4": "INTRODUCING BROKERS",
        "3.5": "IB CUSTOMERS",
        "3.6": "OBLIGATIONS APPLICABLE TO ALL PARTICIPANTS",
        "3.7": "OBLIGATIONS APPLICABLE TO ALL MEMBERS",
        "3.8": "FCM OBLIGATIONS",
        "3.9": "COMMUNICATIONS BETWEEN KALSHI AND MEMBERS",
        "3.10": "DUES, FEES, AND EXPENSES PAYABLE BY MEMBERS",
        "3.11": "REJECTION OF APPLICANT AND LIMITATIONS OF TRADING",
        "4.1": "ELIGIBILITY TO BE DESIGNATED AS A MARKET MAKER",
        "4.2": "DESIGNATION AS A MARKET MAKER",
        "4.3": "MARKET MAKER BENEFITS",
        "4.4": "MARKET MAKER OBLIGATIONS",
        "4.5": "MARKET MAKER POSITION ACCOUNTABILITY LEVELS",
        "5.1": "PRIOR REVIEW OF THESE RULES AND ACCEPTANCE OF TERMS",
        "5.2": "SELF-CLEARING MEMBER ACCESS TO KALSHI",
        "5.3": "TRADING CONTRACTS",
        "5.4": "SELF-CLEARING MEMBER ORDER AND CANCELLATION",
        "5.5": "FCM CUSTOMER ORDERS, CANCELLATIONS, AND TRADING",
        "5.6": "IB CUSTOMER ORDERS, CANCELLATIONS, AND TRADING",
        "5.7": "HANDLING OF ORDERS",
        "5.8": "DISPUTED ORDERS",
        "5.9": "PRIORITY OF ORDERS",
        "5.10": "FILLING ORDERS TO TRADE CONTRACTS",
        "5.11": "TRADE CANCELLATIONS",
        "5.12": "INVALIDATION OF ORDERS AND TRADES UPON SUSPENSION",
        "5.13": "RECORDKEEPING OF FCM CUSTOMERS\u2019 ORDERS",
        "5.14": "RECORDKEEPING OF IB CUSTOMERS\u2019 ORDERS",
        "5.15": "VIEWING THE MARKET AND EXECUTED ORDERS",
        "5.16": "HOURS FOR TRADING CONTRACTS",
        "5.17": "PROHIBITED TRANSACTIONS AND ACTIVITIES",
        "5.18": "POSITION ACCOUNTABILITY",
        "5.19": "POSITION LIMITS",
        "6.1": "CLEARANCE",
        "6.2": "SETTLING CONTRACT TRADES",
        "6.3": "SETTLEMENT",
        "6.4": "SETTLING MEMBER WITHDRAWAL REQUESTS",
        "7.1": "THE MARKET OUTCOME REVIEW PROCESS",
        "7.2": "CONTRACT MODIFICATIONS",
        "8.1": "INVESTMENT OF PARTICIPANT FUNDS",
        "9.1": "MONITORING THE MARKET",
        "9.2": "INVESTIGATIONS, HEARINGS, AND APPEALS",
        "9.3": "SETTLEMENT OF INVESTIGATIONS",
        "9.4": "NOTICE AND PUBLICATION OF DISCIPLINARY ACTION",
        "9.5": "PENALTIES",
        "9.6": "SUMMARY SUSPENSION",
        "9.7": "REPRESENTATION BY COUNSEL",
        "9.8": "REPORTING VIOLATIONS TO THE COMMISSION",
        "10.1": "GENERAL",
        "10.2": "FAIR AND EQUITABLE ARBITRATION PROCEDURES",
        "10.3": "WITHDRAWAL OF ARBITRATION CLAIM",
        "10.4": "PENALTIES",
        "10.5": "ARBITRATION PANEL",
        "11.1": "PROPERTY RIGHTS",
        "11.2": "SIGNATURES",
        "11.3": "LIMITATION OF LIABILITY",
        "12.1": "ACTIVITIES OF SELF-REGULATORY ORGANIZATION",
        "12.2": "SERVICE ON SELF-REGULATORY ORGANIZATION GOVERNING",
        "12.3": "VOTING BY INTERESTED MEMBERS OF SELF-REGULATORY",
        "13.1": "TERMS THAT ARE UNIFORM ACROSS CONTRACTS",
    },
    "1.29": {
        "2.1": "OWNERSHIP",
        "2.2": "BOARD OF DIRECTORS",
        "2.3": "OFFICERS",
        "2.4": "RESTRICTIONS ON WHO MAY BE MEMBERS OF THE BOARD,",
        "2.5": "COMMITTEES AND SUBCOMMITTEES",
        "2.6": "REGULATORY OVERSIGHT COMMITTEE",
        "2.7": "DISCIPLINARY PANEL, APPEALS COMMITTEE, AND OUTCOME",
        "2.8": "EMERGENCY RULES",
        "2.9": "VOTING BY INTERESTED BOARD MEMBERS",
        "2.10": "INDEMNIFICATION OF DIRECTORS, OFFICERS, AND OTHERS",
        "2.11": "PROHIBITION ON USE OF MATERIAL, NON-PUBLIC",
        "2.12": "LIMITATION ON TRADING BY AFFILIATES",
        "2.13": "CONSENT TO JURISDICTION",
        "2.14": "RECORDKEEPING",
        "2.15": "INFORMATION-SHARING AGREEMENTS",
        "2.16": "RECORDKEEPING AND REPORTING REQUIREMENTS",
        "2.17": "PUBLIC INFORMATION",
        "3.1": "SELF-CLEARING MEMBERS - APPLICATIONS, AGREEMENTS,",
        "3.2": "FCMS",
        "3.3": "FCM CUSTOMERS",
        "3.4": "INTRODUCING BROKERS",
        "3.5": "IB CUSTOMERS",
        "3.6": "OBLIGATIONS APPLICABLE TO ALL PARTICIPANTS",
        "3.7": "OBLIGATIONS APPLICABLE TO ALL MEMBERS",
        "3.8": "FCM OBLIGATIONS",
        "3.9": "IB OBLIGATIONS",
        "3.10": "AUTHORIZED TRADERS",
        "3.11": "REJECTION OF APPLICANT AND LIMITATIONS OF TRADING",
        "3.12": "COMMUNICATIONS BETWEEN KALSHI AND MEMBERS",
        "3.13": "DUES, FEES, AND EXPENSES PAYABLE BY MEMBERS",
        "4.1": "ELIGIBILITY TO BE DESIGNATED AS A MARKET MAKER",
        "4.2": "DESIGNATION AS A MARKET MAKER",
        "4.3": "MARKET MAKER BENEFITS",
        "4.4": "MARKET MAKER OBLIGATIONS",
        "4.5": "MARKET MAKER POSITION ACCOUNTABILITY LEVELS",
        "5.1": "PRIOR REVIEW OF THESE RULES AND ACCEPTANCE OF TERMS",
        "5.2": "SELF-CLEARING MEMBER ACCESS TO KALSHI",
        "5.3": "TRADING CONTRACTS",
        "5.4": "SELF-CLEARING MEMBER ORDER AND CANCELLATION",
        "5.5": "FCM CUSTOMER ORDERS, CANCELLATIONS, AND TRADING",
        "5.6": "IB CUSTOMER ORDERS, CANCELLATIONS, AND TRADING",
        "5.7": "HANDLING OF ORDERS",
        "5.8": "DISPUTED ORDERS",
        "5.9": "PRIORITY OF ORDERS",
        "5.10": "FILLING ORDERS TO TRADE CONTRACTS",
        "5.11": "TRADE CANCELLATIONS",
        "5.12": "INVALIDATION OF ORDERS AND TRADES UPON SUSPENSION OR",
        "5.13": "RECORDKEEPING OF FCM CUSTOMERS\u2019 ORDERS",
        "5.14": "RECORDKEEPING OF IB CUSTOMERS\u2019 ORDERS",
        "5.15": "VIEWING THE MARKET AND EXECUTED ORDERS",
        "5.16": "HOURS FOR TRADING CONTRACTS",
        "5.17": "PROHIBITED TRANSACTIONS AND ACTIVITIES",
        "5.18": "POSITION ACCOUNTABILITY",
        "5.19": "POSITION LIMITS",
        "6.1": "CLEARANCE",
        "6.2": "SETTLING CONTRACT TRADES",
        "6.3": "SETTLEMENT",
        "6.4": "SETTLING MEMBER WITHDRAWAL REQUESTS",
        "7.1": "THE MARKET OUTCOME REVIEW PROCESS",
        "7.2": "CONTRACT MODIFICATIONS",
        "8.1": "INVESTMENT OF PARTICIPANT FUNDS",
        "9.1": "MONITORING THE MARKET",
        "9.2": "INVESTIGATIONS, HEARINGS, AND APPEALS",
        "9.3": "SETTLEMENT OF INVESTIGATIONS",
        "9.4": "NOTICE AND PUBLICATION OF DISCIPLINARY ACTION",
        "9.5": "PENALTIES",
        "9.6": "SUMMARY SUSPENSION",
        "9.7": "REPRESENTATION BY COUNSEL",
        "9.8": "REPORTING VIOLATIONS TO THE COMMISSION",
        "10.1": "GENERAL",
        "10.2": "FAIR AND EQUITABLE ARBITRATION PROCEDURES",
        "10.3": "WITHDRAWAL OF ARBITRATION CLAIM",
        "10.4": "PENALTIES",
        "10.5": "ARBITRATION PANEL",
        "11.1": "PROPERTY RIGHTS",
        "11.2": "SIGNATURES",
        "11.3": "LIMITATION OF LIABILITY",
        "12.1": "ACTIVITIES OF SELF-REGULATORY ORGANIZATION EMPLOYEES",
        "12.2": "SERVICE ON SELF-REGULATORY ORGANIZATION GOVERNING",
        "12.3": "VOTING BY INTERESTED MEMBERS OF SELF-REGULATORY",
        "13.1": "TERMS THAT ARE UNIFORM ACROSS CONTRACTS",
    },
}


def headings_for_version(version: str | None) -> dict[str, str] | None:
    """Rule headings for one filed version, or ``None`` if we did not parse it."""
    return RULE_HEADINGS.get(version or "")


_CFTC = "https://www.cftc.gov/sites/default/files/filings"

V114: Final = SourceVersion(
    source_name=EXCHANGE_RULEBOOK,
    version="1.14",
    effective_date=date(2024, 11, 27),
    filed_date=date(2024, 11, 14),
    document_sha256="8113d2d4967217b37005f6a45aaf06b6986fca87f63c2eb8aa5ae80167f4c082",
    url=f"{_CFTC}/orgrules/24/11/rules1114248723.pdf",
    note=(
        "Rule 6.3: (a) general payout, (b) indeterminate-outcome payout determination, "
        "(c) Settlement Date mechanics, (d) Market Outcome Review, (e) notification. "
        "No Binary/Scalar distinction yet."
    ),
)

V116: Final = SourceVersion(
    source_name=EXCHANGE_RULEBOOK,
    version="1.16",
    effective_date=date(2025, 3, 3),
    filed_date=date(2025, 2, 18),
    document_sha256="f2d3a3026aad218e93a0298a61529aed5e3d4a2d5f14d13f1393385532cdb614",
    url=f"{_CFTC}/orgrules/25/02/rules02172515652.pdf",
    note="Clean copy enclosed with the scalar-contracts amendment filing.",
)

V118: Final = SourceVersion(
    source_name=EXCHANGE_RULEBOOK,
    version="1.18",
    effective_date=date(2025, 7, 1),
    filed_date=date(2025, 7, 1),
    document_sha256="d8d185862a439a8f1a178d5044bbe9c4ccfd931ae4a54619b9f2602493865c8f",
    url=f"{_CFTC}/orgrules/25/07/rules07012525155.pdf",
    note=(
        "Rule 6.3: (a) Binary, (b) Scalar, (c) payout determination, (d) Settlement "
        "Date mechanics, (e) Market Outcome Review, (f) notification."
    ),
)

V129: Final = SourceVersion(
    source_name=EXCHANGE_RULEBOOK,
    version="1.29",
    effective_date=date(2026, 8, 17),
    document_sha256="3b6d4ffd5b32330d3466179d4cae610372d07511123c9976bc6cbb1b5185240b",
    url=(
        "https://kalshi-public-docs.s3.amazonaws.com/regulatory/rulebook/"
        "Kalshi%20DCM%20Rulebook%20v.1.29.pdf"
    ),
    note=(
        "Rule 6.3: (a) Binary, (b) Scalar, (c) payout determination, (d) Settlement "
        "Date mechanics, (e) death of a natural person, (f) Market Outcome Review, "
        "(g) notification."
    ),
)

SCALAR_AMENDMENT: Final = SourceAmendment(
    source_name=EXCHANGE_RULEBOOK,
    amendment_id="rules02172515652",
    url=f"{_CFTC}/orgrules/25/02/rules02172515652.pdf",
    document_sha256="f2d3a3026aad218e93a0298a61529aed5e3d4a2d5f14d13f1393385532cdb614",
    filed_date=date(2025, 2, 18),
    effective_date=date(2025, 3, 3),
    version_before="1.15",
    version_after="1.16",
    carries_tracked_changes=True,
    affects=("Rule 6.3",),
    evidence=(
        "Rulebook Amendment for Scalar Contracts: “The amendments are intended to "
        "include scalar contracts... the Rulebook is being amended to add definitions "
        "for Binary Contracts and Scalar Contracts, as well as to make conforming "
        "changes throughout. Attached to this cover letter is a copy of the Rulebook "
        "showing changes, as well a clean copy.” The tracked copy inserts a new "
        "Rule 6.3(b) for Scalar Contracts."
    ),
)

DEATH_AMENDMENT: Final = SourceAmendment(
    source_name=EXCHANGE_RULEBOOK,
    amendment_id="rules03022640155",
    url=f"{_CFTC}/orgrules/26/03/rules03022640155.pdf",
    document_sha256="cb84ebe8b15adc5e3a501a06ab104aa865e0bc6279f88c46bf66fa8b78907953",
    filed_date=date(2026, 3, 2),
    effective_date=date(2026, 3, 17),
    carries_tracked_changes=True,
    affects=("Rule 6.3",),
    evidence=(
        "Settlement of Contracts: “amending Rule 6.3 of the Exchange Rulebook "
        "(\u2018Settlement\u2019), effective March 17, 2026… Appendix A Rulebook "
        "Amendment (Clean) RULE 6.3 SETTLEMENT [Existing subsections (a) through (d) "
        "unchanged.] (e) If a natural person who is the primary subject of a "
        "Contract\u2019s Underlying or Payout Criterion dies prior to Expiration…”"
    ),
)

EXCHANGE_RULEBOOK_HISTORY: Final = SourceHistory(
    source_name=EXCHANGE_RULEBOOK,
    versions=(V114, V116, V118, V129),
    amendments=(SCALAR_AMENDMENT, DEATH_AMENDMENT),
    record_complete_from=date(2024, 11, 27),
)

_SCALAR_HOP_EVIDENCE: Final = (
    "Scalar-contracts amendment, tracked copy: a new “(b) For a Scalar Contract, "
    "when a Contract expires, the Payout Criterion will determine the proportion of the "
    "Settlement Value to be distributed to holders of long and short positions…” "
    "is inserted, displacing the subsections below it by one letter."
)
_DEATH_HOP_EVIDENCE: Final = (
    "Settlement-of-Contracts amendment, Appendix A (Clean): “RULE 6.3 SETTLEMENT "
    "[Existing subsections (a) through (d) unchanged.] (e) If a natural person…” "
    "-- a new (e) is inserted, displacing the subsections below it by one letter."
)


def _hop(
    from_reference: str,
    to_reference: str,
    amendment: SourceAmendment,
    evidence: str,
    basis: HopBasis,
) -> LineageHop:
    return LineageHop(
        from_reference=from_reference,
        to_reference=to_reference,
        basis=basis,
        effective_date=amendment.effective_date,
        amendment_id=amendment.amendment_id,
        amendment_url=amendment.url,
        amendment_sha256=amendment.document_sha256,
        version_before=amendment.version_before,
        version_after=amendment.version_after,
        evidence=evidence,
    )


BOND_ISSUANCE: Final = date(2025, 1, 17)

BOND_LINEAGES: Final[tuple[ReferenceLineage, ...]] = (
    ReferenceLineage(
        source_document="BOND contract terms",
        citation_as_written="Rule 6.3(b)",
        source_name=EXCHANGE_RULEBOOK,
        issuance_date=BOND_ISSUANCE,
        issuance_version="1.14",
        issuance_version_sha256=V114.document_sha256,
        issuance_target=(
            "“If, when a Contract expires, it cannot be determined whether the Payout "
            "Criterion encompasses the Expiration Value of the Underlying, Kalshi will "
            "determine the payouts to the holders of long and short positions…” "
            "-- exactly the provision the citing sentence relies on."
        ),
        issuance_reference_valid=True,
        hops=(
            _hop(
                "Rule 6.3(b)",
                "Rule 6.3(c)",
                SCALAR_AMENDMENT,
                _SCALAR_HOP_EVIDENCE,
                HopBasis.TRACKED_AMENDMENT,
            ),
            _hop(
                "Rule 6.3(c)",
                "Rule 6.3(c)",
                DEATH_AMENDMENT,
                "Appendix A (Clean): “[Existing subsections (a) through (d) "
                "unchanged.]” -- (c) is explicitly untouched.",
                HopBasis.AMENDMENT_STATEMENT,
            ),
        ),
        current_reference="Rule 6.3(c)",
        current_version="1.29",
    ),
    ReferenceLineage(
        source_document="BOND contract terms",
        citation_as_written="Rule 6.3(d)",
        source_name=EXCHANGE_RULEBOOK,
        issuance_date=BOND_ISSUANCE,
        issuance_version="1.14",
        issuance_version_sha256=V114.document_sha256,
        issuance_target=(
            "“Before Settlement, Kalshi may, at its sole discretion initiate the "
            "Market Outcome Review Process as provided in Rule 7.1.” -- exactly the "
            "provision the citing sentence relies on."
        ),
        issuance_reference_valid=True,
        hops=(
            _hop(
                "Rule 6.3(d)",
                "Rule 6.3(e)",
                SCALAR_AMENDMENT,
                _SCALAR_HOP_EVIDENCE,
                HopBasis.TRACKED_AMENDMENT,
            ),
            _hop(
                "Rule 6.3(e)",
                "Rule 6.3(f)",
                DEATH_AMENDMENT,
                _DEATH_HOP_EVIDENCE,
                HopBasis.AMENDMENT_STATEMENT,
            ),
        ),
        current_reference="Rule 6.3(f)",
        current_version="1.29",
    ),
)

CRIMECHARGE_ISSUANCE: Final = date(2025, 7, 24)

CRIMECHARGE_LINEAGES: Final[tuple[ReferenceLineage, ...]] = (
    ReferenceLineage(
        source_document="CRIMECHARGE contract terms",
        citation_as_written="Rule 6.3(b)",
        source_name=EXCHANGE_RULEBOOK,
        issuance_date=CRIMECHARGE_ISSUANCE,
        issuance_version="1.18",
        issuance_version_sha256=V118.document_sha256,
        issuance_target=(
            "“For a Scalar Contract, when a Contract expires, the Payout Criterion "
            "will determine the proportion of the Settlement Value to be distributed to "
            "holders of long and short positions…” -- the Scalar Contract rule, "
            "not the payout-determination provision the citing sentence relies on."
        ),
        issuance_reference_valid=False,
        note=(
            "Certified 2025-07-24, nearly five months after the scalar amendment took "
            "effect on 2025-03-03. The citation was already wrong on the day the product "
            "was certified, under any post-scalar Rulebook version."
        ),
    ),
    ReferenceLineage(
        source_document="CRIMECHARGE contract terms",
        citation_as_written="Rule 6.3(d)",
        source_name=EXCHANGE_RULEBOOK,
        issuance_date=CRIMECHARGE_ISSUANCE,
        issuance_version="1.18",
        issuance_version_sha256=V118.document_sha256,
        issuance_target=(
            "“On the Settlement Date, Kalshi will cause and/or instruct Clearing "
            "House to…” -- Settlement Date mechanics, not the Market Outcome "
            "Review Process the citing sentence relies on."
        ),
        issuance_reference_valid=False,
        note="Same chronology as the 6.3(b) citation above.",
    ),
)


def lineages_for_series(series_ticker: str | None) -> tuple[ReferenceLineage, ...]:
    """Recorded lineages for one product family. Empty means nobody has looked."""
    return {
        "KXBOND": BOND_LINEAGES,
        "KXFEDERALCHARGE": CRIMECHARGE_LINEAGES,
    }.get(series_ticker or "", ())
