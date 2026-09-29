"""Adjudication: does a product's Rule 7.1 contingency displace Rule 6.3(c)?

Subject: ``KXRIEMANNRES-40-27JAN01``, the cleanest modern Binary Contract on the
exchange -- a non-natural-person underlying, no tie split, no last-fair-price
clause, no void or refund provision, evidence COMPLETE, live book.

Result: ``PRECEDENCE_UNRESOLVED``.

The one sub-finding that *is* well supported
--------------------------------------------
Rule 7.1's output is confined to a binary Market Outcome for this contract. Three
texts converge: the Committee is defined as "a committee of the Board **to
determine Market Outcomes** in accordance with Chapter 7"; Rule 7.1(a) says it
"will determine the final Market Outcome"; and "Market Outcome" for a Binary
Contract is YES or NO "unless otherwise specified in the contract terms", which
these terms do not do. Rule 7.1(c)'s "full discretion" is discretion "in
resolving the Market Outcome Review Process" -- over the determination, not over
what kind of thing is determined. So if Rule 7.1 governs, settlement is binary.

Why that is not enough
----------------------
The question is not what Rule 7.1 yields. It is whether Rule 6.3(c) still fires.
And there the evidence runs both ways with nothing to break the tie:

* Rule 6.3(a) does let product terms control -- but that permission sits inside
  the ordinary-payout subsection and says nothing about 6.3(c)'s machinery.
* Rule 6.3(c) has its own trigger, opens by reserving to Kalshi "sole discretion
  to interpret a Contract's Terms and Conditions", carries **no** notwithstanding
  clause and **no** carve-out, and expressly extends beyond contingencies the
  terms fail to address ("includes, but is not limited to").
* The Rulebook contains no general precedence rule. Searched for
  "notwithstanding", "shall control", "supersede", "takes precedence", "rules of
  construction", "in the event of any conflict": the only conflict rule found
  governs Exchange-versus-Clearing-House, which is a different question.
* The Rulebook contradicts itself about this very committee. Chapter 1 defines it
  to determine Market Outcomes; Rule 6.3(c)(b) has it make "a binding
  determination of fair allocation". Both cannot be its mandate in the same
  state, and nothing says which yields.
* No standalone Market Outcome Review Process Guidelines were located.

What Kalshi said it was doing
-----------------------------
Three migration filings, and none describes a change of mechanism:

* 2026-02-04 (rainfall) -- the change list names only a Source Agency and
  Underlying amendment. The contingency citation changed **without being
  mentioned at all**.
* 2026-04-08 (acquisition) -- "Contingencies have been updated in line with the
  new Rulebook."
* 2026-06-08 (word-said) -- "Amendment to Contingencies to align with Exchange
  Rulebook."

"Align with" and "in line with" are conformance language. No filing calls it a
payout-treatment change, an outcome-review change, or a settlement
clarification; none says substantive or non-substantive. That cuts against
Reading A, which needs the amendment to have genuinely moved payout
determination out of 6.3(c).

But it does not establish Reading B either, and here is the bind: citing Rule 7.1
for *payouts* does not in fact align with a Rulebook whose payout-determination
provision is 6.3(c). The stated rationale and the filed text point in different
directions. An intent to conform cannot override the words actually certified,
and the words actually certified name a rule that determines outcomes rather
than payouts. Reading either one as controlling means overriding the other, and
no authority found does that.

This is the end of the road for the question. It is a legal-interpretation
matter that the governing documents leave open.
"""

from __future__ import annotations

from typing import Final

from predarb.semantics.precedence import (
    ControlFinding,
    SettlementPrecedenceProof,
    SupportingAuthority,
)

__all__ = ["RIEMANN_PRECEDENCE"]

_RULEBOOK_SHA: Final = "3b6d4ffd5b32330d3466179d4cae610372d07511123c9976bc6cbb1b5185240b"
_TERMS_SHA: Final = "109af26b85199725539a0f5d5b3567f63b97eac6be3fb4bfaa0d373949a8e618"

