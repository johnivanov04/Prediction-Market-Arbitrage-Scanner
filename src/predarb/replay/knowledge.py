"""Point-in-time resolution of everything a detector needs.

One rule, applied uniformly: **a decision at horizon H may use a fact only if
that fact was observed at or before H.** Then, and only then, valid-time
semantics decide whether it applies at H's instant.

The mode has a name, and the name matters
-----------------------------------------
This is ``AS_KNOWN_AT_TIME``: what the system would have been *justified in
concluding* using only what it had. It is deliberately not "objective historical
truth". A later research mode could apply corrections learned afterwards --
restated metadata, a fee record that arrived late, evidence drift discovered
next week -- and that is a different and equally legitimate analysis. Mixing
them would produce a backtest whose results nobody can interpret.

Missing knowledge is a result
-----------------------------
When the horizon contains nothing applicable, the resolver returns ``None`` and
the completeness report records it. It never reaches forward for a later
observation to fill the gap, because a gap filled from the future is exactly
the leak this module exists to prevent.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from predarb.domain.fees import ResolvedFeeConfiguration
from predarb.replay.observation import (
    KnowledgeHorizon,
    Observation,
    ObservationKind,
    ObservationStream,
    ObservedVersion,
    VersionHistory,
)
from predarb.semantics.fingerprint import SettlementEvidenceFingerprint

__all__ = [
    "KnowledgeBase",
    "ReplayMode",
    "ResolvedContext",
    "relation_identity",
]


class ReplayMode(StrEnum):
    """Which question a replay is answering."""

    AS_KNOWN_AT_TIME = "AS_KNOWN_AT_TIME"
    """What could the system have concluded from what it had then?

    The only mode implemented. Anything observed after the horizon is invisible,
    however true it later turned out to be."""


@dataclass(frozen=True, slots=True)
class ResolvedContext:
    """Everything resolvable at one horizon, plus what was missing."""

    horizon: KnowledgeHorizon
    market_metadata: Mapping[str, Any]
    fee_configuration: Mapping[str, ResolvedFeeConfiguration]
    settlement_certificates: Mapping[str, Any]
    relation_certificates: Mapping[str, Any]
    settlement_fingerprints: Mapping[str, SettlementEvidenceFingerprint]
    relation_fingerprints: Mapping[str, SettlementEvidenceFingerprint]
    missing: tuple[str, ...] = ()
    scheduled_not_yet_effective: tuple[str, ...] = ()
    """Facts we knew about whose effective time is still ahead of the horizon.

    Knowable but not applicable -- a Monday observation of a Wednesday change is
    real knowledge on Tuesday, and using it on Tuesday would still be wrong.
    Surfaced so a report can distinguish "we did not know" from "we knew and
    correctly did not apply it yet"."""

    def describe(self) -> str:
        return (
            f"{self.horizon.describe()}: "
            f"{len(self.market_metadata)} instruments, "
            f"{len(self.fee_configuration)} fee configs, "
            f"{len(self.settlement_certificates)} settlement certs, "
            f"{len(self.relation_certificates)} relation certs"
            + (f", missing: {', '.join(self.missing)}" if self.missing else "")
        )


def relation_identity(event_ticker: str, claim: str, members: Sequence[str]) -> str:
    """``event|CLAIM|A,B,C`` -- the full identity of a relation certificate.

    All three parts are load-bearing.

    **Event** scopes it. **Claim** separates AT_MOST_ONE from AT_LEAST_ONE,
    which forbid opposite terminal states. **Member set** is what the reviewer
    actually answered questions about: a certificate reviewed over
    ``{A, B, C}`` is not a certificate about ``{A, B, C, D}``, and both may
    legitimately exist for one event at the same time.

    Keying on ``event|claim`` alone would let the second shadow the first, so a
    lookup for the reviewed set would return a different set's certificate --
    and the detector would then block a valid basket because the certificate it
    was handed covers something else. Fail-closed, but wrong.

    Members are canonicalised, so the same set supplied in any order resolves
    identically. Duplicates are collapsed rather than raising: an index must not
    lose a whole stream because one payload repeated a ticker.
    """
    canonical = ",".join(sorted({str(m) for m in members}))
    return f"{event_ticker}|{claim}|{canonical}"


