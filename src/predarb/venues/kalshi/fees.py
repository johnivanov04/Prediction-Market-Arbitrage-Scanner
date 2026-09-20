"""Pre-execution fee estimation from an :class:`ExecutionQuote`.

Why this cannot be exact
------------------------
Step 5 produces fill *slices*, one per L2 price level. A price level is
**aggregate depth**, not a fill: it may contain one resting order or a hundred,
and a taker order crossing it produces one exchange fill per resting order it
matches. The book does not reveal that fragmentation, and the fee mechanics are
per-fill with an order-scoped accumulator.

So the exact fee is unknowable pre-trade. It is, however, **bounded**, and that
is the useful result.

What fragmentation can and cannot change
----------------------------------------
Derivations live in :mod:`predarb.venues.kalshi.fee_model` and ``docs/fees.md``.

* The **raw model fee is exactly fragmentation-independent** -- the formula is
  linear in quantity, so any split sums to the same value.
* The **total net fee is bounded on both sides**, because fill count is bounded:
  the venue's documented 0.01-contract minimum granularity means an order of
  quantity ``Q`` cannot split into more than ``Q / 0.01`` fills.

      ceil_6dp(F)  <=  net_total  <=  ceil_6dp(F) + k_max·B - μ

  Both ends are proven, not sampled.
* The net fee is **not invariant** to fragmentation, for direct and non-direct
  members alike. The upper bound is wide -- for a non-direct member it can
  exceed the position's own notional -- but it is finite and provable, which is
  what a detector needs to reason about.

A wide proven bound lets a detector say "not provably riskless". No bound at
all would force it to say nothing.

Unknown is not zero
-------------------
When a fee cannot be established, this module returns :class:`UnavailableFee`,
which carries **no monetary fields at all**. It is deliberately not a quote with
zeros in it: zero is a real, reachable fee (``fee_multiplier = 0`` is live on
14 series), so a zero standing in for "unknown" is a number a caller would
subtract by accident.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from predarb.books.execution import ExecutionQuote
from predarb.domain.costs import FeeBounds, FeesUnavailable, LegFees
from predarb.domain.enums import Liquidity
from predarb.domain.fees import FeeConfiguration, ResolvedFeeConfiguration
from predarb.domain.money import Money, Quantity
from predarb.venues.kalshi.fee_engine import (
    AccumulatorState,
    Fill,
    FillFeeResult,
    apply_fills,
    total_net_fee,
    total_trade_fee,
)
from predarb.venues.kalshi.fee_model import (
    BalancePrecision,
    UnsupportedFeeTypeError,
    max_fill_count,
    net_fee_bounds,
    raw_model_fee,
)

__all__ = [
    "Exactness",
    "FeeEstimate",
    "FeeQuote",
    "MultiLegFeeEstimate",
    "MultiplierStatus",
    "SegmentationAssumption",
    "UnavailableFee",
    "estimate_fees",
    "estimate_multi_leg_fees",
    "leg_fee_bounds",
]


class SegmentationAssumption(StrEnum):
    """How an execution quote's levels were mapped onto hypothetical fills."""

    ONE_FILL_PER_PRICE_LEVEL = "ONE_FILL_PER_PRICE_LEVEL"
    """Each L2 slice treated as a single exchange fill.

    Deterministic and the natural default, but an *assumption*. It is the
    minimum-fragmentation case, so the resulting point estimate is the lowest
    plausible fee, and it is never the exact executable fee."""

    ACTUAL_FILLS = "ACTUAL_FILLS"
    """Real fill segmentation was supplied. Only then is the result exact."""


class Exactness(StrEnum):
    """How much the caller may rely on a fee figure.

    Machine-readable on purpose: a detector has to branch on this, and a
    human-readable warning string cannot be branched on safely.

    There is deliberately no ``UNBOUNDED`` member. Fill count is provably finite
    for any known quantity, and a bound valid for both documented member classes
    can always be produced, so every computable quote is at least bounded. A
    configuration we cannot compute is represented by :class:`UnavailableFee`
    instead, which is a different type rather than a weaker enum value.
    """

    EXACT_ACTUAL_FILLS = "EXACT_ACTUAL_FILLS"
    """Computed from a known fill stream. Exact; bounds collapse to the value."""

    BOUNDED_ESTIMATE = "BOUNDED_ESTIMATE"
    """A point estimate plus proven lower and upper bounds on the true fee."""


