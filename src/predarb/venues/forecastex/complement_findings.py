"""Whether ForecastEx proves YES + NO = $1.00 for every permitted terminal state.

This is the invariant Phase 1 could not prove on Kalshi. ForecastEx comes far
closer, and the reason is worth stating plainly: where Kalshi's rules pay "the
Settlement Value" to one named side and leave the other side's amount to
inference, ForecastEx writes both sides down, every time, and in the fractional
cases writes the second side as the **residual of the first**.

    Shared Outcomes: "'Yes' holders receive $1 divided by the number of
    participants declared, rounded down to the nearest cent, and 'No' holders
    will receive $1 minus the 'Yes' payout."

That single sentence solves the problem that defeated Kalshi's ENTITYOUTCOME
"$1/N, rounded down" clause. Rounding falls on one side and the other is
whatever is left, so the pair sums to exactly $1.00 no matter what the division
does.

The one gap
-----------
Rule 414(b)(3) -- Accelerated Settlement, ForecastEx's analogue of Kalshi
6.3(c) -- hands an undetermined case to the Event Review Committee for "a binding
determination of fair allocation" and then constrains it with:

    "In no event shall the combined payout for a single 'Yes' Position and a
    single 'No' Position exceed $1.00."

That is an **upper bound with no floor**. The Success Terms and Conditions say
the combined Settlement Values "will always equal $1.00", which is an equality;
the Rulebook says not more than $1.00, which permits less. The conflict runs in
exactly the direction that breaks conservation, and no precedence rule in either
document settles which controls.

Why that gap may not matter for the strategy
--------------------------------------------
Rule 604(a) **prohibits** simultaneously holding both sides of the same Forecast
Market, and requires an offsetting pair to be netted by 16:00 CST the same day.
Rule 604(b) then credits "$1.00 for each pair of Forecast Contracts offset" --
unconditional, with no discretion and no committee. A hedged pair therefore never
reaches resolution, so Rules 414 and 415 never touch it.

So the arbitrage-relevant path is proven, and the resolution-path gap applies
only to positions held unhedged to resolution.

The economics, which are a separate matter
------------------------------------------
Rule 401(d) is the sting. Contracts are created by *inverse pricing*: "When the
combined Bids for the 'Yes' Position and 'No' Position equal $1.01, the Exchange
executes the Forecast Contracts by pairing the Bids." A matched pair therefore
costs exactly $1.01 to create and pays exactly $1.00, and the $0.01 difference is
collected as ForecastEx's fee under Rule 603(b)(2)(iii). There is no combination
of bids that acquires a pair for less.

The complement invariant is beautiful here and the same-market arbitrage is worth
exactly minus one cent, by construction. Both facts belong in the record.
"""

from __future__ import annotations

from typing import Final

from predarb.domain.money import Price
from predarb.semantics.complement_proof import (
    ComplementConservationProof,
    MechanismProof,
    MechanismStatus,
    RoundingModel,
    SourceComponent,
)
from predarb.semantics.settlement_census import SettlementMechanism
from predarb.venues.forecastex.governing_sources import SUCCESS_TERMS_SHA256

__all__ = [
    "PAIR_CREATION_COST",
    "SOURCES",
    "SUCCESS_FAMILY_PROOF",
    "mechanism_findings",
]

PAIR_CREATION_COST: Final = Price.from_value("1.0100")
"""What one matched pair costs to create, fixed by Rule 401(d) inverse pricing."""

_RB_SHA: Final = "fedeba0a1c75786a83089882e3e1502f126d3d932513e00c03697eab6b0ebf45"

