"""Venue membership evidence: which markets the venue lists under an event.

This answers a question that is *not* the Step 11 target, and keeping the two
apart is the whole reason this module is separate from
:mod:`predarb.semantics.relation`:

    membership completeness   "we enumerated every Kalshi market for event E"
    outcome exhaustiveness    "at least one of these must settle YES"

The first is a fact about a venue's catalogue. The second is a fact about the
world and the governing contract. A complete market list establishes nothing
about exhaustiveness: Kalshi may simply not have listed a market for every
possible outcome, and no amount of enumeration discovers that.

Why this is *evidence* and not a completeness certificate
---------------------------------------------------------
The documentation supports a narrow claim and not a broad one.

Documented (A-48): a market that settled before ``market_settled_ts`` is
available only through ``GET /historical/markets``; the live ``GET /markets``
and nested-event responses exclude it.

**Not** documented: that ``live plus historical`` is the set of all markets that
ever belonged to the event. Two things cut against it:

* ``is_provisional`` is documented as "the market **may be removed** after
  determination if there is no activity on it" (A-51). A removed market is in
  neither tier, so membership can shrink and the union can under-report history.
* The cutoff advances (A-48), so a market can migrate between tiers *during*
  enumeration.

The second is mitigated rather than assumed away. Enumeration queries the
**live tier first and the historical tier second**, because a market that
archives mid-enumeration is then returned by both and shows up as a duplicate.
The opposite order can lose it from both, silently. Duplicates are detectable;
omissions are not.

So the object here is named for what it is. It records which paths were
exhausted, under which cutoff, with what provenance -- and refuses to call
itself complete.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from predarb.clock import ensure_utc
from predarb.semantics.fingerprint import ABSENT, SettlementEvidenceFingerprint

__all__ = [
    "MEMBERSHIP_SCHEMA_VERSION",
    "CombinedVenueMembershipEvidence",
    "MembershipAnomaly",
    "MembershipMember",
    "MembershipPathStatus",
    "MembershipSource",
    "PageTrace",
    "membership_snapshot_id",
]

MEMBERSHIP_SCHEMA_VERSION = "venue-membership-evidence/1"


class MembershipSource(StrEnum):
    """Which enumeration path returned a member.

    ``BOTH`` is expected near the cutoff and is not an error: it is what the
    live-then-historical ordering is designed to produce instead of a loss.
    """

    LIVE = "LIVE"
    HISTORICAL = "HISTORICAL"
    BOTH = "BOTH"
    NESTED_EVENT_ONLY = "NESTED_EVENT_ONLY"
    """Returned by the event response but by neither paginated tier. Unexpected,
    and recorded as an anomaly rather than quietly folded into the union."""


class MembershipPathStatus(StrEnum):
    """Whether one enumeration path ran to exhaustion."""

    EXHAUSTED = "EXHAUSTED"
    """Followed cursors until the venue returned none."""

    IDENTITY_UNUSABLE = "IDENTITY_UNUSABLE"
    """Pages were returned, but at least one carried a record whose membership
    identity could not be established. The walk completed; its answer cannot be
    trusted as a member list."""

    TRUNCATED_BY_LIMIT = "TRUNCATED_BY_LIMIT"
    """Stopped at a local page cap. The path may have more members."""

    FAILED = "FAILED"
    NOT_ATTEMPTED = "NOT_ATTEMPTED"

    @property
    def is_exhausted(self) -> bool:
        return self is MembershipPathStatus.EXHAUSTED


class MembershipAnomaly(StrEnum):
    """Something that makes the enumeration less trustworthy than it looks."""

    DATA_QUALITY_ANOMALY = "DATA_QUALITY_ANOMALY"
    """A field the venue sent is unusable, but not one membership depends on.

    Archived markets carry negative top-of-book sizes (A-49). Those values make
    a market unfit for executable book work; they say nothing about whether the
    ticker belongs to the event. The market stays in the enumeration, the bad
    field is excluded from semantic reasoning, and this records that it
    happened."""

    MEMBERSHIP_IDENTITY_UNUSABLE = "MEMBERSHIP_IDENTITY_UNUSABLE"
    """A field membership *does* depend on -- ticker, event_ticker -- was
    missing or contradictory. Nothing is guessed; the evidence becomes
    incomplete."""

    CUTOFF_MOVED_DURING_ENUMERATION = "CUTOFF_MOVED_DURING_ENUMERATION"
    """The live/historical boundary advanced while we were enumerating, so the
    two tiers were read against different boundaries."""

    DUPLICATE_ACROSS_TIERS = "DUPLICATE_ACROSS_TIERS"
    CONFLICTING_PAYLOADS = "CONFLICTING_PAYLOADS"
    """The same ticker came back from both tiers with materially different
    settlement facts. Nothing is merged; the conflict is reported."""

    NESTED_MEMBER_MISSING_FROM_TIERS = "NESTED_MEMBER_MISSING_FROM_TIERS"
    LIVE_MEMBER_SETTLED_BEFORE_CUTOFF = "LIVE_MEMBER_SETTLED_BEFORE_CUTOFF"
    """A live-tier market claims a settlement time older than the cutoff, which
    the documented partition says should not happen."""

    HISTORICAL_MEMBER_SETTLED_AFTER_CUTOFF = "HISTORICAL_MEMBER_SETTLED_AFTER_CUTOFF"
    HISTORICAL_MEMBER_UNSETTLED = "HISTORICAL_MEMBER_UNSETTLED"
    WRONG_EVENT_TICKER_RETURNED = "WRONG_EVENT_TICKER_RETURNED"
    PROVISIONAL_MEMBER_PRESENT = "PROVISIONAL_MEMBER_PRESENT"
    """At least one member may be removed by the venue later, so this snapshot
    is not a stable record of what the event will appear to contain."""


@dataclass(frozen=True, slots=True)
class PageTrace:
    """Proof that one paginated path was actually walked to its end."""

    path: str
    query: Mapping[str, str]
    pages: int
    returned: int
    cursors: tuple[str, ...]
    """Each cursor the venue handed back, in order. The final page returns none;
    a trailing cursor means the walk stopped early."""

    response_hashes: tuple[str, ...]
    status: MembershipPathStatus
    error: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "query", dict(sorted(self.query.items())))

    def describe(self) -> str:
        detail = f" ({self.error})" if self.error else ""
        return (
            f"{self.path} {dict(self.query)}: {self.returned} market(s) over "
            f"{self.pages} page(s) [{self.status.value}]{detail}"
        )


@dataclass(frozen=True, slots=True)
class MembershipMember:
    """One market the venue listed under the event, with the facts that matter.

    Deliberately narrow. Prices and volumes change every second and would make
    a membership fingerprint churn without any change to membership; what is
    kept is identity, lifecycle, settlement outcome and the **documented
    structured strike fields**, which are the only machine-readable basis a
    partition argument may rest on.
    """

    ticker: str
    source: MembershipSource
    event_ticker: str | None = None
    data_quality_anomalies: Mapping[str, str] = field(default_factory=dict)
    """Fields the venue sent that could not be read, kept verbatim.

    Present so a reader can see exactly what was unusable rather than inferring
    it from an absence. None of these participate in
    :meth:`component_values`."""

    status: str | None = None
    market_type: str | None = None
    result: str | None = None
    settlement_value: str | None = None
    settled_ts: datetime | None = None
    created_time: datetime | None = None
    close_time: datetime | None = None
    is_provisional: bool | None = None
    strike_type: str | None = None
    floor_strike: str | None = None
    cap_strike: str | None = None
    functional_strike: str | None = None
    custom_strike: Mapping[str, str] | None = None
    primary_participant_key: str | None = None
    title: str | None = None
    yes_sub_title: str | None = None

    def component_values(self) -> dict[str, Any]:
        """The material half: what makes this a different membership snapshot.

        ``source`` is excluded. Which tier answered is a fact about when we
        asked relative to the cutoff, not about what the event contains, and
        including it would make an identical membership re-fingerprint every
        time the cutoff swept past a member.
        """

        def value(raw: object) -> object:
            return ABSENT if raw is None else raw

        return {
            f"member.{self.ticker}.event_ticker": value(self.event_ticker),
            f"member.{self.ticker}.status": value(self.status),
            f"member.{self.ticker}.market_type": value(self.market_type),
            f"member.{self.ticker}.result": value(self.result),
            f"member.{self.ticker}.settlement_value": value(self.settlement_value),
            f"member.{self.ticker}.strike_type": value(self.strike_type),
            f"member.{self.ticker}.floor_strike": value(self.floor_strike),
            f"member.{self.ticker}.cap_strike": value(self.cap_strike),
            f"member.{self.ticker}.functional_strike": value(self.functional_strike),
            f"member.{self.ticker}.custom_strike": (
                ABSENT if self.custom_strike is None else dict(sorted(self.custom_strike.items()))
            ),
            f"member.{self.ticker}.is_provisional": value(self.is_provisional),
        }

    def audit_values(self) -> dict[str, Any]:
        """Recorded, reviewable, and deliberately **not** fingerprinted."""
        return {
            "source": self.source.value,
            "title": self.title,
            "yes_sub_title": self.yes_sub_title,
            "primary_participant_key": self.primary_participant_key,
            "settled_ts": self.settled_ts.isoformat() if self.settled_ts else None,
            "created_time": self.created_time.isoformat() if self.created_time else None,
            "close_time": self.close_time.isoformat() if self.close_time else None,
            "data_quality_anomalies": dict(self.data_quality_anomalies),
        }


@dataclass(frozen=True, slots=True)
class CombinedVenueMembershipEvidence:
    """What enumeration observed, under which boundary, having walked what.

    Named for the evidence rather than the conclusion. It does **not** assert
    that membership is complete; it asserts which paths were exhausted, and
    leaves the completeness judgement to a reviewer who can read the caveats.
    """

    snapshot_id: str
    event_ticker: str
    observed_at: datetime
    """When the enumeration ran."""

    knowledge_at: datetime
    """When this evidence entered our possession. Equal to ``observed_at`` for a
    live capture, and deliberately separate so a bundle assembled later cannot
    backdate what was known."""

    schema_version: str
    cutoff_before: datetime | None
    cutoff_after: datetime | None
    """The boundary read before and after enumeration. Two different values mean
    the tiers were queried against two different partitions."""

    live_trace: PageTrace
    historical_trace: PageTrace
    nested_members: tuple[str, ...]
    members: tuple[MembershipMember, ...]
    event_status: str | None = None
    event_last_updated_ts: datetime | None = None
    event_mutually_exclusive: bool | None = None
    anomalies: tuple[MembershipAnomaly, ...] = ()
    anomaly_detail: tuple[str, ...] = ()
    capture_errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("observed_at", "knowledge_at"):
            object.__setattr__(self, name, ensure_utc(getattr(self, name)))
        object.__setattr__(self, "members", tuple(sorted(self.members, key=lambda m: m.ticker)))
        object.__setattr__(self, "nested_members", tuple(sorted(set(self.nested_members))))

    # -- views -------------------------------------------------------------

    @property
    def member_tickers(self) -> tuple[str, ...]:
        return tuple(member.ticker for member in self.members)

    def by_source(self, source: MembershipSource) -> tuple[str, ...]:
        return tuple(m.ticker for m in self.members if m.source is source)

    @property
    def nested_omissions(self) -> tuple[str, ...]:
        """Union members the nested event response did not return.

        The documented cause is the historical cutoff, and measuring it is how
        A-46 stops being a guess. Reading only the event response yields exactly
        this many silent omissions.
        """
        return tuple(t for t in self.member_tickers if t not in set(self.nested_members))

    @property
    def latest_member_created_time(self) -> datetime | None:
        stamps = [m.created_time for m in self.members if m.created_time is not None]
        return max(stamps) if stamps else None

    @property
    def both_paths_exhausted(self) -> bool:
        return self.live_trace.status.is_exhausted and self.historical_trace.status.is_exhausted

    @property
    def cutoff_stable(self) -> bool:
        return self.cutoff_before is not None and self.cutoff_before == self.cutoff_after

    @property
    def is_event_open(self) -> bool:
        """Whether the venue may still add markets to this event.

        An open event has no final membership, so evidence about it expires by
        construction rather than by policy.
        """
        return any(
            m.status in {"initialized", "inactive", "active", "closed"} for m in self.members
        )

    # -- the claim, stated no more strongly than the evidence allows -------

    def caveats(self) -> tuple[str, ...]:
        """Every reason this evidence falls short of proving completeness."""
        notes: list[str] = []
        if not self.live_trace.status.is_exhausted:
            notes.append(f"live path {self.live_trace.status.value}")
        if not self.historical_trace.status.is_exhausted:
            notes.append(f"historical path {self.historical_trace.status.value}")
        if not self.cutoff_stable:
            notes.append(
                f"historical cutoff moved during enumeration "
                f"({self.cutoff_before} -> {self.cutoff_after}); the two tiers were "
                "read against different boundaries"
            )
        if self.cutoff_before is None:
            notes.append("historical cutoff could not be read, so the partition is unverified")
        provisional = [m.ticker for m in self.members if m.is_provisional]
        if provisional:
            notes.append(
                f"{len(provisional)} provisional member(s) the venue may remove after "
                f"determination: {', '.join(provisional[:5])}"
            )
        if self.is_event_open:
            notes.append(
                "the event still has unsettled markets, so the venue may create more; "
                "this membership is point-in-time and not final"
            )
        if self.anomaly_detail:
            notes.extend(self.anomaly_detail)
        # Always last, and always present: the structural limit no amount of
        # successful enumeration removes.
        notes.append(
            "the documentation guarantees where an archived market can be found, not "
            "that live + historical is every market the event ever had; a removed "
            "provisional market appears in neither tier"
        )
        return tuple(notes)

    def statement(self) -> str:
        """The strongest sentence this evidence supports. Never 'complete'."""
        if not self.both_paths_exhausted:
            return (
                f"{self.event_ticker}: enumeration did not exhaust both paths "
                f"({self.live_trace.status.value} live, "
                f"{self.historical_trace.status.value} historical); membership is "
                "partially observed and nothing may be concluded from its size."
            )
        qualifier = "" if self.cutoff_stable else ", though the cutoff moved mid-enumeration"
        return (
            f"{self.event_ticker}: under the documented live/historical partition at "
            f"cutoff {self.cutoff_before}, both paginated paths were exhausted{qualifier} "
            f"and returned {len(self.members)} distinct market(s). This is the observed "
            "venue membership at capture time; it is not a proof that no other market "
            "ever belonged to the event, and it says nothing about outcome "
            "exhaustiveness."
        )

    # -- fingerprint -------------------------------------------------------

    def component_values(self) -> dict[str, Any]:
        """The material inputs: who is in, and what each one's semantics are.

        Page counts, cursors and which tier answered are excluded. They are
        provenance -- worth recording, and they change for reasons that have
        nothing to do with membership.
        """
        components: dict[str, Any] = {
            "event_ticker": self.event_ticker,
            "schema_version": self.schema_version,
            "members": list(self.member_tickers),
            "event_mutually_exclusive": (
                ABSENT if self.event_mutually_exclusive is None else self.event_mutually_exclusive
            ),
        }
        for member in self.members:
            components.update(member.component_values())
        return components

    def fingerprint(self) -> SettlementEvidenceFingerprint:
        return SettlementEvidenceFingerprint.over(
            self.component_values(), schema_version=self.schema_version
        )

    def audit_payload(self) -> dict[str, Any]:
        """Everything recorded, fingerprinted or not, for a human to read."""
        return {
            "snapshot_id": self.snapshot_id,
            "event_ticker": self.event_ticker,
            "observed_at": self.observed_at.isoformat(),
            "knowledge_at": self.knowledge_at.isoformat(),
            "schema_version": self.schema_version,
            "cutoff_before": self.cutoff_before.isoformat() if self.cutoff_before else None,
            "cutoff_after": self.cutoff_after.isoformat() if self.cutoff_after else None,
            "cutoff_stable": self.cutoff_stable,
            "live": self.live_trace.describe(),
            "historical": self.historical_trace.describe(),
            "live_query": dict(self.live_trace.query),
            "historical_query": dict(self.historical_trace.query),
            "live_response_hashes": list(self.live_trace.response_hashes),
            "historical_response_hashes": list(self.historical_trace.response_hashes),
            "counts": {
                "live_only": len(self.by_source(MembershipSource.LIVE)),
                "historical_only": len(self.by_source(MembershipSource.HISTORICAL)),
                "both": len(self.by_source(MembershipSource.BOTH)),
                "nested_only": len(self.by_source(MembershipSource.NESTED_EVENT_ONLY)),
                "union": len(self.members),
                "nested": len(self.nested_members),
                "nested_omissions": len(self.nested_omissions),
            },
            "nested_omissions": list(self.nested_omissions),
            "members": {m.ticker: m.audit_values() for m in self.members},
            "event_status": self.event_status,
            "event_mutually_exclusive": self.event_mutually_exclusive,
            "anomalies": [a.value for a in self.anomalies],
            "caveats": list(self.caveats()),
            "statement": self.statement(),
            "fingerprint": self.fingerprint().digest,
            "capture_errors": list(self.capture_errors),
        }

    def describe(self) -> str:
        return (
            f"{self.event_ticker} {len(self.members)} member(s) "
            f"(live {len(self.by_source(MembershipSource.LIVE))}, "
            f"historical {len(self.by_source(MembershipSource.HISTORICAL))}, "
            f"both {len(self.by_source(MembershipSource.BOTH))}), "
            f"{len(self.nested_omissions)} omitted by nested event, "
            f"{len(self.anomalies)} anomaly(ies)"
        )


def membership_snapshot_id(
    *, event_ticker: str, fingerprint: SettlementEvidenceFingerprint, observed_at: datetime
) -> str:
    payload = f"{event_ticker}\x1e{fingerprint.digest}\x1e{ensure_utc(observed_at).isoformat()}"
    return hashlib.sha256(payload.encode()).hexdigest()
