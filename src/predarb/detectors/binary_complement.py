"""Same-market YES + NO complement detector.

What it proves, and what it does not
------------------------------------
Buying ``q`` YES and ``q`` NO in one binary market holds a portfolio that pays
``q x notional`` whichever way the market settles. If the total acquisition cost
including the worst admissible fee is strictly below that payout, the profit is
positive in **every** allowed settlement state. That is a contractual claim
about the payoff table, proven by enumeration, not a prediction.

It is not a claim that the trade is safe. The two legs are separate orders on a
live venue, so the book can move between them; every live result carries
``RACE_EXPOSED``. Nothing here places an order, and no result may ever report a
locked execution.

Mostly a canary
---------------
Kalshi's matching engine mints complementary pairs, so a persistent, executable,
fee-surviving YES+NO violation in a single market should be rare. If the live
scanner reports many large ones, the first hypotheses are bugs in *our* stack --
complement arithmetic, book inversion, sequence corruption, stale state,
duplicated liquidity, fee handling -- not free money at the venue. The detector
is worth having chiefly because it exercises the entire pipeline end to end
against a payoff invariant that is easy to check by hand.

Purity
------
No network, no database, no clock of its own. Every input is passed in,
including the evaluation instant, so live scanning and replay call exactly this
function and get exactly this answer. The structural requirement, not an
aspiration: the module imports nothing from ``venues``, ``ingest`` or
``storage``, and fees arrive through a callable the caller supplies.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

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
from predarb.domain.costs import FeeBounds, FeesUnavailable, LegFees
from predarb.domain.enums import MarketSide
from predarb.domain.models import VenueInstrument
from predarb.domain.money import Money, Quantity
from predarb.domain.payoff import (
    IncompletePayoffSpecificationError,
    PortfolioPayoff,
    PortfolioSolver,
    SettlementState,
    evaluate_portfolio,
)
from predarb.opportunities.models import (
    Classification,
    CostStatus,
    ExecutionStatus,
    OpportunityIdentity,
    PayoffStatus,
    ProfitInterval,
    SemanticStatus,
)
from predarb.semantics.certificate import SettlementCertificate
from predarb.semantics.fingerprint import SettlementEvidenceFingerprint

__all__ = [
    "BinaryComplementResult",
    "BinaryComplementSearch",
    "LegFeeQuoter",
    "evaluate_quantity",
    "search_binary_complement",
]

MIN_QUANTITY_STEP: Quantity = Quantity.from_value("0.01")
"""The venue's documented minimum quantity granularity (A-43)."""


class LegFeeQuoter(Protocol):
    """Supplies a proven fee interval for one acquisition leg.

    Structural on purpose: a venue adapter satisfies it by having the right
    signature and never imports this module, so the detector stays free of any
    venue import while still consuming venue-specific fee mechanics.
    """

    def __call__(self, quote: ExecutionQuote) -> LegFees: ...


