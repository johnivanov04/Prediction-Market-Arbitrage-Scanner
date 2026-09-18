"""Executable depth: what can actually be bought right now, and at what gross cost.

This is the first layer that turns venue truth into executable economics. It
answers *"if I wanted q contracts of this outcome immediately, what would the
displayed book give me and what would it cost before fees?"* -- and deliberately
stops there.

What is absent, on purpose
--------------------------
No fees, no payoff, no expected value, no notion of profit. In particular there
is **no ``max_profitable_size``**: profitability cannot be determined without
fees, fee rounding, guaranteed payoff, settlement semantics and every required
leg. Computing it here would mean inventing some of those, and a number that
looks like profit but was computed without fees is worse than no number.

What this layer produces is a *curve*: cost as a function of quantity. Later
layers combine curves with fees and payoff constraints to find profitable size.
Keeping the curve free of economics is what makes it reusable for every strategy.

Authoritative book vs execution view
------------------------------------
The authoritative book holds exactly what Kalshi sends -- YES bids and NO bids.
Derived asks live only here. To buy YES you cross resting **NO** bids, so:

    yes_ask = notional - no_bid        (depth = that NO bid's quantity)
    no_ask  = notional - yes_bid       (depth = that YES bid's quantity)

The notional comes from instrument metadata, never a hardcoded $1.00. Every
sampled Phase 1 binary market has had a $1 notional, and reading the field costs
nothing while assuming it silently rescales every cost.

Displayed price is not executable price
---------------------------------------
The best ask is a price for *some* quantity, often small. This module exists
because the price for the quantity you actually want is a different number, and
the only way to know it is to walk the book.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from predarb.books.liquidity import BookLiquidityId, ExecutableLevel
from predarb.books.orderbook import BookView
from predarb.books.state import BookIntegrity, BookProvenance
from predarb.domain.average_price import AveragePrice
from predarb.domain.enums import MarketSide, SettlementKind
from predarb.domain.models import (
    EXCLUSION_MULTIVARIATE,
    EXCLUSION_NO_NOTIONAL,
    EXCLUSION_NON_BINARY,
    EXCLUSION_PROVISIONAL,
    VenueInstrument,
)
from predarb.domain.money import Money, Price, Quantity

__all__ = [
    "EXECUTION_BLOCKING_EXCLUSIONS",
    "BookNotExecutableError",
    "DepthBreakpoint",
    "ExecutionContext",
    "ExecutionCurve",
    "ExecutionQuote",
    "FillSlice",
    "InsufficientDepthError",
    "UnsupportedExecutionSemanticsError",
    "build_execution_curve",
]


EXECUTION_BLOCKING_EXCLUSIONS: Final[frozenset[str]] = frozenset(
    {
        EXCLUSION_MULTIVARIATE,
        EXCLUSION_PROVISIONAL,
        EXCLUSION_NON_BINARY,
        EXCLUSION_NO_NOTIONAL,
    }
)
"""Exclusions that make a quote meaningless, as opposed to merely under-verified.

Not every exclusion belongs here, and the distinction is deliberate.

**Blocking**: a combo or provisional market is outside Phase 1 entirely; a
non-binary contract has no complementary relationship to derive from; without a
notional the complement cannot be computed at all.

**Not blocking**: a missing or inconsistent ``price_ranges`` grid. The grid is
metadata used to validate *quoted* prices, and its absence says nothing about
whether the resting bids are real. Those bids are liquidity the venue published;
refusing to price against them because we could not independently confirm the
tick grid would reject genuine depth on a metadata technicality. The derived ask
is still bounds-checked against ``[0, notional]`` by
:meth:`Price.complement`, which is the check that actually matters here.

