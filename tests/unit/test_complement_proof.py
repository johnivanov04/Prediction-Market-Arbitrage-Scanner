"""The complement-conservation research proof model.

The invariant under investigation: for every permitted terminal state there is
p in [0, N] with YES = p and NO = N - p, so YES + NO == N. Strict two-state is
the special case p in {0, N}, and the whole point of this model is that the
general claim can hold where the special case fails.

Every gate here fails closed. One unresolved path blocks; one non-complementary
path disproves; an unspecified rounding model blocks whatever the mechanisms
say, because p + (N - p) == N is arithmetic and what a venue actually pays is
not.
"""

from __future__ import annotations

import dataclasses

import pytest

from predarb.domain.money import Price
from predarb.semantics.complement_proof import (
    ComplementConservationProof,
    MechanismProof,
    MechanismStatus,
    ProofStatus,
    RoundingModel,
    SourceComponent,
)
from predarb.semantics.settlement_census import SettlementMechanism
from predarb.venues.kalshi.complement_findings import CONJECTURE_FAMILY_PROOF

pytestmark = pytest.mark.unit

N = Price.from_value("1.0000")

SOURCE = SourceComponent(
    name="exchange_rulebook",
    version="1.29",
    document_sha256="3b6d4ffd5b32330d3466179d4cae610372d07511123c9976bc6cbb1b5185240b",
    url="https://example.test/rulebook.pdf",
)


def mechanism(
    kind: SettlementMechanism,
    status: MechanismStatus,
    *,
    reachable: bool = True,
    because: str | None = None,
    both_branches: bool | None = None,
) -> MechanismProof:
    explicit = (
        both_branches
        if both_branches is not None
        else status is MechanismStatus.PROVEN_COMPLEMENTARY
    )
    return MechanismProof(
        mechanism=kind,
        status=status,
        rule_reference="Rule 6.3(a)",
        quoted_text="quoted governing text",
        reasoning="test reasoning",
        reachable=reachable,
        unreachable_because=because,
        both_branches_explicit=explicit,
    )


def proof(
    *mechanisms: MechanismProof,
    rounding: RoundingModel = RoundingModel.RESIDUAL,
    sources: tuple[SourceComponent, ...] = (SOURCE,),
    closure: bool = True,
) -> ComplementConservationProof:
    return ComplementConservationProof(
        subject="test market",
        notional=N,
        contract_type="Binary Contract",
        mechanisms=mechanisms,
        sources=sources,
        rounding=rounding,
        mechanism_closure_established=closure,
    )


ORDINARY_OK = mechanism(SettlementMechanism.ORDINARY_BINARY, MechanismStatus.PROVEN_COMPLEMENTARY)


class TestOrdinaryBinary:
    def test_a_single_proven_path_with_exact_rounding_proves_the_subset(self):
        assert proof(ORDINARY_OK).status is ProofStatus.PROVEN_FOR_SUBSET

    def test_the_bound_is_stated_exactly(self):
        assert "YES + NO == 1.0000 exactly" in proof(ORDINARY_OK).exact_bound()

    def test_a_proven_path_does_not_block(self):
        assert not ORDINARY_OK.blocks


class TestLosingSideZeroMustBeStated:
    """A payoff invariant must not rest on the absence of contrary text."""

    def test_proven_requires_both_branches_in_the_text(self):
        with pytest.raises(ValueError, match="inferred from silence"):
            mechanism(
                SettlementMechanism.ORDINARY_BINARY,
                MechanismStatus.PROVEN_COMPLEMENTARY,
                both_branches=False,
            )

    def test_the_requirement_applies_to_every_mechanism_not_just_binary(self):
        with pytest.raises(ValueError, match="both sides' payouts"):
            mechanism(
                SettlementMechanism.FAIR_ALLOCATION,
                MechanismStatus.PROVEN_COMPLEMENTARY,
                both_branches=False,
            )

    def test_unresolved_does_not_require_explicit_branches(self):
        """Only a positive claim carries the burden."""
        assert (
            mechanism(
                SettlementMechanism.FAIR_ALLOCATION,
                MechanismStatus.UNRESOLVED,
                both_branches=False,
            ).status
            is MechanismStatus.UNRESOLVED
        )

    def test_the_recorded_finding_carries_the_explicit_flag(self):
        ordinary = next(
            m
            for m in CONJECTURE_FAMILY_PROOF.mechanisms
            if m.mechanism is SettlementMechanism.ORDINARY_BINARY
        )
        assert ordinary.both_branches_explicit
        assert "receive no payment" in ordinary.quoted_text

    def test_the_recorded_finding_cites_this_products_own_filing(self):
        """Not generic prose from a different product."""
        ordinary = next(
            m
            for m in CONJECTURE_FAMILY_PROOF.mechanisms
            if m.mechanism is SettlementMechanism.ORDINARY_BINARY
        )
        assert "CONJECTURE.pdf" in ordinary.rule_reference


