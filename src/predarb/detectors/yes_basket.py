"""AT_LEAST_ONE BUY-YES basket: buy YES in every member of a certified set.

If at least one of the selected propositions must settle YES, then a long-YES
position in all of them has a guaranteed payoff floor -- whichever member wins,
that leg pays. Buy each leg for less than that floor and the difference is
contractual, not directional.

Book arithmetic reminder: buying YES crosses **NO bids**, so ``yes_ask =
notional - no_bid``. A basket of n YES legs costs ``sum(N_i - no_bid_i)`` and
pays at least ``q * min_i(N_i)``, so it is profitable when the NO bids are
collectively rich -- the mirror image of Step 9's NO basket.

What this detector needs, and what it deliberately does not
------------------------------------------------------------
It requires an ``AT_LEAST_ONE`` relation certificate over the **exact** member
set, plus a current settlement certificate for every member. Neither alone is
enough: the relation says which joint outcomes are possible, the individual
certificates say what YES pays in them, and a proof needs both.

It does **not** require AT_MOST_ONE, and asking for it would be a real mistake.
Under the supported payoff model, additional members settling YES only *raises*
the basket's payoff. The floor is attained when exactly one wins, and every
richer state is above it. So this detector stays valid on events that are not
mutually exclusive at all -- which is most of them (65 of 94 sampled events
carry ``mutually_exclusive = false``).

No state enumeration
--------------------
``AT_LEAST_ONE`` over n members permits ``2 ** n - 1`` joint outcomes, and a
live event with 300 members was observed. The floor comes from a symbolic
theorem with checked preconditions
(:mod:`predarb.domain.at_least_one_payoff`), never from building that set.
Tests cross-check the symbolic minimum against real enumeration for small n.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from predarb.books.execution import (
    BookNotExecutableError,
    ExecutionContext,
    ExecutionCurve,
    ExecutionQuote,
    UnsupportedExecutionSemanticsError,
    build_execution_curve,
)
from predarb.books.liquidity import BookLiquidityId
from predarb.books.multileg import detect_liquidity_collisions
from predarb.books.orderbook import BookView
from predarb.detectors.binary_complement import (
    MIN_QUANTITY_STEP,
    LegFeeQuoter,
    require_tradeable_quantity,
)
from predarb.domain.at_least_one_payoff import (
    AtLeastOnePayoffProof,
    MemberPayoffSpec,
    UnprovablePayoffError,
    prove_at_least_one_floor,
)
from predarb.domain.costs import FeeBounds, FeesUnavailable, LegFees
from predarb.domain.enums import MarketSide
from predarb.domain.models import VenueInstrument
from predarb.domain.money import Money, Quantity
from predarb.opportunities.models import (
    Classification,
    CostStatus,
    ExecutionStatus,
    PayoffStatus,
    ProfitInterval,
    SemanticStatus,
)
from predarb.semantics.certificate import SettlementCertificate, SettlementModel
from predarb.semantics.fingerprint import SettlementEvidenceFingerprint
from predarb.semantics.relation import (
    ALL_SELECTED_MEMBERS_LOSE,
    MIN_BASKET_MEMBERS,
    RelationCertificate,
    RelationClaim,
    canonical_members,
)

__all__ = [
    "YesBasketMember",
    "YesBasketResult",
    "YesBasketSearch",
    "evaluate_yes_basket_quantity",
    "search_yes_basket",
]


@dataclass(frozen=True, slots=True)
class YesBasketMember:
    """One selected member: its instrument, book, certificate and evidence."""

    instrument: VenueInstrument
    view: BookView
    certificate: SettlementCertificate
    current_settlement_fingerprint: SettlementEvidenceFingerprint | None

    @property
    def ticker(self) -> str:
        return self.instrument.ticker


@dataclass(frozen=True, slots=True)
class YesBasketResult:
    """One AT_LEAST_ONE BUY-YES evaluation at one exact quantity.

    Four independent status axes, and no single ``profit`` or ``confidence``
    field: a number that collapses a proof, a bound and a race into one figure
    invites being read as a promise.
    """

    detected_at: datetime
    event_ticker: str
    members: tuple[str, ...]
    quantity: Quantity
    classification: Classification
    semantic_status: SemanticStatus
    payoff_status: PayoffStatus
    cost_status: CostStatus
    execution_status: ExecutionStatus

    relation_claim: str = RelationClaim.AT_LEAST_ONE.value
    relation_certificate_id: str | None = None
    relation_evidence_fingerprint: str | None = None
    member_certificate_ids: Mapping[str, str] = field(default_factory=dict)
    member_evidence_fingerprints: Mapping[str, str] = field(default_factory=dict)

    payoff_proof: AtLeastOnePayoffProof | None = None
    quotes: Mapping[str, ExecutionQuote] = field(default_factory=dict)
    gross_cost_by_leg: Mapping[str, Money] = field(default_factory=dict)
    fees_by_leg: Mapping[str, LegFees] = field(default_factory=dict)
    total_gross_cost: Money | None = None
    profit: ProfitInterval | None = None

    blocking_reason: str | None = None
    blocking_legs: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def leg_count(self) -> int:
        return len(self.members)

    @property
    def non_atomic(self) -> bool:
        """Always true: nothing here submits orders, let alone atomically."""
        return True

    @property
    def is_arbitrage_claim(self) -> bool:
        return self.classification.is_arbitrage_claim

    @property
    def guaranteed_payoff(self) -> Money | None:
        return None if self.payoff_proof is None else self.payoff_proof.worst_case_payoff

    @property
    def minimising_members(self) -> tuple[str, ...]:
        """Which member's singleton win attains the floor."""
        return () if self.payoff_proof is None else self.payoff_proof.witnesses

    @property
    def forbidden_state(self) -> str:
        return ALL_SELECTED_MEMBERS_LOSE

    @property
    def liquidity_ids(self) -> tuple[BookLiquidityId, ...]:
        ids: list[BookLiquidityId] = []
        for quote in self.quotes.values():
            ids.extend(quote.liquidity_ids)
        return tuple(ids)

    def describe(self) -> str:
        head = (
            f"{self.event_ticker} YES-basket over certified AT_LEAST_ONE set of "
            f"{self.leg_count} q={self.quantity}: {self.classification.value}"
        )
        if self.profit is None:
            return f"{head} ({self.blocking_reason or 'no economics computed'})"
        return f"{head} {self.profit.describe()} [{self.execution_status.value}]"


