"""AT_MOST_ONE NO-basket detector.

Buy ``q`` NO in every market of a certified mutually-exclusive subset. If at
most one of them can settle YES, then at most one NO leg can lose, so the
basket's worst case is "every leg pays except one" -- and if that exceeds the
acquisition cost including the worst admissible fee, the profit is positive in
every joint state the relation permits.

Two proofs, not one
-------------------
The basket needs both:

* a :class:`RelationCertificate` saying which joint YES combinations are
  possible, and
* a :class:`SettlementCertificate` **per member** saying what YES and NO
  actually pay.

The relation alone proves nothing economic: "at most one of these settles YES"
says nothing about what a NO pays, or what a void does. Either proof missing or
stale blocks the basket.

Incomplete event membership is fine here
----------------------------------------
``GET /events`` omits markets settled before the historical cutoff (A-46), so
the event's market list is current observed membership, not a proven complete
outcome universe. AT_MOST_ONE over a **selected subset** tolerates that: a
winner outside the subset is economically identical to "no selected market
won", which is already an enumerated state. Exhaustiveness claims would not
tolerate it, and none is implemented.

Payoff is derived, never hardcoded
----------------------------------
Each member's NO payoff is lifted from its own certified table into the joint
state space. The familiar ``q * (n - 1) * N`` emerges from the generic engine
for equal notionals rather than being written into the detector -- which is why
unequal notionals come out right too, as ``q * (sum(N) - max(N))``.

Purity: no network, no filesystem, no registry, no clock of its own.
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
from predarb.domain.costs import FeeBounds, FeesUnavailable, LegFees
from predarb.domain.enums import MarketSide
from predarb.domain.models import VenueInstrument
from predarb.domain.money import Money, Price, Quantity
from predarb.domain.payoff import (
    IncompletePayoffSpecificationError,
    PortfolioPayoff,
    PortfolioSolver,
    Position,
    SettlementState,
    evaluate_portfolio,
)
from predarb.opportunities.models import (
    Classification,
    CostStatus,
    ExecutionStatus,
    PayoffStatus,
    ProfitInterval,
    SemanticStatus,
)
from predarb.semantics.certificate import SettlementCertificate
from predarb.semantics.fingerprint import SettlementEvidenceFingerprint
from predarb.semantics.relation import (
    MIN_BASKET_MEMBERS,
    RelationCertificate,
    RelationClaim,
    canonical_members,
)

__all__ = [
    "BasketMember",
    "BasketResult",
    "BasketSearch",
    "JointMemberNoPayoff",
    "evaluate_basket_quantity",
    "search_no_basket",
]


@dataclass(frozen=True, slots=True)
class BasketMember:
    """One member's fully resolved inputs. The caller does all the I/O."""

    instrument: VenueInstrument
    view: BookView
    certificate: SettlementCertificate
    current_settlement_fingerprint: SettlementEvidenceFingerprint | None

    @property
    def ticker(self) -> str:
        return self.instrument.ticker


@dataclass(frozen=True, slots=True)
class JointMemberNoPayoff:
    """A member's NO payoff, lifted into the joint AT_MOST_ONE state space.

    The member certificate describes payouts over that market's own two states.
    The relation describes which joint combinations are reachable. This maps one
    onto the other: in joint state ``T`` the market ``T`` is in its YES state
    and every other member is in its NO state.

    ``NONE_OF_SELECTED`` puts every member in its NO state -- which is what
    makes the basket robust to a winner outside the selected subset.
    """

    ticker: str
    certificate: SettlementCertificate
    joint_states: tuple[str, ...]

    @property
    def covered_states(self) -> frozenset[str]:
        return frozenset(self.joint_states)

    def payoff_per_contract(self, state: SettlementState) -> Price:
        if state.name not in self.covered_states:
            raise IncompletePayoffSpecificationError(
                f"{self.ticker}: joint state {state.name!r} is outside the certified "
                f"relation state space {sorted(self.joint_states)}"
            )
        member_state = (
            self.certificate.yes_state if state.name == self.ticker else self.certificate.no_state
        )
        return self.certificate.no_payoff.payoff_per_contract(member_state)