The instrument's own ``is_excluded`` remains the right gate for *scanning*; this
narrower set is the gate for *quoting*.
"""


class BookNotExecutableError(Exception):
    """The book cannot currently support an execution quote.

    Raised rather than returning an empty quote: an empty quote reads as "no
    depth", which is a statement about the market. This is a statement about
    *us* -- we cannot prove the book is what the venue sent.
    """


class UnsupportedExecutionSemanticsError(Exception):
    """The instrument's settlement model does not support complementary derivation.

    ``yes_ask = notional - no_bid`` is a property of a binary contract whose two
    outcomes partition the payout. It is not a universal truth about prediction
    markets, and applying it to a scalar or unverified contract would produce
    confident nonsense.
    """


class InsufficientDepthError(Exception):
    """Requested quantity is not available, and the caller demanded all of it.

    Only raised by :meth:`ExecutionCurve.require_full_quantity`. The default
    behaviour is a partial quote, because "the book is thinner than you hoped"
    is ordinary information, not an error.
    """


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """Proof that a book view is currently authoritative.

    Passed explicitly rather than read from a global so that a replay caller
    must *construct* authority for the moment being replayed, rather than
    disabling a check. There is deliberately no ``skip_validation`` flag: the
    only way to quote a book is to assert what made it trustworthy.
    """

    current_connection_epoch: int
    journal_healthy: bool = True
    connection_healthy: bool = True

    def describe_failure(self, view: BookView) -> str | None:
        """Why this view may not be quoted, or ``None`` if it may."""
        if not self.journal_healthy:
            return "raw journal is unhealthy; this book's inputs were not fully recorded"
        if not self.connection_healthy:
            return "connection is not healthy; the book cannot be shown to be current"
        if view.integrity is not BookIntegrity.VALID:
            return f"book integrity is {view.integrity.value}, not VALID"
        if view.provenance.connection_epoch != self.current_connection_epoch:
            return (
                f"book belongs to connection epoch {view.provenance.connection_epoch}, "
                f"but the live epoch is {self.current_connection_epoch}; sequence "
                "authority does not cross connections"
            )
        return None


@dataclass(frozen=True, slots=True)
class FillSlice:
    """One level's contribution to a fill."""

    quantity: Quantity
    execution_price: Price
    gross_cost: Money
    liquidity: BookLiquidityId
    source_outcome: MarketSide
    source_bid_price: Price
    derived: bool = True


@dataclass(frozen=True, slots=True)
class DepthBreakpoint:
    """Cumulative state at the exhaustion of one source level."""

    cumulative_quantity: Quantity
    cumulative_cost: Money
    vwap: AveragePrice
    marginal_price: Price
    """Price of the level that was being consumed up to this point."""

    liquidity: BookLiquidityId


@dataclass(frozen=True, slots=True)
class ExecutionQuote:
    """What the displayed book would give for a requested quantity.

    A snapshot, not an instruction: nothing here submits anything. ``fees_included``
    is a permanent ``False`` so no downstream consumer can mistake a gross cost
    for an all-in cost.
    """

    market_ticker: str
    acquired_outcome: MarketSide
    requested_quantity: Quantity
    filled_quantity: Quantity
    unfilled_quantity: Quantity
    fully_fillable: bool
    slices: tuple[FillSlice, ...]
    gross_cost: Money
    vwap: AveragePrice | None
    best_execution_price: Price | None
    worst_execution_price: Price | None
    connection_epoch: int
    sid: int
    book_seq: int | None
    provenance: BookProvenance
    quoted_at: datetime
    fees_included: bool = False

    @property
    def liquidity_ids(self) -> tuple[BookLiquidityId, ...]:
        """Every source level this quote would consume."""
        return tuple(slice_.liquidity for slice_ in self.slices)

    @property
    def is_empty(self) -> bool:
        return self.filled_quantity.is_zero

    def describe(self) -> str:
        vwap = f"{self.vwap}" if self.vwap else "n/a"
        return (
            f"buy {self.acquired_outcome.value} {self.filled_quantity}/"
            f"{self.requested_quantity} {self.market_ticker} "
            f"gross={self.gross_cost} vwap={vwap} "
            f"({'full' if self.fully_fillable else 'partial'}, fees excluded)"
        )