class MultiplierStatus(StrEnum):
    """Whether the series' ``fee_multiplier`` has established taker semantics.

    The fee schedule presents maker and taker multipliers in separate columns;
    the API exposes a single ``fee_multiplier``. Which column it corresponds to
    is not stated anywhere reachable (``docs/api_assumptions.md`` A-14).
    """

    IDENTITY = "IDENTITY"
    """``M = 1``. Every reading of the schedule agrees, so the ambiguity cannot
    change the number. Live on 14,134 of 14,167 listed series."""

    UNRESOLVED_MAPPING = "UNRESOLVED_MAPPING"
    """``M != 1``. The computed figure depends on an unproven mapping, so it
    must not support a contractual-arbitrage claim. The number is still
    reported, as a hypothetical."""


def _multiplier_status(configuration: FeeConfiguration) -> MultiplierStatus:
    if configuration.multiplier == 1:
        return MultiplierStatus.IDENTITY
    return MultiplierStatus.UNRESOLVED_MAPPING


@dataclass(frozen=True, slots=True)
class UnavailableFee:
    """A leg whose fee could not be established.

    Carries no monetary fields, on purpose. Any arithmetic a caller attempts
    fails loudly -- at type-check time under mypy, and with ``AttributeError``
    at runtime -- rather than silently contributing zero to a total.
    """

    market_ticker: str
    effective_config: ResolvedFeeConfiguration
    requested_quantity: Quantity
    filled_quantity: Quantity
    reason: str
    warnings: tuple[str, ...] = ()

    @property
    def is_exact(self) -> bool:
        return False

    @property
    def supports_arbitrage_claim(self) -> bool:
        return False

    def describe(self) -> str:
        return f"{self.market_ticker}: fee unavailable -- {self.reason}"


@dataclass(frozen=True, slots=True)
class FeeQuote:
    """Fee information for one acquisition leg, with its reliability attached.

    Every monetary field is a real figure. A configuration that cannot produce
    real figures yields :class:`UnavailableFee` instead.
    """

    market_ticker: str
    effective_config: ResolvedFeeConfiguration
    balance_precision: BalancePrecision
    requested_quantity: Quantity
    filled_quantity: Quantity
    raw_model_fee: Decimal
    """Exact and fragmentation-independent. The one number stateable precisely
    before execution."""

    estimated_trade_fee: Money
    estimated_rounding_fee: Money
    estimated_rebate: Money
    estimated_net_fee: Money
    lower_bound_net_fee: Money
    """``ceil_6dp(F)``. Proven for any fragmentation and any number of levels."""

    upper_bound_net_fee: Money
    """``ceil_6dp(F) + k_max·B - μ``. Proven; wide, but finite."""

    max_fill_count: int
    """``Q / 0.01`` -- the documented granularity bound on fill count."""

    segmentation: SegmentationAssumption
    exactness: Exactness
    multiplier_status: MultiplierStatus
    fill_results: tuple[FillFeeResult, ...]
    final_accumulator: AccumulatorState
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.lower_bound_net_fee > self.upper_bound_net_fee:
            raise ValueError(
                f"lower bound {self.lower_bound_net_fee} exceeds upper bound "
                f"{self.upper_bound_net_fee}; the derivation is wrong"
            )
        if not (self.lower_bound_net_fee <= self.estimated_net_fee <= self.upper_bound_net_fee):
            raise ValueError(
                f"point estimate {self.estimated_net_fee} lies outside its own bounds "
                f"[{self.lower_bound_net_fee}, {self.upper_bound_net_fee}]"
            )

    @property
    def is_exact(self) -> bool:
        return self.exactness is Exactness.EXACT_ACTUAL_FILLS

    @property
    def supports_arbitrage_claim(self) -> bool:
        """Whether a contractual-arbitrage claim may rely on this.

        Requires a usable bound *and* resolved multiplier semantics. A bound
        derived through an unproven maker/taker mapping is not a proven bound.
        """
        return self.multiplier_status is MultiplierStatus.IDENTITY

    @property
    def bound_width(self) -> Money:
        return self.upper_bound_net_fee - self.lower_bound_net_fee

    def describe(self) -> str:
        return (
            f"{self.market_ticker}: raw={self.raw_model_fee} "
            f"net~{self.estimated_net_fee} in "
            f"[{self.lower_bound_net_fee}, {self.upper_bound_net_fee}] "
            f"[{self.exactness.value}/{self.multiplier_status.value}]"
        )


FeeEstimate = FeeQuote | UnavailableFee
"""One leg's fee result. Callers must narrow before touching any amount."""


