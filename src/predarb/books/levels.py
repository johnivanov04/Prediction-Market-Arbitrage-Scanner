"""Normalised order-book levels and their provenance.

Kalshi publishes **bids only**, on two sides (``yes`` and ``no``). There is no
ask book; an ask is always a derived quantity, computed from the opposing side's
bid against the contract's notional (``docs/api_assumptions.md`` A-06).

This module defines the level and book types and the sort order. It does **not**
derive asks and does not reconstruct from deltas: that is the reconstruction
layer, built in a later step. What matters here is that the raw, quoted state is
represented faithfully and orderably.

Ordering
--------
The venue's array order is not trusted. The API reference describes levels as
"best to worst", but live responses are ascending by price, which for a bid book
is worst-to-best -- the exact opposite (A-07). Rather than pick a reading, this
module sorts explicitly: **bids are stored descending by price, best first**, so
``yes_bids[0]`` is the best YES bid by construction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from predarb.domain.enums import MarketSide
from predarb.domain.money import Price, Quantity

__all__ = [
    "BookLevel",
    "BookSource",
    "BookTransport",
    "QuotedBook",
    "sort_levels_best_first",
]


class BookTransport(StrEnum):
    """How a book state reached us. Part of every book's provenance."""

    REST_SNAPSHOT = "REST_SNAPSHOT"
    WS_SNAPSHOT = "WS_SNAPSHOT"
    WS_DELTA_APPLIED = "WS_DELTA_APPLIED"
    SYNTHETIC = "SYNTHETIC"
    """Test fixtures only. Never eligible for an arbitrage claim."""


@dataclass(frozen=True, slots=True)
class BookSource:
    """Where a book state came from, and when.

    ``received_at`` is what staleness is measured against. ``exchange_ts`` is
    recorded when the venue supplies one and left ``None`` when it does not --
    Kalshi omits ``ts_ms`` on some messages, and that absence is information
    rather than something to backfill with a local clock reading.
    """

    transport: BookTransport
    instrument_ticker: str
    received_at: datetime
    exchange_ts: datetime | None = None
    sequence: int | None = None
    subscription_id: int | None = None
    raw_message_id: str | None = None


@dataclass(frozen=True, slots=True)
class BookLevel:
    """One aggregated price level.

    ``side`` is the outcome the resting **bid** is for, not a buy/sell flag:
    a ``NO`` level is resting interest to buy NO at ``price``.
    """

    price: Price
    quantity: Quantity
    side: MarketSide
    derived: bool = False
    """True when this level was computed from the opposing side rather than quoted.

    A derived level's depth is the *same* resting interest as the quoted level
    it came from. Consuming both would double-count liquidity that does not
    exist, so the distinction is never discarded."""

    derived_from_price: Price | None = None
    """The quoted price this level was derived from, when ``derived`` is set."""

    def __post_init__(self) -> None:
        if self.derived and self.derived_from_price is None:
            raise ValueError(
                f"derived level at {self.price} must record the quoted price it came from"
            )
        if not self.derived and self.derived_from_price is not None:
            raise ValueError(f"quoted level at {self.price} must not claim a derivation source")


@dataclass(frozen=True, slots=True)
class QuotedBook:
    """The quoted (bids-only) state of one market, with provenance.

    Contains no derived asks by construction: deriving them requires the
    instrument's notional, which belongs to the layer that holds instrument
    metadata.
    """

    source: BookSource
    yes_bids: tuple[BookLevel, ...]
    no_bids: tuple[BookLevel, ...]

    def __post_init__(self) -> None:
        for name, levels, side in (
            ("yes_bids", self.yes_bids, MarketSide.YES),
            ("no_bids", self.no_bids, MarketSide.NO),
        ):
            for level in levels:
                if level.side is not side:
                    raise ValueError(f"{name} contains a {level.side} level")
                if level.derived:
                    raise ValueError(
                        f"{name} contains a derived level; this book holds quotes only"
                    )
            prices = [level.price.units for level in levels]
            if prices != sorted(prices, reverse=True):
                raise ValueError(f"{name} must be sorted descending by price, best first")
            if len(set(prices)) != len(prices):
                raise ValueError(f"{name} contains duplicate price levels")

    @property
    def best_yes_bid(self) -> BookLevel | None:
        return self.yes_bids[0] if self.yes_bids else None

    @property
    def best_no_bid(self) -> BookLevel | None:
        return self.no_bids[0] if self.no_bids else None

    @property
    def is_empty(self) -> bool:
        return not self.yes_bids and not self.no_bids


def sort_levels_best_first(levels: list[BookLevel]) -> tuple[BookLevel, ...]:
    """Sort bid levels descending by price.

    Always call this on venue input. The API's documented ordering and its
    observed ordering disagree (A-07), so relying on either is a latent
    inversion bug that would present the worst price in the book as the best.
    """
    return tuple(sorted(levels, key=lambda level: level.price.units, reverse=True))