class ExecutionCurve:
    """Immutable cost-versus-quantity curve for one outcome of one market.

    Built from a book view at a moment in time and never changes afterwards: the
    levels are copied into tuples, so a later mutation of the live book leaves an
    existing curve untouched. A caller holding a curve is holding a fact about a
    specific ``(epoch, sid, seq)``, not a live handle.
    """

    __slots__ = (
        "_cumulative_cost",
        "_cumulative_quantity",
        "acquired_outcome",
        "book_seq",
        "breakpoints",
        "built_at",
        "connection_epoch",
        "levels",
        "market_ticker",
        "notional",
        "provenance",
        "sid",
    )

    def __init__(
        self,
        *,
        market_ticker: str,
        acquired_outcome: MarketSide,
        notional: Price,
        levels: tuple[ExecutableLevel, ...],
        provenance: BookProvenance,
        built_at: datetime,
    ) -> None:
        self.market_ticker = market_ticker
        self.acquired_outcome = acquired_outcome
        self.notional = notional
        self.levels = levels
        self.provenance = provenance
        self.built_at = built_at
        self.connection_epoch = provenance.connection_epoch
        self.sid = provenance.sid
        self.book_seq = provenance.latest_seq

        # Prefix sums, so gross_cost(q) is a bisect plus one multiply rather
        # than a fresh walk of the book on every call.
        cumulative_quantity: list[int] = []
        cumulative_cost: list[int] = []
        breakpoints: list[DepthBreakpoint] = []
        running_quantity = 0
        running_cost = 0
        for level in levels:
            running_quantity += level.available_quantity.units
            running_cost += level.full_cost.units
            cumulative_quantity.append(running_quantity)
            cumulative_cost.append(running_cost)
            breakpoints.append(
                DepthBreakpoint(
                    cumulative_quantity=Quantity.from_units(running_quantity),
                    cumulative_cost=Money.from_units(running_cost),
                    vwap=AveragePrice(
                        Money.from_units(running_cost), Quantity.from_units(running_quantity)
                    ),
                    marginal_price=level.execution_price,
                    liquidity=level.liquidity,
                )
            )
        self._cumulative_quantity = tuple(cumulative_quantity)
        self._cumulative_cost = tuple(cumulative_cost)
        self.breakpoints = tuple(breakpoints)

    # -- depth ---------------------------------------------------------------

    @property
    def max_fillable_quantity(self) -> Quantity:
        """Total displayed depth for this outcome."""
        if not self._cumulative_quantity:
            return Quantity.zero()
        return Quantity.from_units(self._cumulative_quantity[-1])

    @property
    def is_empty(self) -> bool:
        """No executable depth. A legitimate state, not a fault."""
        return not self.levels

    @property
    def best_execution_price(self) -> Price | None:
        return self.levels[0].execution_price if self.levels else None

    @property
    def worst_execution_price(self) -> Price | None:
        return self.levels[-1].execution_price if self.levels else None

    # -- evaluation ----------------------------------------------------------

    def gross_cost(self, quantity: Quantity) -> Money:
        """Exact gross cost of ``quantity``, before fees.

        Evaluates at arbitrary quantities, not just breakpoints: a request that
        stops halfway through a level pays that level's price for the part it
        takes. Raises if the quantity exceeds displayed depth -- callers wanting
        a partial answer should use :meth:`quote_up_to`, which says how much it
        could actually get.
        """
        self._require_quantity(quantity)
        if quantity.is_zero:
            return Money.zero()
        if quantity.units > self.max_fillable_quantity.units:
            raise InsufficientDepthError(
                f"requested {quantity} but only {self.max_fillable_quantity} is "
                f"displayed for {self.acquired_outcome.value} {self.market_ticker}"
            )
        index = bisect.bisect_left(self._cumulative_quantity, quantity.units)
        previous_quantity = self._cumulative_quantity[index - 1] if index else 0
        previous_cost = self._cumulative_cost[index - 1] if index else 0
        remainder = quantity.units - previous_quantity
        level = self.levels[index]
        return Money.from_units(previous_cost + level.execution_price.units * remainder)

    def vwap(self, quantity: Quantity) -> AveragePrice | None:
        """Average price for ``quantity``, or ``None`` at zero."""
        if quantity.is_zero:
            return None
        return AveragePrice(self.gross_cost(quantity), quantity)

    def marginal_price(self, quantity: Quantity) -> Price | None:
        """Price of the level the next contract would come from."""
        if not self.levels:
            return None
        if quantity.units >= self.max_fillable_quantity.units:
            return None
        index = bisect.bisect_right(self._cumulative_quantity, quantity.units)
        return self.levels[index].execution_price

    # -- quoting -------------------------------------------------------------

    def quote_up_to(self, quantity: Quantity, *, at: datetime | None = None) -> ExecutionQuote:
        """Quote up to ``quantity``, filling as much as the book allows.

        A shortfall is reported, not raised. "The book is thinner than you
        hoped" is ordinary information; the caller decides what to do with it.
        """
        self._require_quantity(quantity)
        slices: list[FillSlice] = []
        remaining = quantity.units
        total_cost = 0

        for level in self.levels:
            if remaining <= 0:
                break
            take = min(remaining, level.available_quantity.units)
            cost = level.execution_price.units * take
            slices.append(
                FillSlice(
                    quantity=Quantity.from_units(take),
                    execution_price=level.execution_price,
                    gross_cost=Money.from_units(cost),
                    liquidity=level.liquidity,
                    source_outcome=level.source_outcome,
                    source_bid_price=level.source_bid_price,
                )
            )
            total_cost += cost
            remaining -= take

        filled = Quantity.from_units(quantity.units - remaining)
        gross = Money.from_units(total_cost)
        return ExecutionQuote(
            market_ticker=self.market_ticker,
            acquired_outcome=self.acquired_outcome,
            requested_quantity=quantity,
            filled_quantity=filled,
            unfilled_quantity=Quantity.from_units(remaining),
            fully_fillable=remaining == 0,
            slices=tuple(slices),
            gross_cost=gross,
            vwap=AveragePrice(gross, filled) if not filled.is_zero else None,
            best_execution_price=slices[0].execution_price if slices else None,
            worst_execution_price=slices[-1].execution_price if slices else None,
            connection_epoch=self.connection_epoch,
            sid=self.sid,
            book_seq=self.book_seq,
            provenance=self.provenance,
            quoted_at=at or self.built_at,
        )

    def require_full_quantity(
        self, quantity: Quantity, *, at: datetime | None = None
    ) -> ExecutionQuote:
        """Quote ``quantity`` or raise. For research that needs all-or-nothing."""
        quote = self.quote_up_to(quantity, at=at)
        if not quote.fully_fillable:
            raise InsufficientDepthError(
                f"requested {quantity} of {self.acquired_outcome.value} "
                f"{self.market_ticker} but only {quote.filled_quantity} is displayed"
            )
        return quote

    @staticmethod
    def _require_quantity(quantity: Quantity) -> None:
        if not isinstance(quantity, Quantity):
            raise TypeError(f"quantity must be a Quantity, got {type(quantity).__name__}")

    def __repr__(self) -> str:
        return (
            f"ExecutionCurve({self.market_ticker!r}, buy={self.acquired_outcome.value}, "
            f"levels={len(self.levels)}, depth={self.max_fillable_quantity}, "
            f"epoch={self.connection_epoch}, seq={self.book_seq})"
        )