def estimate_fees(
    quote: ExecutionQuote,
    resolved: ResolvedFeeConfiguration,
    precision: BalancePrecision,
    *,
    actual_fills: Sequence[Fill] | None = None,
) -> FeeEstimate:
    """Estimate taker fees for one execution quote.

    Pass ``actual_fills`` when the real segmentation is known -- from a
    post-trade record or a replay -- and the result is exact. Otherwise each
    price level is treated as one fill for the point estimate, and proven
    bounds are attached that hold for *every* segmentation.

    Pass ``BalancePrecision.unknown_member()`` when the account's member class
    is unknown; the resulting bounds are valid for both documented classes.
    """
    warnings: list[str] = []
    configuration = resolved.configuration

    if not resolved.is_resolvable:
        return _unavailable(
            quote,
            resolved,
            reason=(
                f"fee type {configuration.fee_type_raw!r} is not in the documented enum; "
                "its taker semantics are unknown"
            ),
        )

    if actual_fills is not None:
        fills = tuple(actual_fills)
        segmentation = SegmentationAssumption.ACTUAL_FILLS
    else:
        fills = tuple(
            Fill(
                price=slice_.execution_price,
                quantity=slice_.quantity,
                configuration=configuration,
                liquidity=Liquidity.TAKER,
            )
            for slice_ in quote.slices
        )
        segmentation = SegmentationAssumption.ONE_FILL_PER_PRICE_LEVEL

    try:
        results, final_state = apply_fills(fills, precision)
    except UnsupportedFeeTypeError as exc:
        return _unavailable(quote, resolved, reason=str(exc))

    exact_raw = sum(
        (
            raw_model_fee(price=fill.price, quantity=fill.quantity, configuration=configuration)
            for fill in fills
        ),
        Decimal(0),
    )
    net = total_net_fee(results)
    filled = Quantity.from_units(sum(fill.quantity.units for fill in fills))

    if segmentation is SegmentationAssumption.ACTUAL_FILLS:
        exactness = Exactness.EXACT_ACTUAL_FILLS
        lower = upper = net
        fill_ceiling = len(fills)
    else:
        exactness = Exactness.BOUNDED_ESTIMATE
        lower, upper = net_fee_bounds(
            total_raw_model_fee=exact_raw, quantity=filled, precision=precision
        )
        fill_ceiling = max_fill_count(filled)
        warnings.append(
            "point estimate assumes one exchange fill per L2 price level; a level is "
            "aggregate depth and may execute as many fills, which can only increase fees"
        )
        warnings.append(
            f"true net fee proven to lie in [{lower.to_str()}, {upper.to_str()}] "
            f"for any of the up to {fill_ceiling} fills this quantity permits at "
            f"{precision.describe()}"
        )

    status = _multiplier_status(configuration)
    if status is MultiplierStatus.UNRESOLVED_MAPPING:
        warnings.append(
            f"fee_multiplier is {configuration.multiplier}, not 1; the schedule's "
            "maker/taker column mapping for this field is unproven (A-14), so this "
            "figure is a hypothetical and must not support an arbitrage claim"
        )
    if precision.assumed:
        warnings.append(
            "member class unknown; bounds use the coarser non-direct grid and are "
            "valid for both documented classes"
        )

    return FeeQuote(
        market_ticker=quote.market_ticker,
        effective_config=resolved,
        balance_precision=precision,
        requested_quantity=quote.requested_quantity,
        filled_quantity=quote.filled_quantity,
        raw_model_fee=exact_raw,
        estimated_trade_fee=total_trade_fee(results),
        estimated_rounding_fee=Money.from_units(sum(r.rounding_fee.units for r in results)),
        estimated_rebate=Money.from_units(sum(r.rebate.units for r in results)),
        estimated_net_fee=net,
        lower_bound_net_fee=lower,
        upper_bound_net_fee=upper,
        max_fill_count=fill_ceiling,
        segmentation=segmentation,
        exactness=exactness,
        multiplier_status=status,
        fill_results=results,
        final_accumulator=final_state,
        warnings=tuple(warnings),
    )


def _unavailable(
    quote: ExecutionQuote, resolved: ResolvedFeeConfiguration, *, reason: str
) -> UnavailableFee:
    return UnavailableFee(
        market_ticker=quote.market_ticker,
        effective_config=resolved,
        requested_quantity=quote.requested_quantity,
        filled_quantity=quote.filled_quantity,
        reason=reason,
        warnings=(reason, "no fee figure exists for this configuration; it is not zero"),
    )