@dataclass(frozen=True, slots=True)
class BinaryComplementResult:
    """One evaluation at one quantity. Immutable, and reproducible from its own
    provenance fields.

    Economic fields are ``None`` whenever the evaluation was blocked. There is
    no zero standing in for "we could not tell".
    """

    identity: OpportunityIdentity
    quantity: Quantity
    classification: Classification
    semantic_status: SemanticStatus
    payoff_status: PayoffStatus
    cost_status: CostStatus
    execution_status: ExecutionStatus

    certificate_identity: str | None = None
    certificate_rules_hash: str | None = None
    certificate_evidence_fingerprint: str | None = None
    current_evidence_fingerprint: str | None = None
    instrument_rules_hash: str | None = None
    allowed_states: tuple[SettlementState, ...] = ()
    portfolio_payoff: PortfolioPayoff | None = None

    yes_quote: ExecutionQuote | None = None
    no_quote: ExecutionQuote | None = None
    gross_yes_cost: Money | None = None
    gross_no_cost: Money | None = None
    total_gross_cost: Money | None = None

    yes_fees: LegFees | None = None
    no_fees: LegFees | None = None
    profit: ProfitInterval | None = None

    blocking_reason: str | None = None
    warnings: tuple[str, ...] = ()

    @property
    def is_arbitrage_claim(self) -> bool:
        return self.classification.is_arbitrage_claim

    @property
    def worst_case_payoff(self) -> Money | None:
        return None if self.portfolio_payoff is None else self.portfolio_payoff.worst_case

    @property
    def liquidity_ids(self) -> tuple[BookLiquidityId, ...]:
        ids: list[BookLiquidityId] = []
        for quote in (self.yes_quote, self.no_quote):
            if quote is not None:
                ids.extend(quote.liquidity_ids)
        return tuple(ids)

    def describe(self) -> str:
        head = f"{self.identity.market_ticker} q={self.quantity}: {self.classification.value}"
        if self.profit is None:
            return f"{head} ({self.blocking_reason or 'no economics computed'})"
        return f"{head} {self.profit.describe()} [{self.execution_status.value}]"


def _blocked(
    *,
    identity: OpportunityIdentity,
    quantity: Quantity,
    classification: Classification,
    reason: str,
    semantic: SemanticStatus = SemanticStatus.VERIFIED,
    execution: ExecutionStatus = ExecutionStatus.RACE_EXPOSED,
    cost: CostStatus = CostStatus.UNAVAILABLE,
    certificate: SettlementCertificate | None = None,
    instrument_rules_hash: str | None = None,
    current_fingerprint: SettlementEvidenceFingerprint | None = None,
    yes_quote: ExecutionQuote | None = None,
    no_quote: ExecutionQuote | None = None,
    yes_fees: LegFees | None = None,
    no_fees: LegFees | None = None,
    portfolio_payoff: PortfolioPayoff | None = None,
) -> BinaryComplementResult:
    """A verdict with no economics attached, and a reason why.

    Every blocked result still carries whatever provenance was established
    before the block, so an audit can see how far the evaluation got.
    """
    return BinaryComplementResult(
        identity=identity,
        quantity=quantity,
        classification=classification,
        semantic_status=semantic,
        payoff_status=PayoffStatus.NOT_EVALUATED,
        cost_status=cost,
        execution_status=execution,
        certificate_identity=None if certificate is None else certificate.identity,
        certificate_rules_hash=None if certificate is None else certificate.rules_hash,
        certificate_evidence_fingerprint=(
            None if certificate is None else certificate.evidence_fingerprint.digest
        ),
        current_evidence_fingerprint=(
            None if current_fingerprint is None else current_fingerprint.digest
        ),
        instrument_rules_hash=instrument_rules_hash,
        allowed_states=() if certificate is None else certificate.allowed_states,
        yes_quote=yes_quote,
        no_quote=no_quote,
        yes_fees=yes_fees,
        no_fees=no_fees,
        portfolio_payoff=portfolio_payoff,
        blocking_reason=reason,
    )


def require_tradeable_quantity(quantity: Quantity) -> None:
    """Reject quantities the venue could not execute.

    :class:`Quantity` already refuses more than two decimal places and refuses
    negatives, so the only thing left to reject is zero. Nothing is ever
    silently rounded onto the grid: a quantity the caller cannot actually trade
    is an error, not something to adjust behind their back.
    """
    if quantity.units <= 0:
        raise ValueError(f"quantity must be at least {MIN_QUANTITY_STEP} contracts, got {quantity}")


