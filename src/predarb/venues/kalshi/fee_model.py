"""The Kalshi taker fee model and its documented rounding mechanics.

Five distinct quantities
------------------------
Conflating any two of these produces a wrong number, so they are separate types
and separate steps::

    raw_model_fee   M * rate * C * P * (1 - P)      real-valued, never rounded
    trade_fee       ceil_6dp(raw_model_fee)          charged on the fill
    rounding_fee    realigns the balance to the member's precision grid
    rebate          returned from the per-order accumulator
    net_fee         trade_fee + rounding_fee - rebate, floored at zero

``raw_model_fee`` is deliberately a ``Decimal`` rather than ``Money``: it
routinely carries more than six decimal places (the documented worked example
uses ``$0.00363825``), and it becomes money exactly once, at the documented
ceiling step.

Per fill, not per order
-----------------------
``trade_fee`` and the rounding mechanics apply to **every fill**, while the
rounding accumulator persists **across an order's fills**. That asymmetry is
what makes a pre-trade estimate inexact: an L2 price level is aggregate depth,
not a fill, so we cannot know in advance how many fills a level will produce.
See :mod:`predarb.venues.kalshi.fees` for how that uncertainty is represented
rather than hidden.

Taker only
----------
Phase 1 acquires immediately from resting liquidity, so only the taker model is
implemented. The maker model exists and is documented, but maker execution
brings queue position, fill uncertainty and possibly different fee types --
none of which the current immediate-execution model covers. The types here keep
a ``Liquidity`` axis so maker support can be added without reshaping them.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from predarb.domain.enums import FeeType, Liquidity
from predarb.domain.fees import FeeConfiguration
from predarb.domain.money import Money, Price, Quantity

__all__ = [
    "MIN_QUANTITY_INCREMENT",
    "QUADRATIC_MAKER_RATE",
    "QUADRATIC_TAKER_RATE",
    "SUPPORTED_TAKER_FEE_TYPES",
    "TRADE_FEE_STEP",
    "BalancePrecision",
    "UnsupportedFeeTypeError",
    "max_fill_count",
    "max_rounding_fee_per_fill",
    "net_fee_bounds",
    "net_fee_lower_bound",
    "net_fee_upper_bound",
    "raw_model_fee",
    "signed_revenue_for_buy",
    "taker_rate_for",
    "trade_fee_from",
]

MIN_QUANTITY_INCREMENT: Final = Quantity.from_value("0.01")
"""Smallest quantity the venue will trade.

DOCUMENTED: "Minimum granularity is 0.01 contracts"
(docs.kalshi.com/getting_started/fixed_point_migration).

This is what makes fill fragmentation **finite**. A positive fill is a whole
number of these increments, so an order of quantity Q cannot be split into more
than ``Q / 0.01`` fills -- which turns the per-fill rounding exposure from an
unbounded worry into an arithmetic bound. See :func:`max_fill_count`.
"""

TRADE_FEE_STEP: Final = Money.from_units(1)
"""One micro-dollar: the ``ceil_6dp`` grid that trade fees are charged on."""

QUADRATIC_TAKER_RATE: Final = Decimal("0.07")
"""Taker rate in the general quadratic model.

Source: the official Kalshi Fee Schedule (effective 2026-07-07). That PDF is
unreachable from this environment (HTTP 429), so the constant is recorded as a
**reviewer-supplied official-document finding** -- see ``docs/api_assumptions.md``
A-14. It is not derived from anything the API exposes, and it is not something
this code can re-verify on its own.
"""

QUADRATIC_MAKER_RATE: Final = Decimal("0.0175")
"""Maker rate, recorded for completeness. Phase 1 never applies it."""

SUPPORTED_TAKER_FEE_TYPES: Final[frozenset[FeeType]] = frozenset({FeeType.QUADRATIC})
"""Fee types whose **taker** semantics are established well enough to compute.

Only ``quadratic``. The others are deliberately absent:

``quadratic_with_maker_fees`` / ``quadratic_with_combo_maker_fees``
    The names imply the quadratic taker leg is unchanged and a maker leg is
    added, which would make the taker rate identical -- but "implied by the
    name" is not a specification. No documentation reachable from here states
    the taker rate for these types, and assuming it costs nothing to be wrong
    about only until it does.