SOURCES: Final[tuple[SourceComponent, ...]] = (
    SourceComponent(
        name="forecastex_rulebook",
        version="2026-03-16",
        document_sha256=_RB_SHA,
        url=(
            "https://www.cftc.gov/sites/default/files/filings/orgrules/26/03/rules03022640208.pdf"
        ),
        retrieved_note="74 pages, pypdf CLEAN; covers both Exchange and Clearinghouse",
    ),
    SourceComponent(
        name="forecastex_success_terms",
        version="filed 2025-09-16",
        document_sha256=SUCCESS_TERMS_SHA256,
        url=("https://www.cftc.gov/sites/default/files/filings/ptc/25/09/ptc09162530506.pdf"),
        retrieved_note="7 pages, pypdf CLEAN; carries the Participant Split and Fair Price rules",
    ),
    SourceComponent(
        name="forecastex_fes_terms",
        version="last-modified 2026-08-14",
        document_sha256="93973a2f6de0dcf9abcf618215090229b140881b189233aab30f945296308853",
        url="https://data.forecastex.com/regulatory/FESTermsandConditions.pdf",
        retrieved_note=(
            "2 pages, pypdf CLEAN; a pure binary product with no alternative payout "
            "structure at all"
        ),
    ),
)


def mechanism_findings() -> tuple[MechanismProof, ...]:
    """Every terminal mechanism found, with the text it rests on."""
    return (
        MechanismProof(
            mechanism=SettlementMechanism.OFFSET_NETTING,
            status=MechanismStatus.PROVEN_COMPLEMENTARY,
            rule_reference="Rulebook Rule 604(a)-(b)",
            quoted_text=(
                "(a) A Market Participant may not simultaneously hold both “Yes” "
                "Positions and “No” Positions for the same Forecast Market. If a "
                "Market Participant enters into an execution which results in such "
                "offsetting positions prior to Resolution Time, the responsible Member must "
                "notify the Clearinghouse that the Forecast Contracts should be offset no "
                "later than 16:00 CST each day… (b) When Forecast Contracts are offset, "
                "the offsetting positions are cancelled and the Member\u2019s account is "
                "credited $1.00 for each pair of Forecast Contracts offset."
            ),
            reasoning=(
                "The arbitrage-relevant path, and the strongest finding in the corpus. Both "
                "sides are accounted for in one sentence -- the pair is cancelled and $1.00 "
                "is credited per pair -- with no discretion, no committee and no condition. "
                "Because holding both sides is prohibited, a hedged pair is netted within "
                "the day and never reaches resolution, so Rules 414 and 415 cannot apply "
                "to it."
            ),
            both_branches_explicit=True,
        ),
        MechanismProof(
            mechanism=SettlementMechanism.ORDINARY_BINARY,
            status=MechanismStatus.PROVEN_COMPLEMENTARY,
            rule_reference="Rulebook Rule 603(a); Success T&C Payout Criteria",
            quoted_text=(
                "Rule 603(a): “if the Outcome of the Event Question is “Yes”, "
                "then holders of the “Yes” Position will be entitled to receive the "
                "Settlement Value of $1.00 per contract and holders of the “No” "
                "Position will receive $0.00. If the Outcome of the Event Question is "
                "“No”, then holders of the “No” Position will be entitled "
                "to receive the Settlement Value of $1.00 per contract and holders of the "
                "“Yes” Position will receive $0.00.” The Success Terms and "
                "Conditions repeat both branches and add: “The combined Settlement "
                "Values of the “Yes” and “No” Positions will always equal "
                "$1.00.”"
            ),
            reasoning=(
                "Both sides stated in both branches, in the Rulebook itself rather than only "
                "in a product filing. “Outcome” is defined as “whether an Event "
                "Question resolves to \u2018Yes\u2019 or \u2018No\u2019”, so the two "
                "branches are exhaustive. This is what Kalshi Rule 6.3(a) leaves to "
                "inference."
            ),
            both_branches_explicit=True,
        ),
        MechanismProof(
            mechanism=SettlementMechanism.TIE_SPLIT,
            status=MechanismStatus.PROVEN_COMPLEMENTARY,
            rule_reference="Success T&C payout rule 2 (Shared Outcomes / Participant Split)",
            quoted_text=(
                "“If more than one participant is officially declared to have achieved "
                "the result specified in the Event Question, the markets for those "
                "participants will resolve to the Participant Split Rule, so that “Yes” "
                "holders receive $1 divided by the number of participants declared, rounded "
                "down to the nearest cent, and “No” holders will receive $1 minus "
                "the “Yes” payout.” The tie limb repeats the construction: "
                "“\u2018Yes\u2019 holders receiving $1 divided by the number of "
                "participants involved in the tie, rounded down to the nearest cent, and "
                "\u2018No\u2019 holders receiving $1 minus the \u2018Yes\u2019 payout.”"
            ),
            reasoning=(
                "Explicitly residual: rounding is applied to the YES side and NO is defined "
                "as $1 minus it, so the pair sums to exactly $1.00 whatever the division "
                "and whatever the rounding does. This is precisely the sentence Kalshi's "
                "ENTITYOUTCOME terms omit."
            ),
            both_branches_explicit=True,
        ),
        MechanismProof(
            mechanism=SettlementMechanism.LAST_FAIR_PRICE,
            status=MechanismStatus.PROVEN_COMPLEMENTARY,
            rule_reference=(
                "Success T&C payout rule 5 (Event Cancellation / Fair Price), first limb"
            ),
            quoted_text=(
                "“If the event referenced in the Event Question is canceled in its "
                "entirety and no official result is declared by a recognized authority, the "
                "Contract will resolve to the Fair Price Rule. In such case, the Contract "
                "shall resolve so “Yes” holders receive the last traded price prior "
                "to cancellation and “No” holders receive $1 minus the Yes "
                "payout.”"
            ),
            reasoning=(
                "Residual again, and stated for a discretion-free input. Kalshi's equivalent "
                "gives a complementary worked example and no general rule; this gives the "
                "general rule."
            ),
            both_branches_explicit=True,
        ),
        MechanismProof(
            mechanism=SettlementMechanism.FAIR_ALLOCATION,
            status=MechanismStatus.UNRESOLVED,
            rule_reference="Rulebook Rule 414(b)(3); Success T&C payout rule 5, second limb",
            quoted_text=(
                "Rule 414(b)(3): “If the most recent last price is not available, or if "
                "ForecastEx determines in its sole discretion that the most recent last price "
                "does not represent a fair allocation, the Event Review Committee will be "
                "responsible for making a binding determination of fair allocation. "
                "**In no event shall the combined payout for a single “Yes” Position "
                "and a single “No” Position exceed $1.00.** Determinations by the "
                "Event Review Committee are final and not subject to review.” The Success "
                "T&C states instead that the combined Settlement Values “will always "
                "equal $1.00”."
            ),
            reasoning=(
                "The single gap, and a narrower one than Kalshi's. The Rulebook supplies an "
                "upper bound with no floor -- “shall not exceed $1.00” permits less "
                "than $1.00 -- while the product terms assert an equality. The conflict runs "
                "in the direction that breaks conservation, and neither document states a "
                "precedence rule. Unlike Kalshi, the worst case here is at least *bounded*: "
                "combined payout cannot exceed $1.00. What is missing is the floor."
            ),
        ),
        MechanismProof(
            mechanism=SettlementMechanism.LAST_TRADED_PRICE,
            status=MechanismStatus.UNRESOLVED,
            rule_reference="Rulebook Rule 414(b)(2)",
            quoted_text=(
                "“If available, ForecastEx will use the most recent last prices of the "
                "Forecast Contracts affected to determine to the payout.”"
            ),
            reasoning=(
                "“Last prices” is plural and refers to both contracts. Under Rule "
                "401(d) inverse pricing a YES and a NO trade as a pair summing to $1.01, so "
                "taking both last prices literally would produce a combined payout of $1.01 "
                "-- which Rule 414(b)(3)'s own cap forbids. Some normalisation must occur and "
                "the rule does not say what it is."
            ),
        ),
        MechanismProof(
            mechanism=SettlementMechanism.OUTCOME_REVIEW,
            status=MechanismStatus.PROVEN_COMPLEMENTARY,
            rule_reference="Rulebook Rule 415(e); definition of Outcome",
            quoted_text=(
                "Rule 415(e): “The Event Review Committee shall review all relevant "
                "evidence and determine a final Outcome as soon as is feasible after the "
                "Event Review Process is initiated.” Definition: “Outcome \u2013 As "
                "related to Event Questions, whether an Event Question resolves to "
                "“Yes” or “No”.” And: “Event Question \u2013 "
                "Binary Yes/No questions… The Outcome of an Event Question is either "
                "“Yes” or “No”.”"
            ),
            reasoning=(
                "The Committee's output under Rule 415 is an Outcome, and Outcome is defined "
                "twice as binary. A binary Outcome feeds Rule 603(a), which pays $1.00 and "
                "$0.00 explicitly. So this path conserves. Kalshi has the same structure and "
                "cannot use it, because Kalshi's 6.3(c)(b) gives the same committee a "
                "competing allocation mandate; here the competing mandate sits in Rule 414 "
                "and is recorded separately above."
            ),
            both_branches_explicit=True,
        ),
        MechanismProof(
            mechanism=SettlementMechanism.NATURAL_PERSON_SCALAR,
            status=MechanismStatus.UNRESOLVED,
            rule_reference="no equivalent provision found",
            quoted_text=(
                "Searched the Rulebook for “death”, “dies” and "
                "“natural person”: the only hits concern Director tenure, "
                "information sharing and Public Director eligibility. No settlement "
                "provision keyed to the death of a contract's subject exists."
            ),
            reasoning=(
                "Recorded as unreachable rather than omitted. ForecastEx has no analogue of "
                "Kalshi Rule 6.3(e), so the natural-person scalar path that blocks "
                "person-subject markets on Kalshi does not arise here at all."
            ),
            reachable=False,
            unreachable_because=(
                "No provision in the Rulebook or either set of product terms keys settlement "
                "to the death or incapacity of a contract's subject."
            ),
        ),
    )


