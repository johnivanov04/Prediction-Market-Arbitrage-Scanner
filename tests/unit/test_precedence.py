"""Which settlement rule controls: the product's citation or the general fallback.

The adjudication has three findings and they are tracked apart on purpose,
because collapsing them is how one reading wins by assumption:

* does Rule 7.1 necessarily yield a binary Market Outcome?
* does the product's citation displace Rule 6.3(c)?
* does Rule 6.3(c) fire anyway?

The first can be well supported while the others stay open -- which is exactly
where the evidence leaves the real case.
"""

from __future__ import annotations

import pytest

from predarb.semantics.complement_proof import RoundingModel
from predarb.semantics.precedence import (
    ControlFinding,
    PrecedenceStatus,
    SettlementPrecedenceProof,
    SupportingAuthority,
)
from predarb.venues.kalshi.complement_findings import CONJECTURE_FAMILY_PROOF
from predarb.venues.kalshi.precedence_findings import RIEMANN_PRECEDENCE

pytestmark = pytest.mark.unit

RULEBOOK_SHA = "3b6d4ffd5b32330d3466179d4cae610372d07511123c9976bc6cbb1b5185240b"
TERMS_SHA = "109af26b85199725539a0f5d5b3567f63b97eac6be3fb4bfaa0d373949a8e618"

AUTHORITY = SupportingAuthority(
    source="Exchange Rulebook v1.29",
    reference="Rule 7.1(a)",
    quoted_text="the Outcome Review Committee will determine the final Market Outcome",
    supports="A",
    document_sha256=RULEBOOK_SHA,
)


def proof(
    *,
    binary_output: bool = True,
    controls: ControlFinding = ControlFinding.PRODUCT_RULE_CONTROLS,
    also_reachable: bool | None = False,
    authorities: tuple[SupportingAuthority, ...] = (AUTHORITY,),
    terms_sha: str = TERMS_SHA,
) -> SettlementPrecedenceProof:
    return SettlementPrecedenceProof(
        subject="test market",
        product_terms_sha256=terms_sha,
        rulebook_version="1.29",
        rulebook_sha256=RULEBOOK_SHA,
        product_contingency_text="determine payouts pursuant to Rule 7.1",
        referenced_rule="Rule 7.1",
        general_fallback_rule="Rule 6.3(c)",
        market_outcome_definition="YES or NO for a Binary Contract",
        committee_output_semantics="a Market Outcome",
        authorities=authorities,
        committee_output_is_binary=binary_output,
        product_rule_controls=controls,
        general_rule_also_reachable=also_reachable,
    )


class TestRule71ProducesAMarketOutcome:
    def test_a_binary_output_with_control_and_no_fallback_is_exclusive(self):
        assert proof().status is PrecedenceStatus.RULE7_BINARY_PATH_PROVEN_EXCLUSIVE

    def test_only_the_exclusive_status_unlocks_strict_two_state(self):
        assert proof().unlocks_strict_two_state
        for status in PrecedenceStatus:
            if status is not PrecedenceStatus.RULE7_BINARY_PATH_PROVEN_EXCLUSIVE:
                assert not status.permits_strict_two_state

    def test_a_non_binary_committee_output_blocks(self):
        """If the Committee could return something other than YES/NO, the whole
        Reading A chain fails at its first link."""
        assert proof(binary_output=False).status is PrecedenceStatus.PRECEDENCE_UNRESOLVED

    def test_the_recorded_finding_holds_the_output_binary(self):
        assert RIEMANN_PRECEDENCE.committee_output_is_binary

    def test_the_recorded_finding_quotes_the_committee_definition(self):
        quoted = " ".join(a.quoted_text for a in RIEMANN_PRECEDENCE.authorities)
        assert "to determine Market Outcomes" in quoted
        assert "full discretion" in quoted


