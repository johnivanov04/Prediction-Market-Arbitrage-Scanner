"""What Rothera pays, and the one citation that does not hold up.

Rothera is the first venue in this research whose product terms state the
residual construction outright. Every one of the fourteen families read says,
for its cancellation path:

    "Long position holders will receive the number of Contracts held multiplied
     by the fair market price, and short position holders will receive the
     number of Contracts held multiplied by $1 minus the fair market price."

That is a complete answer to the question phases 1 and 2 kept failing on. It
does not matter who picks the price or how badly: the short is *defined* as the
residual, so long + short is one notional for every value of ``p``. Discretion
over ``p`` is harmless; discretion over two independent payouts is not, and this
clause is unambiguously the first kind.

Ordinary settlement is equally explicit -- "the long position holders are paid
... and the short position holders receive no payment", and the converse -- so
the losing side's zero is written down rather than inferred.

Three things stop this from being a proof.

**The citation does not hold.** Every fair-market clause says the price is
determined "pursuant to Rothera DCM Rule 7.2", and the products add "Consistent
with DCM Rule 7.2, Rothera reserves the right to make settlement
determinations". Rule 7.2 is titled *Procedures*. It authorises the Company to
adopt trading procedures -- daily settlement prices, price dissemination,
recordkeeping, surveillance, order and position limits, daily price fluctuation
limits -- and says nothing about final settlement of an event contract, about a
fair market price, or about reserving settlement determinations. The rulebook's
one definition of a fair market price is in the Error Trade Policy, for applying
No Cancellation Ranges, a different purpose entirely.

This does **not** break conservation, and saying so would repeat the mistake
phase 2C was corrected for. The residual formula lives in the product terms and
holds whatever Rule 7.2 does or does not authorise. What it breaks is the
*procedure* for choosing ``p``: Rule 7.2(B) says procedures adopted under it are
published on the Website, so the real content is a further incorporated document
that has not been located.

**Two paths omit the formula.** Within the same certifications, two fair-market
clauses state the price but not the split: baseball's disqualification-before-
first-pitch, and Core PCE's "release is cancelled entirely". The inference that
they carry the same construction is strong and contextual. It is still an
inference.

**The emergency power is unbounded.** DCM Rule 1.11(B)(3) permits "alternative
settlement mechanisms ... (including by altering the settlement terms or
conditions or fixing the settlement price)" and (B)(9) permits modifying or
suspending any provision of the Rules. Fixing *the settlement price* is the
harmless kind of discretion; altering *the settlement terms* is not. This is a
universal feature of CFTC-regulated venues and cannot discriminate between them,
which is exactly why it is recorded here rather than quietly excluded.
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
from predarb.semantics.venue_intervention import ResidualVenueInterventionRisk
from predarb.venues.rothera.governing_sources import (
    DCM_RULEBOOK_SHA256,
    DCO_RULEBOOK_SHA256,
)

__all__ = [
    "BASEBALL_PROOF",
    "EMERGENCY_INTERVENTION",
    "FAIR_MARKET_RESIDUAL",
    "MIN_TICK",
    "NOTIONAL",
    "ORDINARY",
    "RULE_72_CITATION_NOTE",
    "SETTLEMENT_DETERMINATION_RESERVATION",
    "SOCCER_PROOF",
    "UNSPLIT_FAIR_MARKET",
    "family_proof",
]

NOTIONAL: Final = Price.from_value("1.0000")
MIN_TICK: Final = Price.from_value("0.0100")

_DCM: Final = SourceComponent(
    name="Rothera DCM Rulebook",
    version="2026-05-20",
    document_sha256=DCM_RULEBOOK_SHA256,
    url="https://www.rothera.io/reg-notices",
    retrieved_note="105 pages, extracted clean.",
)

_DCO: Final = SourceComponent(
    name="Rothera DCO Rulebook",
    version="2026-05-20",
    document_sha256=DCO_RULEBOOK_SHA256,
    url="https://www.rothera.io/reg-notices",
    retrieved_note="101 pages, extracted clean.",
)

ORDINARY: Final = MechanismProof(
    mechanism=SettlementMechanism.ORDINARY_BINARY,
    status=MechanismStatus.PROVEN_COMPLEMENTARY,
    rule_reference="Contract Terms, 'Trading and Settlement'",
    quoted_text=(
        "If the Expiration Value is 'Yes', then the long position holders are "
        "paid an absolute amount proportional to the size of their position and "
        "the short position holders receive no payment. If the Expiration Value "
        "is 'No,' then the short position holders are paid an absolute amount "
        "proportional to the size of their position and the long position "
        "holders receive no payment."
    ),
    reasoning=(
        "Both sides named in both branches, with the losing side's zero stated. "
        "The amount is fixed elsewhere in the same document: 'The Settlement "
        "Value of an in-the-money Event Contract is $1.'"
    ),
    both_branches_explicit=True,
)

FAIR_MARKET_RESIDUAL: Final = MechanismProof(
    mechanism=SettlementMechanism.LAST_FAIR_PRICE,
    status=MechanismStatus.PROVEN_COMPLEMENTARY,
    rule_reference="Contract Terms, cancellation/abandonment bullet",
    quoted_text=(
        "the Contract will resolve based on the last fair market price as "
        "determined by the Exchange pursuant to Rothera DCM Rule 7.2. Long "
        "position holders will receive the number of Contracts held multiplied "
        "by the fair market price, and short position holders will receive the "
        "number of Contracts held multiplied by $1 minus the fair market price."
    ),
    reasoning=(
        "The short is defined as the residual of the long, so long + short is "
        "one notional for every value of the fair market price. The exchange's "
        "discretion runs to the price and not to the split, which is the "
        "distinction that decides whether discretion is fatal. The broken Rule "
        "7.2 citation does not reach this: it governs how the price is chosen, "
        "and the identity holds for any choice."
    ),
    both_branches_explicit=True,
)

UNSPLIT_FAIR_MARKET: Final = MechanismProof(
    mechanism=SettlementMechanism.LAST_FAIR_PRICE,
    status=MechanismStatus.UNRESOLVED,
    rule_reference="Baseball, disqualification before first pitch; Core PCE, release cancelled",
    quoted_text=(
        "the market will resolve based on the last fair market price as "
        "determined by the Exchange pursuant to DCM Rule 7.2. ... then the "
        "Contract will settle to the fair market price as determined by the "
        "Exchange pursuant to DCM Rule 7.2."
    ),
    reasoning=(
        "Same document, same defined phrase, and every neighbouring clause "
        "states long = p and short = $1 - p -- so the contextual inference that "
        "these two inherit it is strong. It is an inference all the same, and "
        "this project does not settle payout questions by strong inference. "
        "UNRESOLVED, which blocks; a single sentence in the next amendment would "
        "close it."
    ),
)

SETTLEMENT_DETERMINATION_RESERVATION: Final = MechanismProof(
    mechanism=SettlementMechanism.INDETERMINATE_FALLBACK,
    status=MechanismStatus.UNRESOLVED,
    rule_reference="Contract Terms, 'Contingencies' (all fourteen families)",
    quoted_text=(
        "If the Source Agency does not actually announce a result consistent "
        "with the settlement methodology or the Payment Criterion on or before "
        "the Expiration Date due to a delay, postponement, cancellation or "
        "otherwise in such release announcement by the Source Agency, the "
        "Settlement Date, Expiration Date and Expiration Time will be delayed "
        "until the Underlying outcome or results are released or as otherwise "
        "set forth on the Exchange pursuant to DCM Rule 7.2. Consistent with "
        "DCM Rule 7.2, Rothera reserves the right to make settlement "
        "determinations."
    ),
    reasoning=(
        "The stated remedy is a delay, which is a timing remedy and conserves. "
        "Two things escape it. 'Or as otherwise set forth on the Exchange "
        "pursuant to DCM Rule 7.2' points somewhere unbounded, and 'Rothera "
        "reserves the right to make settlement determinations' reserves a "
        "settlement power with no stated output -- not a price, not a side, not "
        "a split. Source delay is an ordinary contingency reachable in the "
        "normal life of every contract, so this stays inside the semantic proof "
        "and is not excluded with the emergency authority.\n\n"
        "Nothing here authorises a shortfall, so UNRESOLVED rather than "
        "NOT_COMPLEMENTARY. It is the reason the residual construction, which "
        "is itself proven, does not carry any family to a proof: a separate "
        "open-ended reservation sits beside it in every certification."
    ),
)
"""Present verbatim in all fourteen certifications. The blocker that survives
the emergency-scope correction, and the one that is genuinely about settling a
contract rather than about running an exchange."""

EMERGENCY_INTERVENTION: Final = ResidualVenueInterventionRisk(
    venue="Rothera Exchange and Clearing LLC",
    rule_reference="DCM Rule 1.11 Emergency Rules (and the DCO Rule 1.11 equivalent)",
    quoted_text=(
        "During an Emergency, the Company may implement temporary emergency "
        "procedures and rules ... (1) suspend or curtail trading in, or limit "
        "trading to liquidation, for any Contract; ... (3) provide alternative "
        "settlement mechanisms for any Contract (including by altering the "
        "settlement terms or conditions or fixing the settlement price) or "
        "suspend the transfer of the Underlying; ... (9) modify or suspend any "
        "provisions of the Rules; or (10) any other action, if so directed by "
        "the CFTC."
    ),
    powers=(
        "suspend or curtail trading venue-wide, or limit it to liquidation",
        "provide alternative settlement mechanisms, including altering the "
        "settlement terms or conditions",
        "modify or suspend any provision of the Rules",
        "act as directed by the CFTC",
    ),
    rationale=(
        "This is authority over the venue and over the Rules themselves, not a "
        "way of resolving a contract. It fires on a declared Emergency rather "
        "than on any scheduled contingency, and nothing in the life of a listed "
        "contract reaches it: an abandoned match, a delayed BEA release or a "
        "disqualified team are all handled by product clauses that stay inside "
        "the proof. Requiring a proof against it would require proving that no "
        "regulator or exchange will ever intervene, which no regulated venue can "
        "supply -- so the requirement would reject every venue for a property "
        "none of them has, rather than discriminate between them."
    ),
    approval_gate=(
        "Rule 1.11(B) limits the determination to the CEO, President or CCO or "
        "their designees; Rule 1.11(D) requires prior Regulatory Oversight "
        "Committee approval at a duly convened meeting; Rule 1.11(C) requires "
        "the effects on underlying and linked markets to be considered and "
        "documented."
    ),
)
"""Rule 1.11 leaves the semantic proof and is disclosed beside it.