def evaluate_quantity(
    *,
    instrument: VenueInstrument,
    view: BookView,
    certificate: SettlementCertificate,
    current_evidence_fingerprint: SettlementEvidenceFingerprint | None,
    context: ExecutionContext,
    fee_quoter: LegFeeQuoter,
    quantity: Quantity,
    at: datetime,
    solver: PortfolioSolver | None = None,
) -> BinaryComplementResult:
    """Authoritative verdict for exactly this quantity. Pure.

    ``current_evidence_fingerprint`` is the fingerprint of the settlement
    evidence as it stands right now. It has no default: a caller must say what
    the current evidence is, and passing ``None`` -- meaning "could not be
    established" -- blocks rather than proceeding.

    The order of checks is deliberate: semantics before economics, and depth
    before fees. A market whose settlement we cannot prove is blocked before
    any number is computed, so an encouraging figure never exists to be quoted
    out of context.
    """
    require_tradeable_quantity(quantity)
    identity = _identity_for(view, instrument, quantity, at)

    # 1. Settlement semantics. Nothing is computed until the payoff table is
    #    proven to apply to the rules currently in force.
    blocking = certificate.blocking_reason(current_fingerprint=current_evidence_fingerprint, at=at)
    if blocking is not None:
        return _blocked(
            identity=identity,
            quantity=quantity,
            classification=Classification.BLOCKED_SETTLEMENT_SEMANTICS,
            reason=blocking,
            semantic=SemanticStatus.BLOCKED,
            certificate=certificate,
            instrument_rules_hash=instrument.rules_hash,
            current_fingerprint=current_evidence_fingerprint,
        )
    if certificate.market_ticker != instrument.ticker:
        return _blocked(
            identity=identity,
            quantity=quantity,
            classification=Classification.BLOCKED_SETTLEMENT_SEMANTICS,
            reason=(f"certificate is for {certificate.market_ticker}, not {instrument.ticker}"),
            semantic=SemanticStatus.BLOCKED,
            certificate=certificate,
            instrument_rules_hash=instrument.rules_hash,
            current_fingerprint=current_evidence_fingerprint,
        )
    if instrument.notional_value is not None and instrument.notional_value != certificate.notional:
        return _blocked(
            identity=identity,
            quantity=quantity,
            classification=Classification.BLOCKED_SETTLEMENT_SEMANTICS,
            reason=(
                f"{instrument.ticker}: instrument notional {instrument.notional_value} "
                f"disagrees with the certified notional {certificate.notional}"
            ),
            semantic=SemanticStatus.BLOCKED,
            certificate=certificate,
            instrument_rules_hash=instrument.rules_hash,
            current_fingerprint=current_evidence_fingerprint,
        )

    # 2. Executable depth, from the book only. Never a displayed or last price.
    try:
        yes_curve = build_execution_curve(view, instrument, context, MarketSide.YES, at=at)
        no_curve = build_execution_curve(view, instrument, context, MarketSide.NO, at=at)
    except BookNotExecutableError as exc:
        return _blocked(
            identity=identity,
            quantity=quantity,
            classification=Classification.BLOCKED_BOOK_INTEGRITY,
            reason=str(exc),
            execution=ExecutionStatus.STALE_OR_INVALID,
            certificate=certificate,
            instrument_rules_hash=instrument.rules_hash,
            current_fingerprint=current_evidence_fingerprint,
        )
    except UnsupportedExecutionSemanticsError as exc:
        return _blocked(
            identity=identity,
            quantity=quantity,
            classification=Classification.BLOCKED_SETTLEMENT_SEMANTICS,
            reason=str(exc),
            semantic=SemanticStatus.BLOCKED,
            certificate=certificate,
            instrument_rules_hash=instrument.rules_hash,
            current_fingerprint=current_evidence_fingerprint,
        )

    return _evaluate_with_curves(
        instrument=instrument,
        certificate=certificate,
        current_fingerprint=current_evidence_fingerprint,
        yes_curve=yes_curve,
        no_curve=no_curve,
        fee_quoter=fee_quoter,
        quantity=quantity,
        at=at,
        identity=identity,
        solver=solver,
    )


