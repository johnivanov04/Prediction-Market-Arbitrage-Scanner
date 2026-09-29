"""What Polymarket US actually pays, and where that is written down.

The finding that decides phase 2C is a correction to an earlier reading in this
same phase. Screening the DCM rulebook for the Kalshi failure modes -- "fair
allocation", "last traded price", any rounding rule at all -- returns nothing,
and the Contract Outcome definition is a clean two-state complement. It is easy
to read that as a venue with exhaustive settlement semantics.

It is the opposite. The rulebook is clean because it specifies no product's
payout. Rule 10.2 sends every specification to "the rules governing such
Contract", and Rule 1.5 makes those govern notwithstanding the Rules. The
payouts live in the Part 40 certifications -- and the athletic certification,
Rule 9.101, contains both of the things the rulebook does not:

* a **third terminal state**: a tie pays $0.50 to each side, so the contract the
  rulebook types as $1.00-or-$0.00 has a state that is neither;
* a **cancellation clause** permitting settlement "based on last-traded prices,
  $0.50 per contract, or other fair and equitable valuation", in the Exchange's
  "sole and absolute discretion", final and binding.

That second clause is the Kalshi Rule 6.3(c)(b) failure verbatim in substance:
an undefined valuation standard with no stated arithmetic. Screening a venue's
rulebook is therefore not a screen of the venue.

The tie state is not itself a defect -- $0.50 + $0.50 conserves the notional
exactly, and both branches are stated. It matters because the combinatorial
product models its own legs as settling "$1.00/$0.00". See
:mod:`predarb.venues.polymarket_us.relation_findings`.
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
from predarb.semantics.dependency import (
    DependencyClosure,
    DependencyDiscovery,
    DependencyMateriality,
    DependencySet,
    GoverningDocumentDependency,
    PayoutImpact,
    VersionBinding,
)
from predarb.semantics.evidence import (
    DocumentRetrieval,
    ExternalDocument,
    TextExtraction,
)
from predarb.semantics.precedence import (
    ControlFinding,
    SettlementPrecedenceProof,
    SupportingAuthority,
)
from predarb.semantics.settlement_census import SettlementMechanism
from predarb.venues.polymarket_us.governing_sources import (
    AEC_TERMS_SHA256,
    CAOC_TERMS_SHA256,
)

__all__ = [
    "AEC_COMPLEMENT_PROOF",
    "AEC_PRECEDENCE",
    "CLEARING_DEPENDENCY",
    "NOTIONAL",
    "RULEBOOK_SHA256",
    "TIE_PAYOUT",
]

NOTIONAL: Final = Price.from_value("1.0000")
TIE_PAYOUT: Final = Price.from_value("0.5000")

RULEBOOK_SHA256: Final = "5e3ba3880e63ffb1cfb649fbe2fb7ad305b85ee382ff194721689f2bec4d696d"

_RULEBOOK: Final = SourceComponent(
    name="Polymarket US Rulebook",
    version="2026-08-05",
    document_sha256=RULEBOOK_SHA256,
    url="https://polymarketexchange.com/files/legal/",
    retrieved_note="84 pages, extracted clean.",
)

_AEC: Final = SourceComponent(
    name="Athletic Event Contracts (Rule 9.101)",
    version="2025-09-30",
    document_sha256=AEC_TERMS_SHA256,
    url="https://www.cftc.gov/filings/ptc/",
    retrieved_note=(
        "Self-certified 2025-09-30, listed no earlier than 2025-10-07. The most "
        "recent athletic-event certification located; a later amendment may exist."
    ),
)

ORDINARY: Final = MechanismProof(
    mechanism=SettlementMechanism.ORDINARY_BINARY,
    status=MechanismStatus.PROVEN_COMPLEMENTARY,
    rule_reference="Rule 9.101(D), first two bullets",
    quoted_text=(
        "If the specified Outcome occurs (e.g., Team A wins), then each long AEC "
        "position shall receive one dollar ($1.00) and each short position shall "
        "receive zero dollars ($0.00). If the Outcome does not occur ..., then "
        "each long AEC position shall receive zero dollars ($0.00) and each short "
        "position shall receive one dollar ($1.00)."
    ),
    reasoning=(
        "Both sides are named in both branches, with figures. Nothing is inferred "
        "from silence, which is what the earlier venues could not supply."
    ),
    both_branches_explicit=True,
)

TIE: Final = MechanismProof(
    mechanism=SettlementMechanism.TIE_SPLIT,
    status=MechanismStatus.PROVEN_COMPLEMENTARY,
    rule_reference="Rule 9.101(D), third bullet",
    quoted_text=(
        "If the Outcome is a tie (e.g., Team A is tied with one or more teams "
        "after tie breaking rules have been applied), then each long and short "
        "AEC position shall receive fifty cents ($0.50)."
    ),
    reasoning=(
        "Conserves the notional exactly and states both sides, so it is a proven "
        "complement -- but it is a third terminal state, so this contract is not "
        "the two-state instrument the Rulebook's Contract Outcome definition "
        "describes. Conservation and binariness are different properties and "
        "this clause separates them."
    ),
    both_branches_explicit=True,
)

CANCELLATION: Final = MechanismProof(
    mechanism=SettlementMechanism.CANCELLATION_LAST_RESULTS,
    status=MechanismStatus.NOT_COMPLEMENTARY,
    rule_reference="Rule 9.101(K), 'Cancellation'",
    quoted_text=(
        "If an event is canceled prior to any Outcome determination, the Exchange, "
        "in its sole and absolute discretion, resolve any remaining open positions "
        "in a manner that it deems fair and appropriate, which may include a final "
        "settlement based on last-traded prices, $0.50 per contract, or other fair "
        "and equitable valuation. All such determinations by the Exchange shall be "
        "final and binding."
    ),
    reasoning=(
        "Nothing here constrains the two sides to sum to the notional. "
        "'Last-traded prices' is a single number, not a pair, and the rule does "
        "not say the short receives the residual; 'other fair and equitable "
        "valuation' states no arithmetic at all. This is reachable on any "
        "cancelled event, and it is the same defect that blocked Kalshi."
    ),
    both_branches_explicit=False,
)

UNDETERMINED_OUTCOME: Final = MechanismProof(
    mechanism=SettlementMechanism.INDETERMINATE_FALLBACK,
    status=MechanismStatus.UNRESOLVED,
    rule_reference="Rule 9.101(K), 'Postponement, Delay, or Suspension'",
    quoted_text=(
        "If no Outcome is determined within two-weeks of the Settlement Date, the "
        "Exchange may, in its sole and absolute discretion, resolve the market "
        "based on the information available at any time after the Settlement Date."
    ),
    reasoning=(
        "'Resolve the market based on the information available' does not say "
        "whether the result is forced onto the Outcome vocabulary of Section D or "
        "is a free valuation. Unresolved, so it blocks."
    ),
)

OUTCOME_REVIEW: Final = MechanismProof(
    mechanism=SettlementMechanism.OUTCOME_REVIEW,
    status=MechanismStatus.PROVEN_COMPLEMENTARY,
    rule_reference="Rulebook Rule 10.4(a) with the Rule 1.1 Contract Outcome definition",
    quoted_text=(
        "Following this review, the Company may determine the final outcome of a "
        "Contract. ... 'Contract Outcome' means the result of a Contract as "
        "determined in accordance with the Contract Terms. If the Expiration Value "
        "satisfies the Payout Condition, the Contract Outcome is $1.00, and the "
        "Settlement Amount is payable to holders of long positions ... If the "
        "Expiration Value does not satisfy the Payout Condition, the Contract "
        "Outcome is $0.00, and the Settlement Amount is payable to holders of "
        "short positions."
    ),
    reasoning=(
        "The review's output is typed to Contract Outcome, and that term is "
        "defined as one of two values with the Settlement Amount going to one "
        "side. This is genuinely stronger than Kalshi Rule 6.3(c)(b), whose "
        "output was an undefined 'fair allocation' of an unnamed quantity."
    ),
    both_branches_explicit=True,
)

EMERGENCY_REFUND: Final = MechanismProof(
    mechanism=SettlementMechanism.VOID_REFUND,
    status=MechanismStatus.UNRESOLVED,
    rule_reference="Rulebook Rule 2.8(d)(iii)",
    quoted_text=(
        "cancellation of a Contract and return of any funds paid to enter Trades on the Contracts"
    ),
    reasoning=(
        "A bounded refund rather than a discretionary valuation, and Rule "
        "9.101(I) does make the two sides' postings pair to the notional -- the "
        "purchaser posts the trade price, the seller one dollar minus it. What is "
        "not established is whether 'funds paid to enter Trades' is that margin "
        "alone or also the taker fee, which is charged to the balance at "
        "execution. Left unresolved rather than argued into a complement."
    ),
)

AEC_COMPLEMENT_PROOF: Final = ComplementConservationProof(
    subject="Polymarket US Athletic Event Contracts (Rule 9.101)",
    notional=NOTIONAL,
    contract_type="binary event contract, $1.00 notional, $0.001 tick",
    mechanisms=(
        ORDINARY,
        TIE,
        CANCELLATION,
        UNDETERMINED_OUTCOME,
        OUTCOME_REVIEW,
        EMERGENCY_REFUND,
    ),
    sources=(_RULEBOOK, _AEC),
    rounding=RoundingModel.UNSPECIFIED,
    rounding_note=(
        "The three enumerated outcomes land on the cent grid, so ordinary "
        "settlement has nothing to round. The cancellation path does: "
        "last-traded prices quote to $0.001 and no rule says how a payout derived "
        "from them is reduced to a payable amount, or whether the short's share "
        "is the residual."
    ),
    mechanism_closure_established=False,
    notes=(
        "Screening the DCM rulebook alone returns zero hits for 'fair allocation', "
        "'last traded price', 'fifty cents' and '0.50'. Every one of those "
        "concepts is present at this venue, in the product certification.",
        "Rule 9.101(B) also makes the Underlying discretionary: 'Notwithstanding "
        "the above, the Exchange may determine the Outcome in its sole and "
        "absolute discretion, using any publicly available data from recognized "
        "distributors of sports data that Polymarket US determines to be "
        "appropriate.'",
        "Closure is not established: Section K governs irregularities 'not covered "
        "in Section D' as an enumerated list, but Rulebook Rules 10.3, 10.4 and "
        "2.8(d) reach the same contracts and the interaction is not stated.",
    ),
)

_A = "product specifications govern settlement"
_B = "the Rules' binary Contract Outcome governs settlement"

AEC_PRECEDENCE: Final = SettlementPrecedenceProof(
    subject="Polymarket US: does Rule 1.5 carry product terms into settlement?",
    product_terms_sha256=AEC_TERMS_SHA256,
    rulebook_version="2026-08-05",
    rulebook_sha256=RULEBOOK_SHA256,
    product_contingency_text=(
        "Rule 9.101(D) third bullet: a tie pays each side fifty cents. Rule "
        "9.101(K): on cancellation the Exchange may settle at last-traded prices, "
        "$0.50, or other fair and equitable valuation."
    ),
    referenced_rule="Rulebook Rule 1.5",
    general_fallback_rule="Rulebook Rule 1.1, definition of Contract Outcome",
    market_outcome_definition=(
        "Contract Outcome is $1.00 if the Payout Condition is satisfied and $0.00 "
        "if it is not, with the Settlement Amount payable to the corresponding side."
    ),
    committee_output_semantics=(
        "Rule 10.4's output is the Contract Outcome, hence binary; Rule 9.101(K)'s "
        "output is an unconstrained valuation."
    ),
    authorities=(
        SupportingAuthority(
            source="Polymarket US Rulebook",
            reference="Rule 1.5",
            quoted_text=(
                "Notwithstanding any provision of these Rules to the contrary, the "
                "Product Specifications with respect to a particular Contract shall "
                "govern the applicability of these Rules to trading in such Contract "
                "and, in the event of any conflict between these Rules and the "
                "Product Specifications, the Product Specifications shall govern "
                "with respect to trading in the relevant Contract."
            ),
            supports="neither",
            document_sha256=RULEBOOK_SHA256,
            reasoning=(
                "Cuts both ways, which is why this is unresolved. The "
                "'notwithstanding' opener is as strong as precedence language gets, "
                "but the operative scope is stated three times as 'trading in' the "
                "Contract, and settlement is not trading. Read narrowly, the clause "
                "that decides a tie payment is outside its reach."
            ),
        ),
        SupportingAuthority(
            source="Polymarket US Rulebook",
            reference="Rule 10.2",
            quoted_text=(
                "Each Contract will meet such specifications, and all trading in "
                "such Contract will be subject to such procedures and requirements, "
                "as set forth in the rules governing such Contract."
            ),
            supports="A",
            document_sha256=RULEBOOK_SHA256,
            reasoning=(
                "Incorporates the product rules wholesale, and the Rulebook "
                "supplies no payout arithmetic of its own -- so on reading B the "
                "venue would have no stated payout for a tie at all, rather than a "
                "conflicting one."
            ),
        ),
        SupportingAuthority(
            source="Polymarket US Rulebook",
            reference="Rule 1.1, 'Contract Outcome'",
            quoted_text=(
                "If the Expiration Value satisfies the Payout Condition, the "
                "Contract Outcome is $1.00 ... If the Expiration Value does not "
                "satisfy the Payout Condition, the Contract Outcome is $0.00."
            ),
            supports="B",
            document_sha256=RULEBOOK_SHA256,
            reasoning=(
                "Exhaustive on its face over the Payout Condition's two truth "
                "values, and it is a Rule, not a product term. A tie is not a third "
                "truth value of a predicate, so on this reading a tied event is a "
                "Payout Condition that is not satisfied and pays the short $1.00 -- "
                "which is not what Rule 9.101(D) says happens."
            ),
        ),
        SupportingAuthority(
            source="Athletic Event Contracts",
            reference="Rule 9.101(D) and (K)",
            quoted_text=(
                "then each long and short AEC position shall receive fifty cents "
                "($0.50) ... which may include a final settlement based on "
                "last-traded prices, $0.50 per contract, or other fair and "
                "equitable valuation."
            ),
            supports="A",
            document_sha256=AEC_TERMS_SHA256,
            reasoning=(
                "The product terms plainly intend to govern settlement -- Section D "
                "is titled 'Settlement' and Section K 'Additional Settlement "
                "Conditions'. Intent is not the same as precedence, but it makes "
                "reading B hard to sustain in practice."
            ),
        ),
    ),
    committee_output_is_binary=True,
    product_rule_controls=ControlFinding.UNRESOLVED,
    general_rule_also_reachable=None,
    guidelines_located=False,
    guidelines_note=(
        "No published interpretation, FAQ or advisory reconciling Rule 1.5's "
        "'trading in' scope with product-level settlement clauses was located."
    ),
    notes=(
        "This is structurally the Kalshi Rule 7.1 versus Rule 6.3(c) problem "
        "again, with the documents swapped: there, two rules in one book; here, a "
        "book and a certification with a precedence clause whose scope does not "
        "quite cover the question.",
        "The two readings are not cosmetic. Under A a tied athletic event pays "
        "$0.50 to both sides; under B it pays the short $1.00. A basket built on "
        "either is wrong half the time.",
        "The CAOC certification " + CAOC_TERMS_SHA256[:12] + " deepens it by "
        "describing its own legs as settling '$1.00/$0.00', which is reading B, "
        "while those legs' own terms are reading A.",
    ),
)


_CLEARING_SHA256: Final = "a5a91041b08f8e1e06c662a62c25fb06ccb6d4eee3c5bf2f3f58aca927ad1fe6"

CLEARING_DEPENDENCY: Final = DependencySet(
    parent="polymarket_us_rulebook",
    closure=DependencyClosure.UNKNOWN,
    dependencies=(
        GoverningDocumentDependency(
            parent="polymarket_us_rulebook",
            reference="Clearinghouse Rules (QC Clearing LLC)",
            source_name="polymarket_us_clearing_rulebook",
            source=ExternalDocument(
                url="https://www.cftc.gov/filings/orgrules/rules12312536031.pdf",
                retrieval=DocumentRetrieval.RETRIEVED,
                content_sha256=_CLEARING_SHA256,
                extraction=TextExtraction.CLEAN,
                note="Version 2025-12-31, 60 pages, extracted clean.",
            ),
            payout_impacts=frozenset({PayoutImpact.PAYOUT_AMOUNT, PayoutImpact.REFUND_OR_VOID}),
            materiality=DependencyMateriality.MATERIAL,
            discovery=DependencyDiscovery.DECLARED_BY_REVIEWER,
            version_binding=VersionBinding.AS_AMENDED_FROM_TIME_TO_TIME,
        ),
    ),
    note=(
        "Closure is UNKNOWN rather than ENUMERATED, and deliberately so. The "
        "clearing rulebook held is dated 2025-12-31 -- four months older than the "
        "DCM rulebook that points at it -- so the version actually in force when "
        "the 2026-08-05 Rules took effect has not been read. The document we do "
        "hold defines no payout of its own (Rule 5.3(d) pays 'in accordance with "
        "the relevant Contract Rules') and restricts offset to Contracts with the "
        "same terms and conditions, but a newer one could do either differently."
    ),
)
"""The clearing link, recorded as an open dependency rather than a closed one.

This is the phase-1 fail-closed rule applied to a currency gap instead of an
unreadable PDF: a document identified but not read in its governing version
cannot contribute an ENUMERATED closure, and an unknown closure blocks.
"""
