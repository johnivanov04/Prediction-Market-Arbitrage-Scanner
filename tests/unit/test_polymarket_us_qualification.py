"""Phase-2C qualification of Polymarket US, checked against the filed text.

The thing these tests are guarding against is the mistake this phase actually
made: screening the DCM rulebook, finding none of the Kalshi failure modes, and
concluding the venue had exhaustive settlement semantics. It does not. The
rulebook is silent on payouts, and the product certification that supplies them
contains both a third terminal state and a discretionary valuation clause.

So several tests here assert the *absence* of language in the rulebook and its
presence in the product terms. That pairing is the finding.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from predarb.domain.money import Price
from predarb.semantics.complement_proof import MechanismStatus, ProofStatus, RoundingModel
from predarb.semantics.cross_market_relation import (
    CombinationContract,
    RelationClass,
    RelationFeasibility,
    combination_and,
)
from predarb.semantics.dependency import DependencyClosure
from predarb.semantics.precedence import ControlFinding, PrecedenceStatus
from predarb.semantics.settlement_census import SettlementMechanism
from predarb.semantics.venue_intervention import (
    IN_SCOPE_MECHANISMS,
    ConservationScope,
)
from predarb.venues.polymarket_us.governing_sources import (
    AEC_TERMS_SHA256,
    CAOC_TERMS_SHA256,
    PMUS_CLEARING_RULEBOOK_HISTORY,
    PMUS_RULEBOOK_HISTORY,
)
from predarb.venues.polymarket_us.relation_findings import (
    CAOC,
    CAOC_BASKET_ECONOMICS,
    CAOC_FEASIBILITY,
    CAOC_TIE_FLOOR,
    PAIR_COST_FLOOR,
    PRODUCT_CANCELLATION_9101K,
    SAME_MARKET_PAIR,
    TIE_SPLIT_9101D,
    LegOutcome,
    basket_payoff,
    caoc_leg_implications,
    caoc_relation,
    worst_case_over,
)
from predarb.venues.polymarket_us.settlement_findings import (
    AEC_COMPLEMENT_PROOF,
    AEC_PRECEDENCE,
    CANCELLATION,
    CLEARING_DEPENDENCY,
    EMERGENCY_INTERVENTION,
    NOTIONAL,
    ORDINARY,
    OUTCOME_REVIEW,
    RULEBOOK_SHA256,
    TIE,
    TIE_PAYOUT,
)

pytestmark = pytest.mark.unit

ZERO = Price.from_value("0")

_TERMINAL_VERDICTS: frozenset[RelationFeasibility] = frozenset(
    {
        RelationFeasibility.NO_USEFUL_RELATION,
        RelationFeasibility.PROVEN_BUT_VENUE_MECHANICS_ELIMINATE_EDGE,
    }
)
"""Verdicts that would end the inquiry: no relation to work with, or one the
venue's own arithmetic forecloses. Neither describes this venue -- what blocks
is which document governs, which is a question with an answer."""


class TestPayoutConditionTrueBranch:
    """1. The satisfied branch is stated, with both sides and figures."""

    def test_the_long_side_is_named_with_an_amount(self):
        assert "each long AEC position shall receive one dollar ($1.00)" in ORDINARY.quoted_text

    def test_the_rulebook_agrees_the_long_is_paid_on_satisfaction(self):
        assert "Contract Outcome is $1.00" in OUTCOME_REVIEW.quoted_text
        assert "payable to holders of long positions" in OUTCOME_REVIEW.quoted_text

    def test_the_branch_is_proven_rather_than_assumed(self):
        assert ORDINARY.status is MechanismStatus.PROVEN_COMPLEMENTARY
        assert ORDINARY.both_branches_explicit


class TestPayoutConditionFalseBranch:
    """2. The losing side's zero is written down, not inferred from silence."""

    def test_the_short_side_is_named_with_an_amount(self):
        assert "each short position shall receive one dollar ($1.00)" in ORDINARY.quoted_text
        assert "each long AEC position shall receive zero dollars ($0.00)" in ORDINARY.quoted_text

    def test_a_proven_complement_requires_both_branches(self):
        """The phase-1 rule, still enforced: silence proves nothing."""
        with pytest.raises(ValueError, match="established, not inferred from silence"):
            type(ORDINARY)(
                mechanism=SettlementMechanism.ORDINARY_BINARY,
                status=MechanismStatus.PROVEN_COMPLEMENTARY,
                rule_reference="x",
                quoted_text="the long receives one dollar",
                reasoning="nothing says the short is paid",
            )

    def test_the_two_ordinary_branches_conserve_the_notional(self):
        assert NOTIONAL.units + ZERO.units == NOTIONAL.units


