"""The evaluation coordinator: shared orchestration for live and replay.

    observation arrives
    -> authoritative state and context update
    -> ScanTriggerPolicy decides what work is affected
    -> point-in-time context is resolved
    -> the production detector is invoked
    -> a DecisionRecord is emitted

Both paths run *this* loop. Only the source of observations differs: a live
session feeds it arriving frames, a replay feeds it recorded ones. If the
orchestration were written twice, "live and replay agree" would only mean the
two copies happened to agree today.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from predarb.books.execution import ExecutionContext
from predarb.books.reconstruction import OrderBookReconstructor
from predarb.books.state import BookIntegrity
from predarb.detectors.binary_complement import evaluate_quantity
from predarb.detectors.no_basket import BasketMember, evaluate_basket_quantity
from predarb.detectors.yes_basket import YesBasketMember, evaluate_yes_basket_quantity
from predarb.domain.money import Quantity
from predarb.opportunities.triggers import ScanTrigger, ScanTriggerPolicy, TriggerReason
from predarb.replay.completeness import ReplayDataCompleteness
from predarb.replay.context import ContextProvider
from predarb.replay.decision import MISSING_KNOWLEDGE, DecisionRecord, DetectorKind
from predarb.replay.fingerprint import (
    fingerprint_basket,
    fingerprint_binary_complement,
    fingerprint_known_absent,
    fingerprint_missing_knowledge,
    fingerprint_yes_basket,
)
from predarb.replay.observation import (
    KnowledgeHorizon,
    Observation,
    ObservationKind,
)
from predarb.replay.plan import BasketPlan, DetectorPlan
from predarb.semantics.relation import RelationClaim

__all__ = ["EvaluationCoordinator"]


@dataclass
class EvaluationCoordinator:
    """Turns observations into decisions. Pure orchestration, no I/O."""

    plan: DetectorPlan
    provider: ContextProvider
    reconstructor: OrderBookReconstructor
    trigger_policy: ScanTriggerPolicy = field(default_factory=ScanTriggerPolicy)
    live_markets: set[str] = field(default_factory=set)
    decision_ordinal: int = 0

    # -- transport ---------------------------------------------------------

    def apply(self, observation: Observation) -> ScanTrigger | None:
        """Feed one observation into authoritative state; return any trigger.

        Lifecycle is replayed rather than inferred: a close invalidates books
        exactly as it did live, and a stream of frames alone would keep a book
        alive through an outage the live system treated as fatal.
        """
        kind, payload = observation.kind, observation.payload

        if kind is ObservationKind.CONNECTION_OPENED:
            self.reconstructor.open_connection(observation.observed_at)
            return None
        if kind in {ObservationKind.CONNECTION_CLOSED, ObservationKind.CONNECTION_FAILED}:
            affected = tuple(sorted(self.live_markets))
            self.reconstructor.close_connection(
                observation.observed_at, reason=str(payload.get("reason", "closed"))
            )
            return (
                self.trigger_policy.on_book_invalidated(affected, observation.ordinal)
                if affected
                else None
            )
        if kind is ObservationKind.SUBSCRIBED:
            markets = {str(m) for m in payload.get("markets", [])}
            self.live_markets.update(markets)
            self.reconstructor.note_subscribed(
                sid=int(payload["sid"]),
                channel=str(payload.get("channel", "orderbook_delta")),
                markets=markets,
                at=observation.observed_at,
            )
            return None
        if kind is ObservationKind.FRAME_RECEIVED:
            self.reconstructor.handle_frame(str(payload["raw"]), observation.observed_at)
            ticker = payload.get("market_ticker")
            if not ticker:
                return None
            self.live_markets.add(str(ticker))
            return self.trigger_policy.on_book_update(str(ticker), observation.ordinal)

        return self._context_trigger(observation)

    def _context_trigger(self, observation: Observation) -> ScanTrigger | None:
        """A book that has not moved can still become newly evaluable."""
        reason = {
            ObservationKind.FEE_OBSERVATION: TriggerReason.FEE_CONTEXT_CHANGED,
            ObservationKind.FEE_KNOWLEDGE_SNAPSHOT: TriggerReason.FEE_CONTEXT_CHANGED,
            ObservationKind.SETTLEMENT_CERTIFICATE: TriggerReason.CERTIFICATE_AVAILABLE,
            ObservationKind.SETTLEMENT_REGISTRY_SNAPSHOT: (TriggerReason.CERTIFICATE_AVAILABLE),
            ObservationKind.SETTLEMENT_EVIDENCE: TriggerReason.CERTIFICATE_STALE,
            ObservationKind.RELATION_CERTIFICATE: (TriggerReason.RELATION_CERTIFICATE_AVAILABLE),
            ObservationKind.RELATION_REGISTRY_SNAPSHOT: (
                TriggerReason.RELATION_CERTIFICATE_AVAILABLE
            ),
            ObservationKind.RELATION_EVIDENCE: TriggerReason.RELATION_CERTIFICATE_STALE,
        }.get(observation.kind)
        if reason is None:
            return None
        declared = observation.payload.get("affects_markets") or (
            [observation.payload["market_ticker"]]
            if observation.payload.get("market_ticker")
            else []
        )
        tickers = tuple(str(t) for t in declared if str(t) in self.live_markets)
        if not tickers:
            return None
        return self.trigger_policy.on_context_change(reason, tickers, observation.ordinal)

    # -- evaluation --------------------------------------------------------

    def evaluate(self, trigger: ScanTrigger, horizon: KnowledgeHorizon) -> list[DecisionRecord]:
        """Every decision this trigger implies, across both detector families."""
        records: list[DecisionRecord] = []
        for ticker in trigger.market_tickers:
            if self.trigger_policy.evaluate_binary_complement and (
                ticker in self.plan.binary_complement_markets
            ):
                records.extend(self._binary_decisions(ticker, trigger, horizon))

        if not self.trigger_policy.evaluate_no_basket:
            return records
        # One trigger can touch several members of the same basket -- a
        # disconnect invalidates them all at once. The basket is still one
        # subject, so it is evaluated once; evaluating per member would emit
        # duplicate identical decisions and inflate every count that reads them.
        affected: dict[str, BasketPlan] = {}
        for ticker in trigger.market_tickers:
            for basket in self.plan.baskets_containing(ticker):
                affected.setdefault(basket.event_ticker, basket)
        for basket in affected.values():
            records.extend(self._basket_decisions(basket, trigger, horizon))

        yes_affected: dict[str, BasketPlan] = {}
        for ticker in trigger.market_tickers:
            for basket in self.plan.yes_baskets_containing(ticker):
                yes_affected.setdefault(basket.event_ticker, basket)
        for basket in yes_affected.values():
            records.extend(self._yes_basket_decisions(basket, trigger, horizon))
        return records

    def _next_ordinal(self) -> int:
        self.decision_ordinal += 1
        return self.decision_ordinal

    def _completeness(
        self, horizon: KnowledgeHorizon, subjects: Sequence[str]
    ) -> ReplayDataCompleteness:
        return self.provider.completeness(horizon, self.plan, subjects)

    def _blocked_record(
        self,
        *,
        detector: DetectorKind,
        trigger: ScanTrigger,
        horizon: KnowledgeHorizon,
        subjects: tuple[str, ...],
        quantity: Quantity | None,
        classification: str,
        reason: str,
        missing: tuple[str, ...],
        context_ids: dict[str, str | None],
        known_absent: tuple[str, ...] = (),
    ) -> DecisionRecord:
        quantity_text = quantity.to_str() if quantity is not None else None
        subject_text = ",".join(subjects)
        fingerprint = (
            fingerprint_missing_knowledge(
                detector=detector.value.lower(),
                market_ticker=subject_text,
                quantity=quantity_text or "",
                decision_time=horizon.at,
                missing=missing,
            )
            if classification == MISSING_KNOWLEDGE
            # A determinate conclusion reached without running a detector gets a
            # fingerprint over *what was found absent*, so two different states
            # of knowledge that happen to share a verdict do not share a digest.
            else fingerprint_known_absent(
                detector=detector.value.lower(),
                market_ticker=subject_text,
                quantity=quantity_text or "",
                decision_time=horizon.at,
                classification=classification,
                known_absent=known_absent or (reason,),
            )
        )
        return DecisionRecord(
            detector=detector,
            trigger_ordinal=trigger.trigger_ordinal,
            trigger_reason=trigger.reason.value,
            decision_ordinal=self._next_ordinal(),
            decision_time=horizon.at,
            subjects=subjects,
            detector_did_run=False,
            classification=classification,
            quantity=quantity_text,
            context_ids=context_ids,
            completeness=self._completeness(horizon, subjects),
            blocking_reason=reason,
            missing_knowledge=missing,
            fingerprint=fingerprint,
        )

    def _binary_decisions(
        self, ticker: str, trigger: ScanTrigger, horizon: KnowledgeHorizon
    ) -> list[DecisionRecord]:
        view = self.reconstructor.book(ticker)
        context = self.provider.binary_context(ticker, horizon)
        subjects = (ticker,)
        records: list[DecisionRecord] = []

        for quantity in self.plan.quantities:
            if view is None:
                records.append(
                    self._blocked_record(
                        detector=DetectorKind.BINARY_COMPLEMENT,
                        trigger=trigger,
                        horizon=horizon,
                        subjects=subjects,
                        quantity=quantity,
                        classification=MISSING_KNOWLEDGE,
                        reason=f"no book state for {ticker} at {horizon.describe()}",
                        missing=(f"book:{ticker}",),
                        context_ids=dict(context.context_ids),
                    )
                )
                continue
            if context.missing:
                records.append(
                    self._blocked_record(
                        detector=DetectorKind.BINARY_COMPLEMENT,
                        trigger=trigger,
                        horizon=horizon,
                        subjects=subjects,
                        quantity=quantity,
                        classification=MISSING_KNOWLEDGE,
                        reason=(
                            "no point-in-time knowledge of "
                            f"{', '.join(context.missing)}; refusing to backfill from "
                            "later observations"
                        ),
                        missing=context.missing,
                        context_ids=dict(context.context_ids),
                    )
                )
                continue
            if not context.can_run_detector:
                # Determinate without the detector: we checked and there is no
                # certificate, so the market is blocked on semantics. That is
                # knowledge, not a gap.
                records.append(
                    self._blocked_record(
                        detector=DetectorKind.BINARY_COMPLEMENT,
                        trigger=trigger,
                        horizon=horizon,
                        subjects=subjects,
                        quantity=quantity,
                        classification=context.blocked_classification,
                        reason=(
                            f"known absent: {', '.join(context.known_absent) or 'no certificate'}"
                        ),
                        missing=(),
                        known_absent=context.known_absent,
                        context_ids=dict(context.context_ids),
                    )
                )
                continue

            assert context.instrument is not None
            assert context.certificate is not None
            assert context.fee_quoter is not None
            result = evaluate_quantity(
                instrument=context.instrument,
                view=view,
                certificate=context.certificate,
                current_evidence_fingerprint=context.evidence_fingerprint,
                context=ExecutionContext(
                    current_connection_epoch=view.provenance.connection_epoch,
                    journal_healthy=self.reconstructor.journal_healthy,
                    connection_healthy=view.integrity is BookIntegrity.VALID,
                ),
                fee_quoter=context.fee_quoter,
                quantity=quantity,
                at=horizon.at,
            )
            records.append(
                DecisionRecord(
                    detector=DetectorKind.BINARY_COMPLEMENT,
                    trigger_ordinal=trigger.trigger_ordinal,
                    trigger_reason=trigger.reason.value,
                    decision_ordinal=self._next_ordinal(),
                    decision_time=horizon.at,
                    subjects=subjects,
                    detector_did_run=True,
                    classification=result.classification.value,
                    quantity=quantity.to_str(),
                    context_ids=dict(context.context_ids),
                    completeness=self._completeness(horizon, subjects),
                    blocking_reason=result.blocking_reason,
                    fingerprint=fingerprint_binary_complement(result, decision_time=horizon.at),
                    warnings=result.warnings,
                )
            )
        return records

    def _basket_decisions(
        self, basket: BasketPlan, trigger: ScanTrigger, horizon: KnowledgeHorizon
    ) -> list[DecisionRecord]:
        context = self.provider.basket_context(basket, horizon)
        subjects = basket.members
        records: list[DecisionRecord] = []

        views = {t: self.reconstructor.book(t) for t in basket.members}
        absent_books = tuple(sorted(t for t, v in views.items() if v is None))

        for quantity in self.plan.quantities:
            if absent_books:
                records.append(
                    self._blocked_record(
                        detector=DetectorKind.AT_MOST_ONE_BASKET,
                        trigger=trigger,
                        horizon=horizon,
                        subjects=subjects,
                        quantity=quantity,
                        classification=MISSING_KNOWLEDGE,
                        reason=f"no book state for {', '.join(absent_books)}",
                        missing=tuple(f"book:{t}" for t in absent_books),
                        context_ids=dict(context.context_ids),
                    )
                )
                continue
            if context.missing:
                records.append(
                    self._blocked_record(
                        detector=DetectorKind.AT_MOST_ONE_BASKET,
                        trigger=trigger,
                        horizon=horizon,
                        subjects=subjects,
                        quantity=quantity,
                        classification=MISSING_KNOWLEDGE,
                        reason=(f"no point-in-time knowledge of {', '.join(context.missing)}"),
                        missing=context.missing,
                        context_ids=dict(context.context_ids),
                    )
                )
                continue
            if not context.can_run_detector:
                records.append(
                    self._blocked_record(
                        detector=DetectorKind.AT_MOST_ONE_BASKET,
                        trigger=trigger,
                        horizon=horizon,
                        subjects=subjects,
                        quantity=quantity,
                        classification=context.blocked_classification,
                        reason=(
                            "known absent: "
                            f"{', '.join(context.known_absent) or 'no relation certificate'}"
                        ),
                        missing=(),
                        known_absent=context.known_absent,
                        context_ids=dict(context.context_ids),
                    )
                )
                continue

            assert context.relation_certificate is not None
            assert context.fee_quoter is not None
            members = [
                BasketMember(
                    instrument=context.members[t].instrument,  # type: ignore[arg-type]
                    view=views[t],  # type: ignore[arg-type]
                    certificate=context.members[t].certificate,  # type: ignore[arg-type]
                    current_settlement_fingerprint=context.members[t].evidence_fingerprint,
                )
                for t in basket.members
            ]
            first_view = views[basket.members[0]]
            assert first_view is not None
            result = evaluate_basket_quantity(
                relation=context.relation_certificate,
                relation_evidence_fingerprint=context.relation_fingerprint,
                members=members,
                context=ExecutionContext(
                    current_connection_epoch=first_view.provenance.connection_epoch,
                    journal_healthy=self.reconstructor.journal_healthy,
                ),
                fee_quoter=context.fee_quoter,
                quantity=quantity,
                at=horizon.at,
            )
            records.append(
                DecisionRecord(
                    detector=DetectorKind.AT_MOST_ONE_BASKET,
                    trigger_ordinal=trigger.trigger_ordinal,
                    trigger_reason=trigger.reason.value,
                    decision_ordinal=self._next_ordinal(),
                    decision_time=horizon.at,
                    subjects=subjects,
                    detector_did_run=True,
                    classification=result.classification.value,
                    quantity=quantity.to_str(),
                    context_ids=dict(context.context_ids),
                    completeness=self._completeness(horizon, subjects),
                    blocking_reason=result.blocking_reason,
                    fingerprint=fingerprint_basket(result, decision_time=horizon.at),
                    warnings=result.warnings,
                )
            )
        return records

    def _yes_basket_decisions(
        self, basket: BasketPlan, trigger: ScanTrigger, horizon: KnowledgeHorizon
    ) -> list[DecisionRecord]:
        """AT_LEAST_ONE BUY-YES, driven through the same production detector.

        Resolved for the AT_LEAST_ONE claim specifically: an AT_MOST_ONE
        certificate over the same members is a different guarantee and must not
        satisfy this lookup.
        """
        context = self.provider.basket_context(basket, horizon, RelationClaim.AT_LEAST_ONE)
        subjects = basket.members
        records: list[DecisionRecord] = []

        views = {t: self.reconstructor.book(t) for t in basket.members}
        absent_books = tuple(sorted(t for t, v in views.items() if v is None))

        for quantity in self.plan.quantities:
            if absent_books:
                records.append(
                    self._blocked_record(
                        detector=DetectorKind.AT_LEAST_ONE_BASKET,
                        trigger=trigger,
                        horizon=horizon,
                        subjects=subjects,
                        quantity=quantity,
                        classification=MISSING_KNOWLEDGE,
                        reason=f"no book state for {', '.join(absent_books)}",
                        missing=tuple(f"book:{t}" for t in absent_books),
                        context_ids=dict(context.context_ids),
                    )
                )
                continue
            if context.missing:
                records.append(
                    self._blocked_record(
                        detector=DetectorKind.AT_LEAST_ONE_BASKET,
                        trigger=trigger,
                        horizon=horizon,
                        subjects=subjects,
                        quantity=quantity,
                        classification=MISSING_KNOWLEDGE,
                        reason=f"no point-in-time knowledge of {', '.join(context.missing)}",
                        missing=context.missing,
                        context_ids=dict(context.context_ids),
                    )
                )
                continue
            if not context.can_run_detector:
                records.append(
                    self._blocked_record(
                        detector=DetectorKind.AT_LEAST_ONE_BASKET,
                        trigger=trigger,
                        horizon=horizon,
                        subjects=subjects,
                        quantity=quantity,
                        classification=context.blocked_classification,
                        reason=(
                            "known absent: "
                            f"{', '.join(context.known_absent) or 'no relation certificate'}"
                        ),
                        missing=(),
                        known_absent=context.known_absent,
                        context_ids=dict(context.context_ids),
                    )
                )
                continue

            assert context.relation_certificate is not None
            assert context.fee_quoter is not None
            members = [
                YesBasketMember(
                    instrument=context.members[t].instrument,  # type: ignore[arg-type]
                    view=views[t],  # type: ignore[arg-type]
                    certificate=context.members[t].certificate,  # type: ignore[arg-type]
                    current_settlement_fingerprint=context.members[t].evidence_fingerprint,
                )
                for t in basket.members
            ]
            first_view = views[basket.members[0]]
            assert first_view is not None
            result = evaluate_yes_basket_quantity(
                relation=context.relation_certificate,
                relation_evidence_fingerprint=context.relation_fingerprint,
                members=members,
                context=ExecutionContext(
                    current_connection_epoch=first_view.provenance.connection_epoch,
                    journal_healthy=self.reconstructor.journal_healthy,
                ),
                fee_quoter=context.fee_quoter,
                quantity=quantity,
                at=horizon.at,
            )
            records.append(
                DecisionRecord(
                    detector=DetectorKind.AT_LEAST_ONE_BASKET,
                    trigger_ordinal=trigger.trigger_ordinal,
                    trigger_reason=trigger.reason.value,
                    decision_ordinal=self._next_ordinal(),
                    decision_time=horizon.at,
                    subjects=subjects,
                    detector_did_run=True,
                    classification=result.classification.value,
                    quantity=quantity.to_str(),
                    context_ids=dict(context.context_ids),
                    completeness=self._completeness(horizon, subjects),
                    blocking_reason=result.blocking_reason,
                    fingerprint=fingerprint_yes_basket(result, decision_time=horizon.at),
                    warnings=result.warnings,
                )
            )
        return records
