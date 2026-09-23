"""AT_LEAST_ONE: the forbidden state, and everything it must not be confused with.

The whole risk in this claim is that it looks like mutual exclusion and is not.
AT_MOST_ONE and AT_LEAST_ONE forbid opposite terminal states, permit opposite
multiplicities, and are falsified by opposite discoveries. These tests pin that
contrast at every level: the claim, the state rule, the checklist and the
detector.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import predarb.semantics.relation as relation_module
import predarb.venues.kalshi.relation_capture as capture_module
from predarb.semantics.relation import (
    ALL_SELECTED_MEMBERS_LOSE,
    MAX_ENUMERABLE_MEMBERS,
    JointStateRule,
    RelationClaim,
    checklist_for_claim,
    enumerate_joint_assignments,
)
from predarb.semantics.review import ChecklistAnswer

pytestmark = pytest.mark.unit

MEMBERS = ("A", "B", "C")


def rule(claim: RelationClaim, members: tuple[str, ...] = MEMBERS) -> JointStateRule:
    return JointStateRule(claim=claim, members=members)


class TestTheForbiddenState:
    """ALL-NO is valid under one claim and forbidden under the other."""

    def test_all_no_is_forbidden_under_at_least_one(self):
        assert rule(RelationClaim.AT_LEAST_ONE).permits(()) is False

    def test_all_no_is_permitted_under_at_most_one(self):
        """Not an oversight. AT_MOST_ONE never claimed anything must happen."""
        assert rule(RelationClaim.AT_MOST_ONE).permits(()) is True

    def test_only_at_least_one_names_a_forbidden_state(self):
        assert rule(RelationClaim.AT_LEAST_ONE).forbidden_state == ALL_SELECTED_MEMBERS_LOSE
        assert rule(RelationClaim.AT_MOST_ONE).forbidden_state is None

    def test_the_forbidden_state_is_described_for_audit(self):
        text = rule(RelationClaim.AT_LEAST_ONE).forbidden_description()
        assert ALL_SELECTED_MEMBERS_LOSE in text
        assert "only terminal state that falsifies" in text

    def test_at_most_one_describes_its_own_forbidden_shape(self):
        text = rule(RelationClaim.AT_MOST_ONE).forbidden_description()
        assert "two or more" in text
        assert f"{ALL_SELECTED_MEMBERS_LOSE} is permitted" in text


class TestMultipleYesIsValid:
    """The mistake this claim invites: reading it as exactly-one."""

    @pytest.mark.parametrize(
        "winners", [("A",), ("A", "B"), ("A", "B", "C")], ids=["one", "two", "all"]
    )
    def test_any_non_empty_winner_set_is_permitted(self, winners):
        assert rule(RelationClaim.AT_LEAST_ONE).permits(winners) is True

    @pytest.mark.parametrize("winners", [("A", "B"), ("A", "B", "C")])
    def test_at_most_one_forbids_exactly_those(self, winners):
        assert rule(RelationClaim.AT_MOST_ONE).permits(winners) is False

    def test_the_claim_advertises_that_multiple_yes_is_allowed(self):
        assert RelationClaim.AT_LEAST_ONE.permits_multiple_yes is True
        assert RelationClaim.AT_MOST_ONE.permits_multiple_yes is False

    def test_the_proposition_disclaims_mutual_exclusion(self):
        text = RelationClaim.AT_LEAST_ONE.proposition
        assert "at least one" in text.lower()
        assert "asserts nothing about whether two of them could both settle YES" in text
        assert "not a claim that the venue's market list is complete" in text


class TestSymbolicRuleMatchesEnumeration:
    """The predicate is symbolic; check it against a real enumeration anyway."""

    @pytest.mark.parametrize("claim", [RelationClaim.AT_MOST_ONE, RelationClaim.AT_LEAST_ONE])
    def test_every_assignment_agrees_with_the_predicate(self, claim):
        members = ("A", "B", "C", "D")
        assignments = enumerate_joint_assignments(rule(claim, members))
        assert len(assignments) == 2 ** len(members)
        for winners, permitted in assignments:
            expected = (
                len(winners) >= 1 if claim is RelationClaim.AT_LEAST_ONE else len(winners) <= 1
            )
            assert permitted is expected, winners

    def test_the_two_claims_agree_on_exactly_one_winner(self):
        """Their intersection is EXACTLY_ONE -- the composition Step 12 may derive."""
        members = ("A", "B", "C")
        most = dict(enumerate_joint_assignments(rule(RelationClaim.AT_MOST_ONE, members)))
        least = dict(enumerate_joint_assignments(rule(RelationClaim.AT_LEAST_ONE, members)))
        both = {w for w in most if most[w] and least[w]}
        assert both == {("A",), ("B",), ("C",)}

    def test_large_member_sets_are_never_enumerated(self):
        """2**n is a denial of service, not a state space. One event had 300."""
        members = tuple(f"M{i}" for i in range(MAX_ENUMERABLE_MEMBERS + 1))
        with pytest.raises(ValueError, match="refusing to enumerate"):
            enumerate_joint_assignments(rule(RelationClaim.AT_LEAST_ONE, members))

    def test_but_the_predicate_still_answers_for_them(self):
        members = tuple(f"M{i}" for i in range(300))
        big = rule(RelationClaim.AT_LEAST_ONE, members)
        assert big.permits(()) is False
        assert big.permits(("M7",)) is True
        assert big.permits(members[:100]) is True

    def test_a_state_over_foreign_members_is_refused(self):
        with pytest.raises(ValueError, match="not members of this relation"):
            rule(RelationClaim.AT_LEAST_ONE).permits(("A", "Z"))


class TestChecklistRouting:
    def test_each_claim_gets_its_own_checklist(self):
        most = checklist_for_claim(RelationClaim.AT_MOST_ONE)
        least = checklist_for_claim(RelationClaim.AT_LEAST_ONE)
        assert {q.key for q in most} != {q.key for q in least}

    def test_the_at_least_one_checklist_asks_about_all_no_directly(self):
        keys = {q.key for q in checklist_for_claim(RelationClaim.AT_LEAST_ONE)}
        assert "can_end_with_none_true" in keys
        assert "cancellation_settles_all_no" in keys

    @pytest.mark.parametrize(
        "key",
        [
            "outcome_universe_identified",
            "rules_guarantee_one_occurs",
            "all_outcomes_represented",
            "void_or_refund_possible",
            "scalar_prevents_full_yes",
            "ties_or_co_winners_understood",
            "member_set_covers_universe",
            "membership_enumeration_exhausted",
            "governing_documents_reviewed",
            "claim_is_not_mutual_exclusion",
        ],
    )
    def test_every_required_topic_is_covered(self, key):
        assert key in {q.key for q in checklist_for_claim(RelationClaim.AT_LEAST_ONE)}

    def test_uncertain_never_satisfies_any_question(self):
        """The claim is a conjunction; an uncertain conjunct is not a safe answer."""
        for question in checklist_for_claim(RelationClaim.AT_LEAST_ONE):
            assert not question.is_satisfied_by(ChecklistAnswer.UNCERTAIN)

    def test_the_two_checklists_ask_opposite_things_about_cancellation(self):
        """Why they cannot be merged into one shared list.

        AT_MOST_ONE needs cancellation not to create *multiple* YES outcomes.
        AT_LEAST_ONE needs it not to make *every* member settle NO. One list
        would have to be satisfied by contradictory answers.
        """
        most = next(
            q for q in checklist_for_claim(RelationClaim.AT_MOST_ONE) if "cancellation" in q.key
        )
        least = next(
            q for q in checklist_for_claim(RelationClaim.AT_LEAST_ONE) if "cancellation" in q.key
        )
        assert "multiple YES" in most.prompt
        assert "every" in least.prompt and "settle NO" in least.prompt


class TestMutuallyExclusiveCannotProveAtLeastOne:
    """A-52, as a regression test rather than a note in a document.

    Observed live: ``KXGOVCANOMR-26`` and ``KXNEWROLEX-26JAN`` both carry
    ``mutually_exclusive = true``, had every member settle, and produced **zero**
    YES winners. The flag bounds the maximum number of winners; it says nothing
    about the minimum.
    """

    def test_no_code_path_derives_at_least_one_from_the_flag(self):
        for module in (relation_module, capture_module):
            assert module.__file__ is not None
            source = Path(module.__file__).read_text()
            for line in source.splitlines():
                if "mutually_exclusive" in line and "AT_LEAST_ONE" in line:
                    pytest.fail(f"flag and claim appear together: {line.strip()}")

    def test_an_all_no_outcome_is_consistent_with_mutual_exclusion(self):
        """The observed KXGOVCANOMR-26 shape, stated as logic."""
        at_most_one = rule(RelationClaim.AT_MOST_ONE)
        assert at_most_one.permits(()) is True
        assert rule(RelationClaim.AT_LEAST_ONE).permits(()) is False