AUTHORITIES: Final[tuple[SupportingAuthority, ...]] = (
    SupportingAuthority(
        source="Exchange Rulebook v1.29",
        reference="Chapter 1 definition of Outcome Review Committee",
        quoted_text=(
            "“Outcome Review Committee” means a committee of the Board to determine "
            "Market Outcomes in accordance with Chapter 7."
        ),
        supports="A",
        document_sha256=_RULEBOOK_SHA,
        reasoning=(
            "The committee's defined mandate is Market Outcomes, not payouts or "
            "allocations. Acting under Chapter 7 it determines an outcome."
        ),
    ),
    SupportingAuthority(
        source="Exchange Rulebook v1.29",
        reference="Chapter 1 definition of Market Outcome",
        quoted_text=(
            "“Market Outcome” means the result of the Contract. For a Binary "
            "Contract, if the Expiration Value is encompassed within the Payout Criterion, "
            "then the “Market Outcome” is YES. Otherwise, the “Market "
            "Outcome” is NO, unless otherwise specified in the contract terms."
        ),
        supports="A",
        document_sha256=_RULEBOOK_SHA,
        reasoning=(
            "Binary and exhaustive for this contract, whose terms do not specify otherwise. "
            "Confines any Market Outcome determination to {YES, NO}."
        ),
    ),
    SupportingAuthority(
        source="Exchange Rulebook v1.29",
        reference="Rule 7.1(a) and 7.1(c)",
        quoted_text=(
            "Under this process, the Outcome Review Committee will determine the final Market "
            "Outcome… The Outcome Review Committee has full discretion in resolving the "
            "Market Outcome Review Process."
        ),
        supports="A",
        document_sha256=_RULEBOOK_SHA,
        reasoning=(
            "Rule 7.1 in full contains no instance of 'payout', 'amount', 'Settlement Value' "
            "or 'price'. Its discretion is over resolving the process, not over the output "
            "type, which 7.1(a) names as the Market Outcome."
        ),
    ),
    SupportingAuthority(
        source="Exchange Rulebook v1.29",
        reference="Rule 6.3(a) final sentence",
        quoted_text=(
            "The terms and conditions of a Binary Contract may include specific settlement "
            "rules that will control the settlement of that Binary Contract."
        ),
        supports="A",
        document_sha256=_RULEBOOK_SHA,
        reasoning=(
            "The only text giving product-specific settlement provisions priority. It sits "
            "inside the ordinary-payout subsection and does not say whether it reaches "
            "6.3(c)'s indeterminacy machinery -- which is exactly the gap."
        ),
    ),
    SupportingAuthority(
        source="Exchange Rulebook v1.29",
        reference="Rule 6.3(c) trigger",
        quoted_text=(
            "To settle a Contract… Kalshi has sole discretion to interpret a Contract's "
            "Terms and Conditions. If, when a Contract expires, it cannot be determined "
            "whether the Expiration Value of the Underlying is within the scope of the Payout "
            "Criterion… Kalshi will determine the payouts to the holders of long and "
            "short positions in such Contracts. This includes, but is not limited to, "
            "circumstances where the Underlying cannot be measured and this contingency is "
            "not explicitly addressed in the Contract terms."
        ),
        supports="B",
        document_sha256=_RULEBOOK_SHA,
        reasoning=(
            "An independent, epistemic trigger with no notwithstanding clause and no "
            "carve-out for product terms. 'Not limited to' extends it past contingencies the "
            "terms fail to address, so addressing one does not evidently switch it off."
        ),
    ),
    SupportingAuthority(
        source="Exchange Rulebook v1.29",
        reference="Rule 6.3(c)(b) committee mandate",
        quoted_text=(
            "the Outcome Review Committee will be responsible for making a binding "
            "determination of fair allocation. Determinations of the Outcome Review Committee "
            "are final and not subject to review."
        ),
        supports="B",
        document_sha256=_RULEBOOK_SHA,
        reasoning=(
            "Assigns the same committee an allocation mandate, contradicting its Chapter 1 "
            "definition. The Rulebook does not say which controls, so the contradiction is "
            "itself unresolved rather than evidence for either reading."
        ),
    ),
    SupportingAuthority(
        source="CFTC filing rules040826937 (2026-04-08)",
        reference="Amendment of 'Which <company> in <set> will be acquired first?'",
        quoted_text="4. Contingencies have been updated in line with the new Rulebook",
        supports="B",
        document_sha256="6b805aa81dfafadf",
        reasoning=(
            "Kalshi's own characterisation is conformance to the Rulebook, not a change of "
            "settlement mechanism. Reading A needs the amendment to have moved payout "
            "determination out of 6.3(c); this says it was aligning citations."
        ),
    ),
    SupportingAuthority(
        source="CFTC filing rules0608265775 (2026-06-08)",
        reference="Amendment of 'Will <word> be said by <person>…?'",
        quoted_text="1. Amendment to Contingencies to align with Exchange Rulebook",
        supports="B",
        document_sha256="1ffe2ab2fcd5782b",
        reasoning="Same conformance framing, three months later.",
    ),
    SupportingAuthority(
        source="CFTC filing rules02042638732 (2026-02-04)",
        reference="Amendment of 'Will there be <count> inches of rain in <city>…?'",
        quoted_text=(
            "The changes are as follows: 1. Amendment to specify the NWS as a primary Source "
            "Agency and modify the Underlying in line with more rapid potential resolution"
        ),
        supports="B",
        document_sha256="7c4eb236d4c4f8d0",
        reasoning=(
            "The contingency citation changed from 6.3(b)/6.3(d) to 7.1 and the change list "
            "does not mention it at all. A mechanism change nobody described is hard to read "
            "as a deliberate mechanism change."
        ),
    ),
    SupportingAuthority(
        source="Exchange Rulebook v1.29",
        reference="absence of a precedence rule",
        quoted_text=(
            "Searched for 'notwithstanding', 'shall control', 'supersede', 'takes "
            "precedence', 'rules of construction' and 'in the event of any conflict'. The "
            "only conflict rule found: 'In the event of any conflict or inconsistency between "
            "these Rules and the Clearing House Rules… the Clearing House Rules shall "
            "prevail.'"
        ),
        supports="neither",
        document_sha256=_RULEBOOK_SHA,
        reasoning=(
            "No general specific-over-general canon is enacted anywhere in the Rulebook, and "
            "the one conflict rule that exists addresses a different pair of documents. "
            "Nothing in the governing text breaks the tie."
        ),
    ),
)