SUCCESS_FAMILY_PROOF: Final = ComplementConservationProof(
    subject="ForecastEx Success family (Participant Split / Fair Price product terms)",
    notional=Price.from_value("1.0000"),
    contract_type="Forecast Contract (binary Yes/No Event Question)",
    mechanisms=mechanism_findings(),
    sources=SOURCES,
    rounding=RoundingModel.RESIDUAL,
    rounding_note=(
        "Explicitly residual wherever a fraction can arise. Shared Outcomes and the Fair "
        "Price Rule both round the YES side and define NO as “$1 minus the "
        "\u2018Yes\u2019 payout”, so the rounding residue cannot escape the pair. The "
        "Success Terms and Conditions add the unconditional sentence “The combined "
        "Settlement Values of the \u2018Yes\u2019 and \u2018No\u2019 Positions will always "
        "equal $1.00.” Minimum tick is $0.01 and the Settlement Value is $1.00, both "
        "exactly representable in the scaled-integer arithmetic Phase 1 already uses."
    ),
    mechanism_closure_established=True,
    notes=(
        "Rule 604(a) prohibits holding both sides of the same Forecast Market and Rule "
        "604(b) nets an offsetting pair at $1.00 the same day, so the arbitrage-relevant "
        "path is OFFSET_NETTING and is proven. Rules 414 and 415 reach only positions held "
        "unhedged to resolution.",
        "Rule 401(d) fixes the cost of creating a pair at exactly $1.01, so same-market "
        "complement arbitrage is worth exactly minus one cent regardless of how sound the "
        "settlement semantics are. The semantic gate and the economic opportunity are "
        "different questions and this venue separates them sharply.",
        "Rule 612(c) pays a monthly coupon on collateral at (EFFR - 50bp) / 2, so a held "
        "position earns carry. That is an economic input, not a payoff-invariant input.",
        "Fees do not touch the Settlement Value: Rule 603(b)(2)(iii) takes ForecastEx's fee "
        "from collateral *in excess of* the settlement amount credited per pair, so the "
        "Phase-1 split between a semantic payoff proof and a separate fee engine survives.",
    ),
)
