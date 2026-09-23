"""The AT_LEAST_ONE evidence policy: what it demands, and when it demands it.

The policy decides whether a packet is complete enough to put in front of a
human. That is a lower bar than being true, and deliberately so -- a reviewer
should never have to notice a gap in their own evidence.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from predarb.semantics.evidence import DocumentRetrieval, ExternalDocument
from predarb.semantics.exhaustiveness import (
    MembershipReliance,
    ProofBasis,
    SupportingEvidence,
    exhaustiveness_incompleteness,
    membership_reliance_reasons,
)
from predarb.semantics.membership import (
    MEMBERSHIP_SCHEMA_VERSION,
    CombinedVenueMembershipEvidence,
    MembershipMember,
    MembershipPathStatus,
    MembershipSource,
    PageTrace,
)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
CUTOFF = datetime(2026, 7, 24, tzinfo=UTC)
MEMBERS = ("A", "B", "C")


def document(usable: bool = True) -> ExternalDocument:
    return ExternalDocument(
        url="https://kalshi.com/contract.pdf",
        retrieval=DocumentRetrieval.RETRIEVED if usable else DocumentRetrieval.HTTP_ERROR,
        content_sha256="d" * 64 if usable else None,
        content_bytes=1024 if usable else None,
    )


def trace(status: MembershipPathStatus = MembershipPathStatus.EXHAUSTED) -> PageTrace:
    return PageTrace(
        path="/markets",
        query={"event_ticker": "E"},
        pages=1,
        returned=3,
        cursors=(),
        response_hashes=("h" * 64,),
        status=status,
    )


def membership(**kwargs: object) -> CombinedVenueMembershipEvidence:
    defaults: dict[str, object] = {
        "snapshot_id": "snap",
        "event_ticker": "E",
        "observed_at": T0,
        "knowledge_at": T0,
        "schema_version": MEMBERSHIP_SCHEMA_VERSION,
        "cutoff_before": CUTOFF,
        "cutoff_after": CUTOFF,
        "live_trace": trace(),
        "historical_trace": trace(),
        "nested_members": MEMBERS,
        "members": tuple(
            MembershipMember(ticker=t, source=MembershipSource.HISTORICAL, status="finalized")
            for t in MEMBERS
        ),
    }
    return CombinedVenueMembershipEvidence(**{**defaults, **kwargs})  # type: ignore[arg-type]


def reasons(**kwargs: object) -> tuple[str, ...]:
    defaults: dict[str, object] = {
        "basis": ProofBasis.CONTRACT_LANGUAGE,
        "selected_members": MEMBERS,
        "documents": {"contract": document()},
        "settlement_sources": ["NOAA"],
        "membership": membership(),
        "cites_membership": False,
        "partition_supported": True,
    }
    return exhaustiveness_incompleteness(**{**defaults, **kwargs})  # type: ignore[arg-type]


class TestBaselineRequirements:
    def test_a_complete_packet_has_no_reasons(self):
        assert reasons() == ()

    def test_a_missing_basis_blocks(self):
        """A reviewer cannot check a proof without being told what it rests on."""
        assert any("no proof basis declared" in r for r in reasons(basis=None))

    def test_fewer_than_two_members_blocks(self):
        result = reasons(selected_members=("A",))
        assert any("certainty claim about a single market" in r for r in result)

    def test_an_unretrieved_document_blocks(self):
        result = reasons(documents={"contract": document(usable=False)})
        assert any("referenced but not retrieved" in r for r in result)

    def test_no_document_at_all_blocks(self):
        """Stricter than AT_MOST_ONE: cancellation clauses live in the contract."""
        result = reasons(documents={})
        assert any("no governing document was retrieved" in r for r in result)

    def test_no_settlement_source_blocks(self):
        result = reasons(settlement_sources=[])
        assert any("no settlement source" in r for r in result)


class TestMembershipIsNotAProofBasis:
    """Correction 1: the catalogue can corroborate a proof, never be one.

    Enumerating every endpoint we know about is a fact about a catalogue. "One
    of these propositions must be true" is a fact about the world. The type
    system refuses to let one stand in for the other, so this is not a rule a
    reviewer can forget to apply.
    """

    def test_the_proof_basis_enum_has_no_membership_member(self):
        assert {b.value for b in ProofBasis} == {"CONTRACT_LANGUAGE", "STRUCTURED_PARTITION"}
        assert not any("MEMBERSHIP" in b.value for b in ProofBasis)

    def test_membership_coverage_is_typed_as_supporting_evidence(self):
        assert SupportingEvidence.VENUE_MEMBERSHIP_COVERAGE.value == "VENUE_MEMBERSHIP_COVERAGE"
        assert "never a proof" in SupportingEvidence.VENUE_MEMBERSHIP_COVERAGE.describe

    def test_membership_union_alone_cannot_issue_at_least_one(self):
        """Perfect enumeration, no declared basis: still not approvable."""
        result = reasons(basis=None, cites_membership=True)
        assert any("no proof basis declared" in r for r in result)

    def test_membership_union_plus_mutually_exclusive_cannot_issue_at_least_one(self):
        """A-52: the flag bounds the maximum winners, never the minimum.

        Observed live: KXGOVCANOMR-26 and KXNEWROLEX-26JAN are both mutually
        exclusive, fully settled, and had zero YES winners.
        """
        result = reasons(
            basis=None,
            cites_membership=True,
            membership=membership(event_mutually_exclusive=True),
        )
        assert any("no proof basis declared" in r for r in result)

    def test_complete_pagination_alone_cannot_issue_at_least_one(self):
        flawless = membership(live_trace=trace(), historical_trace=trace())
        assert flawless.both_paths_exhausted
        assert any(
            "no proof basis declared" in r
            for r in reasons(basis=None, membership=flawless, cites_membership=True)
        )

    def test_no_basis_value_exists_that_means_catalogue_coverage(self):
        """There is no string a caller could pass to select it."""
        with pytest.raises(ValueError, match="VENUE_MEMBERSHIP_COVERAGE"):
            ProofBasis("VENUE_MEMBERSHIP_COVERAGE")


class TestMembershipMayCorroborate:
    def test_a_contract_language_proof_may_cite_membership(self):
        assert reasons(basis=ProofBasis.CONTRACT_LANGUAGE, cites_membership=True) == ()

    def test_a_structured_partition_proof_may_cite_membership(self):
        assert (
            reasons(
                basis=ProofBasis.STRUCTURED_PARTITION,
                cites_membership=True,
                partition_supported=True,
            )
            == ()
        )

    def test_cited_membership_must_still_be_sound(self):
        result = reasons(
            basis=ProofBasis.CONTRACT_LANGUAGE,
            cites_membership=True,
            membership=membership(live_trace=trace(MembershipPathStatus.FAILED)),
        )
        assert any("cited membership enumeration did not exhaust" in r for r in result)

    def test_uncited_membership_quality_is_never_demanded(self):
        """A proof that never mentions the catalogue is not blocked by it."""
        assert (
            reasons(
                basis=ProofBasis.CONTRACT_LANGUAGE,
                cites_membership=False,
                membership=membership(live_trace=trace(MembershipPathStatus.FAILED)),
            )
            == ()
        )

    def test_citing_membership_that_was_never_captured_blocks(self):
        result = reasons(basis=ProofBasis.CONTRACT_LANGUAGE, cites_membership=True, membership=None)
        assert any("cited as corroboration but was never captured" in r for r in result)

    def test_cited_membership_with_uncovered_markets_blocks(self):
        result = reasons(
            basis=ProofBasis.CONTRACT_LANGUAGE,
            cites_membership=True,
            selected_members=("A", "B"),
        )
        assert any("does not cover" in r and "C" in r for r in result)

    def test_cited_membership_on_an_open_event_blocks(self):
        result = reasons(
            basis=ProofBasis.CONTRACT_LANGUAGE,
            cites_membership=True,
            membership=membership(
                members=tuple(
                    MembershipMember(ticker=t, source=MembershipSource.LIVE, status="active")
                    for t in MEMBERS
                )
            ),
        )
        assert any("may add more" in r for r in result)

    def test_cited_membership_with_provisional_members_blocks(self):
        result = reasons(
            basis=ProofBasis.CONTRACT_LANGUAGE,
            cites_membership=True,
            membership=membership(
                members=tuple(
                    MembershipMember(
                        ticker=t,
                        source=MembershipSource.HISTORICAL,
                        status="finalized",
                        is_provisional=True,
                    )
                    for t in MEMBERS
                )
            ),
        )
        assert any("provisional" in r for r in result)

    def test_cited_membership_with_a_moving_cutoff_blocks(self):
        result = reasons(
            basis=ProofBasis.CONTRACT_LANGUAGE,
            cites_membership=True,
            membership=membership(cutoff_after=T0),
        )
        assert any("cutoff moved" in r for r in result)


class TestPartitionBasis:
    def test_partition_basis_requires_gap_free_coverage(self):
        result = reasons(basis=ProofBasis.STRUCTURED_PARTITION, partition_supported=False)
        assert any("do not demonstrate gap-free coverage" in r for r in result)

    def test_partition_basis_ignores_membership_coverage_when_uncited(self):
        assert (
            reasons(
                basis=ProofBasis.STRUCTURED_PARTITION,
                selected_members=("A", "B"),
                partition_supported=True,
            )
            == ()
        )

    def test_only_partition_relies_on_strikes(self):
        assert ProofBasis.STRUCTURED_PARTITION.relies_on_partition is True
        assert ProofBasis.CONTRACT_LANGUAGE.relies_on_partition is False


class TestMaterialityFollowsCitation:
    """Correction 1: invalidation tracks what a certificate actually relied on."""

    def test_citation_not_basis_decides_materiality(self):
        assert membership_reliance_reasons(False, None, MEMBERS) == ()
        assert membership_reliance_reasons(True, None, MEMBERS) != ()

    def test_the_reliance_vocabulary_is_explicit(self):
        assert {r.value for r in MembershipReliance} == {"AUDIT_ONLY", "MATERIAL"}