Note what is *not* excluded with it. Rothera's fair-market-price settlement,
its cancellation and abandonment bullets, its source-failure contingencies and
its disqualification clauses are all ordinary contract resolution and all stay
inside the proof -- including the two that omit the split. The exclusion is one
rule, and it is the one that is not about settling a contract.
"""

RULE_72_CITATION_NOTE: Final = (
    "Every fair-market clause cites 'Rothera DCM Rule 7.2' as the authority for "
    "determining the price, and the products add 'Consistent with DCM Rule 7.2, "
    "Rothera reserves the right to make settlement determinations.' Rule 7.2 is "
    "titled 'Procedures' and authorises the Company DCM to adopt procedures for "
    "trading on the Platform: daily settlement prices, price dissemination, "
    "recordkeeping, surveillance, order-size limits, position limits and daily "
    "price fluctuation limits. It confers no final-settlement authority, "
    "mentions no fair market price, and reserves nothing. The rulebook's single "
    "definition of a fair market price is in the Error Trade Policy, for "
    "applying No Cancellation Ranges. Rule 7.2(B) then makes any adopted "
    "procedure a further document published on the Website, which was not "
    "located -- so the method for choosing the price is an open dependency."
)


def family_proof(
    subject: str,
    *,
    includes_unsplit_path: bool = False,
    extra: tuple[MechanismProof, ...] = (),
    notes: tuple[str, ...] = (),
    closure_established: bool = False,
) -> ComplementConservationProof:
    """Assemble one family's proof from the shared Rothera mechanisms.

    The families are drafted from a common template -- identical settlement and
    fair-market language across all fourteen -- so they share mechanisms rather
    than restating them, and differ only where the filed text differs.
    """
    mechanisms = (
        ORDINARY,
        FAIR_MARKET_RESIDUAL,
        SETTLEMENT_DETERMINATION_RESERVATION,
        *extra,
    )
    if includes_unsplit_path:
        mechanisms = (*mechanisms, UNSPLIT_FAIR_MARKET)
    return ComplementConservationProof(
        subject=subject,
        notional=NOTIONAL,
        contract_type="binary event contract, $1.00 Settlement Value, $0.01 tick",
        mechanisms=mechanisms,
        sources=(_DCM, _DCO),
        rounding=RoundingModel.RESIDUAL,
        rounding_note=(
            "The short is computed as $1 minus the long, so whatever precision "
            "the fair market price is carried at, the pair sums exactly and no "
            "residue can be lost. No rounding rule appears in either rulebook "
            "(zero occurrences of 'round' in both), and none is needed for the "
            "payout: the minimum tick is $0.01 and the residual construction is "
            "exact at any precision. Fees are the only place Rothera rounds, "
            "and they are account debits rather than payout adjustments."
        ),
        mechanism_closure_established=closure_established,
        residual_interventions=(EMERGENCY_INTERVENTION,),
        notes=(
            RULE_72_CITATION_NOTE,
            "Neither rulebook defines the vocabulary the products rely on: the "
            "DCM rulebook has zero occurrences of 'Event Contract', 'Expiration "
            "Value', 'Settlement Value', 'Payment Criterion', 'final settlement' "
            "or 'long position'. The payout architecture is entirely in the "
            "certifications.",
            *notes,
        ),
    )


BASEBALL_PROOF: Final = family_proof(
    "Rothera Baseball Outcome Event Contract",
    includes_unsplit_path=True,
    notes=(
        "Baseball admits no tie: the winner must have 'scored more runs (a "
        "strictly greater number) than its opponent at the conclusion of the "
        "full game, including any extra innings'. A forfeit resolves the "
        "forfeiting team to No and the opponent to Yes, preserving the "
        "two-contract partition; a disqualification before first pitch does not.",
    ),
)

SOCCER_PROOF: Final = family_proof(
    "Rothera Soccer Outcome Event Contract",
    notes=(
        "Soccer states a genuine three-way partition over regulation time: a "
        "winner needs 'more goals (a strictly greater number) ... at the "
        "conclusion of regulation time (90 minutes plus stoppage time only)', "
        'and the Exchange \'may, for any <soccer match>, list a separate "tie" '
        "iteration ... each <soccer team> must have scored an equal number of "
        "goals (including 0-0 draws)'.",
        "Extra time and penalty shoot-outs are excluded from the winner "
        "determination except for 'to advance' iterations -- so a match decided "
        "on penalties settles the tie iteration Yes and both team iterations No.",
    ),
)
