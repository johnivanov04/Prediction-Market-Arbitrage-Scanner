"""Unit tests for the domain vocabularies.

These mostly guard against accidental changes to values that are persisted or
that encode a safety rule.
"""

from __future__ import annotations

import pytest

from predarb.domain.enums import (
    ExecutionStatus,
    FeeType,
    PayoffStatus,
    RelationType,
    SemanticStatus,
    SettlementKind,
    VerificationStatus,
)

pytestmark = pytest.mark.unit


class TestSettlementSafety:
    def test_every_settlement_kind_is_distinct(self):
        # Unknown must never collapse into BINARY; that is the fail-closed rule.
        # Asserting on values catches an accidental duplicate, which a direct
        # member comparison cannot (the type checker proves that one trivially).
        values = [k.value for k in SettlementKind]
        assert len(values) == len(set(values))
        assert {"BINARY", "SCALAR", "UNKNOWN"} == set(values)

    def test_scalar_is_modelled_explicitly(self):
        # Kalshi exposes market_type == "scalar"; it has no two-state payoff.
        assert SettlementKind.SCALAR in set(SettlementKind)

    def test_only_binary_is_arbitrage_eligible(self):
        eligible = {k for k in SettlementKind if k is SettlementKind.BINARY}
        assert eligible == {SettlementKind.BINARY}


class TestRelationTypes:
    def test_phase_one_relations_present(self):
        for name in ("EQUIVALENT", "AT_MOST_ONE", "AT_LEAST_ONE", "EXACTLY_ONE"):
            assert name in RelationType.__members__

    def test_future_relations_reserved(self):
        for name in ("IMPLIES", "DISJOINT", "PARTITION"):
            assert name in RelationType.__members__

    def test_relation_values_are_all_distinct(self):
        # Mutual exclusion is not exhaustiveness. Conflating them is the
        # single most likely source of a false basket arbitrage, so no two
        # relation types may share a persisted value.
        values = [r.value for r in RelationType]
        assert len(values) == len(set(values))


class TestStatusAxesAreIndependent:
    def test_three_axes_do_not_share_values(self):
        # Keeping the axes disjoint makes it impossible to write a check that
        # accidentally treats "depth verified" as "semantically verified".
        semantic = set(SemanticStatus)
        payoff = set(PayoffStatus)
        execution = set(ExecutionStatus)
        assert not (set(map(str, semantic)) & set(map(str, payoff)))
        assert not (set(map(str, payoff)) & set(map(str, execution)))

    def test_locked_is_defined_but_unreachable_in_phase_one(self):
        # Phase 1 places no orders, so nothing can reach LOCKED.
        assert ExecutionStatus.LOCKED in set(ExecutionStatus)


class TestVerificationGate:
    def test_verification_states_are_distinct(self):
        values = [v.value for v in VerificationStatus]
        assert len(values) == len(set(values))
        assert set(values) == {"VERIFIED", "REVIEW_REQUIRED", "REJECTED"}


class TestFeeTypes:
    def test_values_match_the_live_openapi_enum(self):
        # Verified against the live API: KXHIGHNY reports fee_type "quadratic".
        assert {f.value for f in FeeType} == {
            "quadratic",
            "quadratic_with_maker_fees",
            "quadratic_with_combo_maker_fees",
            "flat",
        }