class TestBinaryMarketOutcomeDomain:
    def test_the_definition_is_recorded_with_its_escape_clause(self):
        assert "unless otherwise specified in the contract terms" in (
            RIEMANN_PRECEDENCE.market_outcome_definition
        )

    def test_the_subject_terms_do_not_specify_otherwise(self):
        assert "do not specify otherwise" in RIEMANN_PRECEDENCE.market_outcome_definition

    def test_rule_71_output_semantics_note_the_absence_of_payout_language(self):
        semantics = RIEMANN_PRECEDENCE.committee_output_semantics
        assert "no payout" in semantics
        assert "full discretion" in semantics


class TestProductSpecificPrecedence:
    def test_a_controlling_product_rule_is_required_for_exclusivity(self):
        assert proof(controls=ControlFinding.PRODUCT_RULE_CONTROLS).status is (
            PrecedenceStatus.RULE7_BINARY_PATH_PROVEN_EXCLUSIVE
        )

    def test_a_non_controlling_product_rule_gives_the_general_rule(self):
        assert proof(controls=ControlFinding.PRODUCT_RULE_DOES_NOT_CONTROL).status is (
            PrecedenceStatus.RULE63C_PREVAILS
        )

    def test_an_unresolved_control_finding_blocks(self):
        assert proof(controls=ControlFinding.UNRESOLVED).status is (
            PrecedenceStatus.PRECEDENCE_UNRESOLVED
        )

    def test_unresolved_is_not_settled(self):
        assert not ControlFinding.UNRESOLVED.is_settled
        assert ControlFinding.PRODUCT_RULE_CONTROLS.is_settled
        assert ControlFinding.PRODUCT_RULE_DOES_NOT_CONTROL.is_settled


class TestGeneralFallbackStillReachable:
    def test_a_reachable_fallback_prevents_exclusivity(self):
        """A binary Rule 7.1 path and a live 6.3(c) path coexist; the contract is
        not thereby strictly two-state."""
        assert proof(also_reachable=True).status is (
            PrecedenceStatus.RULE7_BINARY_PATH_PROVEN_BUT_63C_ALSO_REACHABLE
        )
        assert not proof(also_reachable=True).unlocks_strict_two_state

    def test_unknown_reachability_blocks_rather_than_defaulting_to_false(self):
        """None means not established. It must not read as 'not reachable'."""
        assert proof(also_reachable=None).status is PrecedenceStatus.PRECEDENCE_UNRESOLVED

    def test_a_rule_71_clause_cannot_globally_suppress_63c(self):
        """Citing 7.1 in one product's terms is not a Rulebook amendment. The
        recorded finding leaves 6.3(c)'s reachability unestablished rather than
        treating the citation as switching it off."""
        assert RIEMANN_PRECEDENCE.general_rule_also_reachable is None
        assert RIEMANN_PRECEDENCE.status is PrecedenceStatus.PRECEDENCE_UNRESOLVED


class TestAmbiguousPrecedenceBlocks:
    def test_the_recorded_adjudication_is_unresolved(self):
        assert RIEMANN_PRECEDENCE.status is PrecedenceStatus.PRECEDENCE_UNRESOLVED

    def test_it_does_not_unlock_strict_two_state(self):
        assert not RIEMANN_PRECEDENCE.unlocks_strict_two_state

    def test_authorities_are_recorded_on_both_sides(self):
        assert len(RIEMANN_PRECEDENCE.authorities_for("A")) >= 3
        assert len(RIEMANN_PRECEDENCE.authorities_for("B")) >= 3

    def test_the_absence_of_a_precedence_rule_is_recorded_as_favouring_neither(self):
        neutral = RIEMANN_PRECEDENCE.authorities_for("neither")
        assert neutral
        assert any("precedence" in a.reference for a in neutral)

    def test_no_guidelines_were_located_and_that_is_recorded(self):
        assert not RIEMANN_PRECEDENCE.guidelines_located
        assert "No standalone Market Outcome Review Process Guidelines" in (
            RIEMANN_PRECEDENCE.guidelines_note
        )

    def test_every_authority_quotes_its_text(self):
        assert all(a.quoted_text.strip() for a in RIEMANN_PRECEDENCE.authorities)

    def test_an_authority_must_declare_which_reading_it_favours(self):
        with pytest.raises(ValueError, match="must be 'A', 'B' or 'neither'"):
            SupportingAuthority(source="x", reference="y", quoted_text="text", supports="maybe")

    def test_an_authority_must_quote_its_text(self):
        with pytest.raises(ValueError, match="must quote its text"):
            SupportingAuthority(source="x", reference="y", quoted_text="  ", supports="A")


