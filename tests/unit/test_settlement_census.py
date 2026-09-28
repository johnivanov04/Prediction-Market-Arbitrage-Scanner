"""Strict two-state and complementarity are separate questions.

The error this module exists to prevent, stated once: a clause permitting a
fractional Settlement Value ("$1/N, rounded down") proves that strict two-state
binary settlement is unavailable. It proves nothing about whether YES + NO still
sums to the notional, because it does not say how the second side is computed.
Reading a shortfall out of "rounded down" would be speculating about whether the
sides are calculated independently or as complements.
"""

from __future__ import annotations

import pytest

from predarb.semantics.settlement_census import (
    FamilyClassification,
    SettlementMechanism,
    StrictBinaryStatus,
    classify_family,
)

pytestmark = pytest.mark.unit

ORDINARY = SettlementMechanism.ORDINARY_BINARY
FRACTIONAL = SettlementMechanism.FRACTIONAL_SHARE
TIE = SettlementMechanism.TIE_SPLIT
FAIR_PRICE = SettlementMechanism.LAST_FAIR_PRICE


class TestFractionalDoesNotImplyAComplementBreak:
    """The correction. A fractional YES payout is not a proven violation."""

    def test_a_fractional_payout_never_asserts_a_complement_break(self):
        for mechanism in SettlementMechanism:
            assert not mechanism.establishes_complement_break, mechanism

    def test_a_fractional_share_leaves_the_complement_unproven_not_disproven(self):
        verdict = classify_family((ORDINARY, FRACTIONAL))
        assert verdict.classification is FamilyClassification.COMPLEMENT_NOT_PROVEN
        assert verdict.classification.value != "STRICT_TWO_STATE_DISPROVEN"
        assert not verdict.complement_is_proven

    def test_the_wording_says_unproven_rather_than_violated(self):
        note = " ".join(classify_family((ORDINARY, FRACTIONAL)).notes)
        assert "does not establish" in note
        assert "prove or disprove" in note
        for forbidden in ("less than the notional", "violates", "breaks", "shortfall"):
            assert forbidden not in note.lower()

    def test_strict_binary_is_separately_disproven(self):
        verdict = classify_family((ORDINARY, FRACTIONAL))
        assert verdict.strict_binary is StrictBinaryStatus.DISPROVEN

    def test_the_two_axes_do_not_follow_from_one_another(self):
        """Strict binary disproven, complement provable -- a coherent state."""
        verdict = classify_family((ORDINARY, FRACTIONAL), complement_proven=True)
        assert verdict.strict_binary is StrictBinaryStatus.DISPROVEN
        assert verdict.classification is (
            FamilyClassification.FRACTIONAL_COMPLEMENT_POTENTIALLY_PROVABLE
        )

    def test_complement_proof_is_an_input_never_an_inference(self):
        """Nothing in the classifier can derive complementarity on its own."""
        assert not classify_family((ORDINARY, FRACTIONAL)).complement_is_proven
        assert not classify_family((ORDINARY, TIE, FAIR_PRICE)).complement_is_proven


class TestStrictTwoState:
    def test_only_ordinary_binary_is_potentially_provable(self):
        verdict = classify_family((ORDINARY,))
        assert verdict.classification is (
            FamilyClassification.STRICT_TWO_STATE_POTENTIALLY_PROVABLE
        )
        assert verdict.strict_binary is StrictBinaryStatus.POTENTIALLY_PROVABLE

    def test_no_mechanism_at_all_is_not_a_pass_by_default(self):
        """An empty mechanism set means nothing was read, not that nothing exists."""
        verdict = classify_family((), unresolved_material=("nothing was read",))
        assert verdict.classification is FamilyClassification.APPLICABLE_RULE_UNRESOLVED

    @pytest.mark.parametrize(
        "mechanism",
        [
            SettlementMechanism.LAST_FAIR_PRICE,
            SettlementMechanism.LAST_TRADED_PRICE,
            SettlementMechanism.FAIR_ALLOCATION,
            SettlementMechanism.FRACTIONAL_SHARE,
            SettlementMechanism.TIE_SPLIT,
            SettlementMechanism.NATURAL_PERSON_SCALAR,
            SettlementMechanism.OUTCOME_REVIEW,
            SettlementMechanism.INDETERMINATE_FALLBACK,
        ],
    )
    def test_each_fractional_path_disproves_strict_two_state(self, mechanism):
        assert classify_family((ORDINARY, mechanism)).strict_binary is (
            StrictBinaryStatus.DISPROVEN
        )

    def test_a_void_refund_does_not_by_itself_disprove_strict_two_state(self):
        """A void returns collateral; whether that is a third terminal value is a
        question about the contract, not something the mechanism name settles."""
        verdict = classify_family((ORDINARY, SettlementMechanism.VOID_REFUND))
        assert verdict.strict_binary is StrictBinaryStatus.POTENTIALLY_PROVABLE

    def test_cancellation_to_last_results_is_still_binary(self):
        """Resolving from the last official standings picks a side; it does not
        pay a fraction."""
        verdict = classify_family((ORDINARY, SettlementMechanism.CANCELLATION_LAST_RESULTS))
        assert verdict.strict_binary is StrictBinaryStatus.POTENTIALLY_PROVABLE


class TestUnresolvedDominates:
    def test_an_unresolved_material_rule_beats_every_other_verdict(self):
        verdict = classify_family((ORDINARY, FRACTIONAL), unresolved_material=("Rule 6.3(b)",))
        assert verdict.classification is FamilyClassification.APPLICABLE_RULE_UNRESOLVED

    def test_disproven_is_not_used_merely_because_something_is_unknown(self):
        """The instruction: STRICT_TWO_STATE_DISPROVEN requires actual permissive
        language, never an absence of knowledge."""
        verdict = classify_family((ORDINARY,), unresolved_material=("Rule 6.3(b)",))
        assert verdict.classification is not FamilyClassification.STRICT_TWO_STATE_DISPROVEN
        assert verdict.strict_binary is StrictBinaryStatus.UNRESOLVED

    def test_the_unresolved_rules_are_reported(self):
        verdict = classify_family((ORDINARY,), unresolved_material=("Rule 6.3(b)", "Rule 7.1"))
        assert verdict.unresolved_material == ("Rule 6.3(b)", "Rule 7.1")


class TestDeterminism:
    def test_mechanism_order_does_not_change_the_verdict(self):
        first = classify_family((ORDINARY, FRACTIONAL, TIE))
        second = classify_family((TIE, FRACTIONAL, ORDINARY))
        assert first.classification is second.classification
        assert set(first.fractional_mechanisms) == set(second.fractional_mechanisms)

    def test_duplicates_are_collapsed(self):
        verdict = classify_family((ORDINARY, FRACTIONAL, FRACTIONAL))
        assert verdict.fractional_mechanisms == (FRACTIONAL,)
