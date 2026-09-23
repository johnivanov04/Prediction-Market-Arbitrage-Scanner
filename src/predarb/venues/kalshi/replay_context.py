"""Resolve Kalshi detector context from recorded observations. Offline.

The venue-specific half of replay. It reads only what a bundle recorded and
hands the coordinator immutable domain objects, so ``replay/`` never learns a
Kalshi schema and never needs a client.

Absence has two meanings, and they are kept apart
-------------------------------------------------
A snapshot observation says "we queried this source at ordinal N; here is what it
held, possibly nothing". No snapshot says "we never asked". Only the first lets a
replay conclude that a certificate does not exist -- and that conclusion is a
determinate `BLOCKED_SETTLEMENT_SEMANTICS`, not a gap.

Staleness is our policy, not the venue's
----------------------------------------
Nothing in the API promises a market's notional or a series' fee configuration
cannot change mid-session. An observation older than the refresh policy is
therefore reported ``INCOMPLETE_STALE_CONTEXT`` rather than treated as still
current.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from functools import partial
from typing import Any

from predarb.domain.enums import SettlementKind, VenueId
from predarb.domain.fees import FeeConfiguration, FeeScope, ResolvedFeeConfiguration
from predarb.domain.models import VenueInstrument
from predarb.domain.money import Price
from predarb.replay.completeness import (
    CompletenessDimension,
    DimensionStatus,
    ReplayDataCompleteness,
)
from predarb.replay.context import BasketContext, BinaryContext
from predarb.replay.knowledge import KnowledgeBase
from predarb.replay.observation import KnowledgeHorizon, ObservationKind
from predarb.replay.plan import BasketPlan, DetectorPlan
from predarb.semantics.certificate import CertificateStatus, standard_binary_complement
from predarb.semantics.fingerprint import SettlementEvidenceFingerprint
from predarb.semantics.relation import (
    RelationCertificate,
    RelationClaim,
    RelationStatus,
)
from predarb.venues.kalshi.fee_model import BalancePrecision
from predarb.venues.kalshi.fees import leg_fee_bounds

__all__ = ["KalshiReplayContext"]

_PRECISIONS = {
    "direct": BalancePrecision.direct_member,
    "non-direct": BalancePrecision.non_direct_member,
    "unknown-conservative": BalancePrecision.unknown_member,
}


def _fingerprint_from(payload: Mapping[str, Any] | None) -> SettlementEvidenceFingerprint | None:
    if payload is None:
        return None
    body = payload.get("fingerprint")
    if isinstance(body, dict) and "digest" in body:
        return SettlementEvidenceFingerprint(
            schema_version=body["schema_version"],
            components=body["components"],
            digest=body["digest"],
        )
    digest = payload.get("evidence_digest") or payload.get("digest")
    if not digest:
        return None
    # A recorded digest without components still identifies the evidence
    # version, which is all a match/mismatch comparison needs.
    return SettlementEvidenceFingerprint.over({"recorded_digest": str(digest)})


@dataclass
class KalshiReplayContext:
    """Point-in-time Kalshi context, resolved from a knowledge base."""

    knowledge: KnowledgeBase
    plan: DetectorPlan

    @property
    def precision(self) -> BalancePrecision:
        return _PRECISIONS.get(self.plan.balance_precision, BalancePrecision.unknown_member)()

    # -- instruments -------------------------------------------------------

    def _instrument(self, ticker: str, horizon: KnowledgeHorizon) -> VenueInstrument | None:
        payload = self.knowledge.market_at(ticker, horizon)
        if payload is None:
            return None
        notional = payload.get("notional")
        return VenueInstrument(
            venue=VenueId.KALSHI,
            ticker=payload.get("ticker", ticker),
            event_ticker=payload.get("event_ticker", ""),
            title=payload.get("title", "") or "",
            status_raw=payload.get("status", "active") or "active",
            settlement_kind=SettlementKind(payload.get("settlement_kind", "UNKNOWN")),
            market_type_raw=payload.get("market_type", "") or "",
            notional_value=Price.from_value(notional) if notional else None,
            price_grid=None,
            yes_sub_title=payload.get("yes_sub_title"),
            no_sub_title=payload.get("no_sub_title"),
            rules_primary=payload.get("rules_primary"),
            rules_secondary=payload.get("rules_secondary"),
            rules_hash=payload.get("rules_hash"),
            open_time=None,
            close_time=None,
            expected_expiration_time=None,
            latest_expiration_time=None,
            settlement_timer_seconds=None,
            result_raw=None,
            settlement_value=None,
            can_close_early=payload.get("can_close_early"),
            yes_bid=None,
            yes_ask=None,
            no_bid=None,
            no_ask=None,
            yes_bid_size=None,
            yes_ask_size=None,
            exclusion_reasons=tuple(payload.get("exclusion_reasons", ())),
        )

    # -- fees --------------------------------------------------------------

    def _fee_config(
        self, ticker: str, horizon: KnowledgeHorizon
    ) -> ResolvedFeeConfiguration | None:
        payload = self.knowledge.fee_record_at(ticker, horizon)
        if payload is None:
            return None
        multiplier = payload.get("multiplier")
        fee_type = payload.get("fee_type")
        if multiplier is None or fee_type is None:
            return None
        return ResolvedFeeConfiguration(
            configuration=FeeConfiguration(
                fee_type_raw=str(fee_type),
                multiplier=Decimal(str(multiplier)),
                scope=FeeScope(payload.get("scope", "SERIES")),
                scope_ticker=str(payload.get("scope_ticker", ticker)),
            ),
            effective_from=(
                datetime.fromisoformat(payload["effective_from"])
                if payload.get("effective_from")
                else None
            ),
            provenance=str(payload.get("provenance", "recorded fee observation")),
        )

    # -- certificates ------------------------------------------------------

    def _settlement_certificate(
        self, ticker: str, horizon: KnowledgeHorizon
    ) -> tuple[Any, tuple[str, ...], tuple[str, ...]]:
        """Returns ``(certificate, missing, known_absent)``.

        A registry snapshot that listed no certificate for this market is
        ``known_absent``; no snapshot at all is ``missing``.
        """
        record = self.knowledge.settlement_certificate_at(ticker, horizon)
        if record is not None:
            notional = record.get("notional", "1.0000")
            certificate = standard_binary_complement(
                market_ticker=ticker,
                evidence_fingerprint=_fingerprint_from(record)
                or SettlementEvidenceFingerprint.over({"certificate": ticker}),
                rules_hash=str(record.get("rules_hash", "")) or "recorded",
                notional=Price.from_value(str(notional)),
                evidence=str(record.get("evidence", "recorded certificate observation")),
                verified_by=str(record.get("verified_by", "recorded")),
                verification_method=str(record.get("verification_method", "recorded")),
                verified_at=datetime.fromisoformat(record["verified_at"])
                if record.get("verified_at")
                else horizon.at,
                valid_from=datetime.fromisoformat(record["valid_from"])
                if record.get("valid_from")
                else horizon.at,
                status=CertificateStatus(record.get("status", "VERIFIED")),
            )
            return certificate, (), ()

        snapshot = self.knowledge.settlement_snapshot_at(ticker, horizon)
        if snapshot is None:
            return None, (f"settlement_registry:{ticker}",), ()
        return None, (), (f"no settlement certificate for {ticker}",)

    def _relation_certificate(
        self, plan: BasketPlan, horizon: KnowledgeHorizon
    ) -> tuple[RelationCertificate | None, tuple[str, ...], tuple[str, ...]]:
        record = self.knowledge.relation_certificate_at(plan.event_ticker, horizon)
        if record is not None:
            return (
                RelationCertificate(
                    certificate_id=str(record["certificate_id"]),
                    # The recorded claim, never a default. A bundle carrying an
                    # AT_LEAST_ONE certificate must not be silently replayed as
                    # AT_MOST_ONE: the two forbid opposite states, and the
                    # basket detector would then be handed a guarantee nobody
                    # reviewed. An unrecognised claim raises here rather than
                    # falling back.
                    claim=RelationClaim(record.get("claim", RelationClaim.AT_MOST_ONE.value)),
                    event_ticker=plan.event_ticker,
                    selected_members=tuple(record.get("selected_members", plan.members)),
                    snapshot_id=str(record.get("snapshot_id", "recorded")),
                    evidence_fingerprint=_fingerprint_from(record)
                    or SettlementEvidenceFingerprint.over({"relation": plan.event_ticker}),
                    member_settlement_fingerprints=dict(
                        record.get("member_settlement_fingerprints", {})
                    ),
                    status=RelationStatus(record.get("status", "VERIFIED")),
                    reviewer=str(record.get("reviewer", "recorded")),
                    reviewed_at=datetime.fromisoformat(record["reviewed_at"])
                    if record.get("reviewed_at")
                    else horizon.at,
                    issued_at=datetime.fromisoformat(record["issued_at"])
                    if record.get("issued_at")
                    else horizon.at,
                    valid_from=datetime.fromisoformat(record["valid_from"])
                    if record.get("valid_from")
                    else horizon.at,
                    valid_to=None,
                    policy_schema_version=str(
                        record.get("policy_schema_version", "relation-evidence/1")
                    ),
                    evidence=str(record.get("evidence", "recorded relation certificate")),
                ),
                (),
                (),
            )
        snapshot = self.knowledge.relation_snapshot_at(plan.event_ticker, horizon)
        if snapshot is None:
            return None, (f"relation_registry:{plan.event_ticker}",), ()
        return None, (), (f"no relation certificate for {plan.event_ticker}",)

    # -- ContextProvider ---------------------------------------------------

    def binary_context(self, ticker: str, horizon: KnowledgeHorizon) -> BinaryContext:
        missing: list[str] = []
        known_absent: list[str] = []

        instrument = self._instrument(ticker, horizon)
        if instrument is None:
            missing.append(f"metadata:{ticker}")

        fee_config = self._fee_config(ticker, horizon)
        if fee_config is None:
            snapshot = self.knowledge.fee_snapshot_at(ticker, horizon)
            if snapshot is None:
                missing.append(f"fee_knowledge:{ticker}")
            else:
                known_absent.append(f"no fee configuration for {ticker}")

        certificate, cert_missing, cert_absent = self._settlement_certificate(ticker, horizon)
        missing.extend(cert_missing)
        known_absent.extend(cert_absent)

        # Deliberately no fallback to the certificate's own fingerprint. That
        # would compare a certificate against itself, which always matches, and
        # would turn "we never checked whether the rules moved" into "the rules
        # have not moved". ``None`` reaches the detector, whose certificate
        # check reports current evidence as unavailable and blocks.
        evidence = self.knowledge.settlement_evidence_at(ticker, horizon)
        fingerprint = _fingerprint_from(evidence)

        quoter = (
            partial(leg_fee_bounds, resolved=fee_config, precision=self.precision)
            if fee_config is not None
            else None
        )
        return BinaryContext(
            instrument=instrument,
            certificate=certificate,
            evidence_fingerprint=fingerprint,
            fee_quoter=quoter,
            context_ids={
                "metadata": None if instrument is None else (instrument.rules_hash or "?"),
                "fee_config": None if fee_config is None else fee_config.provenance,
                "fee_multiplier": (
                    None if fee_config is None else str(fee_config.configuration.multiplier)
                ),
                "fee_type": (None if fee_config is None else fee_config.configuration.fee_type_raw),
                "settlement_certificate": (None if certificate is None else certificate.identity),
                "evidence": None if fingerprint is None else fingerprint.short,
            },
            missing=tuple(missing),
            known_absent=tuple(known_absent),
        )

    def basket_context(self, plan: BasketPlan, horizon: KnowledgeHorizon) -> BasketContext:
        relation, missing, known_absent = self._relation_certificate(plan, horizon)
        members = {t: self.binary_context(t, horizon) for t in plan.members}
        for ticker, member in members.items():
            missing = (*missing, *member.missing)
            if member.certificate is None and not member.missing:
                known_absent = (*known_absent, f"no settlement certificate for {ticker}")

        # Same rule as the binary path: an unobserved relation evidence state is
        # not a matching one.
        relation_evidence = self.knowledge.relation_evidence_at(plan.event_ticker, horizon)
        relation_fingerprint = _fingerprint_from(relation_evidence)

        first = members.get(plan.members[0]) if plan.members else None
        return BasketContext(
            plan=plan,
            relation_certificate=relation,
            relation_fingerprint=relation_fingerprint,
            members=members,
            fee_quoter=first.fee_quoter if first is not None else None,
            context_ids={
                "relation_certificate": None if relation is None else relation.certificate_id,
                "relation_evidence": (
                    None if relation_fingerprint is None else relation_fingerprint.short
                ),
                **{
                    f"member.{t}.certificate": m.context_ids.get("settlement_certificate")
                    for t, m in members.items()
                },
            },
            missing=tuple(sorted(set(missing))),
            known_absent=tuple(sorted(set(known_absent))),
        )

    def completeness(
        self, horizon: KnowledgeHorizon, plan: DetectorPlan, subjects: Sequence[str]
    ) -> ReplayDataCompleteness:
        dimensions: dict[CompletenessDimension, DimensionStatus] = {}
        reasons: dict[CompletenessDimension, tuple[str, ...]] = {}
        policy = plan.refresh_policy

        def judge(
            dimension: CompletenessDimension,
            *,
            observed_at: datetime | None,
            note: str,
        ) -> None:
            if observed_at is None:
                dimensions[dimension] = DimensionStatus.INCOMPLETE_MISSING_OBSERVATION
                reasons[dimension] = (note,)
                return
            max_age = policy.max_age_for(dimension)
            if max_age is not None and horizon.at - observed_at > max_age:
                dimensions[dimension] = DimensionStatus.INCOMPLETE_STALE_CONTEXT
                reasons[dimension] = (
                    f"last observed {horizon.at - observed_at} ago, beyond the "
                    f"{max_age} freshness policy",
                )
                return
            dimensions[dimension] = DimensionStatus.COMPLETE

        counts = self.knowledge.counts_at(horizon)
        dimensions[CompletenessDimension.BOOK_STREAM] = (
            DimensionStatus.COMPLETE
            if counts.get(ObservationKind.FRAME_RECEIVED.value)
            else DimensionStatus.INCOMPLETE_MISSING_OBSERVATION
        )
        dimensions[CompletenessDimension.LIFECYCLE] = (
            DimensionStatus.COMPLETE
            if counts.get(ObservationKind.CONNECTION_OPENED.value)
            else DimensionStatus.INCOMPLETE_MISSING_OBSERVATION
        )

        # Relation knowledge is per *event*, not per market: looking it up by
        # ticker would report a missing snapshot for every basket member.
        events = tuple(basket.event_ticker for basket in plan.baskets)

        for dimension, resolver, keys, note in (
            (
                CompletenessDimension.MARKET_METADATA,
                self.knowledge.last_metadata_observation,
                tuple(subjects),
                "no market metadata observation",
            ),
            (
                CompletenessDimension.FEE_KNOWLEDGE,
                self.knowledge.last_fee_observation,
                tuple(subjects),
                "no fee knowledge snapshot; absence of fees is unknown, not known",
            ),
            (
                CompletenessDimension.SETTLEMENT_KNOWLEDGE,
                self.knowledge.last_settlement_observation,
                tuple(subjects),
                "no settlement registry snapshot; certificate absence is unknown",
            ),
            (
                CompletenessDimension.RELATION_KNOWLEDGE,
                self.knowledge.last_relation_observation,
                events,
                "no relation registry snapshot; relation absence is unknown",
            ),
        ):
            if dimension is CompletenessDimension.RELATION_KNOWLEDGE and not events:
                # A session monitoring no baskets never consults this source, so
                # its absence is not a gap in that session's knowledge.
                dimensions[dimension] = DimensionStatus.NOT_REQUIRED
                continue
            observed_values = [resolver(key, horizon) for key in keys or ("*",)]
            observed = (
                None
                if any(value is None for value in observed_values) or not observed_values
                else min(value for value in observed_values if value is not None)
            )
            judge(dimension, observed_at=observed, note=note)

        return ReplayDataCompleteness(dimensions=dimensions, reasons=reasons)
