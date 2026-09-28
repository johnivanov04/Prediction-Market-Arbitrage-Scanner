"""Reviewed materiality declarations for the federal regulations Kalshi cites.

Each declaration below records a judgement that one exact set of citations,
made in one kind of document, cannot reach the ``STANDARD_BINARY_COMPLEMENT``
claim. Every regulation's title was read from the eCFR (Title 17, snapshot
2026-09-01, 3,525 sections indexed) rather than guessed from its number.

Scope of the judgement
----------------------
The claim is narrow: *for every permitted terminal settlement state of this
market, YES payout + NO payout equals the explicit notional*. A regulation
bears on it only if it can change a terminal state, an amount, who is paid, a
void or refund, an allocation, or who determines the Expiration Value.

None of the regulations here does. They govern who may register as an
intermediary, what capital they must hold, what records must be kept, how
disciplinary notices are published, and how a product or rule is filed with the
Commission. A contract's terminal economics are unchanged by all of it.

What this is deliberately *not*
-------------------------------
Not "CFTC regulations are procedural". Not "anything in 17 CFR is procedural".
Part 38 is the DCM core principles and Part 40 is product filing -- both parts
could perfectly well contain a future rule about settlement, and a
part-level or domain-level exemption would wave it straight through. Each
declaration names its citations exactly; a regulation not on the list arrives
unclassified and blocks, which is the intended behaviour.

Each is additionally vetoed by payout language in the citing sentence, so the
same regulation cited in a settlement paragraph is material regardless of what
is declared here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

from predarb.semantics.materiality import (
    CitationScope,
    ContextScope,
    DependencyMaterialityPolicy,
    MaterialityClass,
    MaterialityDeclaration,
    ParentContext,
)
from predarb.venues.kalshi.governing_sources import CFTC_REGULATIONS, EXCHANGE_RULEBOOK

__all__ = [
    "FEE_RULE_NOT_SETTLEMENT",
    "KALSHI_MATERIALITY_POLICY",
    "MATERIALITY_POLICY_VERSION",
    "PRODUCT_FILING_AUTHORITY",
]

MATERIALITY_POLICY_VERSION: Final = "kalshi-materiality/1"

_REVIEWED_AT: Final = datetime(2026, 9, 27, tzinfo=UTC)
_REVIEWER: Final = "phase-1 acceptance review"
_ECFR: Final = (
    "eCFR Title 17 structure snapshot 2026-09-01 "
    "(https://www.ecfr.gov/api/versioner/v1/structure/2026-09-01/title-17.json)"
)


def _declare(
    citations: tuple[str, ...],
    parents: tuple[ParentContext, ...],
    rationale: str,
    titles: str,
    *,
    required_phrases: tuple[str, ...] = (),
) -> MaterialityDeclaration:
    return MaterialityDeclaration(
        claim="STANDARD_BINARY_COMPLEMENT",
        citation_scope=CitationScope(source_name=CFTC_REGULATIONS, citations=citations),
        context_scope=ContextScope(parents=parents, required_phrases=required_phrases),
        classification=MaterialityClass.PROCEDURAL_FOR_CLAIM,
        rationale=rationale,
        authority=f"{titles} -- titles read from {_ECFR}",
        reviewer=_REVIEWER,
        reviewed_at=_REVIEWED_AT,
        policy_version=MATERIALITY_POLICY_VERSION,
    )


PRODUCT_FILING_AUTHORITY: Final = _declare(
    (
        "CFTC Regulation 40.2(a)",
        "COMMISSION RULE 40.2",
        "Commission Rule 40.2(a)",
        "Section 40.2(a)",
    ),
    (ParentContext.PRODUCT_CERTIFICATION_FILING,),
    (
        "Cited in a self-certification cover letter as the authority under which the "
        "product was filed with the Commission. It governs how a contract comes to be "
        "listed, not what the listed contract pays."
    ),
    "17 CFR 40.2 'Listing products for trading by certification'",
)

RULE_FILING_PROCEDURE: Final = _declare(
    ("CFTC Regulation 40.6(a)",),
    (ParentContext.EXCHANGE_RULEBOOK,),
    (
        "Cited for the lead time before an amended fee schedule may take effect. It "
        "sets the filing timetable for a rule change; it does not determine any "
        "contract's payout."
    ),
    "17 CFR 40.6 'Self-certification of rules'",
)

INTERMEDIARY_REGISTRATION_AND_DEFINITIONS: Final = _declare(
    (
        "CFTC Regulation 1.3",
        "Commission Regulation 1.3(j)",
        "Commission Regulation 1.17(b)",
        "Commission Regulation 3.1(a)",
        "Commission Regulation 156.1",
    ),
    (ParentContext.EXCHANGE_RULEBOOK,),
    (
        "Cited for definitions the Rulebook borrows -- 'Person', 'controlled accounts', "
        "'proprietary accounts', 'principal', 'broker association'. They fix who a rule "
        "applies to, never what a contract pays on expiry."
    ),
    (
        "17 CFR 1.3 'Definitions'; 3.1 'Definitions'; 156.1 'Definition'; "
        "1.17 'Minimum financial requirements for futures commission merchants and "
        "introducing brokers'"
    ),
)

INTERMEDIARY_FINANCIAL_REQUIREMENTS: Final = _declare(
    (
        "CFTC Regulation 1.11",
        "CFTC Regulation 1.17",
        "Commission Rule 1.17",
        "Commission Regulation 1.12",
        "CFTC Regulation 155.3",
    ),
    (ParentContext.EXCHANGE_RULEBOOK,),
    (
        "Cited for eligibility, risk-management programmes, minimum capital and "
        "proprietary-trading standards applying to FCMs and IBs. These bear on an "
        "intermediary's solvency and conduct, which is counterparty risk, not a term "
        "of the contract's settlement."
    ),
    (
        "17 CFR 1.11 'Risk Management Program for futures commission merchants'; "
        "1.12 'Maintenance of minimum financial requirements by futures commission "
        "merchants and introducing brokers'; 1.17 'Minimum financial requirements...'; "
        "155.3 'Trading standards for futures commission merchants'"
    ),
)

RECORDKEEPING: Final = _declare(
    (
        "Commission Regulation 1.31",
        "CFTC Regulation 1.35",
        "CFTC Regulation 1.64(b)",
    ),
    (ParentContext.EXCHANGE_RULEBOOK,),
    (
        "Cited for record retention periods and for the board-composition report the "
        "exchange files. Recordkeeping obligations do not alter terminal economics."
    ),
    (
        "17 CFR 1.31 'Regulatory records; retention and production'; "
        "1.35 'Records of commodity interest and related cash or forward transactions'; "
        "1.64 'Composition of various self-regulatory organization governing boards and "
        "major disciplinary committees'"
    ),
)

SELF_REGULATORY_GOVERNANCE: Final = _declare(
    (
        "CFTC Regulation 1.63",
        "CFTC Regulation 1.63(a)",
        "COMMISSION REGULATION 1.59",
        "COMMISSION REGULATION 1.63",
        "COMMISSION REGULATION 1.69",
        "Regulation 1.59",
        "Regulation 1.69",
    ),
    (ParentContext.EXCHANGE_RULEBOOK,),
    (
        "Cited for who may serve on the exchange's governing bodies, what counts as a "
        "disciplinary offence, and when an interested member must abstain. These "
        "constrain the exchange's governance, not any contract's payout table."
    ),
    (
        "17 CFR 1.59 'Activities of self-regulatory organization employees, governing "
        "board members, committee members, and consultants'; 1.63 'Service on "
        "self-regulatory organization governing boards or committees by persons with "
        "disciplinary histories'; 1.69 'Voting by interested members of self-regulatory "
        "organization governing boards and various committees'"
    ),
)

DISCIPLINARY_NOTICE: Final = _declare(
    (
        "CFTC Regulation 9.11",
        "Commission Regulation 9.11",
        "Commission Regulation 9.13",
        "Commission Regulation 38.710",
    ),
    (ParentContext.EXCHANGE_RULEBOOK,),
    (
        "Cited for the form, delivery and publication of disciplinary notices and "
        "sanctions against members. A member being disciplined does not change what a "
        "contract pays at expiry."
    ),
    (
        "17 CFR 9.11 'Form, contents and delivery of notice of disciplinary or access "
        "denial action'; 9.13 'Publication of notice'; 38.710 'Disciplinary sanctions'"
    ),
)

# The one declaration covering a *Rulebook* rule rather than external law, and
# the narrowest in the set. It turns on the citing sentence's subject matter,
# not on the rule number: "Members will be charged fees in accordance with Rule
# 3.13 of the Rulebook" is a statement about charges, and the settlement
# certificate proves terminal payoff semantics, not what trading costs.
#
# What it does not mean, spelled out because the distinction is easy to lose:
# Rule 3.13 is not globally procedural; Rulebook fee references in general are
# not harmless; fees are emphatically not economically irrelevant -- they are
# proved separately by the Step-6 fee engine and an arbitrage conclusion still
# depends on that proof; and the historical target of the citation stays
# unresolved on its own axis. A reference can be NON-MATERIAL to this claim and
# still have ReferenceResolution UNKNOWN, and the audit shows both.
FEE_RULE_NOT_SETTLEMENT: Final = MaterialityDeclaration(
    claim="STANDARD_BINARY_COMPLEMENT",
    citation_scope=CitationScope(
        source_name=EXCHANGE_RULEBOOK, citations=("Rule 3.13", "RULE 3.13")
    ),
    context_scope=ContextScope(
        parents=(
            ParentContext.PRODUCT_CERTIFICATION_FILING,
            ParentContext.CONTRACT_TERMS,
        ),
        any_of_phrases=(
            "fees",
            "fee",
            "transaction fee",
            "settlement fee",
            "fee schedule",
            "charges",
            "charged",
        ),
    ),
    classification=MaterialityClass.PROCEDURAL_FOR_CLAIM,
    rationale=(
        "Cited for what Members are charged to trade. Trading and settlement "
        "fees are proved separately by the fee engine and are not part of the "
        "terminal payoff table this claim asserts. This says nothing about the "
        "rule in any other context, and nothing about where the citation points."
    ),
    authority=(
        "Kalshi Exchange Rulebook v1.29 Rule 3.13 'DUES, FEES, AND EXPENSES PAYABLE "
        "BY MEMBERS', read at sha256 3b6d4ffd5b32330d3466179d4cae610372d07511123c99"
        "76bc6cbb1b5185240b"
    ),
    reviewer=_REVIEWER,
    reviewed_at=_REVIEWED_AT,
    policy_version=MATERIALITY_POLICY_VERSION,
    source_hashes={
        "exchange_rulebook": ("3b6d4ffd5b32330d3466179d4cae610372d07511123c9976bc6cbb1b5185240b")
    },
)

KALSHI_MATERIALITY_POLICY: Final = DependencyMaterialityPolicy(
    version=MATERIALITY_POLICY_VERSION,
    declarations=(
        PRODUCT_FILING_AUTHORITY,
        RULE_FILING_PROCEDURE,
        INTERMEDIARY_REGISTRATION_AND_DEFINITIONS,
        INTERMEDIARY_FINANCIAL_REQUIREMENTS,
        RECORDKEEPING,
        SELF_REGULATORY_GOVERNANCE,
        DISCIPLINARY_NOTICE,
        FEE_RULE_NOT_SETTLEMENT,
    ),
)
