"""Classifying what a contract's governing text permits at settlement.

Research vocabulary, not a certificate type. Nothing here issues, approves or
proves anything; it records which settlement mechanisms a product's authoritative
text actually permits, and draws the two conclusions those mechanisms support.

Two questions, and neither implies the other
--------------------------------------------
**Strict two-state binary**: does every permitted terminal state pay the whole
notional to exactly one side?

**Complementarity**: does every permitted terminal state satisfy
``YES + NO == notional``, whether or not either side gets the whole thing?

A clause reading "the Contract shall resolve at a Settlement Value equal to
$1/N, rounded down" settles the first question -- a fractional Settlement Value
is permitted, so strict two-state is **disproven**. It settles nothing about the
second. To know whether YES + NO still sums to the notional you need the NO
side's mechanics, and that clause does not give them. Inferring a shortfall from
"rounded down" would be speculating about whether the two sides are computed
independently or as complements, which the text does not say.

That asymmetry is the whole reason this module exists, and
:func:`classify_family` is written so the inference cannot be made by accident.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

__all__ = [
    "FamilyClassification",
    "SettlementMechanism",
    "StrictBinaryStatus",
    "classify_family",
]


class SettlementMechanism(StrEnum):
    """A way a contract's governing text permits it to settle."""

    ORDINARY_BINARY = "ORDINARY_BINARY"
    """Whole notional to one side on the ordinary Payout Criterion."""

    LAST_FAIR_PRICE = "LAST_FAIR_PRICE"
    """Settles at a price the exchange determines, at its discretion."""

    LAST_TRADED_PRICE = "LAST_TRADED_PRICE"
    """Settles at the last traded price."""

    FAIR_ALLOCATION = "FAIR_ALLOCATION"
    """An Outcome Review Committee allocates between long and short."""

    FRACTIONAL_SHARE = "FRACTIONAL_SHARE"
    """An explicit fractional Settlement Value, e.g. ``$1/N``."""

    TIE_SPLIT = "TIE_SPLIT"
    """A tie or shared placement divides the Settlement Value."""

    CANCELLATION_LAST_RESULTS = "CANCELLATION_LAST_RESULTS"
    """On cancellation, resolves from the last official standings."""

    VOID_REFUND = "VOID_REFUND"
    """Voids, cancels or refunds instead of paying out."""

    NATURAL_PERSON_SCALAR = "NATURAL_PERSON_SCALAR"
    """Rulebook 6.3(e): the contract's primary subject is a natural person, so
    death settles at a price rather than to one side."""

    OUTCOME_REVIEW = "OUTCOME_REVIEW"
    """Market Outcome Review may determine the outcome."""

    INDETERMINATE_FALLBACK = "INDETERMINATE_FALLBACK"
    """A general rule governs when the Expiration Value cannot be determined."""

    @property
    def permits_fractional_payout(self) -> bool:
        """Whether this mechanism can pay something other than 0 or the notional.

        ``OUTCOME_REVIEW`` and ``INDETERMINATE_FALLBACK`` are here because on
        this exchange they route into last-traded-price and fair-allocation --
        that is what the rules they point at actually say.
        """
        return self in {
            SettlementMechanism.LAST_FAIR_PRICE,
            SettlementMechanism.LAST_TRADED_PRICE,
            SettlementMechanism.FAIR_ALLOCATION,
            SettlementMechanism.FRACTIONAL_SHARE,
            SettlementMechanism.TIE_SPLIT,
            SettlementMechanism.NATURAL_PERSON_SCALAR,
            SettlementMechanism.OUTCOME_REVIEW,
            SettlementMechanism.INDETERMINATE_FALLBACK,
        }

    @property
    def establishes_complement_break(self) -> bool:
        """Whether the mechanism's own text proves YES + NO can miss the notional.

        Deliberately ``False`` for every member. No governing text examined in
        this corpus states how the second side is computed when the first is
        fractional, and a mechanism that permits a fractional payout says
        nothing on its own about the sum. A future mechanism whose text does
        establish a shortfall would set this, and only then.
        """
        return False