class TestFractionalComplementary:
    def test_a_fractional_path_can_still_be_proven_complementary(self):
        """The case the whole model exists for: strict two-state false,
        conservation true."""
        fractional = mechanism(
            SettlementMechanism.FRACTIONAL_SHARE, MechanismStatus.PROVEN_COMPLEMENTARY
        )
        assert proof(ORDINARY_OK, fractional).status is ProofStatus.PROVEN_FOR_SUBSET

    def test_a_scalar_path_proven_complementary_is_accepted(self):
        scalar = mechanism(
            SettlementMechanism.LAST_TRADED_PRICE, MechanismStatus.PROVEN_COMPLEMENTARY
        )
        assert proof(ORDINARY_OK, scalar).status is ProofStatus.PROVEN_FOR_SUBSET


class TestOneUnresolvedPathBlocks:
    def test_a_single_unresolved_path_blocks_the_whole_proof(self):
        unresolved = mechanism(SettlementMechanism.FAIR_ALLOCATION, MechanismStatus.UNRESOLVED)
        assert proof(ORDINARY_OK, unresolved).status is ProofStatus.NOT_PROVEN

    def test_the_blocking_path_is_named(self):
        unresolved = mechanism(SettlementMechanism.FAIR_ALLOCATION, MechanismStatus.UNRESOLVED)
        built = proof(ORDINARY_OK, unresolved)
        assert [m.mechanism for m in built.blocking] == [SettlementMechanism.FAIR_ALLOCATION]
        assert "FAIR_ALLOCATION is UNRESOLVED" in built.exact_bound()

    def test_many_proven_paths_do_not_outvote_one_unresolved(self):
        proven = [
            mechanism(kind, MechanismStatus.PROVEN_COMPLEMENTARY)
            for kind in (
                SettlementMechanism.ORDINARY_BINARY,
                SettlementMechanism.LAST_TRADED_PRICE,
                SettlementMechanism.TIE_SPLIT,
            )
        ]
        unresolved = mechanism(SettlementMechanism.OUTCOME_REVIEW, MechanismStatus.UNRESOLVED)
        assert proof(*proven, unresolved).status is ProofStatus.NOT_PROVEN


class TestOneNonComplementaryPathDisproves:
    def test_a_non_complementary_path_disproves_the_proof(self):
        broken = mechanism(SettlementMechanism.FAIR_ALLOCATION, MechanismStatus.NOT_COMPLEMENTARY)
        assert proof(ORDINARY_OK, broken).status is ProofStatus.DISPROVEN

    def test_disproven_outranks_missing_evidence(self):
        """The strongest thing known is reported: no further evidence repairs a
        rule that permits a shortfall."""
        broken = mechanism(SettlementMechanism.FAIR_ALLOCATION, MechanismStatus.NOT_COMPLEMENTARY)
        built = proof(ORDINARY_OK, broken, sources=(), closure=False)
        assert built.status is ProofStatus.DISPROVEN

    def test_an_unreachable_non_complementary_path_does_not_disprove(self):
        broken = mechanism(
            SettlementMechanism.NATURAL_PERSON_SCALAR,
            MechanismStatus.NOT_COMPLEMENTARY,
            reachable=False,
            because="the subject is not a natural person",
        )
        assert proof(ORDINARY_OK, broken).status is ProofStatus.PROVEN_FOR_SUBSET