class TestProductRulePrecedence:
    """3. Rule 1.5 does not settle whether product terms reach settlement."""

    def test_the_precedence_question_is_unresolved(self):
        assert AEC_PRECEDENCE.product_rule_controls is ControlFinding.UNRESOLVED
        assert AEC_PRECEDENCE.status is PrecedenceStatus.PRECEDENCE_UNRESOLVED

    def test_it_does_not_unlock_a_strict_two_state_claim(self):
        assert not AEC_PRECEDENCE.unlocks_strict_two_state

    def test_the_notwithstanding_clause_is_quoted_in_full(self):
        rule_15 = next(a for a in AEC_PRECEDENCE.authorities if a.reference == "Rule 1.5")
        assert "Notwithstanding any provision of these Rules to the contrary" in rule_15.quoted_text

    def test_and_its_scope_is_trading_rather_than_settlement(self):
        """The whole difficulty in one phrase, which is why it supports neither."""
        rule_15 = next(a for a in AEC_PRECEDENCE.authorities if a.reference == "Rule 1.5")
        assert rule_15.quoted_text.count("trading in") == 2
        assert rule_15.supports == "neither"

    def test_both_readings_are_evidenced(self):
        assert AEC_PRECEDENCE.authorities_for("A")
        assert AEC_PRECEDENCE.authorities_for("B")

    def test_no_interpretive_guidance_was_located(self):
        assert not AEC_PRECEDENCE.guidelines_located


class TestOutcomeReviewReachability:
    """4. Rule 10.4 is reachable, and unlike Kalshi 6.3(c) its output is typed."""

    def test_the_review_is_reachable(self):
        assert OUTCOME_REVIEW.reachable

    def test_its_output_is_the_binary_contract_outcome(self):
        assert AEC_PRECEDENCE.committee_output_is_binary
        assert OUTCOME_REVIEW.status is MechanismStatus.PROVEN_COMPLEMENTARY

    def test_the_definition_it_is_typed_to_states_both_sides(self):
        assert "payable to holders of long positions" in OUTCOME_REVIEW.quoted_text
        assert "payable to holders of short positions" in OUTCOME_REVIEW.quoted_text

    def test_a_typed_output_alone_does_not_carry_the_venue(self):
        """It is the best clause at the venue and it still is not enough,
        because a different, untyped path is also reachable."""
        assert OUTCOME_REVIEW.status is MechanismStatus.PROVEN_COMPLEMENTARY
        assert AEC_COMPLEMENT_PROOF.status is not ProofStatus.PROVEN_FOR_SUBSET


class TestUnknownClearingDependencyBlocks:
    """5. A clearing rulebook older than the rules that cite it stays open."""

    def test_the_clearing_closure_is_unknown(self):
        assert CLEARING_DEPENDENCY.closure is DependencyClosure.UNKNOWN
        assert not CLEARING_DEPENDENCY.closure.is_established

    def test_an_unknown_closure_still_carries_what_was_found(self):
        """Fails closed without discarding evidence."""
        assert len(CLEARING_DEPENDENCY.dependencies) == 1

    def test_neither_source_history_claims_to_be_exhaustive(self):
        assert not PMUS_RULEBOOK_HISTORY.versions_are_exhaustive
        assert not PMUS_CLEARING_RULEBOOK_HISTORY.versions_are_exhaustive

    def test_the_clearing_document_predates_the_rulebook_citing_it(self):
        clearing = PMUS_CLEARING_RULEBOOK_HISTORY.versions[0].effective_date
        dcm = PMUS_RULEBOOK_HISTORY.versions[0].effective_date
        assert clearing is not None and dcm is not None
        assert clearing < dcm

    def test_mechanism_closure_is_not_established_either(self):
        assert not AEC_COMPLEMENT_PROOF.mechanism_closure_established