def _blocked(
    *,
    at: datetime,
    relation: RelationCertificate | None,
    members: Sequence[str],
    quantity: Quantity,
    classification: Classification,
    reason: str,
    event_ticker: str,
    semantic: SemanticStatus = SemanticStatus.VERIFIED,
    execution: ExecutionStatus = ExecutionStatus.RACE_EXPOSED,
    blocking_legs: Sequence[str] = (),
    quotes: Mapping[str, ExecutionQuote] | None = None,
    fees: Mapping[str, LegFees] | None = None,
    payoff_proof: AtLeastOnePayoffProof | None = None,
) -> YesBasketResult:
    return YesBasketResult(
        detected_at=at,
        event_ticker=event_ticker,
        members=tuple(members),
        quantity=quantity,
        classification=classification,
        semantic_status=semantic,
        payoff_status=PayoffStatus.NOT_EVALUATED,
        cost_status=CostStatus.UNAVAILABLE,
        execution_status=execution,
        relation_certificate_id=None if relation is None else relation.certificate_id,
        relation_evidence_fingerprint=(
            None if relation is None else relation.evidence_fingerprint.digest
        ),
        payoff_proof=payoff_proof,
        quotes=dict(quotes or {}),
        fees_by_leg=dict(fees or {}),
        blocking_reason=reason,
        blocking_legs=tuple(blocking_legs),
    )


