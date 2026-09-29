"""ProphetX: screened and closed on one rule.

The phase-2C screen could not reach a verdict because the only document
obtained was an Exhibit L Core Principles Chart. The rulebook itself -- DCM
Exhibit M, Document Version 1.0, filed December 2025, 54 pages, extracted clean
-- settles it, and settles it against ProphetX.

Rule 5.2(c) hands the Exchange sole discretion over the Settlement Value and,
on a Settlement Disruption, over five remedies including the last traded price,
voiding contracts, and "such other action as it deems appropriate". Rule 5.2(d)
is the only constraint on the pair:

    "In no case shall the combined payout across positions exceed the stated
     maximum Settlement Value of the Contract."

That is a ceiling, and a ceiling is not a complement. ``long + short <= $1`` is
satisfied by ``$0.40 + $0.40``, by ``$0 + $0``, and by every other shortfall.
Conservation needs an equality or a residual definition, and Rule 5.2 supplies
neither -- while Rule 5.2(f) makes every determination final and unappealable.

Classified NOT_PROVEN rather than DISPROVEN, on the distinction phase 2C was
corrected for: a cap permits a shortfall without authorising one, and no
ProphetX text says any terminal state pays less than the notional in total. The
practical consequence is identical -- it blocks -- but the record says what is
true, and the gap could be closed by a product-specific rule proven to override
Rule 5.2 for every reachable terminal state. None was located.
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

__all__ = [
    "PROPHETX_PROOF",
    "RULEBOOK_SHA256",
    "SETTLEMENT_DISCRETION",
]

RULEBOOK_SHA256: Final = "178d04d7408b493c28ed7961eeeec7c2cdaebd2d25b48c015bb206685e6b0ea7"
"""DCM Exhibit M Rulebook, Document Version 1.0, filed 2025-12-05.

Retrieved from ``cftc.gov/sites/default/files/filings/documents/2025/
orgdcmprophxexhibgm251205.pdf``; 54 pages, no empty pages.
"""

_RULEBOOK: Final = SourceComponent(
    name="ProphetX DCM Rulebook (Exhibit M)",
    version="1.0",
    document_sha256=RULEBOOK_SHA256,
    url="https://www.cftc.gov/sites/default/files/filings/documents/2025/orgdcmprophxexhibgm251205.pdf",
    retrieved_note="54 pages, extracted clean.",
)

SETTLEMENT_DISCRETION: Final = MechanismProof(
    mechanism=SettlementMechanism.LAST_TRADED_PRICE,
    status=MechanismStatus.UNRESOLVED,
    rule_reference="ProphetX DCM Rulebook Rule 5.2(c)-(f)",
    quoted_text=(
        "The Exchange will determine the Settlement Value of any Contract, in "
        "its sole discretion, based on information it deems reliable. If a "
        "Settlement Disruption prevents the determination of a Settlement "
        "Value, the Exchange may, in its sole discretion: (1) Use the last "
        "traded price of the Contract as the basis for allocating Settlement "
        "Value; (2) Delay settlement; (3) Void or cancel affected Contracts; "
        "(4) Interpret the Contract's terms and conditions where ambiguity "
        "exists ...; or (5) Take such other action as it deems appropriate "
        "under these Rules. (d) In no case shall the combined payout across "
        "positions exceed the stated maximum Settlement Value of the Contract. "
        "... (f) All determinations made by the Exchange under this Rule are "
        "final and not subject to appeal."
    ),
    reasoning=(
        "Rule 5.2(d) bounds the combined payout from above and never from "
        "below, so it is consistent with the two sides summing to anything at "
        "or under the notional. 'Allocating Settlement Value' from a last "
        "traded price names no split, and (c)(5) is open-ended. A Settlement "
        "Disruption is defined broadly enough -- 'errors, technical or human "
        "mistakes, data discrepancies, cancellations, postponements, "
        "alterations, technical failures, force majeure events, or other "
        "comparable events' -- that this path is plainly reachable."
    ),
)

PROPHETX_PROOF: Final = ComplementConservationProof(
    subject="ProphetX LLC event contracts",
    notional=Price.from_value("1.0000"),
    contract_type="event contract, Settlement Value per Contract Specifications",
    mechanisms=(SETTLEMENT_DISCRETION,),
    sources=(_RULEBOOK,),
    rounding=RoundingModel.UNSPECIFIED,
    rounding_note="No rounding rule was located; moot while the split is unstated.",
    mechanism_closure_established=False,
    notes=(
        "Screen closed. No further ProphetX research is warranted unless a "
        "product-specific rule is proven to override Rule 5.2 for every "
        "reachable terminal state.",
        "A cap is not a complement: 'combined payout shall not exceed the "
        "maximum Settlement Value' is satisfied by any shortfall.",
        "Rule 5.2(a) additionally lets the Exchange 'reverse, amend, or "
        "resettle a settlement' after the fact, so even a conserving settlement "
        "is not final in the direction that matters to a holder.",
    ),
)
