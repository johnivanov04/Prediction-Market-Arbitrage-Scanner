"""Assemble the evidence packet for one AT_LEAST_ONE review.

Builds on the Step 9 relation capture rather than replacing it: the member
semantics, event fields and governing documents an AT_LEAST_ONE review needs are
the same ones AT_MOST_ONE needs. What this adds is the evidence specific to
*exhaustiveness*:

* **venue membership** -- live plus historical enumeration, so a reviewer can
  see whether the selected set covers what Kalshi lists, and see the caveats
  attached to that;
* **structured partition coverage** -- whether the members' documented strike
  intervals tile a domain without gaps.

Both are captured whatever the declared basis, because a reviewer benefits from
seeing them either way. Only the declared basis decides whether they are
*material* -- that is, whether a change in them should invalidate the resulting
certificate. Capturing everything and binding only what the proof used is the
distinction between an informative packet and a certificate that expires for
reasons its own argument never depended on.

Nothing here approves, issues, or scores anything.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal

from predarb.semantics.exhaustiveness import ProofBasis, SupportingEvidence
from predarb.semantics.membership import CombinedVenueMembershipEvidence
from predarb.semantics.partition import (
    CoverageStatus,
    DomainBound,
    IntervalCoverage,
    StrikeInterval,
    UnsupportedStrikeError,
    analyse_coverage,
    interval_from_strike,
)
from predarb.semantics.relation import RelationEvidenceBundle
from predarb.venues.kalshi.client import KalshiReadOnlyClient
from predarb.venues.kalshi.evidence_capture import DocumentFetcher
from predarb.venues.kalshi.membership_capture import enumerate_event_membership
from predarb.venues.kalshi.relation_capture import capture_relation_evidence

__all__ = ["ExhaustivenessEvidence", "capture_exhaustiveness_evidence", "coverage_for_members"]


@dataclass(frozen=True, slots=True)
class ExhaustivenessEvidence:
    """Everything one AT_LEAST_ONE review needs, with its parts kept separate."""

    bundle: RelationEvidenceBundle
    membership: CombinedVenueMembershipEvidence | None
    coverage: IntervalCoverage
    basis: ProofBasis
    cites_membership: bool = False
    """Whether the proof cites membership as corroboration. Never a basis."""

    @property
    def partition_supported(self) -> bool:
        return self.coverage.supports_exhaustiveness_argument

    def describe(self) -> str:
        membership = (
            self.membership.describe() if self.membership is not None else "no membership evidence"
        )
        return (
            f"{self.bundle.event_ticker} basis={self.basis.value} | "
            f"{membership} | coverage {self.coverage.status.value}"
        )


def coverage_for_members(
    membership: CombinedVenueMembershipEvidence | None,
    selected_members: tuple[str, ...],
    *,
    domain: DomainBound | None = None,
) -> IntervalCoverage:
    """Read the selected members' documented strike fields as intervals.

    Members whose strike semantics have no documented grammar -- ``functional``,
    ``custom``, ``structured`` -- are listed as unreadable rather than skipped.
    A coverage result computed over a subset of the members would describe a
    different, smaller claim than the one under review.
    """
    if membership is None:
        return IntervalCoverage(
            status=CoverageStatus.NOT_APPLICABLE,
            intervals=(),
            detail="no membership evidence, so no strike fields were read",
        )

    by_ticker = {m.ticker: m for m in membership.members}
    intervals: list[StrikeInterval] = []
    unreadable: list[str] = []
    for ticker in selected_members:
        member = by_ticker.get(ticker)
        if member is None:
            unreadable.append(f"{ticker}: not present in membership evidence")
            continue
        try:
            intervals.append(
                interval_from_strike(
                    ticker=ticker,
                    strike_type=member.strike_type,
                    floor_strike=(
                        Decimal(member.floor_strike) if member.floor_strike is not None else None
                    ),
                    cap_strike=(
                        Decimal(member.cap_strike) if member.cap_strike is not None else None
                    ),
                )
            )
        except UnsupportedStrikeError as exc:
            unreadable.append(str(exc))

    if not intervals:
        return IntervalCoverage(
            status=CoverageStatus.NOT_APPLICABLE,
            intervals=(),
            unreadable=tuple(unreadable),
            detail="no member exposes a readable numeric strike interval",
        )
    return analyse_coverage(intervals, domain=domain, unreadable=unreadable)


async def capture_exhaustiveness_evidence(
    client: KalshiReadOnlyClient,
    *,
    event_ticker: str,
    selected_members: list[str],
    basis: ProofBasis,
    cites_membership: bool = False,
    at: datetime | None = None,
    fetcher: DocumentFetcher | None = None,
    fetch_documents: bool = True,
    domain: DomainBound | None = None,
) -> ExhaustivenessEvidence:
    """Capture the full packet. Read-only public market data throughout."""
    moment = at or datetime.now(tz=UTC)

    base = await capture_relation_evidence(
        client,
        event_ticker=event_ticker,
        selected_members=selected_members,
        at=moment,
        fetcher=fetcher,
        fetch_documents=fetch_documents,
    )

    membership: CombinedVenueMembershipEvidence | None = None
    errors = list(base.capture_errors)
    try:
        membership = await enumerate_event_membership(client, event_ticker, at=moment)
    except Exception as exc:
        errors.append(f"membership enumeration: {type(exc).__name__}: {exc}")

    coverage = coverage_for_members(membership, base.selected_members, domain=domain)

    bundle = replace(
        base,
        capture_errors=tuple(errors),
        exhaustiveness_basis=basis.value,
        supporting_evidence=(
            (SupportingEvidence.VENUE_MEMBERSHIP_COVERAGE.value,) if cites_membership else ()
        ),
        membership_fingerprint=(
            membership.fingerprint().digest if membership is not None else None
        ),
        membership_is_material=cites_membership,
        partition_fingerprint=coverage.fingerprint().digest,
        partition_is_material=basis.relies_on_partition,
    )
    return ExhaustivenessEvidence(
        bundle=bundle,
        membership=membership,
        coverage=coverage,
        basis=basis,
        cites_membership=cites_membership,
    )