class TestCancellationPath:
    """6. The clause that decides the venue, and where it is written."""

    def test_cancellation_leaves_conservation_unestablished(self):
        assert CANCELLATION.status is MechanismStatus.UNRESOLVED
        assert CANCELLATION.blocks
        assert CANCELLATION.reachable

    def test_it_permits_last_traded_prices(self):
        assert "last-traded prices" in CANCELLATION.quoted_text

    def test_it_permits_an_undefined_valuation_standard(self):
        assert "other fair and equitable valuation" in CANCELLATION.quoted_text

    def test_it_lives_in_the_product_terms_not_the_rulebook(self):
        """The correction this phase had to make: a rulebook screen misses it."""
        assert CANCELLATION.rule_reference.startswith("Rule 9.101(K)")
        assert "Rulebook" not in CANCELLATION.rule_reference

    def test_an_unconstrained_valuation_is_not_an_authorised_shortfall(self):
        """The distinction this phase had to be corrected on. Silence about how
        two payouts relate is not permission for them to miss the notional."""
        assert CANCELLATION.status is not MechanismStatus.NOT_COMPLEMENTARY

    def test_no_mechanism_at_this_venue_is_disproven(self):
        assert not any(
            m.status is MechanismStatus.NOT_COMPLEMENTARY for m in AEC_COMPLEMENT_PROOF.mechanisms
        )

    def test_so_the_family_verdict_is_not_disproven(self):
        assert AEC_COMPLEMENT_PROOF.status is not ProofStatus.DISPROVEN

    def test_the_emergency_chapter_does_not_rescue_the_product_clause(self):
        """The scope exclusion is narrow. Rule 2.8 emergency authority leaves
        the proof; Rule 9.101(K) cancellation is a product clause reachable on
        any rained-off game and stays in."""
        assert EMERGENCY_INTERVENTION in AEC_COMPLEMENT_PROOF.residual_interventions
        assert CANCELLATION in AEC_COMPLEMENT_PROOF.mechanisms
        assert CANCELLATION.mechanism in IN_SCOPE_MECHANISMS
        assert CANCELLATION.blocks

    def test_the_residual_risk_is_disclosed_in_the_description(self):
        assert "RESIDUAL_VENUE_INTERVENTION_RISK" in AEC_COMPLEMENT_PROOF.describe()

    def test_the_verdict_is_unchanged_by_the_scope_correction(self):
        """Polymarket US never depended on the emergency power to fail."""
        assert AEC_COMPLEMENT_PROOF.conservation_scope is ConservationScope.NOT_ESTABLISHED
        assert [m.mechanism for m in AEC_COMPLEMENT_PROOF.blocking] == [
            SettlementMechanism.CANCELLATION_LAST_RESULTS,
            SettlementMechanism.INDETERMINATE_FALLBACK,
        ]

    def test_it_is_blocked_by_incomplete_evidence_instead(self):
        """Stricter than NOT_PROVEN, and for a stated reason: the mechanism list
        is not known to be closed and the governing clearing version is unread."""
        assert AEC_COMPLEMENT_PROOF.status is ProofStatus.EVIDENCE_INCOMPLETE
        assert not AEC_COMPLEMENT_PROOF.mechanism_closure_established

    def test_closing_the_evidence_gap_would_leave_not_proven(self):
        """What the verdict becomes once the remaining documents are read: still
        blocking, still not a claim that anything fails to conserve."""
        closed = replace(AEC_COMPLEMENT_PROOF, mechanism_closure_established=True)
        assert closed.status is ProofStatus.NOT_PROVEN

    def test_no_rounding_model_governs_that_path(self):
        assert AEC_COMPLEMENT_PROOF.rounding is RoundingModel.UNSPECIFIED
        assert not AEC_COMPLEMENT_PROOF.rounding.conserves_exactly