@dataclass(frozen=True, slots=True)
class BasketResult:
    """One basket evaluation at one quantity. Reproducible from its own fields."""

    detected_at: datetime
    event_ticker: str
    members: tuple[str, ...]
    quantity: Quantity
    classification: Classification
    semantic_status: SemanticStatus
    payoff_status: PayoffStatus
    cost_status: CostStatus
    execution_status: ExecutionStatus

    relation_certificate_id: str | None = None
    relation_evidence_fingerprint: str | None = None
    member_certificate_ids: Mapping[str, str] = field(default_factory=dict)
    joint_states: tuple[SettlementState, ...] = ()
    portfolio_payoff: PortfolioPayoff | None = None

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
    def worst_case_payoff(self) -> Money | None:
        return None if self.portfolio_payoff is None else self.portfolio_payoff.worst_case

    @property
    def liquidity_ids(self) -> tuple[BookLiquidityId, ...]:
        ids: list[BookLiquidityId] = []
        for quote in self.quotes.values():
            ids.extend(quote.liquidity_ids)
        return tuple(ids)

    def describe(self) -> str:
        head = (
            f"{self.event_ticker} NO-basket over selected certified "
            f"mutually-exclusive subset of {self.leg_count} q={self.quantity}: "
            f"{self.classification.value}"
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
    payoff: PortfolioPayoff | None = None,
) -> BasketResult:
    return BasketResult(
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
        joint_states=() if relation is None else relation.states(),
        quotes=dict(quotes or {}),
        fees_by_leg=dict(fees or {}),
        portfolio_payoff=payoff,
        blocking_reason=reason,
        blocking_legs=tuple(blocking_legs),
    )


def _semantic_blocking_reason(
    *,
    relation: RelationCertificate,
    members: Sequence[BasketMember],
    relation_fingerprint: SettlementEvidenceFingerprint | None,
    at: datetime,
) -> tuple[str, tuple[str, ...]] | None:
    """Every semantic precondition, checked before any number is computed."""
    # The claim itself, checked first. This detector's whole payoff table is the
    # AT_MOST_ONE state space -- n + 1 states, one per possible single winner
    # plus none-of-them. An AT_LEAST_ONE certificate forbids a different state
    # and permits multiple simultaneous winners, so using one here would price a
    # basket against a guarantee nobody reviewed. There is no fallback: a claim
    # this detector does not model is a refusal, not a default.
    if relation.claim is not RelationClaim.AT_MOST_ONE:
        return (
            f"relation certificate asserts {relation.claim.value}, but this detector "
            "prices the AT_MOST_ONE state space; the two forbid different terminal "
            "states and are not interchangeable",
            (),
        )
    supplied = canonical_members([member.ticker for member in members])
    if len(supplied) < MIN_BASKET_MEMBERS:
        return (
            f"a basket needs at least {MIN_BASKET_MEMBERS} members; "
            f"AT_MOST_ONE over {len(supplied)} asserts nothing",
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

    stale: list[str] = []
    for member in members:
        reason = member.certificate.blocking_reason(
            current_fingerprint=member.current_settlement_fingerprint, at=at
        )
        if reason is not None:
            stale.append(member.ticker)
    if stale:
        return (
            f"member settlement certificates unusable: {', '.join(sorted(stale))}",
            tuple(sorted(stale)),
        )
    return None


def evaluate_basket_quantity(
    *,
    relation: RelationCertificate,
    relation_evidence_fingerprint: SettlementEvidenceFingerprint | None,
    members: Sequence[BasketMember],
    context: ExecutionContext,
    fee_quoter: LegFeeQuoter,
    quantity: Quantity,
    at: datetime,
    solver: PortfolioSolver | None = None,
) -> BasketResult:
    """Authoritative verdict for exactly this basket at exactly this quantity."""
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

    curves: dict[str, ExecutionCurve] = {}
    for member in members:
        try:
            curves[member.ticker] = build_execution_curve(
                member.view, member.instrument, context, MarketSide.NO, at=at
            )
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
                blocking_legs=(member.ticker,),
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
                blocking_legs=(member.ticker,),
            )

    return _evaluate_with_curves(
        relation=relation,
        members=members,
        curves=curves,
        fee_quoter=fee_quoter,
        quantity=quantity,
        at=at,
        solver=solver,
    )