def _semantic_blocking_reason(
    *,
    relation: RelationCertificate,
    members: Sequence[YesBasketMember],
    relation_fingerprint: SettlementEvidenceFingerprint | None,
    at: datetime,
) -> tuple[str, tuple[str, ...]] | None:
    """Every semantic precondition, checked before any number is computed."""
    # The claim itself, first. This detector prices the AT_LEAST_ONE state
    # family -- everything except all-NO. An AT_MOST_ONE certificate forbids a
    # different state and permits all-NO, which is precisely the state this
    # basket's floor depends on being impossible.
    if relation.claim is not RelationClaim.AT_LEAST_ONE:
        return (
            f"relation certificate asserts {relation.claim.value}, but a BUY-YES "
            "basket needs AT_LEAST_ONE; the two forbid different terminal states "
            "and are not interchangeable",
            (),
        )

    supplied = canonical_members([member.ticker for member in members])
    if len(supplied) < MIN_BASKET_MEMBERS:
        return (
            f"a basket needs at least {MIN_BASKET_MEMBERS} members; AT_LEAST_ONE "
            f"over {len(supplied)} is a certainty claim about a single market and "
            "belongs in a settlement certificate",
            (),
        )
    if not relation.covers(supplied):
        return (
            f"relation certificate covers {list(relation.selected_members)}, "
            f"not {list(supplied)}; a subset or superset was not what was reviewed",
            (),
        )

    member_fingerprints = {
        member.ticker: (
            member.current_settlement_fingerprint.digest
            if member.current_settlement_fingerprint is not None
            else None
        )
        for member in members
    }
    known = {t: d for t, d in member_fingerprints.items() if d is not None}
    blocking = relation.blocking_reason(
        current_fingerprint=relation_fingerprint,
        member_settlement_fingerprints=known if len(known) == len(members) else None,
        at=at,
    )
    if blocking is not None:
        return blocking, ()

    stale = [
        member.ticker
        for member in members
        if member.certificate.blocking_reason(
            current_fingerprint=member.current_settlement_fingerprint, at=at
        )
        is not None
    ]
    if stale:
        return (
            f"member settlement certificate(s) unusable: {', '.join(sorted(stale))}",
            tuple(sorted(stale)),
        )
    return None


def _payoff_specs(
    members: Sequence[YesBasketMember],
) -> tuple[list[MemberPayoffSpec], tuple[str, ...]]:
    """Reduce each member's certified YES table to what the theorem needs.

    Step 12 supports only the standard binary model. A certificate carrying a
    scalar, fair-price or partially-winning table is refused rather than
    approximated: the floor theorem assumes a losing leg pays nothing and a
    winning leg pays the notional, and a table that says otherwise breaks it.
    """
    specs: list[MemberPayoffSpec] = []
    unsupported: list[str] = []
    for member in members:
        certificate = member.certificate
        if certificate.settlement_model is not SettlementModel.STANDARD_BINARY_COMPLEMENT:
            unsupported.append(
                f"{member.ticker}: settlement model {certificate.settlement_model.value} "
                "is not the standard binary payoff this detector can prove a floor for"
            )
            continue
        states = certificate.allowed_states
        winning = max(states, key=lambda s: certificate.yes_payoff.payoff_per_contract(s).units)
        losing = min(states, key=lambda s: certificate.yes_payoff.payoff_per_contract(s).units)
        specs.append(
            MemberPayoffSpec(
                ticker=member.ticker,
                winning_payoff_per_contract=certificate.yes_payoff.payoff_per_contract(winning),
                losing_payoff_per_contract=certificate.yes_payoff.payoff_per_contract(losing),
            )
        )
    return specs, tuple(unsupported)


