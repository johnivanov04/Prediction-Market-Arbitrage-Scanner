"""What a session was monitoring, and how fresh its context had to be.

A detector evaluates only what the session plan says it was watching. Replay must
not infer basket groups from event metadata at replay time: that would let a
replay evaluate a group the live system never considered, and the two would
diverge for reasons that have nothing to do with the economics.

So the plan is captured with the session and travels in the bundle.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from predarb.domain.money import Quantity
from predarb.replay.completeness import CompletenessDimension

__all__ = [
    "DEFAULT_REFRESH_POLICY",
    "BasketPlan",
    "ContextRefreshPolicy",
    "DetectorPlan",
]


@dataclass(frozen=True, slots=True)
class ContextRefreshPolicy:
    """How stale a knowledge source may be before it stops counting as known.

    These are **our** safety margins, not venue guarantees. Nothing in the API
    promises that a market's notional, a series' fee configuration or an event's
    structure cannot change mid-session, so treating an old observation as still
    current past this window would be an assumption dressed as knowledge.
    """

    market_metadata: timedelta = timedelta(minutes=30)
    fee_knowledge: timedelta = timedelta(minutes=30)
    settlement_knowledge: timedelta = timedelta(minutes=15)
    relation_knowledge: timedelta = timedelta(minutes=15)

    def max_age_for(self, dimension: CompletenessDimension) -> timedelta | None:
        return {
            CompletenessDimension.MARKET_METADATA: self.market_metadata,
            CompletenessDimension.FEE_KNOWLEDGE: self.fee_knowledge,
            CompletenessDimension.SETTLEMENT_KNOWLEDGE: self.settlement_knowledge,
            CompletenessDimension.RELATION_KNOWLEDGE: self.relation_knowledge,
        }.get(dimension)

    def to_payload(self) -> dict[str, float]:
        return {
            "market_metadata_s": self.market_metadata.total_seconds(),
            "fee_knowledge_s": self.fee_knowledge.total_seconds(),
            "settlement_knowledge_s": self.settlement_knowledge.total_seconds(),
            "relation_knowledge_s": self.relation_knowledge.total_seconds(),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> ContextRefreshPolicy:
        return cls(
            market_metadata=timedelta(seconds=float(payload["market_metadata_s"])),
            fee_knowledge=timedelta(seconds=float(payload["fee_knowledge_s"])),
            settlement_knowledge=timedelta(seconds=float(payload["settlement_knowledge_s"])),
            relation_knowledge=timedelta(seconds=float(payload["relation_knowledge_s"])),
        )


DEFAULT_REFRESH_POLICY = ContextRefreshPolicy()


@dataclass(frozen=True, slots=True)
class BasketPlan:
    """One AT_MOST_ONE group the session was monitoring.

    The member set is explicit and exact. A relation certificate covers exactly
    the set it was reviewed over, so a plan that named a different set would not
    match any certificate -- correctly.
    """

    event_ticker: str
    members: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "members", tuple(sorted(set(self.members))))

    def to_payload(self) -> dict[str, Any]:
        return {"event_ticker": self.event_ticker, "members": list(self.members)}


@dataclass(frozen=True, slots=True)
class DetectorPlan:
    """The immutable monitoring configuration for one session."""

    binary_complement_markets: tuple[str, ...] = ()
    baskets: tuple[BasketPlan, ...] = ()
    """AT_MOST_ONE groups, evaluated as NO baskets."""

    yes_baskets: tuple[BasketPlan, ...] = ()
    """AT_LEAST_ONE groups, evaluated as YES baskets.

    Separate from ``baskets`` rather than tagged, because the two claims need
    different certificates and produce different economics. One list with a flag
    would make "which claim does this group need" a runtime question on every
    lookup."""

    quantities: tuple[Quantity, ...] = field(default_factory=lambda: (Quantity.from_value("1.00"),))
    refresh_policy: ContextRefreshPolicy = DEFAULT_REFRESH_POLICY
    balance_precision: str = "unknown-conservative"
    """Which member-class assumption the fee bounds used. Recorded because it
    changes every fee upper bound, and a replay that assumed differently would
    produce different economics for identical books."""

    notes: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "binary_complement_markets", tuple(sorted(set(self.binary_complement_markets)))
        )

    @property
    def monitored_markets(self) -> tuple[str, ...]:
        members = {m for basket in (*self.baskets, *self.yes_baskets) for m in basket.members}
        return tuple(sorted(set(self.binary_complement_markets) | members))

    @property
    def all_baskets(self) -> tuple[BasketPlan, ...]:
        return (*self.baskets, *self.yes_baskets)

    def baskets_containing(self, ticker: str) -> tuple[BasketPlan, ...]:
        return tuple(basket for basket in self.baskets if ticker in basket.members)

    def yes_baskets_containing(self, ticker: str) -> tuple[BasketPlan, ...]:
        return tuple(basket for basket in self.yes_baskets if ticker in basket.members)

    def to_payload(self) -> dict[str, Any]:
        return {
            "binary_complement_markets": list(self.binary_complement_markets),
            "baskets": [basket.to_payload() for basket in self.baskets],
            "yes_baskets": [basket.to_payload() for basket in self.yes_baskets],
            "quantities": [q.to_str() for q in self.quantities],
            "refresh_policy": self.refresh_policy.to_payload(),
            "balance_precision": self.balance_precision,
            "notes": self.notes,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> DetectorPlan:
        return cls(
            binary_complement_markets=tuple(payload.get("binary_complement_markets", [])),
            baskets=tuple(
                BasketPlan(event_ticker=entry["event_ticker"], members=tuple(entry["members"]))
                for entry in payload.get("baskets", [])
            ),
            yes_baskets=tuple(
                BasketPlan(event_ticker=entry["event_ticker"], members=tuple(entry["members"]))
                for entry in payload.get("yes_baskets", [])
            ),
            quantities=tuple(Quantity.from_value(q) for q in payload.get("quantities", ["1.00"])),
            refresh_policy=(
                ContextRefreshPolicy.from_payload(payload["refresh_policy"])
                if "refresh_policy" in payload
                else DEFAULT_REFRESH_POLICY
            ),
            balance_precision=payload.get("balance_precision", "unknown-conservative"),
            notes=payload.get("notes", ""),
        )

    def describe(self) -> str:
        return (
            f"{len(self.binary_complement_markets)} complement market(s), "
            f"{len(self.baskets)} NO basket(s), "
            f"{len(self.yes_baskets)} YES basket(s), "
            f"quantities {[q.to_str() for q in self.quantities]}, "
            f"precision {self.balance_precision}"
        )