class TestCombinationTruthTable:
    """7. The AND is checked over every assignment, not argued."""

    def test_the_combination_pays_only_when_every_leg_does(self):
        for a in (True, False):
            for b in (True, False):
                outcome = CAOC.yes_given({"aec:leg_a": a, "aec:leg_b": b})
                assert outcome == (a and b)

    def test_a_missing_leg_outcome_is_an_error_not_a_default(self):
        with pytest.raises(KeyError, match="no outcome supplied"):
            CAOC.yes_given({"aec:leg_a": True})

    def test_the_basket_is_at_least_one_over_two_state_legs(self):
        assert caoc_relation().relation is RelationClass.AT_LEAST_ONE
        assert worst_case_over((LegOutcome.OCCURS, LegOutcome.DOES_NOT_OCCUR)) == NOTIONAL

    def test_every_two_state_assignment_pays_at_least_the_notional(self):
        for a in (LegOutcome.OCCURS, LegOutcome.DOES_NOT_OCCUR):
            for b in (LegOutcome.OCCURS, LegOutcome.DOES_NOT_OCCUR):
                payoff = basket_payoff({"aec:leg_a": a, "aec:leg_b": b})
                assert payoff.units >= NOTIONAL.units, (a, b)

    def test_a_combination_needs_at_least_two_legs(self):
        with pytest.raises(ValueError, match="at least two distinct legs"):
            CombinationContract(identifier="x", legs=("only",))

    def test_repeated_legs_do_not_manufacture_a_combination(self):
        with pytest.raises(ValueError, match="at least two distinct legs"):
            CombinationContract(identifier="x", legs=("same", "same"))


class TestCombinationComponentUnresolvedBlocks:
    """8. The legs reach a state the combination does not model."""

    def test_the_legs_have_a_third_terminal_state(self):
        assert TIE.status is MechanismStatus.PROVEN_COMPLEMENTARY
        assert "fifty cents ($0.50)" in TIE.quoted_text

    def test_that_state_conserves_the_notional_even_so(self):
        """Conservation and binariness are different properties."""
        assert TIE_PAYOUT.units * 2 == NOTIONAL.units

    def test_the_tie_halves_the_basket_worst_case(self):
        assert worst_case_over(LegOutcome.ALL) == CAOC_TIE_FLOOR
        assert CAOC_TIE_FLOOR.units * 2 == NOTIONAL.units

    def test_the_worst_state_is_one_tie_and_one_satisfied_leg(self):
        payoff = basket_payoff({"aec:leg_a": LegOutcome.TIE, "aec:leg_b": LegOutcome.OCCURS})
        assert payoff == CAOC_TIE_FLOOR

    def test_the_combination_models_its_legs_as_two_state(self):
        """CAOC describes constituent settlement as '$1.00/$0.00'."""
        assert "$1.00/$0.00" in AEC_PRECEDENCE.notes[-1]
        assert CAOC_TERMS_SHA256[:12] in AEC_PRECEDENCE.notes[-1]

    def test_the_verdict_is_unresolved_semantics_not_an_absent_relation(self):
        """The distinction matters: a missing relation cannot be repaired by
        better drafting, and this one could be."""
        assert CAOC_FEASIBILITY is RelationFeasibility.SEMANTICS_UNRESOLVED
        assert CAOC_FEASIBILITY not in _TERMINAL_VERDICTS


class TestNestedImplication:
    """9. The forward direction of the biconditional, per leg."""

    def test_the_combination_implies_each_leg(self):
        proofs = caoc_leg_implications()
        assert len(proofs) == len(CAOC.legs)
        assert all(p.relation is RelationClass.NESTED_IMPLICATION for p in proofs)

    def test_the_implication_holds_in_every_two_state_assignment(self):
        for a in (True, False):
            for b in (True, False):
                if CAOC.yes_given({"aec:leg_a": a, "aec:leg_b": b}):
                    assert a and b

    def test_the_converse_does_not_hold(self):
        assert not CAOC.yes_given({"aec:leg_a": True, "aec:leg_b": False})

    def test_each_implication_names_the_combination_and_one_leg(self):
        for proof in caoc_leg_implications():
            assert CAOC.identifier in proof.members
            assert len(proof.members) == 2