def _fee_blocking_reason(fees: Mapping[str, LegFees]) -> str | None:
    """Any leg whose fee figure cannot carry a contractual claim blocks all of them."""
    unusable = tuple(
        ticker
        for ticker, leg in fees.items()
        if isinstance(leg, FeesUnavailable) or not leg.supports_arbitrage_claim
    )
    if not unusable:
        return None

    def why(leg: LegFees) -> str:
        if isinstance(leg, FeesUnavailable):
            return f"unavailable ({leg.reason})"
        return f"cannot support a contractual claim ({leg.provenance})"

    return "; ".join(f"{t}: {why(fees[t])}" for t in sorted(unusable))


def evaluate_yes_basket_quantity(
    *,
    relation: RelationCertificate,
    relation_evidence_fingerprint: SettlementEvidenceFingerprint | None,
    members: Sequence[YesBasketMember],
    context: ExecutionContext,
    fee_quoter: LegFeeQuoter,
    quantity: Quantity,
    at: datetime,
    curves: Mapping[str, ExecutionCurve] | None = None,
) -> YesBasketResult:
    """Authoritative verdict for exactly this basket at exactly this quantity.

    Pure: no network, no filesystem, no registry, no wall clock. Every input is
    already a resolved immutable object, so the same inputs give the same
    verdict wherever it runs.
    """
    require_tradeable_quantity(quantity)
    tickers = tuple(member.ticker for member in members)
    event_ticker = relation.event_ticker

    semantic = _semantic_blocking_reason(
        relation=relation,
        members=members,
        relation_fingerprint=relation_evidence_fingerprint,
        at=at,
    )
    if semantic is not None:
        reason, legs = semantic
        return _blocked(
            at=at,
            relation=relation,
            members=tickers,
            quantity=quantity,
            classification=Classification.BLOCKED_SETTLEMENT_SEMANTICS,
            reason=reason,
            event_ticker=event_ticker,
            semantic=SemanticStatus.BLOCKED,
            blocking_legs=legs,
        )

    specs, unsupported = _payoff_specs(members)
    if unsupported:
        return _blocked(
            at=at,
            relation=relation,
            members=tickers,
            quantity=quantity,
            classification=Classification.BLOCKED_SETTLEMENT_SEMANTICS,
            reason="; ".join(unsupported),
            event_ticker=event_ticker,
            semantic=SemanticStatus.BLOCKED,
            blocking_legs=tuple(u.split(":")[0] for u in unsupported),
        )

    try:
        payoff_proof = prove_at_least_one_floor(
            specs, quantity=quantity, forbidden_state=ALL_SELECTED_MEMBERS_LOSE
        )
    except UnprovablePayoffError as exc:
        return _blocked(
            at=at,
            relation=relation,
            members=tickers,
            quantity=quantity,
            classification=Classification.BLOCKED_SETTLEMENT_SEMANTICS,
            reason=str(exc),
            event_ticker=event_ticker,
            semantic=SemanticStatus.BLOCKED,
        )

    if curves is None:
        try:
            curves = {
                member.ticker: build_execution_curve(
                    member.view, member.instrument, context, MarketSide.YES, at=at
                )
                for member in members
            }
        except BookNotExecutableError as exc:
            return _blocked(
                at=at,
                relation=relation,
                members=tickers,
                quantity=quantity,
                classification=Classification.BLOCKED_BOOK_INTEGRITY,
                reason=str(exc),
                event_ticker=event_ticker,
                execution=ExecutionStatus.STALE_OR_INVALID,
                payoff_proof=payoff_proof,
            )
        except UnsupportedExecutionSemanticsError as exc:
            return _blocked(
                at=at,
                relation=relation,
                members=tickers,
                quantity=quantity,
                classification=Classification.BLOCKED_SETTLEMENT_SEMANTICS,
                reason=str(exc),
                event_ticker=event_ticker,
                semantic=SemanticStatus.BLOCKED,
                payoff_proof=payoff_proof,
            )

    return _evaluate_with_curves(
        relation=relation,
        members=members,
        curves=curves,
        fee_quoter=fee_quoter,
        quantity=quantity,
        at=at,
        payoff_proof=payoff_proof,
    )


