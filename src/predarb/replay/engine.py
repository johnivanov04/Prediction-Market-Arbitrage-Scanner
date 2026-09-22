"""The replay coordinator's driver.

Replay substitutes **data sources and the clock**. It does not substitute
economic logic: books, execution curves, fee bounds, payoff enumeration and both
detector families come from the same code the live path runs, through the same
:class:`~predarb.replay.coordinator.EvaluationCoordinator`.

There is deliberately no ``ReplayOrderBookReconstructor``, no
``detect_binary_replay``, no ``detect_no_basket_replay`` and no replay-only
pricing shortcut. If replay needed its own version of any of those, "live and
replay agree" would only mean two copies happened to agree today.

Offline by construction: this module imports no HTTP or WebSocket client and no
venue client. Missing historical context is reported as missing, never fetched,
because backfilling from today's API is the leak the whole design exists to
prevent.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from predarb.books.reconstruction import OrderBookReconstructor
from predarb.clock import FrozenClock
from predarb.opportunities.models import Classification
from predarb.replay.bundle import ReplayBundle
from predarb.replay.completeness import (
    BASKET_DIMENSIONS,
    BINARY_COMPLEMENT_DIMENSIONS,
    ReplayDataCompleteness,
)
from predarb.replay.context import ContextProvider
from predarb.replay.coordinator import EvaluationCoordinator
from predarb.replay.decision import MISSING_KNOWLEDGE, DecisionRecord, DetectorKind
from predarb.replay.journal import ReplayFrameJournal
from predarb.replay.knowledge import ReplayMode
from predarb.replay.observation import KnowledgeHorizon, ObservationStream
from predarb.replay.plan import DetectorPlan

__all__ = ["ReplayEngine", "ReplayResult"]

_EPOCH = datetime.fromtimestamp(0, tz=UTC)


@dataclass(frozen=True, slots=True)
class ReplayResult:
    """The outcome of one replay run. Derived output, never source evidence."""

    bundle_id: str
    stream_hash: str
    mode: ReplayMode
    plan: DetectorPlan
    observations_processed: int
    triggers: int
    decisions: tuple[DecisionRecord, ...]
    completeness: ReplayDataCompleteness
    integrity: str
    network_calls: int = 0
    warnings: tuple[str, ...] = ()

    @property
    def digest(self) -> str:
        """Deterministic digest over every economic decision in the run."""
        payload = "\x1d".join(d.fingerprint.digest for d in self.decisions)
        return hashlib.sha256(payload.encode()).hexdigest()

    @property
    def classification_counts(self) -> Mapping[str, int]:
        counts: dict[str, int] = {}
        for decision in self.decisions:
            counts[decision.classification] = counts.get(decision.classification, 0) + 1
        return dict(sorted(counts.items()))

    @property
    def missing_knowledge_count(self) -> int:
        return sum(1 for d in self.decisions if d.classification == MISSING_KNOWLEDGE)

    @property
    def detector_run_count(self) -> int:
        return sum(1 for d in self.decisions if d.detector_did_run)

    def decisions_for(self, detector: DetectorKind) -> tuple[DecisionRecord, ...]:
        return tuple(d for d in self.decisions if d.detector is detector)

    def absence_statement(self, scope: str, detector: DetectorKind) -> str:
        """The strongest sentence this run entitles a report to print.

        Three separate things could each make "we found nothing" wrong, so each
        is checked before it is said: something *was* found; nothing was ever
        evaluated; or evaluation happened but some of it ran on knowledge the
        bundle did not contain. Only when all three are excluded does the run
        support a clean negative -- which is a valid and expected result, and
        the one most easily faked by an under-captured dataset.
        """
        required = (
            BINARY_COMPLEMENT_DIMENSIONS
            if detector is DetectorKind.BINARY_COMPLEMENT
            else BASKET_DIMENSIONS
        )
        decisions = self.decisions_for(detector)
        if not decisions:
            return (
                f"No {detector.value} evaluation ran within {scope}; this run says "
                "nothing about whether a candidate existed."
            )
        proven = sum(
            1
            for d in decisions
            if d.classification == Classification.PROVEN_CONTRACTUAL_ARBITRAGE.value
        )
        if proven:
            return (
                f"{proven} of {len(decisions)} {detector.value} decision(s) within {scope} "
                "proved a contractual arbitrage, subject to execution race risk."
            )
        gaps = sum(1 for d in decisions if d.classification == MISSING_KNOWLEDGE)
        blocking = self.completeness.blocking_dimensions_for(required)
        if blocking or gaps:
            detail = ", ".join(
                f"{dimension.value}={self.completeness.status(dimension).value}"
                for dimension in blocking
            )
            if gaps:
                gap_text = f"{gaps} decision(s) lacked point-in-time knowledge"
                detail = f"{detail}; {gap_text}" if detail else gap_text
            return (
                f"No {detector.value} candidate observed within {scope}, but replay "
                f"inputs are incomplete for a definitive claim ({detail})."
            )
        return (
            f"No proven {detector.value} candidate existed within {scope}, across "
            f"{len(decisions)} decision(s) on complete inputs."
        )

    def describe(self) -> str:
        return (
            f"{self.bundle_id[:12]} {self.observations_processed} observations, "
            f"{self.triggers} triggers, {len(self.decisions)} decisions "
            f"({self.detector_run_count} ran), digest {self.digest[:12]}"
        )


@dataclass
class ReplayEngine:
    """Drives recorded observations through the shared coordinator."""

    plan: DetectorPlan
    provider: ContextProvider
    mode: ReplayMode = ReplayMode.AS_KNOWN_AT_TIME

    def run(self, bundle: ReplayBundle) -> ReplayResult:
        """Replay a verified bundle. Fails closed on a dataset it cannot trust."""
        if not bundle.permits_replay:
            raise ValueError(
                f"{bundle.root}: bundle integrity is "
                f"{bundle.integrity.integrity.value}; refusing to replay a dataset "
                "that cannot speak for the period it claims"
            )
        return self.run_stream(
            bundle.stream,
            bundle_id=bundle.manifest.bundle_id,
            stream_hash=bundle.manifest.stream_hash,
            integrity=bundle.integrity.integrity.value,
        )

    def run_stream(
        self,
        stream: ObservationStream,
        *,
        bundle_id: str = "(in-memory)",
        stream_hash: str = "",
        integrity: str = "VERIFIED",
    ) -> ReplayResult:
        """Replay an observation stream directly.

        Used by the equivalence harness, which drives this identical coordinator
        from an in-memory stream so the comparison is of orchestration, not of
        two separately written loops.
        """
        coordinator = EvaluationCoordinator(
            plan=self.plan,
            provider=self.provider,
            # The production reconstructor with a write-nothing journal: the
            # frames are already recorded, and re-journalling would duplicate
            # the very evidence being replayed.
            reconstructor=OrderBookReconstructor(journal=ReplayFrameJournal()),  # type: ignore[arg-type]
        )
        decisions: list[DecisionRecord] = []
        triggers = 0
        clock = FrozenClock(stream.observations[0].observed_at if stream.observations else _EPOCH)
        final_completeness = ReplayDataCompleteness()

        for observation in stream:
            clock = clock.set_to(observation.observed_at)
            horizon = KnowledgeHorizon(ordinal=observation.ordinal, at=observation.observed_at)
            trigger = coordinator.apply(observation)
            if trigger is None:
                continue
            triggers += 1
            decisions.extend(coordinator.evaluate(trigger, horizon))

        if stream.observations:
            last = stream.observations[-1]
            final_completeness = self.provider.completeness(
                KnowledgeHorizon(ordinal=last.ordinal, at=last.observed_at),
                self.plan,
                self.plan.monitored_markets,
            )

        warnings: list[str] = []
        missing = sum(1 for d in decisions if d.classification == MISSING_KNOWLEDGE)
        if missing:
            warnings.append(
                f"{missing} decision(s) lacked point-in-time context and were recorded "
                "as missing knowledge rather than backfilled from later observations"
            )
        return ReplayResult(
            bundle_id=bundle_id,
            stream_hash=stream_hash or stream.content_hash(),
            mode=self.mode,
            plan=self.plan,
            observations_processed=len(stream),
            triggers=triggers,
            decisions=tuple(decisions),
            completeness=final_completeness,
            integrity=integrity,
            network_calls=0,
            warnings=tuple(warnings),
        )