def _evaluate_with_curves(
    *,
    instrument: VenueInstrument,
    certificate: SettlementCertificate,
    current_fingerprint: SettlementEvidenceFingerprint | None,
    yes_curve: ExecutionCurve,
    no_curve: ExecutionCurve,
    fee_quoter: LegFeeQuoter,
    quantity: Quantity,
    at: datetime,
    identity: OpportunityIdentity,
    solver: PortfolioSolver | None,
) -> BinaryComplementResult:
    """The quantity-dependent half, split out so a search can reuse the curves.

    Curves are immutable and derived once per book state; re-deriving them per
    quantity would be pure waste and would also risk evaluating different
    quantities against subtly different inputs.
    """
    # 3. Both legs must fully fill. A partial fill is a different portfolio,
    #    and half of a complement has a state-dependent payoff.
    yes_quote = yes_curve.quote_up_to(quantity, at=at)
    no_quote = no_curve.quote_up_to(quantity, at=at)
    if not yes_quote.fully_fillable or not no_quote.fully_fillable:
        return _blocked(
            identity=identity,
            quantity=quantity,
            classification=Classification.INSUFFICIENT_DEPTH,
            reason=(
                f"requested {quantity}; displayed depth fills "
                f"YES {yes_quote.filled_quantity}, NO {no_quote.filled_quantity}"
            ),
            execution=ExecutionStatus.DEPTH_VERIFIED,
            certificate=certificate,
            instrument_rules_hash=instrument.rules_hash,
            current_fingerprint=current_fingerprint,
            yes_quote=yes_quote,
            no_quote=no_quote,
        )

    # 4. The two legs must not consume the same resting liquidity. Buying YES
    #    crosses NO bids and vice versa, so they normally cannot collide -- but
    #    "normally" is not a guarantee, and double-counting one aggregate level
    #    would manufacture depth that does not exist.
    collisions = detect_liquidity_collisions((yes_curve, no_curve), up_to=quantity)
    if collisions:
        return _blocked(
            identity=identity,
            quantity=quantity,
            classification=Classification.BLOCKED_LIQUIDITY_COLLISION,
            reason=(
                f"{len(collisions)} source level(s) would be consumed by both legs: "
                f"{', '.join(c.describe() for c in collisions[:3])}"
            ),
            certificate=certificate,
            instrument_rules_hash=instrument.rules_hash,
            current_fingerprint=current_fingerprint,
            yes_quote=yes_quote,
            no_quote=no_quote,
        )

    # 5. Payoff, from the certificate's own tables, over its own allowed states.
    positions = certificate.positions_for(yes_quantity=quantity, no_quantity=quantity)
    try:
        payoff = evaluate_portfolio(positions, certificate.allowed_states, solver=solver)
    except IncompletePayoffSpecificationError as exc:
        return _blocked(
            identity=identity,
            quantity=quantity,
            classification=Classification.BLOCKED_SETTLEMENT_SEMANTICS,
            reason=str(exc),
            semantic=SemanticStatus.BLOCKED,
            certificate=certificate,
            instrument_rules_hash=instrument.rules_hash,
            current_fingerprint=current_fingerprint,
            yes_quote=yes_quote,
            no_quote=no_quote,
        )

    # 6. Fees: two separate hypothetical orders, so two independent accumulators.
    yes_fees = fee_quoter(yes_quote)
    no_fees = fee_quoter(no_quote)
    fee_block = _fee_blocking_reason(yes_fees, no_fees)
    if fee_block is not None:
        return _blocked(
            identity=identity,
            quantity=quantity,
            classification=Classification.BLOCKED_FEE_SEMANTICS,
            reason=fee_block,
            certificate=certificate,
            instrument_rules_hash=instrument.rules_hash,
            current_fingerprint=current_fingerprint,
            yes_quote=yes_quote,
            no_quote=no_quote,
            yes_fees=yes_fees,
            no_fees=no_fees,
            portfolio_payoff=payoff,
        )
    assert isinstance(yes_fees, FeeBounds)
    assert isinstance(no_fees, FeeBounds)

    gross = yes_quote.gross_cost + no_quote.gross_cost
    interval = ProfitInterval(
        worst_case_payoff=payoff.worst_case,
        gross_cost=gross,
        fee_lower=yes_fees.lower + no_fees.lower,
        fee_upper=yes_fees.upper + no_fees.upper,
    )
    payoff_status = interval.payoff_status()
    classification = {
        PayoffStatus.STATE_INDEPENDENT_PROFIT: Classification.PROVEN_CONTRACTUAL_ARBITRAGE,
        PayoffStatus.NON_POSITIVE: Classification.PROVEN_NOT_PROFITABLE,
        PayoffStatus.INDETERMINATE: Classification.INDETERMINATE_COST_BOUNDS,
    }[payoff_status]

    warnings: list[str] = []
    if classification is Classification.PROVEN_CONTRACTUAL_ARBITRAGE:
        warnings.append(
            "payoff proof holds only once BOTH legs are filled; the two orders are "
            "not atomic, so this is race-exposed and not a locked profit"
        )
    if not payoff.is_state_independent:
        warnings.append(f"payoff varies by state ({payoff.describe()}); the worst case was used")
    warnings.extend(yes_fees.warnings)
    warnings.extend(no_fees.warnings)

    return BinaryComplementResult(
        identity=identity,
        quantity=quantity,
        classification=classification,
        semantic_status=SemanticStatus.VERIFIED,
        payoff_status=payoff_status,
        cost_status=CostStatus.EXACT if (yes_fees.exact and no_fees.exact) else CostStatus.BOUNDED,
        # Depth was verified, but acquiring two legs is still a race. The
        # stronger-sounding DEPTH_VERIFIED is reserved for results that make no
        # profitability claim at all.
        execution_status=ExecutionStatus.RACE_EXPOSED,
        portfolio_payoff=payoff,
        yes_quote=yes_quote,
        no_quote=no_quote,
        gross_yes_cost=yes_quote.gross_cost,
        gross_no_cost=no_quote.gross_cost,
        total_gross_cost=gross,
        yes_fees=yes_fees,
        no_fees=no_fees,
        profit=interval,
        warnings=tuple(warnings),
        certificate_identity=certificate.identity,
        certificate_rules_hash=certificate.rules_hash,
        certificate_evidence_fingerprint=certificate.evidence_fingerprint.digest,
        current_evidence_fingerprint=(
            None if current_fingerprint is None else current_fingerprint.digest
        ),
        instrument_rules_hash=instrument.rules_hash,
        allowed_states=certificate.allowed_states,
    )