def _evaluate_with_curves(
    *,
    relation: RelationCertificate,
    members: Sequence[YesBasketMember],
    curves: Mapping[str, ExecutionCurve],
    fee_quoter: LegFeeQuoter,
    quantity: Quantity,
    at: datetime,
    payoff_proof: AtLeastOnePayoffProof,
) -> YesBasketResult:
    """The quantity-dependent half, so a sweep can reuse the curves."""
    tickers = tuple(member.ticker for member in members)
    event_ticker = relation.event_ticker
    identities = {
        "member_certificate_ids": {m.ticker: m.certificate.identity for m in members},
        "member_evidence_fingerprints": {
            m.ticker: m.current_settlement_fingerprint.digest
            for m in members
            if m.current_settlement_fingerprint is not None
        },
    }

    quotes = {t: curves[t].quote_up_to(quantity, at=at) for t in tickers}
    short = tuple(t for t in tickers if not quotes[t].fully_fillable)
    if short:
        return _blocked(
            at=at,
            relation=relation,
            members=tickers,
            quantity=quantity,
            classification=Classification.INSUFFICIENT_DEPTH,
            reason=(
                f"requested {quantity}; these legs cannot fill it: "
                + ", ".join(f"{t}={quotes[t].filled_quantity}" for t in short)
            ),
            event_ticker=event_ticker,
            execution=ExecutionStatus.DEPTH_VERIFIED,
            blocking_legs=short,
            quotes=quotes,
            payoff_proof=payoff_proof,
        )

    # Across ALL legs, not pairwise. Distinct tickers normally imply distinct
    # liquidity, but "normally" is not a guarantee, and consuming one source
    # level twice would manufacture depth that does not exist.
    collisions = detect_liquidity_collisions([curves[t] for t in tickers], up_to=quantity)
    if collisions:
        return _blocked(
            at=at,
            relation=relation,
            members=tickers,
            quantity=quantity,
            classification=Classification.BLOCKED_LIQUIDITY_COLLISION,
            reason=(
                f"{len(collisions)} source liquidity level(s) would be consumed by "
                "more than one leg"
            ),
            event_ticker=event_ticker,
            quotes=quotes,
            payoff_proof=payoff_proof,
        )

    fees = {t: fee_quoter(quotes[t]) for t in tickers}
    fee_block = _fee_blocking_reason(fees)
    if fee_block is not None:
        return _blocked(
            at=at,
            relation=relation,
            members=tickers,
            quantity=quantity,
            classification=Classification.BLOCKED_FEE_SEMANTICS,
            reason=fee_block,
            event_ticker=event_ticker,
            quotes=quotes,
            fees=fees,
            payoff_proof=payoff_proof,
        )

    bounded = {t: leg for t, leg in fees.items() if isinstance(leg, FeeBounds)}
    # Gross by leg, never netted: collateral_return_type / MECNET semantics are
    # unresolved (A-16), so no exchange netting benefit may enter the economics.
    gross_by_leg = {t: quotes[t].gross_cost for t in tickers}
    interval = ProfitInterval(
        worst_case_payoff=payoff_proof.worst_case_payoff,
        gross_cost=Money.from_units(sum(cost.units for cost in gross_by_leg.values())),
        fee_lower=Money.from_units(sum(leg.lower.units for leg in bounded.values())),
        fee_upper=Money.from_units(sum(leg.upper.units for leg in bounded.values())),
    )
    payoff_status = interval.payoff_status()
    classification = {
        PayoffStatus.STATE_INDEPENDENT_PROFIT: Classification.PROVEN_CONTRACTUAL_ARBITRAGE,
        PayoffStatus.NON_POSITIVE: Classification.PROVEN_NOT_PROFITABLE,
        PayoffStatus.INDETERMINATE: Classification.INDETERMINATE_COST_BOUNDS,
    }[payoff_status]

    warnings: list[str] = [
        f"AT_LEAST_ONE over a certified set of {len(tickers)}; the floor is attained "
        f"when exactly one member wins ({list(payoff_proof.witnesses)}), and every "
        "state with more winners pays more",
        "this claim does NOT assert mutual exclusion; two or more members settling "
        "YES is permitted and only increases the payoff",
        "acquisition cash is reported gross by leg; no collateral-netting "
        "(collateral_return_type / MECNET) benefit is assumed",
    ]
    if classification is Classification.PROVEN_CONTRACTUAL_ARBITRAGE:
        warnings.append(
            f"payoff proof holds only once ALL {len(tickers)} legs are filled; they are "
            "separate non-atomic orders, so this is race-exposed and not a locked profit"
        )
    for leg in bounded.values():
        warnings.extend(leg.warnings)

    return YesBasketResult(
        detected_at=at,
        event_ticker=event_ticker,
        members=tickers,
        quantity=quantity,
        classification=classification,
        semantic_status=SemanticStatus.VERIFIED,
        payoff_status=payoff_status,
        cost_status=CostStatus.BOUNDED,
        execution_status=ExecutionStatus.RACE_EXPOSED,
        relation_certificate_id=relation.certificate_id,
        relation_evidence_fingerprint=relation.evidence_fingerprint.digest,
        member_certificate_ids=identities["member_certificate_ids"],
        member_evidence_fingerprints=identities["member_evidence_fingerprints"],
        payoff_proof=payoff_proof,
        quotes=quotes,
        gross_cost_by_leg=gross_by_leg,
        fees_by_leg=fees,
        total_gross_cost=interval.gross_cost,
        profit=interval,
        warnings=tuple(warnings),
    )