@dataclass(frozen=True, slots=True)
class MultiLegFeeEstimate:
    """Fees across the legs of one strategy.

    Each leg is its own order, so each gets its **own** accumulator. Sharing one
    between hypothetical orders would invent rebate timing the exchange would
    not produce.

    Totals are ``None`` whenever any leg is unavailable. A partial total would
    be a number that silently omits a leg.
    """

    legs: tuple[FeeEstimate, ...]
    total_raw_model_fee: Decimal | None
    total_estimated_net_fee: Money | None
    total_lower_bound: Money | None
    total_upper_bound: Money | None
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def available_legs(self) -> tuple[FeeQuote, ...]:
        return tuple(leg for leg in self.legs if isinstance(leg, FeeQuote))

    @property
    def unavailable_legs(self) -> tuple[UnavailableFee, ...]:
        return tuple(leg for leg in self.legs if isinstance(leg, UnavailableFee))

    @property
    def is_complete(self) -> bool:
        return bool(self.legs) and not self.unavailable_legs

    @property
    def supports_arbitrage_claim(self) -> bool:
        return self.is_complete and all(leg.supports_arbitrage_claim for leg in self.legs)


def estimate_multi_leg_fees(
    quotes: Sequence[ExecutionQuote],
    configs: Sequence[ResolvedFeeConfiguration],
    precision: BalancePrecision,
) -> MultiLegFeeEstimate:
    """Fee every leg independently and aggregate.

    Bounds add: if each leg's true fee lies in its own interval, the total lies
    in the sum of the intervals. Totals exist only when every leg is available.
    """
    if len(quotes) != len(configs):
        raise ValueError(
            f"got {len(quotes)} quotes and {len(configs)} fee configs; "
            "each leg needs its own resolved configuration"
        )

    legs: tuple[FeeEstimate, ...] = tuple(
        estimate_fees(quote, config, precision)
        for quote, config in zip(quotes, configs, strict=True)
    )
    available = tuple(leg for leg in legs if isinstance(leg, FeeQuote))
    complete = bool(legs) and len(available) == len(legs)

    warnings: list[str] = []
    if not legs:
        warnings.append("no legs supplied; there is nothing to aggregate")
    elif not complete:
        missing = [leg.market_ticker for leg in legs if isinstance(leg, UnavailableFee)]
        warnings.append(
            f"no totals: fees are unavailable for {', '.join(missing)}; "
            "a partial total would omit a leg without saying so"
        )
    elif any(not leg.supports_arbitrage_claim for leg in available):
        warnings.append(
            "aggregate cannot support an arbitrage claim; at least one leg has "
            "unresolved fee_multiplier semantics (A-14)"
        )

    if not complete:
        return MultiLegFeeEstimate(
            legs=legs,
            total_raw_model_fee=None,
            total_estimated_net_fee=None,
            total_lower_bound=None,
            total_upper_bound=None,
            warnings=tuple(warnings),
        )

    return MultiLegFeeEstimate(
        legs=legs,
        total_raw_model_fee=sum((leg.raw_model_fee for leg in available), Decimal(0)),
        total_estimated_net_fee=Money.from_units(
            sum(leg.estimated_net_fee.units for leg in available)
        ),
        total_lower_bound=Money.from_units(sum(leg.lower_bound_net_fee.units for leg in available)),
        total_upper_bound=Money.from_units(sum(leg.upper_bound_net_fee.units for leg in available)),
        warnings=tuple(warnings),
    )


def leg_fee_bounds(
    quote: ExecutionQuote,
    resolved: ResolvedFeeConfiguration,
    precision: BalancePrecision,
    *,
    actual_fills: Sequence[Fill] | None = None,
) -> LegFees:
    """Adapt a Kalshi fee estimate to the venue-neutral cost vocabulary.

    This is the seam that lets a detector consume Kalshi fees without importing
    anything Kalshi-shaped. It is a plain function matching the structural
    signature detectors expect, so nothing here depends on the detector layer
    either -- the dependency runs in neither direction.

    An unresolved ``fee_multiplier`` yields real numbers with
    ``supports_arbitrage_claim = False`` rather than an unavailable result: the
    figures are sound as a hypothetical, they merely cannot carry a proof.
    """
    estimate = estimate_fees(quote, resolved, precision, actual_fills=actual_fills)
    if isinstance(estimate, UnavailableFee):
        return FeesUnavailable(
            reason=estimate.reason,
            provenance=estimate.effective_config.provenance,
            warnings=estimate.warnings,
        )
    return FeeBounds(
        lower=estimate.lower_bound_net_fee,
        upper=estimate.upper_bound_net_fee,
        exact=estimate.is_exact,
        supports_arbitrage_claim=estimate.supports_arbitrage_claim,
        provenance=(
            f"{estimate.effective_config.provenance}; "
            f"{estimate.exactness.value}; {estimate.multiplier_status.value}; "
            f"{precision.describe()}; k_max={estimate.max_fill_count}"
        ),
        warnings=estimate.warnings,
    )