RIEMANN_PRECEDENCE: Final = SettlementPrecedenceProof(
    subject="KXRIEMANNRES-40-27JAN01 (CONJECTURE template, certified 2026-05-22)",
    product_terms_sha256=_TERMS_SHA,
    rulebook_version="1.29",
    rulebook_sha256=_RULEBOOK_SHA,
    product_contingency_text=(
        "Contingencies: Before Settlement, Kalshi may, at its sole discretion, initiate the "
        "Market Outcome Review Process pursuant to Rule 7.1 of the Rulebook. If an Expiration "
        "Value cannot be determined on the Expiration Date, Kalshi has the right to determine "
        "payouts pursuant to Rule 7.1 in the Rulebook."
    ),
    referenced_rule="Rule 7.1 (The Market Outcome Review Process)",
    general_fallback_rule="Rule 6.3(c) (payout determination when indeterminate)",
    market_outcome_definition=(
        "For a Binary Contract: YES if the Expiration Value is encompassed within the Payout "
        "Criterion, otherwise NO, unless otherwise specified in the contract terms. These "
        "terms do not specify otherwise."
    ),
    committee_output_semantics=(
        "A Market Outcome. The Committee is defined as existing 'to determine Market Outcomes "
        "in accordance with Chapter 7'; Rule 7.1 contains no payout, amount, Settlement Value "
        "or price language anywhere. Its 'full discretion' is over resolving the process."
    ),
    authorities=AUTHORITIES,
    committee_output_is_binary=True,
    product_rule_controls=ControlFinding.UNRESOLVED,
    general_rule_also_reachable=None,
    guidelines_located=False,
    guidelines_note=(
        "No standalone Market Outcome Review Process Guidelines were located in the CFTC "
        "filing record or on the public regulatory page. The Rulebook's own Chapter 7 is the "
        "only text found governing the process, and it does not address its interaction with "
        "Rule 6.3(c)."
    ),
    notes=(
        "The committee-output-is-binary finding is well supported and is the durable result "
        "of this pass; it is the control question that fails.",
        "Rule 6.3(e) is unreachable for this contract: the Underlying's primary subject is a "
        "mathematical conjecture, not a natural person.",
        "If a future filing or official interpretation established that Rule 7.1 displaces "
        "6.3(c) for these templates, mechanisms would collapse into 6.3(a), p would be "
        "confined to {0, N}, and the rounding blocker would fall away with them -- that is "
        "the single cheapest thing to chase if this question is ever reopened.",
    ),
)