def _fee_blocking_reason(yes_fees: LegFees, no_fees: LegFees) -> str | None:
    """Why fees cannot support a verdict, or ``None`` if they can.

    An unavailable fee is never treated as zero, and a fee that cannot support
    an arbitrage claim blocks the whole evaluation rather than quietly producing
    a number that reads as proven.
    """
    for side, fees in (("YES", yes_fees), ("NO", no_fees)):
        if isinstance(fees, FeesUnavailable):
            return f"{side} leg: {fees.reason}"
        if not fees.supports_arbitrage_claim:
            return f"{side} leg: fee figure cannot support a contractual claim ({fees.provenance})"
    return None


def _identity_for(
    view: BookView, instrument: VenueInstrument, quantity: Quantity, at: datetime
) -> OpportunityIdentity:
    provenance = view.provenance
    return OpportunityIdentity(
        detected_at=at,
        market_ticker=instrument.ticker,
        quantity=quantity,
        connection_epoch=provenance.connection_epoch,
        sid=provenance.sid,
        book_seq=provenance.latest_seq,
        snapshot_raw_id=provenance.snapshot_raw_id,
        latest_raw_id=provenance.latest_raw_id,
    )


@dataclass(frozen=True, slots=True)
class BinaryComplementSearch:
    """Results of a bounded exhaustive sweep, with its own limits attached.

    ``search_complete`` is true for the **interval that was searched**, never
    for the whole feasible domain. A caller that reports "no arbitrage" from
    this object without also reporting the interval is misquoting it, which is
    why the interval travels with the answer.
    """

    market_ticker: str
    search_min_quantity: Quantity
    search_max_quantity: Quantity
    search_step: Quantity
    evaluated_quantity_count: int
    search_complete: bool
    results: tuple[BinaryComplementResult, ...]
    classification_counts: Mapping[Classification, int] = field(default_factory=dict)
    """Tally over **every** evaluation, not just the retained ones.

    Retention is a memory decision; the funnel is a finding. Deriving counts
    from ``results`` would silently under-report whenever ``keep`` discards
    anything, which is the default."""

    warnings: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        tallied = sum(self.classification_counts.values())
        if self.classification_counts and tallied != self.evaluated_quantity_count:
            raise ValueError(
                f"classification counts sum to {tallied} but "
                f"{self.evaluated_quantity_count} quantities were evaluated"
            )

    @property
    def proven_candidates(self) -> tuple[BinaryComplementResult, ...]:
        return tuple(r for r in self.results if r.is_arbitrage_claim)

    @property
    def best_candidate(self) -> BinaryComplementResult | None:
        """The proven candidate with the largest guaranteed profit floor."""
        candidates = self.proven_candidates
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda r: r.profit.profit_lower_bound.units if r.profit else 0,
        )

    def summary(self) -> str:
        found = len(self.proven_candidates)
        scope = f"[{self.search_min_quantity}, {self.search_max_quantity}] step {self.search_step}"
        if found:
            return f"{self.market_ticker}: {found} proven candidate(s) in {scope}"
        return f"{self.market_ticker}: no proven candidate in {scope}"


