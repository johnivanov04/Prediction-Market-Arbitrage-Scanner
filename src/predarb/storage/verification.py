"""Verify persisted decisions against what replay regenerates from source.

Persisted decisions are a convenience: they make a session queryable without
re-running anything. **Replay stays authoritative.** If the two disagree, the
replay is right by construction, because it is derived from the source evidence
the persisted row only claims to summarise.

So this compares, reports, and does not repair. Silently rewriting a mismatched
row would destroy the only evidence that something went wrong -- and a mismatch
is exactly the signal worth stopping for.

Offline: the replay half consults no network. That is asserted by the caller
blocking sockets, and counted here as ``network_calls`` so a report can state
it rather than imply it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import Engine

from predarb.replay.context import ContextProvider
from predarb.replay.decision import DecisionRecord
from predarb.replay.engine import ReplayEngine
from predarb.replay.knowledge import KnowledgeBase
from predarb.replay.plan import DetectorPlan
from predarb.storage.catalogue import Catalogue
from predarb.storage.engine import connect

__all__ = ["ProviderFactory", "VerificationReport", "verify_session_decisions"]

ProviderFactory = Callable[[KnowledgeBase, DetectorPlan], ContextProvider]
"""Builds the venue context resolver for a replay.

Injected rather than imported, so ``storage`` stays the leaf the architecture
says it is (`architecture.md` §2). It also keeps verification venue-agnostic:
nothing here knows it is checking Kalshi.
"""


@dataclass(frozen=True, slots=True)
class VerificationReport:
    """What replay found when checked against what was persisted."""

    session_id: str
    verified_at: datetime
    observations_replayed: int
    decisions_persisted: int
    decisions_regenerated: int
    fingerprint_matches: int
    missing_persisted: tuple[int, ...] = ()
    """Decisions replay produced that the catalogue does not hold. Usually a
    derived-write failure, which is recoverable and expected to be rare."""

    extra_persisted: tuple[int, ...] = ()
    """Decisions the catalogue holds that replay does not produce. More
    serious: it means a persisted claim has no source evidence behind it."""

    fingerprint_mismatches: tuple[tuple[int, str, str], ...] = ()
    """``(ordinal, persisted, regenerated)``. The economics changed."""

    network_calls: int = 0
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def exact_match(self) -> bool:
        return not (self.missing_persisted or self.extra_persisted or self.fingerprint_mismatches)

    def payload(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "verified_at": self.verified_at.isoformat(),
            "observations_replayed": self.observations_replayed,
            "decisions_persisted": self.decisions_persisted,
            "decisions_regenerated": self.decisions_regenerated,
            "fingerprint_matches": self.fingerprint_matches,
            "missing_persisted": list(self.missing_persisted),
            "extra_persisted": list(self.extra_persisted),
            "fingerprint_mismatches": [
                {"ordinal": o, "persisted": p, "regenerated": r}
                for o, p, r in self.fingerprint_mismatches
            ],
            "network_calls": self.network_calls,
            "exact_match": self.exact_match,
            "warnings": list(self.warnings),
        }

    def describe(self) -> str:
        if self.exact_match:
            return (
                f"{self.session_id[:12]}: {self.fingerprint_matches} decision(s) "
                f"regenerated from {self.observations_replayed} observation(s), "
                "every fingerprint identical"
            )
        return (
            f"{self.session_id[:12]}: {len(self.missing_persisted)} missing, "
            f"{len(self.extra_persisted)} extra, "
            f"{len(self.fingerprint_mismatches)} fingerprint mismatch(es)"
        )


def verify_session_decisions(
    engine: Engine,
    session_id: str,
    *,
    provider_factory: ProviderFactory,
    verified_at: datetime,
    record: bool = True,
) -> VerificationReport:
    """Replay a session's source observations and compare every decision."""
    with connect(engine) as connection:
        catalogue = Catalogue(connection)
        stored = catalogue.get_session(session_id)
        if stored is None:
            raise KeyError(f"no session {session_id!r} in the catalogue")
        stream = catalogue.load_observations(session_id)
        persisted = catalogue.decision_fingerprints(session_id)

    plan = DetectorPlan.from_payload(stored.detector_plan)
    engine_under_test = ReplayEngine(
        plan=plan, provider=provider_factory(KnowledgeBase.from_stream(stream), plan)
    )
    result = engine_under_test.run_stream(stream, bundle_id=session_id)
    regenerated: dict[int, DecisionRecord] = {
        record_.decision_ordinal: record_ for record_ in result.decisions
    }

    missing = tuple(sorted(set(regenerated) - set(persisted)))
    extra = tuple(sorted(set(persisted) - set(regenerated)))
    mismatches = tuple(
        (ordinal, persisted[ordinal], regenerated[ordinal].fingerprint.digest)
        for ordinal in sorted(set(persisted) & set(regenerated))
        if persisted[ordinal] != regenerated[ordinal].fingerprint.digest
    )
    matches = len(set(persisted) & set(regenerated)) - len(mismatches)

    report = VerificationReport(
        session_id=session_id,
        verified_at=verified_at,
        observations_replayed=len(stream),
        decisions_persisted=len(persisted),
        decisions_regenerated=len(regenerated),
        fingerprint_matches=matches,
        missing_persisted=missing,
        extra_persisted=extra,
        fingerprint_mismatches=mismatches,
        network_calls=result.network_calls,
        warnings=result.warnings,
    )

    if record:
        with connect(engine) as connection:
            Catalogue(connection).record_verification(
                session_id,
                verified_at=verified_at,
                observations_replayed=report.observations_replayed,
                decisions_persisted=report.decisions_persisted,
                decisions_regenerated=report.decisions_regenerated,
                fingerprint_matches=report.fingerprint_matches,
                missing_persisted=len(report.missing_persisted),
                extra_persisted=len(report.extra_persisted),
                fingerprint_mismatches=len(report.fingerprint_mismatches),
                network_calls=report.network_calls,
                detail=report.payload(),
            )
    return report