@dataclass(frozen=True, slots=True)
class YesBasketSearch:
    """A bounded sweep, carrying the scope it actually covered.

    "No candidate" is a statement about ``[min, max]`` at this step and nothing
    wider. The scope travels with the result so a report cannot quietly promote
    a barren interval into a barren market.
    """

    event_ticker: str
    members: tuple[str, ...]
    search_min_quantity: Quantity
    search_max_quantity: Quantity
    search_step: Quantity
    evaluated_quantity_count: int
    search_complete: bool
    results: tuple[YesBasketResult, ...]
    classification_counts: Mapping[Classification, int] = field(default_factory=dict)
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        tallied = sum(self.classification_counts.values())
        if self.classification_counts and tallied != self.evaluated_quantity_count:
            raise ValueError(
                f"classification counts sum to {tallied} but "
                f"{self.evaluated_quantity_count} quantities were evaluated"
            )

    @property
    def proven_candidates(self) -> tuple[YesBasketResult, ...]:
        return tuple(r for r in self.results if r.is_arbitrage_claim)

    @property
    def best_candidate(self) -> YesBasketResult | None:
        candidates = self.proven_candidates
        if not candidates:
            return None
        return max(candidates, key=lambda r: r.profit.profit_lower_bound.units if r.profit else 0)

    def summary(self) -> str:
        scope = f"[{self.search_min_quantity}, {self.search_max_quantity}] step {self.search_step}"
        found = len(self.proven_candidates)
        if found:
            return f"{self.event_ticker}: {found} proven candidate(s) in {scope}"
        return f"{self.event_ticker}: no proven candidate in {scope}"