def build_execution_curve(
    view: BookView,
    instrument: VenueInstrument,
    context: ExecutionContext,
    acquired_outcome: MarketSide,
    *,
    at: datetime | None = None,
) -> ExecutionCurve:
    """Derive the executable curve for buying ``acquired_outcome``.

    Depth comes **only** from the opposing side's resting bids, which is the
    only liquidity that exists: to buy YES you cross NO bids.

    Raises rather than returning something empty when the book cannot be
    trusted or the instrument's semantics do not support the derivation.
    """
    failure = context.describe_failure(view)
    if failure is not None:
        raise BookNotExecutableError(f"{view.market_ticker}: {failure}")

    if instrument.settlement_kind is not SettlementKind.BINARY:
        raise UnsupportedExecutionSemanticsError(
            f"{instrument.ticker}: complementary pricing requires a binary contract, "
            f"but settlement is {instrument.settlement_kind.value}. "
            "yes_ask = notional - no_bid is a property of a two-outcome payout, "
            "not a universal rule."
        )
    if instrument.notional_value is None:
        raise UnsupportedExecutionSemanticsError(
            f"{instrument.ticker}: no notional_value; the complement cannot be "
            "computed and must never be assumed to be $1.00"
        )
    blocking = tuple(
        reason for reason in instrument.exclusion_reasons if reason in EXECUTION_BLOCKING_EXCLUSIONS
    )
    if blocking:
        raise UnsupportedExecutionSemanticsError(
            f"{instrument.ticker} cannot be quoted: {', '.join(blocking)}"
        )
    if instrument.ticker != view.market_ticker:
        raise UnsupportedExecutionSemanticsError(
            f"instrument {instrument.ticker} does not describe book {view.market_ticker}"
        )

    notional = instrument.notional_value
    source_outcome = MarketSide.NO if acquired_outcome is MarketSide.YES else MarketSide.YES
    source_levels = view.no_bids if source_outcome is MarketSide.NO else view.yes_bids

    levels: list[ExecutableLevel] = []
    for source in source_levels:
        if source.quantity.is_zero:
            continue
        execution_price = source.price.complement(notional)
        liquidity = BookLiquidityId(
            connection_epoch=view.provenance.connection_epoch,
            sid=view.provenance.sid,
            market_ticker=view.market_ticker,
            source_outcome=source_outcome,
            source_price=source.price,
            book_seq=view.provenance.latest_seq,
        )
        levels.append(
            ExecutableLevel(
                acquired_outcome=acquired_outcome,
                execution_price=execution_price,
                available_quantity=source.quantity,
                liquidity=liquidity,
                source_outcome=source_outcome,
                source_bid_price=source.price,
            )
        )

    # Cheapest first. Sorted explicitly rather than relying on the book's order:
    # the best bid is the *highest* price, which derives to the *cheapest* ask,
    # so the orders are inverses and depending on one would silently invert the
    # curve. Ties break on source price for determinism.
    levels.sort(key=lambda level: (level.execution_price.units, level.source_bid_price.units))

    return ExecutionCurve(
        market_ticker=view.market_ticker,
        acquired_outcome=acquired_outcome,
        notional=notional,
        levels=tuple(levels),
        provenance=view.provenance,
        built_at=at or datetime.now(tz=UTC),
    )