class TestReachability:
    def test_an_unreachable_unresolved_path_does_not_block(self):
        unreachable = mechanism(
            SettlementMechanism.NATURAL_PERSON_SCALAR,
            MechanismStatus.UNRESOLVED,
            reachable=False,
            because="the Underlying's primary subject is a mathematical conjecture",
        )
        assert proof(ORDINARY_OK, unreachable).status is ProofStatus.PROVEN_FOR_SUBSET

    def test_a_reachable_natural_person_path_does_block(self):
        reachable = mechanism(SettlementMechanism.NATURAL_PERSON_SCALAR, MechanismStatus.UNRESOLVED)
        assert proof(ORDINARY_OK, reachable).status is ProofStatus.NOT_PROVEN

    def test_calling_a_path_unreachable_requires_a_reason(self):
        """Silently dropping a path looks identical to never considering it."""
        with pytest.raises(ValueError, match="must say why"):
            MechanismProof(
                mechanism=SettlementMechanism.NATURAL_PERSON_SCALAR,
                status=MechanismStatus.UNRESOLVED,
                rule_reference="Rule 6.3(e)",
                quoted_text="text",
                reasoning="r",
                reachable=False,
            )

    def test_every_mechanism_must_quote_its_governing_text(self):
        with pytest.raises(ValueError, match="must quote the governing text"):
            MechanismProof(
                mechanism=SettlementMechanism.ORDINARY_BINARY,
                status=MechanismStatus.PROVEN_COMPLEMENTARY,
                rule_reference="Rule 6.3(a)",
                quoted_text="   ",
                reasoning="r",
                both_branches_explicit=True,
            )

    def test_no_reachable_path_is_not_a_proof(self):
        unreachable = mechanism(
            SettlementMechanism.NATURAL_PERSON_SCALAR,
            MechanismStatus.PROVEN_COMPLEMENTARY,
            reachable=False,
            because="not a natural person",
        )
        assert proof(unreachable).status is ProofStatus.EVIDENCE_INCOMPLETE


class TestProductOverride:
    def test_a_product_specific_mechanism_participates(self):
        override = mechanism(SettlementMechanism.TIE_SPLIT, MechanismStatus.UNRESOLVED)
        built = proof(ORDINARY_OK, override)
        assert built.status is ProofStatus.NOT_PROVEN
        assert SettlementMechanism.TIE_SPLIT in {m.mechanism for m in built.blocking}

    def test_a_cancellation_rule_participates(self):
        override = mechanism(SettlementMechanism.LAST_FAIR_PRICE, MechanismStatus.UNRESOLVED)
        assert proof(ORDINARY_OK, override).status is ProofStatus.NOT_PROVEN


class TestRounding:
    def test_residual_rounding_conserves(self):
        assert RoundingModel.RESIDUAL.conserves_exactly
        assert proof(ORDINARY_OK, rounding=RoundingModel.RESIDUAL).status is (
            ProofStatus.PROVEN_FOR_SUBSET
        )

    def test_exact_no_rounding_conserves(self):
        assert proof(ORDINARY_OK, rounding=RoundingModel.EXACT_NO_ROUNDING).status is (
            ProofStatus.PROVEN_FOR_SUBSET
        )

    def test_independent_rounding_blocks(self):
        """Each side rounded on its own can lose the residue."""
        assert not RoundingModel.INDEPENDENT.conserves_exactly
        assert proof(ORDINARY_OK, rounding=RoundingModel.INDEPENDENT).status is (
            ProofStatus.NOT_PROVEN
        )

    def test_unspecified_rounding_blocks(self):
        assert proof(ORDINARY_OK, rounding=RoundingModel.UNSPECIFIED).status is (
            ProofStatus.NOT_PROVEN
        )

    def test_unspecified_rounding_yields_no_bound(self):
        bound = proof(ORDINARY_OK, rounding=RoundingModel.UNSPECIFIED).exact_bound()
        assert "no bound" in bound
        assert "UNSPECIFIED" in bound

    def test_rounding_blocks_even_when_every_mechanism_is_proven(self):
        """Arithmetic conservation is not payment conservation."""
        every = [
            mechanism(kind, MechanismStatus.PROVEN_COMPLEMENTARY)
            for kind in (
                SettlementMechanism.ORDINARY_BINARY,
                SettlementMechanism.FAIR_ALLOCATION,
                SettlementMechanism.LAST_TRADED_PRICE,
            )
        ]
        assert proof(*every, rounding=RoundingModel.UNSPECIFIED).status is (ProofStatus.NOT_PROVEN)


class TestMechanismOrderingIrrelevant:
    def test_order_does_not_change_the_status(self):
        a = mechanism(SettlementMechanism.FAIR_ALLOCATION, MechanismStatus.UNRESOLVED)
        b = mechanism(SettlementMechanism.LAST_TRADED_PRICE, MechanismStatus.UNRESOLVED)
        assert proof(ORDINARY_OK, a, b).status is proof(b, a, ORDINARY_OK).status

    def test_order_does_not_change_the_source_digest(self):
        second = dataclasses.replace(SOURCE, name="klear_dco_rulebook", version="1.3")
        forward = proof(ORDINARY_OK, sources=(SOURCE, second))
        backward = proof(ORDINARY_OK, sources=(second, SOURCE))
        assert forward.source_digest() == backward.source_digest()


