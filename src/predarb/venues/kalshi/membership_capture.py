"""Enumerate every Kalshi market the venue lists under one event.

Three sources are read and kept apart:

    GET /markets?event_ticker=E             the live tier
    GET /historical/markets?event_ticker=E  the archived tier
    GET /events/E?with_nested_markets=true  the convenience view

The third is **not** treated as authoritative. Its own documentation says
"Historical markets settled before the historical cutoff will not be included"
(A-48), so reading it alone silently under-reports membership. It is fetched
anyway, precisely so the size of that omission can be measured rather than
assumed.

Ordering is a correctness property, not a style choice
------------------------------------------------------
The live tier is queried **first**, the historical tier second.

The cutoff advances while we work. If a market crosses the boundary between the
two queries:

    live then historical   it is returned by both -> a duplicate, which we detect
    historical then live   it is returned by neither -> a silent omission

Duplicates are recoverable; omissions are invisible. So the order is fixed, and
the cutoff is read before and after so a mid-enumeration move is recorded rather
than discovered later as an unexplained gap.

Nothing here decides exhaustiveness. This module answers "what does Kalshi list
for E", which is a fact about a catalogue.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from datetime import UTC, datetime
from typing import Any

from predarb.semantics.membership import (
    MEMBERSHIP_SCHEMA_VERSION,
    CombinedVenueMembershipEvidence,
    MembershipAnomaly,
    MembershipMember,
    MembershipPathStatus,
    MembershipSource,
    PageTrace,
    membership_snapshot_id,
)
from predarb.venues.kalshi.client import KalshiReadOnlyClient
from predarb.venues.kalshi.membership_projection import (
    MembershipIdentityError,
    project_membership_member,
)

__all__ = [
    "MEMBERSHIP_PAGE_LIMIT",
    "enumerate_event_membership",
    "membership_summary",
    "sample_event_tickers",
]

MEMBERSHIP_PAGE_LIMIT = 1000


def _page_digest(payloads: Sequence[Mapping[str, Any]], cursor: str | None) -> str:
    """A stable digest of one page's raw content, for provenance.

    Key order and whitespace are normalised away -- two responses differing only
    in those describe the same page, and a digest that disagreed would report
    drift where none happened. The payloads themselves are unmodified, so the
    original record stays recoverable from the digest plus the stored evidence.
    """
    body = json.dumps({"markets": list(payloads), "cursor": cursor}, sort_keys=True, default=str)
    return hashlib.sha256(body.encode()).hexdigest()


@dataclass
class _Walk:
    """One paginated path, walked to its end and able to prove it."""

    path: str
    query: dict[str, str]
    payloads: list[dict[str, Any]]
    cursors: list[str]
    digests: list[str]
    pages: int = 0
    status: MembershipPathStatus = MembershipPathStatus.NOT_ATTEMPTED
    error: str | None = None
    identity_failures: list[str] = dataclass_field(default_factory=list)

    def trace(self) -> PageTrace:
        return PageTrace(
            path=self.path,
            query=self.query,
            pages=self.pages,
            returned=len(self.payloads),
            cursors=tuple(self.cursors),
            response_hashes=tuple(self.digests),
            status=self.status,
            error=self.error,
        )


async def _walk(
    client: KalshiReadOnlyClient,
    *,
    path: str,
    event_ticker: str,
    historical: bool,
    max_pages: int,
) -> _Walk:
    """Page through one tier, collecting **raw** payloads.

    Raw on purpose. The financial invariants ``KalshiMarket`` enforces are right
    for pricing and wrong here: a negative top-of-book size (A-49) says nothing
    about whether a ticker belongs to an event, and letting it fail the page
    turned a data-quality problem into "this event has no members".
    """
    walk = _Walk(
        path=path,
        query={"event_ticker": event_ticker, "limit": str(MEMBERSHIP_PAGE_LIMIT)},
        payloads=[],
        cursors=[],
        digests=[],
    )

    cursor: str | None = None
    seen: set[str] = set()
    try:
        while True:
            page, next_cursor = await client.get_market_payloads_page(
                historical=historical,
                cursor=cursor,
                limit=MEMBERSHIP_PAGE_LIMIT,
                event_ticker=event_ticker,
            )
            walk.pages += 1
            walk.payloads.extend(page)
            walk.digests.append(_page_digest(page, next_cursor))
            if next_cursor is None:
                # The venue declining to offer another cursor is the only
                # evidence of exhaustion the protocol provides.
                walk.status = MembershipPathStatus.EXHAUSTED
                break
            if next_cursor in seen:
                walk.status = MembershipPathStatus.FAILED
                walk.error = "venue returned a repeated cursor; following it would loop"
                break
            seen.add(next_cursor)
            walk.cursors.append(next_cursor)
            cursor = next_cursor
            if walk.pages >= max_pages:
                walk.status = MembershipPathStatus.TRUNCATED_BY_LIMIT
                walk.error = f"stopped at the local cap of {max_pages} page(s)"
                break
    except Exception as exc:
        walk.status = MembershipPathStatus.FAILED
        walk.error = f"{type(exc).__name__}: {exc}"
    return walk


def _project(walk: _Walk, *, event_ticker: str, source: MembershipSource) -> list[MembershipMember]:
    """Turn raw payloads into membership facts, recording identity failures.

    A record whose identity cannot be established is **not** dropped quietly --
    it is counted against the walk, which then cannot claim to be a member list.
    """
    members: list[MembershipMember] = []
    for payload in walk.payloads:
        try:
            members.append(
                project_membership_member(
                    payload, expected_event_ticker=event_ticker, source=source
                )
            )
        except MembershipIdentityError as exc:
            walk.identity_failures.append(str(exc))
    if walk.identity_failures and walk.status is MembershipPathStatus.EXHAUSTED:
        walk.status = MembershipPathStatus.IDENTITY_UNUSABLE
        walk.error = f"{len(walk.identity_failures)} record(s) had unusable membership identity"
    return members


def _settlement_conflict(left: MembershipMember, right: MembershipMember) -> str | None:
    """Whether two views of one ticker disagree about anything material."""
    fields = ("status", "result", "settled_ts", "market_type", "strike_type")
    differing = [name for name in fields if getattr(left, name) != getattr(right, name)]
    return ", ".join(differing) or None


def _merge_tiers(
    live_members: Sequence[MembershipMember], historical_members: Sequence[MembershipMember]
) -> tuple[list[MembershipMember], set[MembershipAnomaly], list[str]]:
    """Union the two tiers, recording rather than resolving any disagreement."""
    anomalies: set[MembershipAnomaly] = set()
    detail: list[str] = []
    live_by_ticker = {m.ticker: m for m in live_members}
    historical_by_ticker = {m.ticker: m for m in historical_members}

    overlap = sorted(set(live_by_ticker) & set(historical_by_ticker))
    if overlap:
        anomalies.add(MembershipAnomaly.DUPLICATE_ACROSS_TIERS)
        detail.append(
            f"{len(overlap)} ticker(s) returned by both tiers, which the "
            f"live-then-historical order is expected to produce near the cutoff: "
            f"{', '.join(overlap[:5])}"
        )
    for ticker in overlap:
        conflict = _settlement_conflict(live_by_ticker[ticker], historical_by_ticker[ticker])
        if conflict:
            anomalies.add(MembershipAnomaly.CONFLICTING_PAYLOADS)
            detail.append(f"{ticker}: tiers disagree on {conflict}; neither view was merged")

    members: list[MembershipMember] = []
    for ticker in sorted(set(live_by_ticker) | set(historical_by_ticker)):
        in_live, in_hist = ticker in live_by_ticker, ticker in historical_by_ticker
        source = (
            MembershipSource.BOTH
            if in_live and in_hist
            else MembershipSource.LIVE
            if in_live
            else MembershipSource.HISTORICAL
        )
        # The live payload wins for a duplicate: it is the fresher of the two,
        # and any disagreement has already been recorded above rather than
        # resolved silently.
        chosen = live_by_ticker.get(ticker) or historical_by_ticker[ticker]
        members.append(replace(chosen, source=source))
        if chosen.data_quality_anomalies:
            anomalies.add(MembershipAnomaly.DATA_QUALITY_ANOMALY)
            detail.append(
                f"{ticker}: unusable field(s) "
                f"{sorted(chosen.data_quality_anomalies)} excluded from semantic "
                "reasoning; membership is unaffected"
            )
    return members, anomalies, detail


def _fold_in_nested(
    members: list[MembershipMember], nested: Sequence[str]
) -> tuple[list[MembershipMember], set[MembershipAnomaly], list[str]]:
    """Add any nested-only ticker, flagged rather than quietly absorbed.

    A market the convenience view returns but neither paginated tier does
    contradicts the documented partition, so it is surfaced.
    """
    anomalies: set[MembershipAnomaly] = set()
    detail: list[str] = []
    known = {m.ticker for m in members}
    for ticker in sorted(set(nested) - known):
        anomalies.add(MembershipAnomaly.NESTED_MEMBER_MISSING_FROM_TIERS)
        detail.append(f"{ticker}: returned by the nested event view but by neither tier")
        members.append(MembershipMember(ticker=ticker, source=MembershipSource.NESTED_EVENT_ONLY))
    return members, anomalies, detail


def _check_partition(
    members: Sequence[MembershipMember], cutoff: datetime | None
) -> tuple[set[MembershipAnomaly], list[str]]:
    """Verify the documented partition rather than assuming it held."""
    anomalies: set[MembershipAnomaly] = set()
    detail: list[str] = []
    if cutoff is None:
        return anomalies, detail
    for member in members:
        if member.settled_ts is None:
            if member.source is MembershipSource.HISTORICAL:
                anomalies.add(MembershipAnomaly.HISTORICAL_MEMBER_UNSETTLED)
                detail.append(f"{member.ticker}: archived but reports no settlement time")
            continue
        # BOTH counts as a live answer here. The live tier *did* return it, and
        # the documentation says a market settled before the cutoff is "only
        # available via GET /historical/markets" -- so the live tier returning
        # it at all is the departure worth recording, duplicate or not.
        returned_live = member.source in {MembershipSource.LIVE, MembershipSource.BOTH}
        if returned_live and member.settled_ts < cutoff:
            anomalies.add(MembershipAnomaly.LIVE_MEMBER_SETTLED_BEFORE_CUTOFF)
            detail.append(
                f"{member.ticker}: live tier returned a market settled "
                f"{member.settled_ts.isoformat()}, before the cutoff {cutoff.isoformat()}"
            )
        if member.source is MembershipSource.HISTORICAL and member.settled_ts >= cutoff:
            anomalies.add(MembershipAnomaly.HISTORICAL_MEMBER_SETTLED_AFTER_CUTOFF)
            detail.append(
                f"{member.ticker}: historical tier returned a market settled "
                f"{member.settled_ts.isoformat()}, at or after the cutoff"
            )
    return anomalies, detail


async def enumerate_event_membership(
    client: KalshiReadOnlyClient,
    event_ticker: str,
    *,
    at: datetime | None = None,
    max_pages: int = 100,
) -> CombinedVenueMembershipEvidence:
    """Walk both tiers plus the nested event view, and report what each held."""
    observed_at = at or datetime.now(tz=UTC)
    errors: list[str] = []

    cutoff_before: datetime | None = None
    cutoff_after: datetime | None = None
    try:
        cutoff_before = (await client.get_historical_cutoff()).market_settled_ts
    except Exception as exc:
        errors.append(f"cutoff (before): {type(exc).__name__}: {exc}")

    # Live first. See the module docstring: this order can only create
    # duplicates, never omissions.
    live = await _walk(
        client, path="/markets", event_ticker=event_ticker, historical=False, max_pages=max_pages
    )
    historical = await _walk(
        client,
        path="/historical/markets",
        event_ticker=event_ticker,
        historical=True,
        max_pages=max_pages,
    )

    try:
        cutoff_after = (await client.get_historical_cutoff()).market_settled_ts
    except Exception as exc:
        errors.append(f"cutoff (after): {type(exc).__name__}: {exc}")

    nested: list[str] = []
    event_status: str | None = None
    event_last_updated: datetime | None = None
    mutually_exclusive: bool | None = None
    try:
        envelope = await client.get_event(event_ticker, with_nested_markets=True)
        nested = [m.ticker for m in envelope.member_markets]
        event_last_updated = envelope.event.last_updated_ts
        mutually_exclusive = envelope.event.mutually_exclusive
    except Exception as exc:
        errors.append(f"event {event_ticker}: {type(exc).__name__}: {exc}")

    live_members = _project(live, event_ticker=event_ticker, source=MembershipSource.LIVE)
    historical_members = _project(
        historical, event_ticker=event_ticker, source=MembershipSource.HISTORICAL
    )
    members, anomalies, detail = _merge_tiers(live_members, historical_members)
    for walk in (live, historical):
        if walk.identity_failures:
            anomalies.add(MembershipAnomaly.MEMBERSHIP_IDENTITY_UNUSABLE)
            detail.extend(walk.identity_failures)
    members, nested_anomalies, nested_detail = _fold_in_nested(members, nested)
    anomalies |= nested_anomalies
    detail.extend(nested_detail)

    partition_anomalies, partition_detail = _check_partition(members, cutoff_before)
    anomalies |= partition_anomalies
    detail.extend(partition_detail)

    if any(m.is_provisional for m in members):
        anomalies.add(MembershipAnomaly.PROVISIONAL_MEMBER_PRESENT)

    if cutoff_before is not None and cutoff_before != cutoff_after:
        anomalies.add(MembershipAnomaly.CUTOFF_MOVED_DURING_ENUMERATION)

    statuses = {m.status for m in members if m.status}
    event_status = ",".join(sorted(statuses)) if statuses else None

    provisional = CombinedVenueMembershipEvidence(
        snapshot_id="pending",
        event_ticker=event_ticker,
        observed_at=observed_at,
        knowledge_at=observed_at,
        schema_version=MEMBERSHIP_SCHEMA_VERSION,
        cutoff_before=cutoff_before,
        cutoff_after=cutoff_after,
        live_trace=live.trace(),
        historical_trace=historical.trace(),
        nested_members=tuple(nested),
        members=tuple(members),
        event_status=event_status,
        event_last_updated_ts=event_last_updated,
        event_mutually_exclusive=mutually_exclusive,
        anomalies=tuple(sorted(anomalies)),
        anomaly_detail=tuple(detail),
        capture_errors=tuple(errors),
    )
    return _with_snapshot_id(provisional)


def _with_snapshot_id(
    evidence: CombinedVenueMembershipEvidence,
) -> CombinedVenueMembershipEvidence:
    return replace(
        evidence,
        snapshot_id=membership_snapshot_id(
            event_ticker=evidence.event_ticker,
            fingerprint=evidence.fingerprint(),
            observed_at=evidence.observed_at,
        ),
    )


def membership_summary(evidence: CombinedVenueMembershipEvidence) -> dict[str, Any]:
    """A flat row for research reporting."""
    return {
        "event_ticker": evidence.event_ticker,
        "live_only": len(evidence.by_source(MembershipSource.LIVE)),
        "historical_only": len(evidence.by_source(MembershipSource.HISTORICAL)),
        "both": len(evidence.by_source(MembershipSource.BOTH)),
        "nested_only": len(evidence.by_source(MembershipSource.NESTED_EVENT_ONLY)),
        "union": len(evidence.members),
        "nested": len(evidence.nested_members),
        "nested_omissions": len(evidence.nested_omissions),
        "live_status": evidence.live_trace.status.value,
        "historical_status": evidence.historical_trace.status.value,
        "live_pages": evidence.live_trace.pages,
        "historical_pages": evidence.historical_trace.pages,
        "cutoff_stable": evidence.cutoff_stable,
        "mutually_exclusive": evidence.event_mutually_exclusive,
        "anomalies": [a.value for a in evidence.anomalies],
    }


async def sample_event_tickers(
    client: KalshiReadOnlyClient,
    *,
    count: int,
    series_limit: int,
    on_progress: Callable[[str], None] | None = None,
) -> list[str]:
    """A stratified spread of events: by category, by status, by size.

    Stratification matters because the behaviour under test is *conditional*.
    An event whose markets all settled last week exercises only the live tier
    and would report a clean partition no matter how the historical tier
    behaved. Reaching the archived tier at all requires old settled events, and
    the categories differ in how they are structured -- sports events are small
    and numerous, election events large and long-lived.
    """
    by_category: dict[str, list[str]] = {}
    series_seen = 0
    async for series in client.iter_series(limit=200):
        series_seen += 1
        by_category.setdefault(series.category or "(uncategorised)", []).append(series.ticker)
        if series_seen >= series_limit:
            break
    report = on_progress or (lambda _message: None)
    report(f"  {series_seen} series across {len(by_category)} category(ies)")

    tickers: list[str] = []
    seen: set[str] = set()
    per_category = max(1, count // max(1, len(by_category)))

    for category, series_tickers in sorted(by_category.items()):
        taken = 0
        # Alternate statuses so each category contributes both a live-tier and
        # an archived-tier case where it has them.
        for series_ticker in series_tickers:
            if taken >= per_category:
                break
            for status in ("settled", "open"):
                if taken >= per_category:
                    break
                try:
                    async for event in client.iter_events(
                        limit=200, series_ticker=series_ticker, status=status
                    ):
                        if event.event_ticker in seen:
                            continue
                        seen.add(event.event_ticker)
                        tickers.append(event.event_ticker)
                        taken += 1
                        break
                except Exception as exc:
                    report(f"    !! {series_ticker} {status}: {type(exc).__name__}")
        report(f"    {category}: {taken}")
    return tickers[:count]