class TestMigrationEvidenceIsRecorded:
    def test_all_three_migration_filings_are_cited(self):
        references = " ".join(a.source for a in RIEMANN_PRECEDENCE.authorities)
        for filing in ("rules02042638732", "rules040826937", "rules0608265775"):
            assert filing in references

    def test_the_conformance_language_is_quoted_verbatim(self):
        quoted = " ".join(a.quoted_text for a in RIEMANN_PRECEDENCE.authorities)
        assert "in line with the new Rulebook" in quoted
        assert "to align with Exchange Rulebook" in quoted

    def test_the_silent_migration_is_recorded(self):
        rainfall = next(a for a in RIEMANN_PRECEDENCE.authorities if "rules02042638732" in a.source)
        assert "does not mention it at all" in rainfall.reasoning


class TestSourceDriftInvalidatesPrecedence:
    def test_an_unchanged_source_set_still_applies(self):
        applies, reasons = RIEMANN_PRECEDENCE.still_applies_to(
            product_terms_sha256=TERMS_SHA, rulebook_sha256=RULEBOOK_SHA
        )
        assert applies
        assert reasons == ()

    def test_changed_product_terms_invalidate_it(self):
        applies, reasons = RIEMANN_PRECEDENCE.still_applies_to(
            product_terms_sha256="f" * 64, rulebook_sha256=RULEBOOK_SHA
        )
        assert not applies
        assert any("product terms" in r for r in reasons)

    def test_a_changed_rulebook_invalidates_it(self):
        applies, reasons = RIEMANN_PRECEDENCE.still_applies_to(
            product_terms_sha256=TERMS_SHA, rulebook_sha256="f" * 64
        )
        assert not applies
        assert any("Rulebook" in r for r in reasons)

    def test_the_digest_tracks_the_hashes(self):
        assert proof().source_digest() != proof(terms_sha="f" * 64).source_digest()


class TestOrderingInvariant:
    def test_authority_order_does_not_change_the_digest(self):
        second = SupportingAuthority(
            source="Exchange Rulebook v1.29",
            reference="Rule 6.3(c)",
            quoted_text="Kalshi will determine the payouts",
            supports="B",
            document_sha256=RULEBOOK_SHA,
        )
        forward = proof(authorities=(AUTHORITY, second))
        backward = proof(authorities=(second, AUTHORITY))
        assert forward.source_digest() == backward.source_digest()

    def test_authority_order_does_not_change_the_status(self):
        second = SupportingAuthority(
            source="Exchange Rulebook v1.29",
            reference="Rule 6.3(c)",
            quoted_text="Kalshi will determine the payouts",
            supports="B",
            document_sha256=RULEBOOK_SHA,
        )
        assert (
            proof(authorities=(AUTHORITY, second)).status
            is proof(authorities=(second, AUTHORITY)).status
        )


class TestNaturalPersonExclusionNeedsEvidence:
    def test_the_recorded_finding_states_why_63e_is_unreachable(self):
        notes = " ".join(RIEMANN_PRECEDENCE.notes)
        assert "6.3(e) is unreachable" in notes
        assert "mathematical conjecture" in notes


class TestRoundingConsequence:
    """If p is confined to {0, N} the rounding blocker falls away with it."""

    def test_the_conditional_relief_is_recorded_not_claimed(self):
        notes = " ".join(RIEMANN_PRECEDENCE.notes)
        assert "If a future filing" in notes
        assert "rounding blocker would fall away" in notes

    def test_relief_is_not_applied_while_precedence_is_unresolved(self):
        """The rounding blocker only lifts for a path proven unreachable."""
        assert RIEMANN_PRECEDENCE.status is PrecedenceStatus.PRECEDENCE_UNRESOLVED
        assert CONJECTURE_FAMILY_PROOF.rounding is RoundingModel.UNSPECIFIED
