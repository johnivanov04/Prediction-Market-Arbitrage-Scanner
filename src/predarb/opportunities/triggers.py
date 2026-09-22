"""When a detector should re-evaluate. Shared by live scanning and replay.

Equivalence between live and replay is only meaningful if both evaluate at the
same moments. A replay that swept every millisecond while live scanned only on
book changes would produce a different set of decisions and could not honestly
claim to reproduce live behaviour -- even if every individual evaluation agreed.

So the policy is one object, consumed by both paths, rather than a rule written
twice.

Context changes are triggers too
--------------------------------
A book that has not moved can still become newly evaluable: a fee change takes
effect, a certificate is issued, evidence drifts and a certificate goes stale.
Those transitions change the answer, so they re-trigger the affected subjects.
Omitting them would make a live scanner miss the moment an opportunity became
provable, and a replay would faithfully reproduce the miss.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "ScanTrigger",
    "ScanTriggerPolicy",
    "TriggerReason",
]


class TriggerReason(StrEnum):
    """Why an evaluation was scheduled."""

    BOOK_UPDATED = "BOOK_UPDATED"
    BOOK_BECAME_VALID = "BOOK_BECAME_VALID"
    BOOK_BECAME_INVALID = "BOOK_BECAME_INVALID"
    FEE_CONTEXT_CHANGED = "FEE_CONTEXT_CHANGED"
    CERTIFICATE_AVAILABLE = "CERTIFICATE_AVAILABLE"
    CERTIFICATE_STALE = "CERTIFICATE_STALE"
    RELATION_CERTIFICATE_AVAILABLE = "RELATION_CERTIFICATE_AVAILABLE"
    RELATION_CERTIFICATE_STALE = "RELATION_CERTIFICATE_STALE"


@dataclass(frozen=True, slots=True)
class ScanTrigger:
    """One scheduled evaluation, and the observation that caused it."""

    reason: TriggerReason
    market_tickers: tuple[str, ...]
    trigger_ordinal: int
    """The observation that caused this. Recorded so an audit can point at the
    exact input that produced a decision."""

    def describe(self) -> str:
        return f"#{self.trigger_ordinal} {self.reason.value} {list(self.market_tickers)}"


@dataclass(frozen=True, slots=True)
class ScanTriggerPolicy:
    """Which subjects to re-evaluate when something changes.

    Deliberately conservative: a redundant evaluation costs time, a missed one
    costs a finding. Baskets re-evaluate on *any* member's book change, because
    a basket's economics depend on every leg.
    """

    evaluate_binary_complement: bool = True
    evaluate_no_basket: bool = True

    def on_book_update(
        self, ticker: str, ordinal: int, *, became_valid: bool = False
    ) -> ScanTrigger:
        return ScanTrigger(
            reason=(
                TriggerReason.BOOK_BECAME_VALID if became_valid else TriggerReason.BOOK_UPDATED
            ),
            market_tickers=(ticker,),
            trigger_ordinal=ordinal,
        )

    def on_book_invalidated(self, tickers: Sequence[str], ordinal: int) -> ScanTrigger:
        """A disconnect invalidates books; the subjects must be re-judged.

        Not merely dropped: a market that *was* eligible and is now blocked is a
        state change a report should show, not a gap in the record.
        """
        return ScanTrigger(
            reason=TriggerReason.BOOK_BECAME_INVALID,
            market_tickers=tuple(sorted(tickers)),
            trigger_ordinal=ordinal,
        )

    def on_context_change(
        self, reason: TriggerReason, tickers: Sequence[str], ordinal: int
    ) -> ScanTrigger:
        return ScanTrigger(
            reason=reason,
            market_tickers=tuple(sorted(tickers)),
            trigger_ordinal=ordinal,
        )

    def affected_baskets(self, ticker: str, baskets: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
        """Baskets containing this market, by event ticker."""
        return tuple(sorted(event for event, members in baskets.items() if ticker in members))
