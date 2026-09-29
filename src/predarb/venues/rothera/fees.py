"""Rothera's quadratic trade fee, computed exactly.

From the Fee Schedule effective 2026-05-20:

    Order fees = MAX(round(k x p x (1 - p) x c, 2), 0.01)

charged to **both** the buyer and the seller of every trade, with "Standard
rounding (round half up)". The schedule lists three participant types and
nothing else -- no maker/taker split, no rebate, no settlement or clearing fee,
and no language altering the contractual payout. Fees are account debits, so
they land on the cost side of a basket inequality and never reduce what a
contract pays.

The shape is worth noting: ``p(1-p)`` peaks at ``p = 0.50`` and vanishes at the
extremes, so the cheapest legs to trade are the ones nearest certainty -- which
is where a partition basket's legs mostly sit. The floor of one cent per order
works the other way and dominates for small orders.

Nothing here is a float. ``p(1-p)`` is evaluated in exact decimal and the
half-up rounding is applied with :class:`~decimal.Decimal`, because a fee that
decides whether an edge exists is money.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Final

from predarb.domain.money import Money, Price

__all__ = [
    "FEE_FLOOR",
    "K_BY_PARTICIPANT",
    "ParticipantType",
    "order_fee",
    "round_trip_fee",
]


class ParticipantType(StrEnum):
    """The fee tiers the schedule names."""

    FCM_RETAIL = "FCM_RETAIL_CUSTOMER"
    FCM_PROFESSIONAL = "FCM_PROFESSIONAL_TRADING_FIRM"
    MARKET_MAKER = "MARKET_MAKER"


K_BY_PARTICIPANT: Final[dict[ParticipantType, Decimal]] = {
    ParticipantType.FCM_RETAIL: Decimal("0.02"),
    ParticipantType.FCM_PROFESSIONAL: Decimal("0.12"),
    ParticipantType.MARKET_MAKER: Decimal("0.03"),
}
"""Note the ordering: the retail tier is the cheapest by a factor of six. A
professional trading firm pays 0.12, which at a mid price costs $0.03 per
contract per side -- three cents against a one-dollar notional, on each of a
basket's legs, twice if the position is closed rather than held."""

FEE_FLOOR: Final = Money.from_value("0.010000")
_CENT: Final = Decimal("0.01")


def order_fee(price: Price, contracts: int, participant: ParticipantType) -> Money:
    """Fee for one side of one order, exactly as the schedule computes it.

    ``MAX(round(k x p x (1 - p) x c, 2), 0.01)`` with half-up rounding.
    """
    if contracts < 0:
        raise ValueError(f"contracts must not be negative, got {contracts}")
    p = Decimal(price.units) / Decimal(10**4)
    if p > Decimal(1):
        raise ValueError(f"price {price} exceeds the $1.00 notional")
    k = K_BY_PARTICIPANT[participant]
    raw = (k * p * (Decimal(1) - p) * Decimal(contracts)).quantize(_CENT, rounding=ROUND_HALF_UP)
    return Money.from_value(max(raw, _CENT))


def round_trip_fee(price: Price, contracts: int, participant: ParticipantType) -> Money:
    """Both sides of one trade -- what the venue collects per matched order.

    Relevant to a basket only insofar as the counterparty's fee is not the
    basket's cost; it is here so the schedule's "the buyer and the seller will
    each be charged" is represented rather than assumed away.
    """
    one = order_fee(price, contracts, participant)
    return Money(one.units * 2)
