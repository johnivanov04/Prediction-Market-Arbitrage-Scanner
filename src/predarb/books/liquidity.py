"""Book-level liquidity identity and derived executable levels.

The problem this module exists to solve
---------------------------------------
Kalshi publishes **bids only**. To buy YES you cross resting **NO** bids, so a
"YES ask" is not a thing the venue sent -- it is arithmetic:

    yes_ask = notional - no_bid

That means a direct NO bid and the derived YES ask computed from it are **two
views of the same aggregate resting liquidity**. Consuming both would
double-count depth that only exists once. A later multi-leg planner needs to
notice when two apparent execution legs reach for the same contracts, and it can
only do that if an identity survives the derivation.

What :class:`BookLiquidityId` is -- and is not
-----------------------------------------------
It identifies **one aggregate L2 price level within one immutable BookView**.
That is all, and the limits matter:

* **Not an exchange order.** Kalshi's depth is aggregated: a single level may
  contain many resting orders from many participants. Nothing here identifies
  an individual one.
* **Not a queue position.** There is no ordering information within a level, so
  nothing can be said about who fills first.
* **Not a reservation handle, and not stable across book versions.**
  ``book_seq`` is the sequence number of the *view*, which advances on any
  update to the sid -- including changes to unrelated markets. Two ids differing
  only in ``book_seq`` may well describe the same underlying contracts; the
  identity deliberately does not claim otherwise.

Its Phase 1 purpose is exactly two things: **provenance**, so a derived level
can be traced to the quoted level it came from, and **collision detection**
between direct and derived representations *within a single view*.

A future execution system that actually places orders will need stronger
semantics -- reservation, version consistency, and a way to track liquidity
across book updates. This is not that, and should not be mistaken for it.
"""

from __future__ import annotations

from dataclasses import dataclass

from predarb.domain.enums import MarketSide
from predarb.domain.money import Money, Price, Quantity

__all__ = ["BookLiquidityId", "ExecutableLevel"]


@dataclass(frozen=True, slots=True, order=True)
class BookLiquidityId:
    """One aggregate L2 price level, within one immutable book view.

    Keyed on the *source* bid -- the liquidity the venue actually published --
    never on the derived ask, so two execution legs reaching for the same
    aggregate level produce equal ids regardless of which outcome each is buying.

    ``book_seq`` scopes the identity to a single view rather than making it
    durable: it is the view's sequence number, which advances on any update to
    the sid. Two ids differing only in ``book_seq`` may describe the same
    underlying contracts, and this type makes no claim either way.

    See the module docstring for what this is not: it is neither an exchange
    order id, nor a queue position, nor a reservation that survives a book
    update.
    """

    connection_epoch: int
    sid: int
    market_ticker: str
    source_outcome: MarketSide
    """Which side the resting **bid** is on -- not what a buyer acquires."""

    source_price: Price
    book_seq: int | None = None

    def describe(self) -> str:
        return (
            f"{self.market_ticker}:{self.source_outcome.value}@{self.source_price}"
            f" (epoch={self.connection_epoch} sid={self.sid} seq={self.book_seq})"
        )


@dataclass(frozen=True, slots=True)
class ExecutableLevel:
    """One price level a buyer can immediately cross into.

    Always derived on Kalshi: ``derived`` is not a flag that is sometimes false
    here, it is a permanent reminder that the price was computed rather than
    quoted, and that the depth belongs to :attr:`liquidity`.
    """

    acquired_outcome: MarketSide
    """What the buyer ends up holding."""

    execution_price: Price
    """What the buyer pays per contract: ``notional - source_bid_price``."""

    available_quantity: Quantity
    liquidity: BookLiquidityId
    source_outcome: MarketSide
    source_bid_price: Price
    derived: bool = True

    def __post_init__(self) -> None:
        if self.source_outcome is self.acquired_outcome:
            raise ValueError(
                f"an executable {self.acquired_outcome.value} level must derive from "
                f"the opposing side's bids, not from {self.source_outcome.value} bids"
            )
        if self.liquidity.source_outcome is not self.source_outcome:
            raise ValueError(
                "liquidity identity disagrees with the level's source outcome; "
                "the identity must name the resting bid it came from"
            )
        if self.liquidity.source_price != self.source_bid_price:
            raise ValueError("liquidity identity disagrees with the level's source bid price")

    def cost_for(self, quantity: Quantity) -> Money:
        """Gross cost of taking ``quantity`` at this level.

        Exact: price (4 dp) times quantity (2 dp) is money (6 dp) with no
        rounding anywhere. Fee rounding happens later, in the fee engine.
        """
        if quantity.units > self.available_quantity.units:
            raise ValueError(
                f"cannot take {quantity} at {self.execution_price}: only "
                f"{self.available_quantity} available"
            )
        return self.execution_price * quantity

    @property
    def full_cost(self) -> Money:
        return self.execution_price * self.available_quantity

    def describe(self) -> str:
        return (
            f"buy {self.acquired_outcome.value} {self.available_quantity} @ "
            f"{self.execution_price} (derived from {self.source_outcome.value} bid "
            f"@ {self.source_bid_price})"
        )
