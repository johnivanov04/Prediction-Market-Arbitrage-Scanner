"""Repositories over the Phase-1 catalogue. All I/O lives here.

Nothing in `domain/`, `detectors/` or `books/` imports this module, and nothing
here imports them for anything but types. Detectors stay pure functions of
resolved immutable inputs; storage sits at the orchestration boundary, which is
what lets live and replay call the same economic code.

Idempotency
-----------
Every write is keyed on a **stable content-derived identity**, so a retry after
an uncertain result either proves the row already exists or inserts it exactly
once. A collector that reconnected mid-write must not produce a second copy of
the same observation: replay over a duplicated stream reconstructs a different
book, and the duplicate is invisible in any count that reads the table.

The safety ordering
-------------------
Source evidence is persisted **before** the state it feeds is mutated, and
derived decisions are persisted **after**. That ordering is not arbitrary:

* if derived persistence fails, the observations remain and replay regenerates
  the decision -- nothing is lost;
* if **source** persistence fails, there is nothing to regenerate from, so the
  collector must stop rather than continue scanning on evidence it cannot
  prove it had.

Reversing it would let the system hold decisions whose inputs were never
durably captured, which is an audit trail that cannot be checked.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, and_, func, select

from predarb.clock import ensure_utc
from predarb.replay.decision import DecisionRecord
from predarb.replay.observation import Observation, ObservationKind, ObservationStream
from predarb.storage.schema import (
    connection_epochs,
    decisions,
    health_events,
    observations,
    replay_verifications,
    sessions,
)

__all__ = [
    "Catalogue",
    "SourcePersistenceError",
    "StoredSession",
    "decision_identity",
    "observation_identity",
    "persist_decisions",
    "persist_observations",
    "session_identity",
]


class SourcePersistenceError(RuntimeError):
    """Source evidence could not be durably stored.

    Fatal by design. A collector that continued would be scanning on evidence
    it cannot prove it had, and no replay could ever check the result.
    """


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1e".join(parts).encode()).hexdigest()


def session_identity(*, detector_plan: dict[str, Any], started_at: datetime) -> str:
    """Stable over (plan, start instant), so a retried startup resumes one session."""
    return _digest(_canonical(detector_plan), ensure_utc(started_at).isoformat())


def observation_identity(session_id: str, observation: Observation) -> str:
    """Stable over session, ordinal, kind and payload content.

    Includes the payload hash rather than only the ordinal: two different
    payloads at one ordinal is a bug worth failing on, not a silent overwrite.
    """
    return _digest(
        session_id,
        str(observation.ordinal),
        observation.kind.value,
        observation.content_hash,
    )


def decision_identity(session_id: str, record: DecisionRecord) -> str:
    """Stable over session, decision ordinal and the economic fingerprint."""
    return _digest(session_id, str(record.decision_ordinal), record.fingerprint.digest)


@dataclass(frozen=True, slots=True)
class StoredSession:
    session_id: str
    started_at: datetime
    detector_plan: dict[str, Any]
    status: str
    raw_journal_path: str | None = None


@dataclass
class Catalogue:
    """Repository over one open transactional connection.

    Deliberately not a long-lived object holding its own engine: the caller
    controls the transaction boundary, because what belongs in one transaction
    is an orchestration decision this layer cannot make.
    """

    connection: Connection

    # -- sessions ----------------------------------------------------------

    def open_session(
        self,
        *,
        detector_plan: dict[str, Any],
        started_at: datetime,
        venue: str = "KALSHI",
        environment: str = "prod",
        schema_version: str,
        raw_journal_path: str | None = None,
        notes: str = "",
    ) -> StoredSession:
        """Create or resume a session. Idempotent on (plan, start instant)."""
        session_id = session_identity(detector_plan=detector_plan, started_at=started_at)
        existing = self.connection.execute(
            select(sessions).where(sessions.c.session_id == session_id)
        ).first()
        if existing is not None:
            return StoredSession(
                session_id=session_id,
                started_at=ensure_utc(existing.started_at),
                detector_plan=json.loads(existing.detector_plan),
                status=existing.status,
                raw_journal_path=existing.raw_journal_path,
            )

        plan_text = _canonical(detector_plan)
        self.connection.execute(
            sessions.insert().values(
                session_id=session_id,
                started_at=ensure_utc(started_at),
                ended_at=None,
                venue=venue,
                environment=environment,
                status="RUNNING",
                detector_plan=plan_text,
                detector_plan_sha256=hashlib.sha256(plan_text.encode()).hexdigest(),
                schema_version=schema_version,
                raw_journal_path=raw_journal_path,
                notes=notes,
            )
        )
        return StoredSession(
            session_id=session_id,
            started_at=ensure_utc(started_at),
            detector_plan=detector_plan,
            status="RUNNING",
            raw_journal_path=raw_journal_path,
        )

    def close_session(self, session_id: str, *, status: str, at: datetime) -> None:
        self.connection.execute(
            sessions.update()
            .where(sessions.c.session_id == session_id)
            .values(ended_at=ensure_utc(at), status=status)
        )

    def get_session(self, session_id: str) -> StoredSession | None:
        row = self.connection.execute(
            select(sessions).where(sessions.c.session_id == session_id)
        ).first()
        if row is None:
            return None
        return StoredSession(
            session_id=row.session_id,
            started_at=ensure_utc(row.started_at),
            detector_plan=json.loads(row.detector_plan),
            status=row.status,
            raw_journal_path=row.raw_journal_path,
        )

    def resolve_session_id(self, prefix: str) -> str | None:
        """Expand an abbreviated session id, or ``None`` if it is not unique.

        The listing prints a 12-character prefix, so operators type prefixes.
        An ambiguous one returns ``None`` rather than picking a row: guessing
        which session an operator meant is how the wrong one gets verified.
        """
        if self.get_session(prefix) is not None:
            return prefix
        matches = [
            row.session_id for row in self.list_sessions() if row.session_id.startswith(prefix)
        ]
        return matches[0] if len(matches) == 1 else None

    def list_sessions(self) -> list[StoredSession]:
        rows = self.connection.execute(
            select(sessions).order_by(sessions.c.started_at.desc())
        ).all()
        return [
            StoredSession(
                session_id=r.session_id,
                started_at=ensure_utc(r.started_at),
                detector_plan=json.loads(r.detector_plan),
                status=r.status,
                raw_journal_path=r.raw_journal_path,
            )
            for r in rows
        ]

    # -- connection lifecycle ---------------------------------------------

    def record_epoch_opened(self, session_id: str, epoch: int, at: datetime) -> None:
        existing = self.connection.execute(
            select(connection_epochs.c.epoch).where(
                and_(
                    connection_epochs.c.session_id == session_id,
                    connection_epochs.c.epoch == epoch,
                )
            )
        ).first()
        if existing is not None:
            return
        self.connection.execute(
            connection_epochs.insert().values(
                session_id=session_id, epoch=epoch, opened_at=ensure_utc(at)
            )
        )

    def record_epoch_closed(self, session_id: str, epoch: int, at: datetime, reason: str) -> None:
        self.connection.execute(
            connection_epochs.update()
            .where(
                and_(
                    connection_epochs.c.session_id == session_id,
                    connection_epochs.c.epoch == epoch,
                )
            )
            .values(closed_at=ensure_utc(at), close_reason=reason)
        )

    # -- source evidence ---------------------------------------------------

    def persist_observation(
        self,
        session_id: str,
        observation: Observation,
        *,
        raw_journal_ref: str | None = None,
        inline_payload: bool = True,
    ) -> bool:
        """Durably record one source observation. Returns whether it was new.

        A frame's bytes already live in the append-only journal, so the payload
        is referenced rather than copied when a reference is available --
        storing multi-kilobyte frames twice doubles the disk cost of the same
        evidence. Everything else is inlined, because a context snapshot is
        small and is what a later reader actually needs to see.
        """
        identity = observation_identity(session_id, observation)
        existing = self.connection.execute(
            select(observations.c.observation_id).where(observations.c.observation_id == identity)
        ).first()
        if existing is not None:
            # A retry after an uncertain write. The row is proven present
            # rather than inserted again.
            return False

        payload_text: str | None = None
        if inline_payload and raw_journal_ref is None:
            payload_text = _canonical(observation.payload)

        self.connection.execute(
            observations.insert().values(
                session_id=session_id,
                ordinal=observation.ordinal,
                observation_id=identity,
                observed_at=observation.observed_at,
                kind=observation.kind.value,
                subject=_subject_of(observation),
                source=observation.source,
                connection_epoch=observation.connection_epoch,
                payload_sha256=observation.content_hash,
                payload_json=payload_text,
                raw_journal_ref=raw_journal_ref,
            )
        )
        return True

    def observation_count(self, session_id: str) -> int:
        return int(
            self.connection.execute(
                select(func.count())
                .select_from(observations)
                .where(observations.c.session_id == session_id)
            ).scalar_one()
        )

    def max_observation_ordinal(self, session_id: str) -> int | None:
        """The highest durably recorded ordinal. Where a restart resumes from."""
        return self.connection.execute(
            select(func.max(observations.c.ordinal)).where(observations.c.session_id == session_id)
        ).scalar()

    def load_observations(self, session_id: str) -> ObservationStream:
        """Rebuild the source stream, in capture order, for replay.

        This is what makes derived data regenerable: the stream that produced a
        decision can be reconstructed exactly and fed back through the same
        coordinator.
        """
        rows = self.connection.execute(
            select(observations)
            .where(observations.c.session_id == session_id)
            .order_by(observations.c.ordinal)
        ).all()
        return ObservationStream.from_iterable(
            Observation(
                ordinal=row.ordinal,
                observed_at=ensure_utc(row.observed_at),
                kind=ObservationKind(row.kind),
                payload=json.loads(row.payload_json) if row.payload_json else {},
                source=row.source,
                connection_epoch=row.connection_epoch,
            )
            for row in rows
        )

    # -- derived decisions -------------------------------------------------

    def persist_decision(self, session_id: str, record: DecisionRecord) -> bool:
        """Durably record one decision. Returns whether it was new.

        Derived, and therefore regenerable: a failure here is recoverable by
        replay and must not stop collection, unlike a source-evidence failure.
        """
        identity = decision_identity(session_id, record)
        existing = self.connection.execute(
            select(decisions.c.decision_id).where(decisions.c.decision_id == identity)
        ).first()
        if existing is not None:
            return False

        self.connection.execute(
            decisions.insert().values(
                session_id=session_id,
                decision_ordinal=record.decision_ordinal,
                decision_id=identity,
                detector=record.detector.value,
                trigger_ordinal=record.trigger_ordinal,
                trigger_reason=record.trigger_reason,
                decision_time=record.decision_time,
                subjects=_canonical(list(record.subjects)),
                quantity=record.quantity,
                detector_did_run=record.detector_did_run,
                classification=record.classification,
                blocking_reason=record.blocking_reason,
                missing_knowledge=_canonical(list(record.missing_knowledge)),
                context_ids=_canonical(dict(record.context_ids)),
                completeness=_canonical(record.completeness.as_dict()),
                fingerprint_version=record.fingerprint.version,
                fingerprint_digest=record.fingerprint.digest,
                fingerprint_components=_canonical(dict(record.fingerprint.components)),
                warnings=_canonical(list(record.warnings)),
            )
        )
        return True

    def decision_count(self, session_id: str) -> int:
        return int(
            self.connection.execute(
                select(func.count())
                .select_from(decisions)
                .where(decisions.c.session_id == session_id)
            ).scalar_one()
        )

    def decision_fingerprints(self, session_id: str) -> dict[int, str]:
        """Decision ordinal -> fingerprint digest, for verification."""
        rows = self.connection.execute(
            select(decisions.c.decision_ordinal, decisions.c.fingerprint_digest)
            .where(decisions.c.session_id == session_id)
            .order_by(decisions.c.decision_ordinal)
        ).all()
        return {int(row[0]): str(row[1]) for row in rows}

    def classification_counts(self, session_id: str) -> dict[str, int]:
        rows = self.connection.execute(
            select(decisions.c.classification, func.count())
            .where(decisions.c.session_id == session_id)
            .group_by(decisions.c.classification)
        ).all()
        return {str(row[0]): int(row[1]) for row in rows}

    def detector_counts(self, session_id: str) -> dict[str, int]:
        rows = self.connection.execute(
            select(decisions.c.detector, func.count())
            .where(decisions.c.session_id == session_id)
            .group_by(decisions.c.detector)
        ).all()
        return {str(row[0]): int(row[1]) for row in rows}

    # -- health ------------------------------------------------------------

    def record_health(
        self,
        session_id: str,
        *,
        kind: str,
        severity: str,
        at: datetime,
        detail: str = "",
        counters: dict[str, Any] | None = None,
    ) -> None:
        payload = _canonical(counters or {})
        identity = _digest(session_id, kind, ensure_utc(at).isoformat(), detail, payload)
        existing = self.connection.execute(
            select(health_events.c.event_id).where(health_events.c.event_id == identity)
        ).first()
        if existing is not None:
            return
        self.connection.execute(
            health_events.insert().values(
                session_id=session_id,
                event_id=identity,
                at=ensure_utc(at),
                kind=kind,
                severity=severity,
                detail=detail,
                counters=payload,
            )
        )

    def health_counts(self, session_id: str) -> dict[str, int]:
        rows = self.connection.execute(
            select(health_events.c.kind, func.count())
            .where(health_events.c.session_id == session_id)
            .group_by(health_events.c.kind)
        ).all()
        return {str(row[0]): int(row[1]) for row in rows}

    # -- replay audit linkage ---------------------------------------------

    def record_verification(
        self,
        session_id: str,
        *,
        verified_at: datetime,
        observations_replayed: int,
        decisions_persisted: int,
        decisions_regenerated: int,
        fingerprint_matches: int,
        missing_persisted: int,
        extra_persisted: int,
        fingerprint_mismatches: int,
        network_calls: int,
        detail: dict[str, Any] | None = None,
    ) -> str:
        identity = _digest(
            session_id,
            ensure_utc(verified_at).isoformat(),
            str(observations_replayed),
            str(fingerprint_matches),
        )
        self.connection.execute(
            replay_verifications.insert().values(
                verification_id=identity,
                session_id=session_id,
                verified_at=ensure_utc(verified_at),
                observations_replayed=observations_replayed,
                decisions_persisted=decisions_persisted,
                decisions_regenerated=decisions_regenerated,
                fingerprint_matches=fingerprint_matches,
                missing_persisted=missing_persisted,
                extra_persisted=extra_persisted,
                fingerprint_mismatches=fingerprint_mismatches,
                network_calls=network_calls,
                detail=_canonical(detail or {}),
            )
        )
        return identity


def _subject_of(observation: Observation) -> str | None:
    """The single ticker or event a row is about, for indexed lookup.

    Best-effort and non-authoritative: an observation covering several subjects
    keeps them in its payload, and nothing resolves semantics from this column.
    """
    payload = observation.payload
    for key in ("market_ticker", "ticker", "event_ticker", "scope_ticker", "subject_ticker"):
        value = payload.get(key)
        if value:
            return str(value)
    subjects = payload.get("subjects")
    if isinstance(subjects, list) and len(subjects) == 1:
        return str(subjects[0])
    return None


def persist_observations(
    catalogue: Catalogue,
    session_id: str,
    stream: Iterable[Observation],
    *,
    refs: dict[int, str] | None = None,
) -> tuple[int, int]:
    """Bulk-persist source evidence. Returns ``(inserted, already_present)``."""
    inserted = duplicates = 0
    for observation in stream:
        ref = (refs or {}).get(observation.ordinal)
        if catalogue.persist_observation(session_id, observation, raw_journal_ref=ref):
            inserted += 1
        else:
            duplicates += 1
    return inserted, duplicates


def persist_decisions(
    catalogue: Catalogue, session_id: str, records: Sequence[DecisionRecord]
) -> tuple[int, int]:
    """Bulk-persist derived decisions. Returns ``(inserted, already_present)``."""
    inserted = duplicates = 0
    for record in records:
        if catalogue.persist_decision(session_id, record):
            inserted += 1
        else:
            duplicates += 1
    return inserted, duplicates