``flat``
    A flat fee has no documented rate, base or cap. There is nothing to compute.
``margin_market_maker_program_fees``
    Observed live on 24 margin/perps series. Undocumented entirely, and outside
    Phase 1's event-contract scope.

An unsupported type raises :class:`UnsupportedFeeTypeError` rather than falling
back to the general quadratic formula. Silently substituting it is exactly how
a confident wrong fee reaches a profitability claim.
"""


class UnsupportedFeeTypeError(Exception):
    """The fee configuration's taker semantics are not established.

    Raised rather than approximated. A fee that is wrong in the optimistic
    direction turns a losing trade into an apparent arbitrage.
    """


@dataclass(frozen=True, slots=True)
class BalancePrecision:
    """The grid a member's balance is aligned to.

    Documented values: ``$0.0001`` for direct members, ``$0.01`` for non-direct
    members. Which one applies is a property of the *account*, and no
    documentation reachable from here exposes a member's classification without
    reading account data Phase 1 has no business touching.

    So it is explicit configuration with no default. Guessing would make every
    rounding_fee, rebate and net_fee silently wrong for half of users while
    still being reported as exact.
    """

    step: Money
    assumed: bool = False
    """True when the member class was not known and the coarser grid was chosen."""

    def __post_init__(self) -> None:
        if self.step.units <= 0:
            raise ValueError(f"balance precision must be positive, got {self.step}")

    @classmethod
    def direct_member(cls) -> BalancePrecision:
        return cls(step=Money.from_value("0.000100"))

    @classmethod
    def non_direct_member(cls) -> BalancePrecision:
        return cls(step=Money.from_value("0.010000"))

    def floor(self, amount: Money) -> Money:
        """Round **down** to the precision grid.

        Down, not toward zero. For a buy the amount is negative, and Python's
        floor division already rounds toward negative infinity -- which is what
        the documented ``floor_cent(-$0.058639) = -$0.060000`` requires.
        Truncation would give ``-$0.050000`` and every downstream number would
        be wrong.
        """
        return Money.from_units((amount.units // self.step.units) * self.step.units)

    def describe(self) -> str:
        suffix = " (assumed, covers both member classes)" if self.assumed else ""
        return f"{self.step.to_str()} per balance step{suffix}"

    @classmethod
    def unknown_member(cls) -> BalancePrecision:
        """The conservative choice when the account's member class is unknown.

        Uses the **coarser** non-direct grid. Every bound derived from a coarser
        grid also holds on a finer one, because the per-fill rounding exposure
        is at most one step either way and the non-direct step is 100x larger.
        So this over-states fees for a direct member rather than under-stating
        them for a non-direct one -- the only safe direction.

        Marked ``assumed`` so a caller can tell an assumption from a fact.
        """
        return cls(step=Money.from_value("0.010000"), assumed=True)


def taker_rate_for(configuration: FeeConfiguration) -> Decimal:
    """The quadratic taker rate for a configuration, or raise.

    Never guesses. An unrecognised fee type -- including one the venue adds
    tomorrow -- raises rather than defaulting to the general quadratic rate.
    """
    fee_type = configuration.fee_type
    if fee_type is None:
        raise UnsupportedFeeTypeError(
            f"fee type {configuration.fee_type_raw!r} is not in the documented enum; "
            "its taker semantics are unknown and must not be assumed to be quadratic"
        )
    if fee_type not in SUPPORTED_TAKER_FEE_TYPES:
        raise UnsupportedFeeTypeError(
            f"fee type {fee_type.value!r} has no established taker formula here; "
            f"only {sorted(t.value for t in SUPPORTED_TAKER_FEE_TYPES)} is supported"
        )
    return QUADRATIC_TAKER_RATE


def raw_model_fee(
    *,
    price: Price,
    quantity: Quantity,
    configuration: FeeConfiguration,
    liquidity: Liquidity = Liquidity.TAKER,
    notional: Price | None = None,
) -> Decimal:
    """``M * rate * C * P * (1 - P)``, exact and unrounded.

    ``P`` is the contract price in dollars and ``C`` the contract count, both
    exact decimals rather than floats. The result keeps full precision: rounding
    here would pre-empt the documented ``ceil_6dp`` step and change the answer.

    **Non-$1 notionals are refused.** The published formula's ``(1 - P)`` term
    is written against a $1 payout, and whether it generalises to ``(N - P)`` or
    to a normalised ``P/N`` is not documented anywhere reachable. Every Phase 1
    market sampled has a $1 notional, so the restriction costs nothing today and
    prevents inventing semantics tomorrow.
    """
    if liquidity is not Liquidity.TAKER:
        raise UnsupportedFeeTypeError(
            "only taker fees are implemented; maker execution involves queue "
            "position and fill uncertainty that Phase 1 does not model"
        )
    if notional is not None and notional != Price.from_value("1.0000"):
        raise UnsupportedFeeTypeError(
            f"notional {notional} is not $1.0000; the published quadratic formula is "
            "written against a $1 payout and its generalisation is undocumented"
        )

    rate = taker_rate_for(configuration)
    price_dollars = price.as_decimal()
    contracts = quantity.as_decimal()
    return configuration.multiplier * rate * contracts * price_dollars * (1 - price_dollars)


def trade_fee_from(model_fee: Decimal) -> Money:
    """``ceil_6dp(model_fee)`` -- the fee actually charged on one fill.

    Rounds **up**, always, which is why a fee estimate can never be optimistic
    by accident.
    """
    return Money.ceil_from_decimal(model_fee)


def signed_revenue_for_buy(gross_cost: Money) -> Money:
    """Signed revenue for a buy, following the venue's sign convention.

    Buying is cash out, so revenue is **negative** -- the documented example
    uses ``-$0.055000``. Named for the convention rather than for "cost"
    because the rounding algorithm is specified in terms of signed revenue, and
    a sign error there silently inverts the floor step.
    """
    if gross_cost.units < 0:
        raise ValueError(f"gross cost must not be negative, got {gross_cost}")
    return -gross_cost


def max_fill_count(quantity: Quantity) -> int:
    """The largest number of exchange fills an order of ``quantity`` can produce.

    PROVEN from a DOCUMENTED fact. Every positive fill is a whole multiple of
    ``MIN_QUANTITY_INCREMENT`` (0.01 contracts), so ``k`` fills require at least
    ``k * 0.01`` contracts. With total quantity ``Q``::

        k * 0.01 <= Q   =>   k <= Q / 0.01

    Because :class:`Quantity` is stored as hundredths of a contract, that is
    exactly its unit count. Q = 0.01 gives 1, Q = 1.00 gives 100, Q = 0 gives 0.

    This is the fact that makes fragmentation exposure finite. Without it the
    per-fill rounding term has no ceiling and no pre-trade fee bound exists.

    No negative guard is needed: :class:`Quantity` refuses negative values at
    construction, so the count cannot be negative by the time it reaches here.
    """
    return quantity.units


def max_rounding_fee_per_fill(precision: BalancePrecision) -> Money:
    """The largest ``rounding_fee`` one fill can incur: ``B - $0.000001``.

    PROVEN. For a fill, ``rounding_fee = unaligned - floor_B(unaligned)``. Every
    quantity involved is an exact integer number of micro-dollars: a 4dp price
    times a 2dp quantity is exactly 6dp (the scale invariant this codebase is
    built on), and ``trade_fee`` is 6dp by construction. So in micro-dollar
    units the rounding fee is ``unaligned mod B_units`` -- an integer remainder,
    hence in ``[0, B_units - 1]``.

    The upper end is attained, so the bound is tight rather than merely safe: a
    fill with ``unaligned ≡ -1 (mod B)`` rounds by exactly ``B - $0.000001``.
    """
    return precision.step - TRADE_FEE_STEP


def net_fee_lower_bound(total_raw_model_fee: Decimal) -> Money:
    """``ceil_6dp(F)`` -- the least an order's total net fee can possibly be.

    PROVEN, for any number of price levels and any fragmentation.

    Write the order's total net fee as a sum over its fills::

        net_total = Σ(trade_i + rounding_i - rebate_i)
                  = Σ trade_i + (Σ rounding_i - Σ rebate_i)
                  = Σ trade_i + A_final

    where ``A_final`` is the accumulator left at the end, because the
    accumulator is exactly rounding charged minus rounding rebated. Two facts
    finish it:

    * ``A_final >= 0``. The accumulator only ever holds overpayment awaiting
      rebate, and a rebate never exceeds what is accrued.
    * ``Σ trade_i >= ceil_6dp(F)``. Ceiling is superadditive --
      ``ceil(a) + ceil(b) >= ceil(a + b)`` -- and ``Σ f_i = F`` exactly, since
      the raw model fee is linear in quantity.

    Hence ``net_total >= ceil_6dp(F)``. Note this does **not** depend on the
    one-fill-per-level assumption, which is why it is the bound published to
    callers as the floor.
    """
    return Money.ceil_from_decimal(total_raw_model_fee)


def net_fee_upper_bound(
    *,
    total_raw_model_fee: Decimal,
    quantity: Quantity,
    precision: BalancePrecision,
) -> Money:
    """The most an order's total net fee can possibly be. PROVEN, and finite.

    Let ``F`` be the total raw model fee, ``μ = $0.000001``, ``B`` the balance
    precision and ``k`` the (unknown) number of fills. Three steps::

        Σ trade_i    <= ceil_6dp(F) + (k - 1)·μ      ceiling overshoot
        Σ rounding_i <= k·(B - μ)                    one full step short, per fill
        Σ rebate_i   >= 0                            rebates never add cost

    Therefore::

        net_total <= ceil_6dp(F) + (k-1)·μ + k·(B - μ)
                   = ceil_6dp(F) + k·B - μ

    The middle term is why ``k`` must be bounded, and it is: by
    :func:`max_fill_count`, ``k <= Q / 0.01``. Substituting ``k_max``::

        net_total <= ceil_6dp(F) + k_max·B - μ

    **The ceiling-overshoot step.** Writing ``ceil(x) = x + δ(x)`` with
    ``δ ∈ [0, 1)`` micro-dollars, ``Σ ceil(f_i) - ceil(F) = Σ δ_i - δ(F) < k``.
    Both sides are whole micro-dollars, so the difference is at most ``k - 1``.

    This bound is **loose** -- for a non-direct member buying 3 contracts it
    allows $3.00 of fees on a $2 position, because it assumes every one of 300
    possible fills rounds against you by a full cent and no rebate ever lands.
    That is fine. A loose proven bound lets a detector say "not provably
    riskless"; an absent bound forces it to say nothing at all.

    **Zero quantity is not an application of this formula.** With no fills there
    is no ceiling overshoot and no rounding step to pay, so the formula's
    ``- μ`` term would hand back a negative bound. ``k_max = 0`` short-circuits
    to an exact zero instead, and the algebra above assumes ``k_max >= 1``.
    """
    fills = max_fill_count(quantity)
    if fills == 0:
        return Money.zero()
    floor = net_fee_lower_bound(total_raw_model_fee)
    return Money.from_units(floor.units + fills * precision.step.units - TRADE_FEE_STEP.units)


def net_fee_bounds(
    *,
    total_raw_model_fee: Decimal,
    quantity: Quantity,
    precision: BalancePrecision,
) -> tuple[Money, Money]:
    """Both proven bounds at once: ``(lower, upper)``.

    Zero quantity is handled as its own case rather than by evaluating the
    positive-quantity formulas. An order that acquires nothing produces no
    fills, so every one of the five fee quantities is exactly zero -- raw model
    fee, trade fee, rounding fee, rebate and net fee alike -- and both bounds
    collapse onto that zero. Feeding ``k_max = 0`` through the upper-bound
    algebra would instead yield ``ceil_6dp(F) - μ``, a *negative* bound, which
    is why the case is separated rather than trusted to the arithmetic.

    A non-zero model fee on a zero quantity is contradictory: the raw model fee
    is linear in quantity, so it cannot be non-zero when nothing was bought. It
    is refused rather than silently zeroed, because it can only mean the caller
    paired a fee with the wrong quantity.
    """
    if quantity.units == 0:
        if total_raw_model_fee != 0:
            raise ValueError(
                f"zero quantity carries a non-zero raw model fee ({total_raw_model_fee}); "
                "the model fee is linear in quantity, so this pairs a fee with the "
                "wrong quantity"
            )
        return Money.zero(), Money.zero()
    return (
        net_fee_lower_bound(total_raw_model_fee),
        net_fee_upper_bound(
            total_raw_model_fee=total_raw_model_fee,
            quantity=quantity,
            precision=precision,
        ),
    )