def _evaluate_with_curves(
    *,
    relation: RelationCertificate,
    members: Sequence[BasketMember],
    curves: Mapping[str, ExecutionCurve],
    fee_quoter: LegFeeQuoter,
    quantity: Quantity,
    at: datetime,
    solver: PortfolioSolver | None,
) -> BasketResult:
    """The quantity-dependent half, so a sweep can reuse the curves."""
    tickers = tuple(member.ticker for member in members)
    event_ticker = relation.event_ticker

    quotes = {t: curves[t].quote_up_to(quantity, at=at) for t in tickers}
    short = tuple(t for t, quote in quotes.items() if not quote.fully_fillable)
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
        )

    # Across ALL legs, not pairwise. Different tickers normally imply distinct
    # liquidity, but "normally" is not a guarantee and double-counting one
    # source level would manufacture depth that does not exist.
    collisions = detect_liquidity_collisions([curves[t] for t in tickers], up_to=quantity)
    if collisions:
        return _blocked(
            at=at,
            relation=relation,
            members=tickers,
            quantity=quantity,
            classification=Classification.BLOCKED_LIQUIDITY_COLLISION,
            reason=(
                f"{len(collisions)} source level(s) would be consumed by more than one "
                f"leg: {', '.join(c.describe() for c in collisions[:3])}"
            ),
            event_ticker=event_ticker,
            quotes=quotes,
        )

    joint_states = relation.states()
    state_names = tuple(state.name for state in joint_states)
    positions = [
        Position(
            label=member.ticker,
            quantity=quantity,
            payoff=JointMemberNoPayoff(
                ticker=member.ticker,
                certificate=member.certificate,
                joint_states=state_names,
            ),
        )
        for member in members
    ]
    try:
        payoff = evaluate_portfolio(positions, joint_states, solver=solver)
    except (IncompletePayoffSpecificationError, ValueError) as exc:
        return _blocked(
            at=at,
            relation=relation,
            members=tickers,
            quantity=quantity,
            classification=Classification.BLOCKED_SETTLEMENT_SEMANTICS,
            reason=str(exc),
            event_ticker=event_ticker,
            semantic=SemanticStatus.BLOCKED,
            quotes=quotes,
        )

    # Each leg is a separate hypothetical order, so a separate accumulator.
    fees = {t: fee_quoter(quotes[t]) for t in tickers}
    unusable = tuple(
        t
        for t, leg in fees.items()
        if isinstance(leg, FeesUnavailable) or not leg.supports_arbitrage_claim
    )
    if unusable:

        def _why(leg: LegFees) -> str:
            if isinstance(leg, FeesUnavailable):
                return leg.reason
            return f"cannot support a contractual claim ({leg.provenance})"

        detail = "; ".join(f"{t}: {_why(fees[t])}" for t in unusable)
        return _blocked(
            at=at,
            relation=relation,
            members=tickers,
            quantity=quantity,
            classification=Classification.BLOCKED_FEE_SEMANTICS,
            reason=f"fee semantics unusable on {len(unusable)} leg(s): {detail}",
            event_ticker=event_ticker,
            blocking_legs=unusable,
            quotes=quotes,
            fees=fees,
            payoff=payoff,
        )

    bounded = {t: leg for t, leg in fees.items() if isinstance(leg, FeeBounds)}
    # Gross by leg, never netted: collateral_return_type / MECNET semantics are
    # unresolved (A-16), so no exchange netting benefit may enter the economics.
    gross_by_leg = {t: quotes[t].gross_cost for t in tickers}
    gross = Money.from_units(sum(cost.units for cost in gross_by_leg.values()))
    interval = ProfitInterval(
        worst_case_payoff=payoff.worst_case,
        gross_cost=gross,
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
        f"AT_MOST_ONE over a selected certified mutually-exclusive subset of "
        f"{len(tickers)}; this is not a claim that the event's outcome set is complete",
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

    return BasketResult(
        detected_at=at,
        event_ticker=event_ticker,
        members=tickers,
        quantity=quantity,
        classification=classification,
        semantic_status=SemanticStatus.VERIFIED,
        payoff_status=payoff_status,
        cost_status=CostStatus.EXACT
        if all(leg.exact for leg in bounded.values())
        else CostStatus.BOUNDED,
        execution_status=ExecutionStatus.RACE_EXPOSED,
        relation_certificate_id=relation.certificate_id,
        relation_evidence_fingerprint=relation.evidence_fingerprint.digest,
        member_certificate_ids={m.ticker: m.certificate.identity for m in members},
        joint_states=joint_states,
        portfolio_payoff=payoff,
        quotes=quotes,
        gross_cost_by_leg=gross_by_leg,
        fees_by_leg=fees,
        total_gross_cost=gross,
        profit=interval,
        warnings=tuple(warnings),
    )


@dataclass(frozen=True, slots=True)
class BasketSearch:
    """A bounded sweep, carrying the scope it actually covered."""

    event_ticker: str
    members: tuple[str, ...]
    search_min_quantity: Quantity
    search_max_quantity: Quantity
    search_step: Quantity
    evaluated_quantity_count: int
    search_complete: bool
    results: tuple[BasketResult, ...]
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
    def proven_candidates(self) -> tuple[BasketResult, ...]:
        return tuple(r for r in self.results if r.is_arbitrage_claim)

    @property
    def best_candidate(self) -> BasketResult | None:
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


def search_no_basket(
    *,
    relation: RelationCertificate,
    relation_evidence_fingerprint: SettlementEvidenceFingerprint | None,
    members: Sequence[BasketMember],
    context: ExecutionContext,
    fee_quoter: LegFeeQuoter,
    min_quantity: Quantity,
    max_quantity: Quantity,
    at: datetime,
    solver: PortfolioSolver | None = None,
    keep: str = "candidates",
) -> BasketSearch:
    """Evaluate every 0.01 increment in ``[min, max]``, reusing the curves.

    Exhaustive **within the requested interval and nowhere else**. A barren
    sweep licenses only "no proven candidate in [min, max]".
    """
    require_tradeable_quantity(min_quantity)
    require_tradeable_quantity(max_quantity)
    if max_quantity < min_quantity:
        raise ValueError(f"search interval is empty: [{min_quantity}, {max_quantity}]")
    if keep not in {"candidates", "all"}:
        raise ValueError(f"keep must be 'candidates' or 'all', got {keep!r}")

    tickers = tuple(member.ticker for member in members)
    probe = evaluate_basket_quantity(
        relation=relation,
        relation_evidence_fingerprint=relation_evidence_fingerprint,
        members=members,
        context=context,
        fee_quoter=fee_quoter,
        quantity=min_quantity,
        at=at,
        solver=solver,
    )
    if probe.classification in {
        Classification.BLOCKED_SETTLEMENT_SEMANTICS,
        Classification.BLOCKED_BOOK_INTEGRITY,
    }:
        return BasketSearch(
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

    curves: dict[str, ExecutionCurve] = {}
    for member in members:
        curves[member.ticker] = build_execution_curve(
            member.view, member.instrument, context, MarketSide.NO, at=at
        )

    reachable = min(*(curve.max_fillable_quantity for curve in curves.values()), max_quantity)
    warnings: list[str] = []
    if reachable < min_quantity:
        return BasketSearch(
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

    kept: list[BasketResult] = []
    counts: Counter[Classification] = Counter()
    evaluated = 0
    explained = False
    for units in range(min_quantity.units, reachable.units + 1, MIN_QUANTITY_STEP.units):
        result = _evaluate_with_curves(
            relation=relation,
            members=members,
            curves=curves,
            fee_quoter=fee_quoter,
            quantity=Quantity.from_units(units),
            at=at,
            solver=solver,
        )
        evaluated += 1
        counts[result.classification] += 1
        if keep == "all" or result.is_arbitrage_claim:
            kept.append(result)
        elif not explained:
            kept.append(result)
            explained = True

    return BasketSearch(
        event_ticker=relation.event_ticker,
        members=tickers,
        search_min_quantity=min_quantity,
        search_max_quantity=reachable if reachable < max_quantity else max_quantity,
        search_step=MIN_QUANTITY_STEP,
        evaluated_quantity_count=evaluated,
        search_complete=True,
        results=tuple(kept),
        classification_counts=dict(counts),
        warnings=tuple(warnings),
    )