class TestSourceDrift:
    def test_an_unchanged_source_set_still_applies(self):
        applies, reasons = proof(ORDINARY_OK).still_applies_to((SOURCE,))
        assert applies
        assert reasons == ()

    def test_a_changed_hash_invalidates_the_proof(self):
        drifted = dataclasses.replace(SOURCE, document_sha256="f" * 64)
        applies, reasons = proof(ORDINARY_OK).still_applies_to((drifted,))
        assert not applies
        assert any("content hash changed" in r for r in reasons)

    def test_a_changed_version_invalidates_the_proof(self):
        bumped = dataclasses.replace(SOURCE, version="1.30")
        applies, reasons = proof(ORDINARY_OK).still_applies_to((bumped,))
        assert not applies
        assert any("1.29 -> 1.30" in r for r in reasons)

    def test_a_missing_source_invalidates_the_proof(self):
        applies, reasons = proof(ORDINARY_OK).still_applies_to(())
        assert not applies
        assert any("no longer present" in r for r in reasons)

    def test_the_digest_changes_with_the_hash(self):
        drifted = dataclasses.replace(SOURCE, document_sha256="f" * 64)
        assert (
            proof(ORDINARY_OK).source_digest()
            != proof(ORDINARY_OK, sources=(drifted,)).source_digest()
        )


class TestUnknownGoverningVersionBlocks:
    def test_a_source_without_a_hash_blocks(self):
        """A version identified but not read cannot carry a proof."""
        unread = SourceComponent(name="klear_dco_rulebook", version="1.4", document_sha256=None)
        built = proof(ORDINARY_OK, sources=(SOURCE, unread))
        assert built.status is ProofStatus.EVIDENCE_INCOMPLETE
        assert built.unpinned_sources == ("klear_dco_rulebook",)

    def test_a_source_without_a_version_blocks(self):
        unversioned = SourceComponent(
            name="member_agreement", version=None, document_sha256="a" * 64
        )
        assert proof(ORDINARY_OK, sources=(SOURCE, unversioned)).status is (
            ProofStatus.EVIDENCE_INCOMPLETE
        )

    def test_no_sources_at_all_blocks(self):
        assert proof(ORDINARY_OK, sources=()).status is ProofStatus.EVIDENCE_INCOMPLETE

    def test_an_unknown_mechanism_closure_blocks(self):
        assert proof(ORDINARY_OK, closure=False).status is ProofStatus.EVIDENCE_INCOMPLETE


class TestRecordedKalshiFindings:
    """The actual research result for a non-natural-person family."""

    def _proof(self) -> ComplementConservationProof:
        return CONJECTURE_FAMILY_PROOF

    def test_the_verdict_is_not_proven(self):
        assert self._proof().status is ProofStatus.NOT_PROVEN

    def test_ordinary_binary_is_the_one_proven_path(self):
        proven = [
            m.mechanism
            for m in self._proof().mechanisms
            if m.status is MechanismStatus.PROVEN_COMPLEMENTARY
        ]
        assert proven == [SettlementMechanism.ORDINARY_BINARY]

    def test_fair_allocation_is_the_blocker(self):
        blocking = {m.mechanism for m in self._proof().blocking}
        assert SettlementMechanism.FAIR_ALLOCATION in blocking

    def test_the_natural_person_path_is_recorded_as_unreachable(self):
        found = next(
            m
            for m in self._proof().mechanisms
            if m.mechanism is SettlementMechanism.NATURAL_PERSON_SCALAR
        )
        assert not found.reachable
        assert "conjecture" in (found.unreachable_because or "")

    def test_nothing_is_disproven(self):
        """No governing text examined permits a shortfall; it is silent, which
        is a different finding."""
        assert all(
            m.status is not MechanismStatus.NOT_COMPLEMENTARY for m in self._proof().mechanisms
        )

    def test_the_rounding_model_is_unspecified(self):
        assert self._proof().rounding is RoundingModel.UNSPECIFIED

    def test_every_source_is_pinned(self):
        assert self._proof().unpinned_sources == ()

    def test_every_mechanism_quotes_its_text(self):
        assert all(m.quoted_text.strip() for m in self._proof().mechanisms)