def search_binary_complement(
    *,
    instrument: VenueInstrument,
    view: BookView,
    certificate: SettlementCertificate,
    current_evidence_fingerprint: SettlementEvidenceFingerprint | None,
    context: ExecutionContext,
    fee_quoter: LegFeeQuoter,
    min_quantity: Quantity,
    max_quantity: Quantity,
    at: datetime,
    solver: PortfolioSolver | None = None,
    keep: str = "candidates",
) -> BinaryComplementSearch:
    """Evaluate every 0.01-contract quantity in ``[min, max]``.

    Exhaustive **within the requested interval and nowhere else**. A search that
    finds nothing licenses exactly one sentence: "no proven candidate in
    [min, max]". It does not license "no arbitrage exists", which would require
    covering the entire feasible domain with every semantic and cost input
    proven.

    The caller chooses the interval. There is no built-in economic cap, because
    any universal cap would be an invented assumption about what size is worth
    looking at.

    ``keep`` controls retention: ``"candidates"`` keeps only proven ones plus
    the first blocked result (enough to explain a barren sweep), ``"all"`` keeps
    every evaluation. Sweeps can be long, and holding every quote would make
    memory the limiting factor rather than the venue.
    """
    require_tradeable_quantity(min_quantity)
    require_tradeable_quantity(max_quantity)
    if max_quantity < min_quantity:
        raise ValueError(f"search interval is empty: [{min_quantity}, {max_quantity}]")
    if keep not in {"candidates", "all"}:
        raise ValueError(f"keep must be 'candidates' or 'all', got {keep!r}")

    identity = _identity_for(view, instrument, min_quantity, at)
    warnings: list[str] = []

    blocking = certificate.blocking_reason(current_fingerprint=current_evidence_fingerprint, at=at)
    if blocking is not None:
        return BinaryComplementSearch(
            market_ticker=instrument.ticker,
            search_min_quantity=min_quantity,
            search_max_quantity=max_quantity,
            search_step=MIN_QUANTITY_STEP,
            evaluated_quantity_count=0,
            search_complete=False,
            results=(
                _blocked(
                    identity=identity,
                    quantity=min_quantity,
                    classification=Classification.BLOCKED_SETTLEMENT_SEMANTICS,
                    reason=blocking,
                    semantic=SemanticStatus.BLOCKED,
                    certificate=certificate,
                    instrument_rules_hash=instrument.rules_hash,
                    current_fingerprint=current_evidence_fingerprint,
                ),
            ),
            warnings=("search not performed: settlement semantics are unproven",),
        )

    try:
        yes_curve = build_execution_curve(view, instrument, context, MarketSide.YES, at=at)
        no_curve = build_execution_curve(view, instrument, context, MarketSide.NO, at=at)
    except (BookNotExecutableError, UnsupportedExecutionSemanticsError) as exc:
        classification = (
            Classification.BLOCKED_BOOK_INTEGRITY
            if isinstance(exc, BookNotExecutableError)
            else Classification.BLOCKED_SETTLEMENT_SEMANTICS
        )
        return BinaryComplementSearch(
            market_ticker=instrument.ticker,
            search_min_quantity=min_quantity,
            search_max_quantity=max_quantity,
            search_step=MIN_QUANTITY_STEP,
            evaluated_quantity_count=0,
            search_complete=False,
            results=(
                _blocked(
                    identity=identity,
                    quantity=min_quantity,
                    classification=classification,
                    reason=str(exc),
                    execution=ExecutionStatus.STALE_OR_INVALID,
                    certificate=certificate,
                    instrument_rules_hash=instrument.rules_hash,
                    current_fingerprint=current_evidence_fingerprint,
                ),
            ),
            warnings=("search not performed: the book could not be quoted",),
        )

    # Depth caps the useful range. Quantities beyond it can only be
    # INSUFFICIENT_DEPTH, so evaluating them would burn time to restate that.
    reachable = min(yes_curve.max_fillable_quantity, no_curve.max_fillable_quantity, max_quantity)
    if reachable < min_quantity:
        # Nothing in the requested interval is executable. Reporting a
        # "complete" search over [min, reachable] would print an inverted
        # interval and imply a sweep that never happened.
        return BinaryComplementSearch(
            market_ticker=instrument.ticker,
            search_min_quantity=min_quantity,
            search_max_quantity=min_quantity,
            search_step=MIN_QUANTITY_STEP,
            evaluated_quantity_count=0,
            search_complete=False,
            results=(),
            classification_counts={},
            warnings=(
                f"no quantity in [{min_quantity}, {max_quantity}] is executable: "
                f"displayed depth supports only {reachable}",
            ),
        )
    if reachable < max_quantity:
        warnings.append(
            f"displayed depth supports only {reachable}; quantities above it were not "
            f"evaluated and are reported as outside the searched interval"
        )

    kept: list[BinaryComplementResult] = []
    counts: Counter[Classification] = Counter()
    evaluated = 0
    blocked_seen = False
    step = MIN_QUANTITY_STEP.units
    for units in range(min_quantity.units, reachable.units + 1, step):
        result = _evaluate_with_curves(
            instrument=instrument,
            certificate=certificate,
            current_fingerprint=current_evidence_fingerprint,
            yes_curve=yes_curve,
            no_curve=no_curve,
            fee_quoter=fee_quoter,
            quantity=Quantity.from_units(units),
            at=at,
            identity=_identity_for(view, instrument, Quantity.from_units(units), at),
            solver=solver,
        )
        evaluated += 1
        counts[result.classification] += 1
        if keep == "all" or result.is_arbitrage_claim:
            kept.append(result)
        elif not blocked_seen:
            kept.append(result)
            blocked_seen = True

    return BinaryComplementSearch(
        market_ticker=instrument.ticker,
        search_min_quantity=min_quantity,
        # Report the interval actually covered, not the one requested.
        search_max_quantity=reachable if reachable < max_quantity else max_quantity,
        search_step=MIN_QUANTITY_STEP,
        evaluated_quantity_count=evaluated,
        search_complete=True,
        results=tuple(kept),
        classification_counts=dict(counts),
        warnings=tuple(warnings),
    )