class TestRelationInvalidUnderAlternateSettlement:
    """10. A relation true only under ordinary settlement is not usable."""

    def test_the_relation_does_not_survive_every_path(self):
        assert not caoc_relation().survives_all_paths

    def test_the_tie_is_named_as_a_breaking_path(self):
        assert TIE_SPLIT_9101D in caoc_relation().broken_by_settlement_paths

    def test_the_cancellation_clause_is_named_too(self):
        assert PRODUCT_CANCELLATION_9101K in caoc_relation().broken_by_settlement_paths

    def test_the_leg_implications_break_on_the_same_paths(self):
        for proof in caoc_leg_implications():
            assert not proof.survives_all_paths
            assert TIE_SPLIT_9101D in proof.broken_by_settlement_paths

    def test_a_hypothetical_clean_combination_would_survive(self):
        """The machinery is not simply returning False for everything."""
        clean = combination_and(CAOC, survives=("ordinary",))
        assert clean.survives_all_paths


class TestSymbolicRelationEconomics:
    """11. The inequality, before any quote is known."""

    def test_the_claimed_inequality_uses_the_notional(self):
        assert "< 1.0000" in CAOC_BASKET_ECONOMICS.required_inequality

    def test_the_basket_counts_the_combination_and_every_leg(self):
        assert CAOC_BASKET_ECONOMICS.legs == 1 + len(CAOC.legs)

    def test_the_cost_symbol_names_every_member(self):
        symbol = CAOC_BASKET_ECONOMICS.total_bid_cost_symbol
        assert "p(combo)" in symbol
        for leg in CAOC.legs:
            assert leg in symbol

    def test_the_real_ceiling_is_half_that(self):
        assert CAOC_BASKET_ECONOMICS.max_affordable_cost == NOTIONAL
        assert CAOC_TIE_FLOOR.units * 2 == CAOC_BASKET_ECONOMICS.max_affordable_cost.units


class TestVenueEnforcedPricingIdentity:
    """12. Whether venue mechanics close the edge before any quote is seen."""

    def test_no_rule_fixes_the_cost_of_a_long_short_pair(self):
        assert not SAME_MARKET_PAIR.is_algebraically_impossible(minimum_total_cost=PAIR_COST_FLOOR)

    def test_the_permitted_floor_is_two_ticks(self):
        """Rule 9.101(F): each side is bounded to $0.001-$0.999, independently."""
        assert Price.from_value("0.0020") == PAIR_COST_FLOOR

    def test_a_forecastex_style_identity_would_have_closed_it(self):
        """The contrast: an inverse pair fixed above the notional is hopeless."""
        assert SAME_MARKET_PAIR.is_algebraically_impossible(
            minimum_total_cost=Price.from_value("1.0100")
        )

    def test_paying_exactly_the_worst_case_is_also_no_edge(self):
        assert SAME_MARKET_PAIR.is_algebraically_impossible(minimum_total_cost=NOTIONAL)

    def test_so_the_blocker_is_semantics_rather_than_venue_algebra(self):
        assert CAOC_FEASIBILITY not in _TERMINAL_VERDICTS


class TestDocumentDrift:
    """13. A conclusion must stop applying when its documents change."""

    def test_the_precedence_proof_applies_to_the_documents_it_cites(self):
        applies, reasons = AEC_PRECEDENCE.still_applies_to(
            product_terms_sha256=AEC_TERMS_SHA256, rulebook_sha256=RULEBOOK_SHA256
        )
        assert applies
        assert reasons == ()

    def test_an_amended_product_certification_invalidates_it(self):
        applies, reasons = AEC_PRECEDENCE.still_applies_to(
            product_terms_sha256="0" * 64, rulebook_sha256=RULEBOOK_SHA256
        )
        assert not applies
        assert "product terms content hash changed" in reasons

    def test_an_amended_rulebook_invalidates_it(self):
        applies, reasons = AEC_PRECEDENCE.still_applies_to(
            product_terms_sha256=AEC_TERMS_SHA256, rulebook_sha256="0" * 64
        )
        assert not applies
        assert "Rulebook content hash changed" in reasons

    def test_the_complement_proof_has_a_content_derived_identity(self):
        assert len(AEC_COMPLEMENT_PROOF.source_digest()) == 32

    def test_every_source_behind_the_complement_proof_is_pinned(self):
        assert AEC_COMPLEMENT_PROOF.unpinned_sources == ()

    def test_the_athletic_certification_hash_is_recorded(self):
        assert len(AEC_TERMS_SHA256) == 64
        assert any(s.document_sha256 == AEC_TERMS_SHA256 for s in AEC_COMPLEMENT_PROOF.sources)
