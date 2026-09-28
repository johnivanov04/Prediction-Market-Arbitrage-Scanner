"""Mechanism-by-mechanism findings for the complement-conservation invariant.

Research record. Every quotation was read from the document named, at the hash
recorded in :mod:`predarb.venues.kalshi.rulebook_history` and
:mod:`predarb.venues.kalshi.klear_history`.

The headline: the clearing layer supplies no conservation constraint at all.
Kalshi Klear Rule 6.2(C) says the Clearing House "will transfer the amount
payable **under the Contract's terms**" -- it defines no payout and imposes no
sum. Its risk concept, Maximum Downside Exposure, is "the Total Exposure less
the minimum possible settlement value of the positions of **a Participant**":
a per-participant measure, not a per-pair pot. The word "matched" does not appear
in the Klear rulebook, and after novation nothing in it links a long to a
particular short. Full collateralization proves each participant can cover their
own worst case; it says nothing about what two opposite positions sum to.

So the entire burden falls on Exchange Rule 6.3 -- and the two places it could
have been discharged are the two places the text goes quiet.
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

__all__ = ["CONJECTURE_FAMILY_PROOF", "SOURCES", "mechanism_findings"]

SOURCES: Final[tuple[SourceComponent, ...]] = (
    SourceComponent(
        name="exchange_rulebook",
        version="1.29",
        document_sha256="3b6d4ffd5b32330d3466179d4cae610372d07511123c9976bc6cbb1b5185240b",
        url=(
            "https://kalshi-public-docs.s3.amazonaws.com/regulatory/rulebook/"
            "Kalshi%20DCM%20Rulebook%20v.1.29.pdf"
        ),
        retrieved_note="read in full, pypdf CLEAN, 85 pages",
    ),
    SourceComponent(
        name="klear_dco_rulebook",
        version="1.3",
        document_sha256="59e85eb6c635b28a9f5baa2589f859e1e17cc7faa988fd0bb38248f6759832f9",
        url="https://www.cftc.gov/sites/default/files/filings/orgrules/25/07/rules07042525566.pdf",
        retrieved_note=(
            "latest publicly readable Klear text; v1.4 is identified but filed "
            "confidentially, and its own certification states the amendments 'do not "
            "alter the economics, risk, or operation of any currently cleared Contract'"
        ),
    ),
    SourceComponent(
        name="member_agreement",
        version="unversioned",
        document_sha256="e1c8a173b7a5c2948a01624251b96be36d9a61bdd09cf134f8f226d61657dfe4",
        url="https://kalshi.com/docs/kalshi-member-agreement.pdf",
        retrieved_note="operator-supplied; the document states no version or date",
    ),
    SourceComponent(
        name="contract_terms/CONJECTURE",
        version="certified 2026-05-22",
        document_sha256="109af26b85199725539a0f5d5b3567f63b97eac6be3fb4bfaa0d373949a8e618",
        url="https://assets.kalshi.com/contract_terms/CONJECTURE.pdf",
        retrieved_note="pypdf CLEAN",
    ),
    SourceComponent(
        name="product_certification/CONJECTURE",
        version="certified 2026-05-22",
        document_sha256="0ae7c3b2638d13c92ed12a58105c959c312f8134b78935b0177ef4b033d62661",
        url="https://assets.kalshi.com/regulatory/product-certifications/CONJECTURE.pdf",
        retrieved_note=(
            "pypdf CLEAN; carries the explicit both-branch payout prose the ordinary-binary "
            "mechanism rests on"
        ),
    ),
)


def mechanism_findings() -> tuple[MechanismProof, ...]:
    """The finding for each settlement mechanism, with the text it rests on."""
    return (
        MechanismProof(
            mechanism=SettlementMechanism.ORDINARY_BINARY,
            status=MechanismStatus.PROVEN_COMPLEMENTARY,
            rule_reference=(
                "Exchange Rulebook v1.29 Rule 6.3(a), with both branches stated expressly in "
                "this product's own certification (CONJECTURE.pdf, sha256 0ae7c3b2638d13c9)"
            ),
            quoted_text=(
                "Product certification: \u201cIf the Market Outcome is \u201cYes,\u201d "
                "meaning that an event occurs that is encompassed within the Payout Criterion, "
                "then the long position holders are paid an absolute amount proportional to "
                "the size of their position and the short position holders receive no payment. "
                "If the Market Outcome is \u201cNo,\u201d then the short position holders are "
                "paid an absolute amount proportional to the size of their position and the "
                "long position holders receive no payment.\u201d And: \u201cthe "
                "Contract\u2019s payout structure is characterized by the payment of an "
                "absolute amount to the holder of one side of the option and no payment to the "
                "counterparty.\u201d Rulebook Rule 6.3(a) supplies the amount: \u201csuch "
                "Contract will pay the Settlement Value for such Contracts (e.g. $1.00) to the "
                "holders of long positions\u2026 Conversely\u2026 to the holders of short "
                "positions.\u201d"
            ),
            reasoning=(
                "Both branches are established from text rather than from silence. The "
                "certification states that the losing side receives no payment in each "
                "direction, and states the structure generally as payment to one side and "
                "none to the counterparty; the Rulebook supplies the amount as the Settlement "
                "Value. So p is N or 0 and the pair sums to N. Bound to this product's filing "
                "hash, not to generic prose from another product."
            ),
            both_branches_explicit=True,
        ),
        MechanismProof(
            mechanism=SettlementMechanism.FAIR_ALLOCATION,
            status=MechanismStatus.UNRESOLVED,
            rule_reference="Exchange Rulebook v1.29 Rule 6.3(c)(b)",
            quoted_text=(
                "If a last traded price is not available, or if Kalshi determines at its sole "
                "discretion that the most recent last traded price does not represent a fair "
                "settlement, the Outcome Review Committee will be responsible for making a "
                "binding determination of fair allocation. Determinations of the Outcome "
                "Review Committee are final and not subject to review."
            ),
            reasoning=(
                "The blocker. 'Allocation' is not defined, and the sentence never says what "
                "is allocated -- there is no 'of the Settlement Value', no proportion, no "
                "pot, no statement that one side's payout is the residual of the other. The "
                "enclosing sentence of 6.3(c) speaks of determining 'the payouts to the "
                "holders of long and short positions', which is two payouts, not a division "
                "of one amount. Searched Rulebook v1.29 and Klear v1.3 for allocation-of, "
                "proportion-of, conservation and per-matched-contract language: none applies "
                "here. The word suggests partition; suggestion is not an invariant."
            ),
        ),
        MechanismProof(
            mechanism=SettlementMechanism.LAST_TRADED_PRICE,
            status=MechanismStatus.UNRESOLVED,
            rule_reference="Exchange Rulebook v1.29 Rule 6.3(c)(a)",
            quoted_text=(
                "If available, Kalshi may use the last traded price of the Contract to "
                "determine the payout (ex. a contract that last traded at $0.10 for the long "
                "side and $0.90 for the short side would pay out $0.10 to holders of long "
                "positions and $0.90 to the holders of short positions)."
            ),
            reasoning=(
                "The worked example is complementary, and that is all it is. The operative "
                "clause is 'may use the last traded price… to determine the payout' with "
                "no algorithm for the second side. The example's own framing -- one trade "
                "described as $0.10 for the long side and $0.90 for the short -- would give "
                "complementarity by construction if the Rulebook defined price that way, but "
                "v1.29 contains no definition of 'Price' at all. So the duality the example "
                "relies on is nowhere established."
            ),
        ),
        MechanismProof(
            mechanism=SettlementMechanism.INDETERMINATE_FALLBACK,
            status=MechanismStatus.UNRESOLVED,
            rule_reference="Exchange Rulebook v1.29 Rule 6.3(c) vs. Rule 7.1",
            quoted_text=(
                "Contract terms (2026 template): 'If an Expiration Value cannot be determined "
                "on the Expiration Date, Kalshi has the right to determine payouts pursuant "
                "to Rule 7.1 in the Rulebook.' Rule 7.1 in full contains no instance of "
                "'payout', 'amount', 'Settlement Value' or 'price'; it provides that 'the "
                "Outcome Review Committee will determine the final Market Outcome'."
            ),
            reasoning=(
                "An unresolved fork, and a consequential one. Reading A: Rule 7.1 governs, "
                "the Committee determines a Market Outcome, and for a Binary Contract that is "
                "YES or NO -- so Rule 6.3(a) pays the whole Settlement Value to one side, "
                "which would be both strictly two-state and complementary. Reading B: 7.1 "
                "supplies no payout methodology, so Rule 6.3(c) applies of its own force and "
                "the fractional paths open. The 2026 template redirected payout determination "
                "from 6.3(b) to a rule that determines outcomes rather than payouts; which "
                "reading governs is a legal question, not one this system can answer."
            ),
        ),
        MechanismProof(
            mechanism=SettlementMechanism.NATURAL_PERSON_SCALAR,
            status=MechanismStatus.UNRESOLVED,
            rule_reference="Exchange Rulebook v1.29 Rule 6.3(e)",
            quoted_text=(
                "If a natural person who is the primary subject of a Contract's Underlying or "
                "Payout Criterion dies prior to Expiration, Kalshi may, in its sole "
                "discretion, settle the Contract at the last traded price prior to the "
                "death… If Kalshi determines that no last traded price represents a fair "
                "settlement, the Outcome Review Committee shall determine the fair settlement "
                "price."
            ),
            reasoning=(
                "Unreachable for this family, but recorded because it is the closest the "
                "Rulebook comes to complementarity: it speaks of settling the Contract at a "
                "*price*, singular, and of the Committee determining a 'fair settlement "
                "price' rather than an allocation. A single price applied to a matched pair "
                "would be complementary; the rule still never says what the short side "
                "receives. Notably stronger drafting than 6.3(c)(b), and still not a proof."
            ),
            reachable=False,
            unreachable_because=(
                "The Underlying of the CONJECTURE family is 'reporting from the Source "
                "Agencies regarding the publication, official recognition, retraction, "
                "withdrawal, and repudiation of a proof, disproof, counterexample, or other "
                "resolution of <conjecture>'. The primary subject is a mathematical "
                "conjecture, not a natural person, so Rule 6.3(e) cannot be triggered."
            ),
        ),
        MechanismProof(
            mechanism=SettlementMechanism.OUTCOME_REVIEW,
            status=MechanismStatus.UNRESOLVED,
            rule_reference="Exchange Rulebook v1.29 Rule 7.1(c)",
            quoted_text=(
                "The Outcome Review Committee has full discretion in resolving the Market "
                "Outcome Review Process. The determinations made by the Outcome Review "
                "Committee are final."
            ),
            reasoning=(
                "Reachable for every contract: initiated at Kalshi's sole discretion with no "
                "carve-out for product terms. Whether it is complementary depends on the same "
                "fork as the indeterminate fallback -- an outcome determination composes with "
                "6.3(a) and conserves, a payout determination does not obviously."
            ),
        ),
    )


CONJECTURE_FAMILY_PROOF: Final = ComplementConservationProof(
    subject="KXRIEMANNRES-40-27JAN01 (CONJECTURE family, non-natural-person)",
    notional=Price.from_value("1.0000"),
    contract_type="Binary Contract",
    mechanisms=mechanism_findings(),
    sources=SOURCES,
    rounding=RoundingModel.UNSPECIFIED,
    rounding_note=(
        "Neither Exchange Rulebook v1.29 nor Klear v1.3 states a settlement precision, a "
        "rounding direction, or whether the two sides are rounded independently or one "
        "computed as the residual of the other. Searched both for 'round', 'nearest cent', "
        "'precision', 'decimal', 'truncat' and 'fraction of a cent': the only hits in the "
        "Exchange Rulebook are the words 'background' and 'grounds'. The one rounding "
        "instruction found anywhere is at product level -- the ENTITYOUTCOME terms' "
        "'Settlement Value equal to $1/N, rounded down' -- and it fixes one side only. "
        "With no rounding model, no exact conservation bound can be stated."
    ),
    mechanism_closure_established=True,
    notes=(
        "Chosen because its Underlying is a mathematical conjecture, which puts Rule 6.3(e) "
        "out of reach without needing an argument about mortality.",
        "Its own terms add no alternate mechanism: no last-fair-price clause, no $1/N tie "
        "split, no void or refund provision, no cancellation-to-last-results rule.",
        "Evidence completeness for this market is COMPLETE under the Step-8 policy as "
        "corrected; that is a separate question from this proof and does not bear on it.",
    ),
)