def _relation_key(payload: Mapping[str, Any]) -> str:
    """The identity of one recorded relation certificate.

    A payload without a ``claim`` predates Step 11's second claim and is
    therefore AT_MOST_ONE. A payload without ``selected_members`` keys to the
    empty set, which no real lookup matches -- fail-closed, and visible as an
    unresolvable certificate rather than as a wrong one.
    """
    event = str(payload.get("event_ticker", ""))
    if not event:
        return ""
    return relation_identity(
        event,
        str(payload.get("claim", "AT_MOST_ONE")),
        payload.get("selected_members") or (),
    )


@dataclass
class KnowledgeBase:
    """Bitemporal index over an observation stream.

    Built once per replay and queried per horizon. Construction walks the stream
    in capture order and records each fact's observation ordinal; nothing here
    consults a wall clock or a network.
    """

    mode: ReplayMode = ReplayMode.AS_KNOWN_AT_TIME
    market_metadata: dict[str, VersionHistory[Any]] = field(default_factory=dict)
    event_metadata: dict[str, VersionHistory[Any]] = field(default_factory=dict)
    series_metadata: dict[str, VersionHistory[Any]] = field(default_factory=dict)
    fee_records: dict[str, VersionHistory[Any]] = field(default_factory=dict)
    settlement_evidence: dict[str, VersionHistory[Any]] = field(default_factory=dict)
    settlement_certificates: dict[str, VersionHistory[Any]] = field(default_factory=dict)
    relation_evidence: dict[str, VersionHistory[Any]] = field(default_factory=dict)
    relation_certificates: dict[str, VersionHistory[Any]] = field(default_factory=dict)

    # Explicit "we checked this source" records. Their presence is what turns an
    # absent certificate from "unknown" into "known absent".
    settlement_snapshots: dict[str, VersionHistory[Any]] = field(default_factory=dict)
    relation_snapshots: dict[str, VersionHistory[Any]] = field(default_factory=dict)
    fee_snapshots: dict[str, VersionHistory[Any]] = field(default_factory=dict)
    metadata_snapshots: dict[str, VersionHistory[Any]] = field(default_factory=dict)
    observation_counts: list[tuple[int, str]] = field(default_factory=list)

    @classmethod
    def from_stream(cls, stream: ObservationStream) -> KnowledgeBase:
        base = cls()
        for observation in stream:
            base.ingest(observation)
        return base

    def ingest(self, observation: Observation) -> None:
        """Index one observation under its own ordinal. Never re-dates it."""
        # The key is the subject the record *answers for*, which is not always
        # the scope it came from. A fee configuration inherited from a series
        # answers for each market in it, so a capture names the market in
        # ``subject_ticker`` and keeps the real origin in ``scope_ticker``.
        handlers: dict[ObservationKind, tuple[dict[str, VersionHistory[Any]], tuple[str, ...]]] = {
            ObservationKind.MARKET_METADATA: (self.market_metadata, ("ticker",)),
            ObservationKind.EVENT_METADATA: (self.event_metadata, ("event_ticker",)),
            ObservationKind.SERIES_METADATA: (self.series_metadata, ("series_ticker",)),
            ObservationKind.FEE_OBSERVATION: (
                self.fee_records,
                ("subject_ticker", "scope_ticker"),
            ),
            ObservationKind.SETTLEMENT_EVIDENCE: (self.settlement_evidence, ("market_ticker",)),
            ObservationKind.SETTLEMENT_CERTIFICATE: (
                self.settlement_certificates,
                ("market_ticker",),
            ),
            ObservationKind.RELATION_EVIDENCE: (self.relation_evidence, ("event_ticker",)),
            # Keyed by event *and claim*: one event can carry both an
            # AT_MOST_ONE and an AT_LEAST_ONE certificate, and they forbid
            # opposite states. A shared key would let a lookup for one return
            # the other.
            ObservationKind.RELATION_CERTIFICATE: (
                self.relation_certificates,
                ("__relation_key__",),
            ),
        }
        self.observation_counts.append((observation.ordinal, observation.kind.value))

        if observation.kind.is_knowledge_snapshot:
            self._ingest_snapshot(observation)
            return

        entry = handlers.get(observation.kind)
        if entry is None:
            return
        store, key_fields = entry
        if key_fields == ("__relation_key__",):
            key = _relation_key(observation.payload)
        else:
            key = next(
                (str(observation.payload[f]) for f in key_fields if observation.payload.get(f)),
                "",
            )
        if not key:
            return
        effective_from = observation.payload.get("effective_from")
        version: ObservedVersion[Any] = ObservedVersion(
            value=observation.payload,
            observed_at=observation.observed_at,
            observed_ordinal=observation.ordinal,
            effective_from=(datetime.fromisoformat(effective_from) if effective_from else None),
            source=observation.source,
            version_id=observation.content_hash[:16],
        )
        history = store.get(key, VersionHistory(key=key))
        store[key] = history.with_version(version)

    def _ingest_snapshot(self, observation: Observation) -> None:
        """Index a "we queried this source" record, one entry per subject.

        A snapshot covering several markets is indexed under each, so a lookup
        for one market can tell that the source *was* checked even when it
        returned nothing for that market specifically.
        """
        store = {
            ObservationKind.SETTLEMENT_REGISTRY_SNAPSHOT: self.settlement_snapshots,
            ObservationKind.RELATION_REGISTRY_SNAPSHOT: self.relation_snapshots,
            ObservationKind.FEE_KNOWLEDGE_SNAPSHOT: self.fee_snapshots,
            ObservationKind.METADATA_SNAPSHOT: self.metadata_snapshots,
        }[observation.kind]
        subjects = observation.payload.get("subjects") or []
        keys = [str(s) for s in subjects] or ["*"]
        for key in keys:
            version: ObservedVersion[Any] = ObservedVersion(
                value=observation.payload,
                observed_at=observation.observed_at,
                observed_ordinal=observation.ordinal,
                source=observation.source,
                version_id=observation.content_hash[:16],
            )
            history = store.get(key, VersionHistory(key=key))
            store[key] = history.with_version(version)

    def _snapshot(
        self, store: Mapping[str, VersionHistory[Any]], key: str, horizon: KnowledgeHorizon
    ) -> dict[str, Any] | None:
        """A snapshot for this subject, or the wildcard one that covered it."""
        for candidate in (key, "*"):
            history = store.get(candidate)
            if history is None:
                continue
            version = history.resolve(horizon)
            if version is not None:
                return dict(version.value)
        return None

    def settlement_snapshot_at(
        self, ticker: str, horizon: KnowledgeHorizon
    ) -> dict[str, Any] | None:
        return self._snapshot(self.settlement_snapshots, ticker, horizon)

    def relation_snapshot_at(
        self, event_ticker: str, horizon: KnowledgeHorizon
    ) -> dict[str, Any] | None:
        return self._snapshot(self.relation_snapshots, event_ticker, horizon)

    def fee_snapshot_at(self, ticker: str, horizon: KnowledgeHorizon) -> dict[str, Any] | None:
        return self._snapshot(self.fee_snapshots, ticker, horizon)

    def metadata_snapshot_at(self, ticker: str, horizon: KnowledgeHorizon) -> dict[str, Any] | None:
        return self._snapshot(self.metadata_snapshots, ticker, horizon)

    # -- freshness ---------------------------------------------------------

    def _last_observed(
        self,
        stores: Sequence[Mapping[str, VersionHistory[Any]]],
        key: str,
        horizon: KnowledgeHorizon,
    ) -> datetime | None:
        """When this knowledge source was last successfully observed by the horizon."""
        latest: datetime | None = None
        for store in stores:
            for candidate in (key, "*"):
                history = store.get(candidate)
                if history is None:
                    continue
                for version in history.known_at(horizon):
                    if latest is None or version.observed_at > latest:
                        latest = version.observed_at
        return latest

    def last_metadata_observation(self, ticker: str, horizon: KnowledgeHorizon) -> datetime | None:
        return self._last_observed([self.metadata_snapshots, self.market_metadata], ticker, horizon)

    def last_fee_observation(self, ticker: str, horizon: KnowledgeHorizon) -> datetime | None:
        return self._last_observed([self.fee_snapshots, self.fee_records], ticker, horizon)

    def last_settlement_observation(
        self, ticker: str, horizon: KnowledgeHorizon
    ) -> datetime | None:
        return self._last_observed(
            [self.settlement_snapshots, self.settlement_certificates], ticker, horizon
        )

    def last_relation_observation(
        self, event_ticker: str, horizon: KnowledgeHorizon
    ) -> datetime | None:
        return self._last_observed(
            [self.relation_snapshots, self.relation_certificates], event_ticker, horizon
        )

    def counts_at(self, horizon: KnowledgeHorizon) -> dict[str, int]:
        counts: dict[str, int] = {}
        for ordinal, kind in self.observation_counts:
            if ordinal <= horizon.ordinal:
                counts[kind] = counts.get(kind, 0) + 1
        return counts

    # -- resolution --------------------------------------------------------

    def _resolve(
        self, store: Mapping[str, VersionHistory[Any]], key: str, horizon: KnowledgeHorizon
    ) -> ObservedVersion[Any] | None:
        history = store.get(key)
        return None if history is None else history.resolve(horizon)

    def market_at(self, ticker: str, horizon: KnowledgeHorizon) -> dict[str, Any] | None:
        version = self._resolve(self.market_metadata, ticker, horizon)
        return None if version is None else dict(version.value)

    def fee_record_at(self, scope: str, horizon: KnowledgeHorizon) -> dict[str, Any] | None:
        """The fee configuration in force, from records we had by the horizon.

        A record whose ``effective_from`` precedes the horizon but which we only
        observed afterwards is invisible here. That is the whole point: a
        backfilled schedule must not reprice a decision that predates our
        knowledge of it.
        """
        version = self._resolve(self.fee_records, scope, horizon)
        return None if version is None else dict(version.value)

    def settlement_certificate_at(
        self, ticker: str, horizon: KnowledgeHorizon
    ) -> dict[str, Any] | None:
        """A certificate issued after the horizon does not exist yet."""
        version = self._resolve(self.settlement_certificates, ticker, horizon)
        return None if version is None else dict(version.value)

    def relation_certificate_at(
        self,
        event_ticker: str,
        horizon: KnowledgeHorizon,
        claim: str = "AT_MOST_ONE",
        members: Sequence[str] = (),
    ) -> dict[str, Any] | None:
        """The certificate for this event, claim **and exact member set**.

        Every part of the identity is in the key, not applied as a filter
        afterwards, and there is deliberately no fallback to a superset or
        subset. A certificate reviewed over ``{A, B, C}`` answers only about
        ``{A, B, C}``; returning it for ``{A, B}`` would broaden a human's
        answer to a question they were never asked.

        Point-in-time *within* that identity: several historical versions of the
        same set resolve to whichever was known at the horizon.
        """
        version = self._resolve(
            self.relation_certificates,
            relation_identity(event_ticker, claim, members),
            horizon,
        )
        return None if version is None else dict(version.value)

    def relation_certificates_for_event(
        self, event_ticker: str, horizon: KnowledgeHorizon
    ) -> tuple[str, ...]:
        """Every relation identity known for this event, for reporting.

        Exposed so a report can say that a certificate exists over a *different*
        member set -- useful to a reader, and never mistaken by the resolver for
        a match.
        """
        prefix = f"{event_ticker}|"
        return tuple(
            sorted(
                key
                for key, history in self.relation_certificates.items()
                if key.startswith(prefix) and history.resolve(horizon) is not None
            )
        )

    def settlement_evidence_at(
        self, ticker: str, horizon: KnowledgeHorizon
    ) -> dict[str, Any] | None:
        """Evidence as last captured *by* the horizon.

        Drift discovered later must not retroactively invalidate a certificate
        that was current at the time. A certificate issued at 10:00 remains
        current at 10:30 even if 11:00 evidence shows the rules changed; at
        11:01 it is stale. Both are correct answers to different questions.
        """
        version = self._resolve(self.settlement_evidence, ticker, horizon)
        return None if version is None else dict(version.value)

    def relation_evidence_at(
        self, event_ticker: str, horizon: KnowledgeHorizon
    ) -> dict[str, Any] | None:
        version = self._resolve(self.relation_evidence, event_ticker, horizon)
        return None if version is None else dict(version.value)

    def scheduled_not_yet_effective(self, horizon: KnowledgeHorizon) -> tuple[str, ...]:
        """Known-but-not-yet-applicable fee records, for reporting."""
        pending: list[str] = []
        for key, history in self.fee_records.items():
            for version in history.scheduled_but_not_yet_effective(horizon):
                effective = version.effective_from
                pending.append(f"fee:{key} effective {effective.isoformat() if effective else '?'}")
        return tuple(sorted(pending))

    def known_keys(self, horizon: KnowledgeHorizon) -> dict[str, tuple[str, ...]]:
        """What the horizon can see at all, by category. Used by reports."""

        def keys(store: Mapping[str, VersionHistory[Any]]) -> tuple[str, ...]:
            return tuple(sorted(k for k, h in store.items() if h.resolve(horizon) is not None))

        return {
            "markets": keys(self.market_metadata),
            "events": keys(self.event_metadata),
            "series": keys(self.series_metadata),
            "fees": keys(self.fee_records),
            "settlement_evidence": keys(self.settlement_evidence),
            "settlement_certificates": keys(self.settlement_certificates),
            "relation_evidence": keys(self.relation_evidence),
            "relation_certificates": keys(self.relation_certificates),
        }
