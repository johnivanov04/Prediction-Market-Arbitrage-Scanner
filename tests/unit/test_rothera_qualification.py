"""Phase-2D qualification of Rothera, checked against the filed text.

Rothera is the first venue in this research to state the residual construction
outright, so the tests are organised around keeping two things apart that the
venue makes it tempting to merge:

* **within one contract** -- long plus short is one notional, for every price;
* **across a set of contracts** -- the partition's floor.

Rothera has the first and not the second, and no amount of the first supplies
the second. Several tests below exist only to make that impossible to blur.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from predarb.domain.money import Money, Price
from predarb.semantics.complement_proof import MechanismStatus, ProofStatus, RoundingModel
from predarb.semantics.cross_market_relation import RelationClass, RelationFeasibility
from predarb.semantics.payout_relation import (
    PartitionBasket,
    PriceCoupling,
    SettlementRegime,
    basket_cost_symbol,
    residual_pair_conserves,
)
from predarb.semantics.settlement_census import SettlementMechanism
from predarb.semantics.venue_intervention import (
    IN_SCOPE_MECHANISMS,
    ConservationScope,
    InterventionScope,
    ResidualVenueInterventionRisk,
    classify_intervention,
)
from predarb.venues.prophetx.screen_findings import (
    PROPHETX_PROOF,
    SETTLEMENT_DISCRETION,
)
from predarb.venues.prophetx.screen_findings import (
    RULEBOOK_SHA256 as PROPHETX_SHA256,
)
from predarb.venues.rothera.fees import (
    FEE_FLOOR,
    K_BY_PARTICIPANT,
    ParticipantType,
    order_fee,
    round_trip_fee,
)
from predarb.venues.rothera.governing_sources import (
    DCM_RULEBOOK_HISTORY,
    DCM_RULEBOOK_SHA256,
    DCO_RULEBOOK_HISTORY,
    PRODUCT_FILINGS,
    SUPERSEDED_TERM_NOTE,
)
from predarb.venues.rothera.relation_findings import (
    BASEBALL_PAIR,
    CORE_PCE_HIGH,
    CORE_PCE_LOW,
    EMERGENCY,
    FAIR_MARKET_DELAY,
    REACHABLE_REGIMES,
    ROTHERA_FEASIBILITY,
    SOCCER_TRIPLE,
    core_pce_ladder,
    hypothetical_coupled_partition,
)
from predarb.venues.rothera.settlement_findings import (
    BASEBALL_PROOF,
    EMERGENCY_INTERVENTION,
    FAIR_MARKET_RESIDUAL,
    MIN_TICK,
    NOTIONAL,
    ORDINARY,
    RULE_72_CITATION_NOTE,
    SETTLEMENT_DETERMINATION_RESERVATION,
    SOCCER_PROOF,
    UNSPLIT_FAIR_MARKET,
)

pytestmark = pytest.mark.unit


class TestOrdinarySettlement:
    """Both branches, both sides, from the filed Contract Terms."""

    def test_the_yes_branch_names_both_sides(self):
        assert "long position holders are paid" in ORDINARY.quoted_text
        assert "short position holders receive no payment" in ORDINARY.quoted_text

    def test_the_no_branch_names_both_sides(self):
        assert "short position holders are paid" in ORDINARY.quoted_text
        assert "long position holders receive no payment" in ORDINARY.quoted_text

    def test_it_is_proven_with_explicit_branches(self):
        assert ORDINARY.status is MechanismStatus.PROVEN_COMPLEMENTARY
        assert ORDINARY.both_branches_explicit

    def test_the_two_branches_conserve(self):
        zero = Price.from_value("0")
        assert NOTIONAL.units + zero.units == NOTIONAL.units


class TestResidualConstruction:
    """The clause this project has been looking for since phase 1."""

    def test_the_short_is_defined_as_the_residual(self):
        assert "multiplied by the fair market price" in FAIR_MARKET_RESIDUAL.quoted_text
        assert "$1 minus the fair market price" in FAIR_MARKET_RESIDUAL.quoted_text

    def test_it_is_proven_complementary(self):
        assert FAIR_MARKET_RESIDUAL.status is MechanismStatus.PROVEN_COMPLEMENTARY
        assert FAIR_MARKET_RESIDUAL.both_branches_explicit

    def test_it_conserves_at_every_price_on_the_tick_grid(self):
        """Exhaustive over all 101 admissible prices, not argued."""
        step = MIN_TICK.units
        for units in range(0, NOTIONAL.units + 1, step):
            assert residual_pair_conserves(NOTIONAL, Price(units)), units

    def test_it_conserves_at_sub_tick_precision_too(self):
        """The point of a residual: the identity does not depend on precision,
        so discretion over the price cannot leak money."""
        for units in (1, 7, 3333, 9999):
            assert residual_pair_conserves(NOTIONAL, Price(units))

    def test_a_price_above_the_notional_does_not_conserve(self):
        assert not residual_pair_conserves(NOTIONAL, Price.from_value("1.0001"))

    def test_the_rounding_model_is_residual(self):
        assert BASEBALL_PROOF.rounding is RoundingModel.RESIDUAL
        assert BASEBALL_PROOF.rounding.conserves_exactly

    def test_discretion_over_the_price_is_not_discretion_over_the_split(self):
        assert SettlementRegime.RESIDUAL_PRICE.conserves_within_contract
        assert not SettlementRegime.INDEPENDENT_PAYOUTS.conserves_within_contract


class TestRule72CitationDoesNotHold:
    """The products cite a rule that does not say what they claim."""

    def test_the_citation_is_recorded_as_a_finding(self):
        assert "titled 'Procedures'" in RULE_72_CITATION_NOTE
        assert "confers no final-settlement authority" in RULE_72_CITATION_NOTE

    def test_the_products_still_cite_it_for_the_price(self):
        assert "pursuant to Rothera DCM Rule 7.2" in FAIR_MARKET_RESIDUAL.quoted_text

    def test_but_the_residual_identity_does_not_depend_on_it(self):
        """The distinction that keeps this from being over-claimed: a broken
        citation about *choosing* p cannot break an identity that holds for
        every p."""
        assert FAIR_MARKET_RESIDUAL.status is MechanismStatus.PROVEN_COMPLEMENTARY
        assert residual_pair_conserves(NOTIONAL, Price.from_value("0.7300"))

    def test_the_open_dependency_is_the_procedure_not_the_formula(self):
        assert "published on the Website" in RULE_72_CITATION_NOTE
        assert "open dependency" in RULE_72_CITATION_NOTE


class TestPathsThatOmitTheSplit:
    """Two clauses state the price and not the split."""

    def test_they_are_unresolved_rather_than_proven(self):
        assert UNSPLIT_FAIR_MARKET.status is MechanismStatus.UNRESOLVED
        assert UNSPLIT_FAIR_MARKET.blocks

    def test_they_are_not_recorded_as_non_complementary(self):
        """Silence about a split is not authorisation of a shortfall."""
        assert UNSPLIT_FAIR_MARKET.status is not MechanismStatus.NOT_COMPLEMENTARY

    def test_baseball_carries_one_of_them(self):
        assert UNSPLIT_FAIR_MARKET in BASEBALL_PROOF.mechanisms

    def test_soccer_does_not(self):
        assert UNSPLIT_FAIR_MARKET not in SOCCER_PROOF.mechanisms

    def test_the_contextual_inference_is_acknowledged_not_used(self):
        assert "strong" in UNSPLIT_FAIR_MARKET.reasoning
        assert "inference" in UNSPLIT_FAIR_MARKET.reasoning


class TestEmergencyPowerIsDisclosedNotProvenAgainst:
    """Rule 1.11 leaves the semantic proof and stays in the report."""

    def test_it_is_not_a_settlement_mechanism(self):
        """The type system already forbids a residual risk appearing among the
        mechanisms, so the check that carries weight is that no mechanism cites
        the rule either -- it left the census rather than being renamed."""
        for proof in (BASEBALL_PROOF, SOCCER_PROOF):
            assert EMERGENCY_INTERVENTION in proof.residual_interventions
            assert not [m for m in proof.mechanisms if "1.11" in m.rule_reference]

    def test_it_reaches_the_venue_and_the_rules_rather_than_a_contract(self):
        joined = " | ".join(EMERGENCY_INTERVENTION.powers)
        assert "suspend or curtail trading venue-wide" in joined
        assert "modify or suspend any provision of the Rules" in joined

    def test_the_rationale_is_recorded_not_assumed(self):
        assert "no regulated venue can" in EMERGENCY_INTERVENTION.rationale

    def test_its_approval_gate_is_recorded(self):
        assert "Regulatory Oversight Committee" in EMERGENCY_INTERVENTION.approval_gate

    def test_the_disclosure_refuses_the_words_risk_free(self):
        disclosure = EMERGENCY_INTERVENTION.disclosure()
        assert "RESIDUAL_VENUE_INTERVENTION_RISK" in disclosure
        assert "may be described as risk-free" in disclosure

    def test_every_family_carries_the_disclosure_in_its_description(self):
        for proof in (BASEBALL_PROOF, SOCCER_PROOF):
            assert "RESIDUAL_VENUE_INTERVENTION_RISK" in proof.describe()

    def test_the_payload_carries_it_too(self):
        payload = SOCCER_PROOF.payload()
        assert len(payload["residual_interventions"]) == 1
        assert payload["conservation_scope"] == ConservationScope.NOT_ESTABLISHED.value

    def test_an_exclusion_must_quote_its_authority(self):
        with pytest.raises(ValueError, match="must quote the authority"):
            ResidualVenueInterventionRisk(
                venue="v", rule_reference="r", quoted_text="", powers=("p",), rationale="x"
            )

    def test_an_exclusion_must_name_the_powers(self):
        with pytest.raises(ValueError, match="name the powers"):
            ResidualVenueInterventionRisk(
                venue="v", rule_reference="r", quoted_text="t", powers=(), rationale="x"
            )


class TestTheExclusionIsNarrow:
    """The loophole this closes: relabelling a product clause as an emergency."""

    def test_every_census_mechanism_stays_inside_the_proof(self):
        for mechanism in SettlementMechanism:
            assert mechanism in IN_SCOPE_MECHANISMS, mechanism

    @pytest.mark.parametrize(
        "mechanism",
        [
            SettlementMechanism.CANCELLATION_LAST_RESULTS,
            SettlementMechanism.LAST_FAIR_PRICE,
            SettlementMechanism.TIE_SPLIT,
            SettlementMechanism.OUTCOME_REVIEW,
            SettlementMechanism.INDETERMINATE_FALLBACK,
            SettlementMechanism.VOID_REFUND,
        ],
    )
    def test_a_settlement_clause_cannot_be_excluded_however_labelled(self, mechanism):
        """Even claimed venue-wide and claimed unreachable in ordinary life."""
        scope = classify_intervention(
            mechanism=mechanism, venue_wide=True, reachable_in_ordinary_resolution=False
        )
        assert scope is InterventionScope.ORDINARY_CONTRACT_RESOLUTION

    def test_venue_authority_that_is_not_a_settlement_path_may_be_excluded(self):
        scope = classify_intervention(
            mechanism=None, venue_wide=True, reachable_in_ordinary_resolution=False
        )
        assert scope is InterventionScope.EXTRAORDINARY_VENUE_INTERVENTION

    def test_reachability_in_ordinary_resolution_keeps_it_in(self):
        scope = classify_intervention(
            mechanism=None, venue_wide=True, reachable_in_ordinary_resolution=True
        )
        assert scope is InterventionScope.ORDINARY_CONTRACT_RESOLUTION

    def test_a_power_that_is_not_venue_wide_stays_in(self):
        scope = classify_intervention(
            mechanism=None, venue_wide=False, reachable_in_ordinary_resolution=False
        )
        assert scope is InterventionScope.ORDINARY_CONTRACT_RESOLUTION


class TestReservationOfSettlementDeterminations:
    """What survives the scope correction, in all fourteen certifications."""

    def test_it_blocks_and_is_ordinary_scope(self):
        assert SETTLEMENT_DETERMINATION_RESERVATION.status is MechanismStatus.UNRESOLVED
        assert SETTLEMENT_DETERMINATION_RESERVATION.blocks
        assert SETTLEMENT_DETERMINATION_RESERVATION.mechanism in IN_SCOPE_MECHANISMS

    def test_the_reservation_is_quoted(self):
        assert "reserves the right to make settlement determinations" in (
            SETTLEMENT_DETERMINATION_RESERVATION.quoted_text
        )

    def test_the_stated_remedy_is_a_delay(self):
        assert "will be delayed" in SETTLEMENT_DETERMINATION_RESERVATION.quoted_text

    def test_but_it_has_an_open_ended_escape(self):
        assert "or as otherwise set forth" in (SETTLEMENT_DETERMINATION_RESERVATION.quoted_text)

    def test_source_delay_is_an_ordinary_contingency(self):
        assert "ordinary contingency" in SETTLEMENT_DETERMINATION_RESERVATION.reasoning

    def test_it_appears_in_every_family(self):
        for proof in (BASEBALL_PROOF, SOCCER_PROOF):
            assert SETTLEMENT_DETERMINATION_RESERVATION in proof.mechanisms

    def test_it_is_why_the_residual_does_not_carry_a_family_to_a_proof(self):
        """The precise answer to the scope question: removing Rule 1.11 leaves
        soccer with exactly one blocker, and it is not the residual."""
        assert [m.mechanism for m in SOCCER_PROOF.blocking] == [
            SETTLEMENT_DETERMINATION_RESERVATION.mechanism
        ]
        assert FAIR_MARKET_RESIDUAL.status is MechanismStatus.PROVEN_COMPLEMENTARY


class TestFamilyVerdicts:
    def test_no_mechanism_at_this_venue_is_disproven(self):
        for proof in (BASEBALL_PROOF, SOCCER_PROOF):
            assert not any(m.status is MechanismStatus.NOT_COMPLEMENTARY for m in proof.mechanisms)

    def test_the_families_block_on_incomplete_evidence(self):
        for proof in (BASEBALL_PROOF, SOCCER_PROOF):
            assert proof.status is ProofStatus.EVIDENCE_INCOMPLETE

    def test_soccer_would_still_block_with_the_evidence_gap_closed(self):
        """Because the Contingencies reservation remains unresolved -- an
        ordinary clause, not the excluded emergency authority."""
        closed = replace(SOCCER_PROOF, mechanism_closure_established=True)
        assert closed.status is ProofStatus.NOT_PROVEN

    def test_no_family_reaches_a_conservation_scope(self):
        for proof in (BASEBALL_PROOF, SOCCER_PROOF):
            assert proof.conservation_scope is ConservationScope.NOT_ESTABLISHED

    def test_removing_every_unresolved_path_would_prove_it(self):
        """The control: the machinery does prove things when the text supports
        it, so the blocked verdict is a finding and not a default."""
        clean = replace(
            SOCCER_PROOF,
            mechanisms=(ORDINARY, FAIR_MARKET_RESIDUAL),
            mechanism_closure_established=True,
        )
        assert clean.status is ProofStatus.PROVEN_FOR_SUBSET

    def test_and_even_then_the_claim_is_qualified_not_risk_free(self):
        """A proven family still carries the residual disclosure, and its scope
        is 'under normal governing settlement' rather than unqualified."""
        clean = replace(
            SOCCER_PROOF,
            mechanisms=(ORDINARY, FAIR_MARKET_RESIDUAL),
            mechanism_closure_established=True,
        )
        assert clean.conservation_scope is ConservationScope.NORMAL_GOVERNING_SETTLEMENT
        assert "extraordinary venue intervention" in clean.exact_bound()
        assert "RESIDUAL_VENUE_INTERVENTION_RISK" in clean.describe()

    def test_every_source_is_pinned_by_hash(self):
        assert BASEBALL_PROOF.unpinned_sources == ()
        assert SOCCER_PROOF.unpinned_sources == ()


class TestSoccerPartition:
    """A three-way partition established by text."""

    def test_the_partition_is_proven(self):
        assert SOCCER_TRIPLE.partition_proven
        assert len(SOCCER_TRIPLE.members) == 3

    def test_it_pays_one_notional_under_ordinary_settlement(self):
        assert SOCCER_TRIPLE.floor_under(SettlementRegime.BINARY_OUTCOME) == NOTIONAL

    def test_the_tie_iteration_is_quoted_from_the_criterion(self):
        assert "equal number of goals" in SOCCER_PROOF.notes[2]

    def test_penalties_are_excluded_from_the_winner_determination(self):
        assert "penalty shoot-outs are excluded" in SOCCER_PROOF.notes[3]

    def test_the_basket_cost_names_every_member(self):
        symbol = basket_cost_symbol(SOCCER_TRIPLE.members)
        for member in SOCCER_TRIPLE.members:
            assert member in symbol


class TestPartitionFloorCollapsesUnderResidualSettlement:
    """Within-contract conservation does not give an across-contract floor."""

    def test_the_residual_regime_drops_the_floor_to_zero(self):
        assert SOCCER_TRIPLE.floor_under(SettlementRegime.RESIDUAL_PRICE) == Price.from_value("0")

    def test_the_guaranteed_floor_is_therefore_zero(self):
        assert SOCCER_TRIPLE.guaranteed_floor == Price.from_value("0")
        assert not SOCCER_TRIPLE.survives_all_regimes

    def test_the_breaking_regime_is_named(self):
        assert SettlementRegime.RESIDUAL_PRICE in SOCCER_TRIPLE.breaking_regimes

    def test_every_member_still_conserves_internally(self):
        """The trap, stated as a test: perfect per-contract conservation and a
        zero basket floor are consistent, and here both hold."""
        for units in (0, 1234, 5000, 9999, NOTIONAL.units):
            assert residual_pair_conserves(NOTIONAL, Price(units))
        assert SOCCER_TRIPLE.guaranteed_floor.units == 0

    def test_the_two_team_pair_fails_the_same_way(self):
        assert BASEBALL_PAIR.partition_proven
        assert BASEBALL_PAIR.floor_under(SettlementRegime.BINARY_OUTCOME) == NOTIONAL
        assert BASEBALL_PAIR.guaranteed_floor == Price.from_value("0")

    def test_coupling_the_prices_would_restore_the_floor(self):
        """Names exactly what one sentence of a future amendment must say."""
        coupled = hypothetical_coupled_partition(SOCCER_TRIPLE)
        assert coupled.guaranteed_floor == NOTIONAL
        assert coupled.survives_all_regimes

    def test_the_coupling_is_recorded_as_independent_not_unknown(self):
        assert SOCCER_TRIPLE.price_coupling is PriceCoupling.INDEPENDENT
        assert not SOCCER_TRIPLE.price_coupling.supports_a_floor

    def test_unknown_coupling_also_fails_closed(self):
        unknown = replace(SOCCER_TRIPLE, price_coupling=PriceCoupling.UNKNOWN)
        assert unknown.guaranteed_floor == Price.from_value("0")

    def test_a_basket_must_record_at_least_one_regime(self):
        with pytest.raises(ValueError, match="no reachable settlement regime"):
            PartitionBasket(
                members=("a", "b"),
                notional=NOTIONAL,
                partition_proven=True,
                reachable_regimes=frozenset(),
            )

    def test_a_partition_needs_two_members(self):
        with pytest.raises(ValueError, match="at least two distinct members"):
            PartitionBasket(
                members=("a",),
                notional=NOTIONAL,
                partition_proven=True,
                reachable_regimes=REACHABLE_REGIMES,
            )


class TestCorePceLadder:
    def test_the_higher_strike_implies_the_lower(self):
        assert core_pce_ladder().relation is RelationClass.NESTED_IMPLICATION

    def test_the_implication_holds_at_every_value(self):
        for raw in ("0.10", "0.20", "0.25", "0.30", "0.31", "1.00"):
            value = Decimal(raw)
            if CORE_PCE_HIGH.yes_at(value):
                assert CORE_PCE_LOW.yes_at(value), raw

    def test_the_measurement_is_pinned_to_the_first_release(self):
        assert "first official release" in CORE_PCE_LOW.resolution_key
        assert "no revisions" in CORE_PCE_LOW.resolution_key

    def test_a_revised_print_would_be_a_different_measurement(self):
        revised = replace(CORE_PCE_HIGH, resolution_key="CorePCE|<month>|BEA revised|revisions")
        assert not CORE_PCE_LOW.same_measurement_as(revised)

    def test_the_ladder_does_not_survive_the_fair_market_path(self):
        proof = core_pce_ladder()
        assert not proof.survives_all_paths
        assert FAIR_MARKET_DELAY in proof.broken_by_settlement_paths
        assert EMERGENCY in proof.broken_by_settlement_paths


class TestFees:
    def test_the_schedules_own_worked_example_reproduces(self, monkeypatch):
        """The Fee Schedule works one example: k=0.06, 100 contracts at $0.35,
        giving $1.37. Run it through the implementation rather than restating
        the arithmetic, so the rounding mode is actually exercised."""
        monkeypatch.setitem(K_BY_PARTICIPANT, ParticipantType.FCM_RETAIL, Decimal("0.06"))
        fee = order_fee(Price.from_value("0.35"), 100, ParticipantType.FCM_RETAIL)
        assert fee == Money.from_value("1.370000")

    def test_rounding_is_half_up_rather_than_bankers(self):
        """A case landing exactly on a half cent, which is the only place the
        two modes differ -- and Polymarket US uses the other one, so this is
        not a hypothetical distinction.

        Professional tier at $0.25 for 2 contracts:
        0.12 x 0.25 x 0.75 x 2 = 0.045 exactly. Half-up gives $0.05;
        banker's rounding would give $0.04.
        """
        fee = order_fee(Price.from_value("0.25"), 2, ParticipantType.FCM_PROFESSIONAL)
        assert fee == Money.from_value("0.050000")

    def test_the_professional_tier_is_six_times_the_retail_tier(self):
        assert K_BY_PARTICIPANT[ParticipantType.FCM_PROFESSIONAL] == Decimal("0.12")
        assert K_BY_PARTICIPANT[ParticipantType.FCM_RETAIL] == Decimal("0.02")

    def test_the_fee_peaks_at_the_midpoint(self):
        mid = order_fee(Price.from_value("0.50"), 100, ParticipantType.FCM_RETAIL)
        edge = order_fee(Price.from_value("0.95"), 100, ParticipantType.FCM_RETAIL)
        assert mid.units > edge.units

    def test_the_one_cent_floor_applies(self):
        assert order_fee(Price.from_value("0.99"), 1, ParticipantType.FCM_RETAIL) == FEE_FLOOR

    def test_both_sides_are_charged(self):
        one = order_fee(Price.from_value("0.50"), 100, ParticipantType.MARKET_MAKER)
        assert round_trip_fee(Price.from_value("0.50"), 100, ParticipantType.MARKET_MAKER) == Money(
            one.units * 2
        )

    def test_a_price_above_the_notional_is_rejected(self):
        with pytest.raises(ValueError, match="exceeds the"):
            order_fee(Price.from_value("1.0001"), 1, ParticipantType.FCM_RETAIL)

    def test_negative_contracts_are_rejected(self):
        with pytest.raises(ValueError, match="must not be negative"):
            order_fee(Price.from_value("0.50"), -1, ParticipantType.FCM_RETAIL)


class TestVenueMechanics:
    def test_no_pricing_identity_is_recorded_as_eliminating_the_edge(self):
        assert ROTHERA_FEASIBILITY is RelationFeasibility.SEMANTICS_UNRESOLVED

    def test_both_settlement_regimes_are_reachable(self):
        assert SettlementRegime.BINARY_OUTCOME in REACHABLE_REGIMES
        assert SettlementRegime.RESIDUAL_PRICE in REACHABLE_REGIMES

    def test_independent_payouts_is_not_a_reachable_regime_here(self):
        """No Rothera clause sets the two sides separately, and saying otherwise
        would overstate the finding."""
        assert SettlementRegime.INDEPENDENT_PAYOUTS not in REACHABLE_REGIMES


class TestGoverningRecord:
    def test_fourteen_families_were_read(self):
        assert len(PRODUCT_FILINGS) == 14

    def test_the_dcm_hash_matches_the_independent_capture(self):
        """Taken again in phase 2D from the exchange's own index, and identical
        to the phase-2C copy -- which is how 'current' is established rather
        than assumed."""
        assert DCM_RULEBOOK_SHA256.startswith("26084b165a930e1c")
        assert len(DCM_RULEBOOK_SHA256) == 64

    def test_neither_history_claims_exhaustiveness(self):
        assert not DCM_RULEBOOK_HISTORY.versions_are_exhaustive
        assert not DCO_RULEBOOK_HISTORY.versions_are_exhaustive

    def test_the_certifications_are_amended_in_place(self):
        assert "position accountability level" in SUPERSEDED_TERM_NOTE
        assert "a hash alone does not establish currency" in SUPERSEDED_TERM_NOTE


class TestProphetXClosed:
    def test_a_cap_is_not_a_complement(self):
        assert "shall the combined payout across positions exceed" in (
            SETTLEMENT_DISCRETION.quoted_text
        )
        assert SETTLEMENT_DISCRETION.status is MechanismStatus.UNRESOLVED

    def test_it_is_not_recorded_as_disproven(self):
        assert SETTLEMENT_DISCRETION.status is not MechanismStatus.NOT_COMPLEMENTARY
        assert PROPHETX_PROOF.status is not ProofStatus.DISPROVEN

    def test_closing_the_evidence_gap_would_leave_not_proven(self):
        closed = replace(PROPHETX_PROOF, mechanism_closure_established=True)
        assert closed.status is ProofStatus.NOT_PROVEN

    def test_the_rulebook_is_pinned_by_hash(self):
        assert len(PROPHETX_SHA256) == 64
        assert PROPHETX_PROOF.unpinned_sources == ()

    def test_settlements_can_be_reversed_after_the_fact(self):
        assert "reverse, amend, or resettle" in PROPHETX_PROOF.notes[2]