class StrictBinaryStatus(StrEnum):
    POTENTIALLY_PROVABLE = "STRICT_TWO_STATE_POTENTIALLY_PROVABLE"
    DISPROVEN = "STRICT_TWO_STATE_DISPROVEN"
    UNRESOLVED = "APPLICABLE_RULE_UNRESOLVED"


class FamilyClassification(StrEnum):
    """The single verdict returned for one product family."""

    STRICT_TWO_STATE_POTENTIALLY_PROVABLE = "STRICT_TWO_STATE_POTENTIALLY_PROVABLE"
    FRACTIONAL_COMPLEMENT_POTENTIALLY_PROVABLE = "FRACTIONAL_COMPLEMENT_POTENTIALLY_PROVABLE"
    COMPLEMENT_NOT_PROVEN = "COMPLEMENT_NOT_PROVEN"
    APPLICABLE_RULE_UNRESOLVED = "APPLICABLE_RULE_UNRESOLVED"
    STRICT_TWO_STATE_DISPROVEN = "STRICT_TWO_STATE_DISPROVEN"


@dataclass(frozen=True, slots=True)
class FamilyVerdict:
    """One family's classification, with the evidence behind it."""

    classification: FamilyClassification
    strict_binary: StrictBinaryStatus
    complement_is_proven: bool
    fractional_mechanisms: tuple[SettlementMechanism, ...] = ()
    unresolved_material: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    def describe(self) -> str:
        detail = ", ".join(m.value for m in self.fractional_mechanisms) or "none"
        return f"{self.classification.value} (fractional paths: {detail})"


def classify_family(
    mechanisms: Iterable[SettlementMechanism],
    *,
    unresolved_material: Iterable[str] = (),
    complement_proven: bool = False,
) -> FamilyVerdict:
    """The verdict a family's permitted mechanisms support.

    ``complement_proven`` is an input, never an inference: it is set only when
    governing text establishes the second side's payout for every fractional
    path. Nothing in this function derives it, because nothing in the corpus
    examined supports deriving it.
    """
    permitted = tuple(dict.fromkeys(mechanisms))
    unresolved = tuple(dict.fromkeys(unresolved_material))
    fractional = tuple(m for m in permitted if m.permits_fractional_payout)

    if unresolved:
        return FamilyVerdict(
            classification=FamilyClassification.APPLICABLE_RULE_UNRESOLVED,
            strict_binary=StrictBinaryStatus.UNRESOLVED,
            complement_is_proven=False,
            fractional_mechanisms=fractional,
            unresolved_material=unresolved,
            notes=("a material payout rule is unresolved, so no verdict is reachable",),
        )

    if not fractional:
        return FamilyVerdict(
            classification=FamilyClassification.STRICT_TWO_STATE_POTENTIALLY_PROVABLE,
            strict_binary=StrictBinaryStatus.POTENTIALLY_PROVABLE,
            complement_is_proven=complement_proven,
            notes=("no examined clause permits a payout other than 0 or the notional",),
        )

    # A fractional path exists, so strict two-state is disproven. Whether the
    # two sides still sum to the notional is a separate question this does not
    # answer -- and must not be answered by inference from the above.
    if complement_proven:
        return FamilyVerdict(
            classification=FamilyClassification.FRACTIONAL_COMPLEMENT_POTENTIALLY_PROVABLE,
            strict_binary=StrictBinaryStatus.DISPROVEN,
            complement_is_proven=True,
            fractional_mechanisms=fractional,
            notes=(
                "a fractional Settlement Value is permitted, so strict two-state is "
                "disproven; governing text establishes the complementary side",
            ),
        )
    return FamilyVerdict(
        classification=FamilyClassification.COMPLEMENT_NOT_PROVEN,
        strict_binary=StrictBinaryStatus.DISPROVEN,
        complement_is_proven=False,
        fractional_mechanisms=fractional,
        notes=(
            "a fractional Settlement Value is permitted, so strict two-state is "
            "disproven; the governing evidence reviewed does not establish the "
            "corresponding NO payout strongly enough to prove or disprove full "
            "complementarity",
        ),
    )
