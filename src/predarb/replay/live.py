"""Live-side processing: the same coordinator, fed as history happens.

:class:`~predarb.replay.engine.ReplayEngine` and :class:`LiveSession` are
mirror images, and the difference between them is the entire no-lookahead
argument.

Both drive the same :class:`~predarb.replay.coordinator.EvaluationCoordinator`
over the same observations, through the same detectors. What differs is the
knowledge base behind the context provider:

    LiveSession    fed one observation at a time -- it *cannot* see forward
    ReplayEngine   indexed from the finished stream -- it *can*, and must not

So when a replay reproduces a live run's decisions exactly, that is not two
copies of a loop agreeing. It is a run that held every future observation
declining to use any of them. A horizon-filtering bug would show up as a
divergence, because only one of the two had the opportunity to cheat.

This class exists so that comparison is never between two separately written
orchestrations. There is one loop; this is the side of it that runs forward.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from predarb.books.reconstruction import OrderBookReconstructor
from predarb.replay.context import ContextProvider
from predarb.replay.coordinator import EvaluationCoordinator
from predarb.replay.decision import DecisionRecord
from predarb.replay.journal import ReplayFrameJournal
from predarb.replay.knowledge import KnowledgeBase
from predarb.replay.observation import KnowledgeHorizon, Observation
from predarb.replay.plan import DetectorPlan

__all__ = ["LiveSession"]


@dataclass
class LiveSession:
    """Processes observations in arrival order, holding only what has arrived."""

    plan: DetectorPlan
    knowledge: KnowledgeBase
    coordinator: EvaluationCoordinator
    decisions: list[DecisionRecord] = field(default_factory=list)
    triggers: int = 0

    @classmethod
    def over(
        cls,
        plan: DetectorPlan,
        knowledge: KnowledgeBase,
        provider: ContextProvider,
        *,
        reconstructor: OrderBookReconstructor | None = None,
    ) -> LiveSession:
        """Build a session over a knowledge base the caller still holds.

        The caller keeps the reference deliberately: a venue context provider is
        constructed against the *same mutable* knowledge base, so what the
        provider can resolve grows exactly as observations are fed in.
        """
        return cls(
            plan=plan,
            knowledge=knowledge,
            coordinator=EvaluationCoordinator(
                plan=plan,
                provider=provider,
                reconstructor=reconstructor or OrderBookReconstructor(journal=ReplayFrameJournal()),  # type: ignore[arg-type]
            ),
        )

    @property
    def reconstructor(self) -> OrderBookReconstructor:
        return self.coordinator.reconstructor

    def observe(self, observation: Observation) -> tuple[DecisionRecord, ...]:
        """Ingest one observation and return the decisions it produced."""
        self.knowledge.ingest(observation)
        trigger = self.coordinator.apply(observation)
        if trigger is None:
            return ()
        self.triggers += 1
        produced = self.coordinator.evaluate(
            trigger,
            KnowledgeHorizon(ordinal=observation.ordinal, at=observation.observed_at),
        )
        self.decisions.extend(produced)
        return tuple(produced)