def search_yes_basket(
    *,
    relation: RelationCertificate,
    relation_evidence_fingerprint: SettlementEvidenceFingerprint | None,
    members: Sequence[YesBasketMember],
    context: ExecutionContext,
    fee_quoter: LegFeeQuoter,
    min_quantity: Quantity,
    max_quantity: Quantity,
    at: datetime,
    keep: str = "candidates",
) -> YesBasketSearch:
    """Evaluate every 0.01 increment in ``[min, max]``, reusing the curves.

    Curves, certificates, relation semantics and the payoff proof are built once
    and reused across quantities. Rebuilding them per quantity would be slower
    and, worse, would let a mid-sweep context change produce a sweep whose
    results were evaluated against different facts.

    Exhaustive **within the requested interval and nowhere else**. No pruning:
    a heuristic that skipped quantities would make "no candidate" mean "no
    candidate among the ones we looked at", which is not the same sentence.
    """
    require_tradeable_quantity(min_quantity)
    require_tradeable_quantity(max_quantity)
    if max_quantity < min_quantity:
        raise ValueError(f"search interval is empty: [{min_quantity}, {max_quantity}]")
    if keep not in {"candidates", "all"}:
        raise ValueError(f"keep must be 'candidates' or 'all', got {keep!r}")

    tickers = tuple(member.ticker for member in members)
    probe = evaluate_yes_basket_quantity(
        relation=relation,
        relation_evidence_fingerprint=relation_evidence_fingerprint,
        members=members,
        context=context,
        fee_quoter=fee_quoter,
        quantity=min_quantity,
        at=at,
    )
    if probe.classification in {
        Classification.BLOCKED_SETTLEMENT_SEMANTICS,
        Classification.BLOCKED_BOOK_INTEGRITY,
    }:
        return YesBasketSearch(
            event_ticker=relation.event_ticker,
            members=tickers,
            search_min_quantity=min_quantity,
            search_max_quantity=min_quantity,
            search_step=MIN_QUANTITY_STEP,
            evaluated_quantity_count=0,
            search_complete=False,
            results=(probe,),
            warnings=("search not performed: semantics or book state blocked it",),
        )

    curves = {
        member.ticker: build_execution_curve(
            member.view, member.instrument, context, MarketSide.YES, at=at
        )
        for member in members
    }
    specs, _ = _payoff_specs(members)

    reachable = min(*(curve.max_fillable_quantity for curve in curves.values()), max_quantity)
    warnings: list[str] = []
    if reachable < min_quantity:
        return YesBasketSearch(
            event_ticker=relation.event_ticker,
            members=tickers,
            search_min_quantity=min_quantity,
            search_max_quantity=min_quantity,
            search_step=MIN_QUANTITY_STEP,
            evaluated_quantity_count=0,
            search_complete=False,
            results=(),
            warnings=(
                f"no quantity in [{min_quantity}, {max_quantity}] is executable on every "
                f"leg: common depth supports only {reachable}",
            ),
        )
    if reachable < max_quantity:
        warnings.append(
            f"common depth across all {len(tickers)} legs supports only {reachable}; "
            "quantities above it were not evaluated"
        )

    kept: list[YesBasketResult] = []
    counts: Counter[Classification] = Counter()
    evaluated = 0
    explained = False
    for units in range(min_quantity.units, reachable.units + 1, MIN_QUANTITY_STEP.units):
        quantity = Quantity.from_units(units)
        result = _evaluate_with_curves(
            relation=relation,
            members=members,
            curves=curves,
            fee_quoter=fee_quoter,
            quantity=quantity,
            at=at,
            payoff_proof=prove_at_least_one_floor(
                specs, quantity=quantity, forbidden_state=ALL_SELECTED_MEMBERS_LOSE
            ),
        )
        evaluated += 1
        counts[result.classification] += 1
        if keep == "all" or result.is_arbitrage_claim:
            kept.append(result)
        elif not explained:
            kept.append(result)
            explained = True

    return YesBasketSearch(
        event_ticker=relation.event_ticker,
        members=tickers,
        search_min_quantity=min_quantity,
        search_max_quantity=reachable,
        search_step=MIN_QUANTITY_STEP,
        evaluated_quantity_count=evaluated,
        search_complete=reachable >= max_quantity,
        results=tuple(kept),
        classification_counts=dict(counts),
        warnings=tuple(warnings),
    )
