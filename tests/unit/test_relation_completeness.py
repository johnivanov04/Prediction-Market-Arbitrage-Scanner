"""The distinction between mutual exclusion and exhaustiveness.

A standing limitation with teeth: ``GET /events/{ticker}`` omits markets that
settled before the historical cutoff (A-46), so the returned market list is
current observed membership, never proven exhaustive membership.

These tests exist so the distinction cannot quietly erode. They assert on the
*enums and vocabulary*, because the failure mode is conceptual: someone adding
an ``EXACTLY_ONE`` detector on the strength of ``mutually_exclusive`` being
true.
"""

from __future__ import annotations

import pkgutil

import pytest

from predarb import detectors
from predarb.domain.enums import RelationType

pytestmark = pytest.mark.unit


class TestRelationVocabulary:
    def test_mutual_exclusion_and_exhaustiveness_are_distinct_relations(self):
        """ "No two can both win" is not "one must win"."""
        relations = {
            RelationType.AT_MOST_ONE,
            RelationType.AT_LEAST_ONE,
            RelationType.EXACTLY_ONE,
        }
        assert len(relations) == 3
        assert len({relation.value for relation in relations}) == 3

    def test_the_exhaustiveness_relations_exist_but_are_unimplemented(self):
        """Declared so the storage schema needs no migration; not proven.

        Each asserts that *some* outcome must occur among a known set, which is
        exactly a completeness claim -- and the endpoint that enumerates the set
        explicitly does not guarantee completeness.
        """
        for relation in (RelationType.AT_LEAST_ONE, RelationType.EXACTLY_ONE):
            assert relation in RelationType


class TestNoExhaustivenessDetectorExists:
    """No detector may prove a completeness claim from current metadata."""

    def test_no_detector_module_implements_them(self):

        names = {module.name for module in pkgutil.iter_modules(detectors.__path__)}
        for forbidden in ("at_least_one", "exactly_one", "partition", "exhaustive"):
            assert forbidden not in names, (
                f"{forbidden} detector exists, but A-46 means event membership "
                "cannot be shown exhaustive"
            )
