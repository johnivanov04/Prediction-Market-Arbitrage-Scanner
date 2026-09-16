"""Pydantic v2 models mirroring the Kalshi wire schema.

These are **wire models, not domain models**. They mirror what Kalshi actually
sends, field name for field name, including the venue's own inconsistencies (the
REST book wraps levels in ``orderbook_fp`` with inner ``yes_dollars``, while the
WebSocket snapshot puts ``yes_dollars_fp`` directly in ``msg``). Translation to
application meaning happens in :mod:`predarb.venues.kalshi.normalize`, so venue
naming never leaks into the domain.

JSON decoding
-------------
Payloads must be decoded with :func:`decode_json`, which uses
``parse_float=Decimal``. Kalshi sends prices and sizes as *strings*, but some
financially relevant values -- ``fee_multiplier``, ``floor_strike``,
``cap_strike`` -- are JSON **numbers**, and ``json.loads`` turns a number into a
``float`` before any model sees it. Decoding with ``parse_float=Decimal``
preserves the exact digits the venue sent.

Forward-compatibility policy
----------------------------
Kalshi is actively changing this API, so strictness is chosen per field rather
than globally:

**Unknown fields are ignored, never fatal.**
    ``extra="ignore"``. Kalshi adding a harmless field must not break ingestion.
    Nothing is lost by this: the raw journal keeps the exact bytes, and
    :func:`unknown_top_level_fields` reports what a model dropped so new fields
    surface in observability instead of silently vanishing.

**Financially relevant known fields are strict.**
    Prices, sizes and money parse eagerly through the exact fixed-point parsers.
    A malformed or re-typed price raises at parse time rather than producing a
    plausible-looking wrong number.

**Enum-like fields are NOT closed enums.**
    ``status``, ``market_type``, ``fee_type``, ``strike_type``,
    ``price_level_structure`` and ``result`` are typed ``str``. This is not
    laziness: ``fee_type`` was observed live carrying
    ``margin_market_maker_program_fees``, which is absent from the documented
    four-value enum. A closed enum would have crashed ingestion on real data.
    Mapping to our own closed vocabularies happens in normalisation, where an
    unrecognised value becomes an explicit ``UNKNOWN`` that fails closed.

**Optional means optional.**
    A missing quote stays ``None`` rather than becoming zero. A market with no
    bid is not a market bidding $0.00, and conflating them would put a free
    contract at the top of the book.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, TypeVar

from pydantic import BaseModel, BeforeValidator, ConfigDict, PlainSerializer, field_validator

from predarb.clock import ensure_utc
from predarb.domain.money import Money, Price, Quantity, QuantityDelta
from predarb.venues.kalshi.fixed_point import (
    parse_fee_multiplier,
    parse_money_dollars,
    parse_price_dollars,
    parse_quantity_delta_fp,
    parse_quantity_fp,
)

__all__ = [
    "KalshiAccountLimits",
    "KalshiBucketLimit",
    "KalshiEndpointCosts",
    "KalshiEndpointTokenCost",
    "KalshiEvent",
    "KalshiEventEnvelope",
    "KalshiEventFeeChange",
    "KalshiEventFeeChangesResponse",
    "KalshiEventsPage",
    "KalshiExchangeShardStatus",
    "KalshiExchangeStatus",
    "KalshiMarket",
    "KalshiMarketEnvelope",
    "KalshiMarketsPage",
    "KalshiMveLeg",
    "KalshiOrderbook",
    "KalshiOrderbookEnvelope",
    "KalshiPriceRange",
    "KalshiSeries",
    "KalshiSeriesEnvelope",
    "KalshiSeriesFeeChange",
    "KalshiSeriesFeeChangesResponse",
    "KalshiSettlementSource",
    "KalshiUsageLevelGrant",
    "KalshiWsDeltaMsg",
    "KalshiWsEnvelope",
    "KalshiWsErrorMsg",
    "KalshiWsSnapshotMsg",
    "decode_json",
    "unknown_top_level_fields",
]


def decode_json(data: bytes | str) -> Any:
    """Decode a Kalshi payload, keeping JSON numbers exact.

    ``parse_float=Decimal`` is the point of this function. Without it,
    ``fee_multiplier`` and the strike values arrive as binary floats and their
    exact decimal digits are gone before validation can object.
    """
    return json.loads(data, parse_float=Decimal)


# --------------------------------------------------------------------------
# Exact field types
# --------------------------------------------------------------------------
# Each validates eagerly through the fixed-point parsers, so a malformed
# financially relevant value fails at parse time. Serialisation renders back to
# the venue's exact wire string, which keeps round-tripping lossless.

PriceField = Annotated[
    Price,
    BeforeValidator(lambda v: v if isinstance(v, Price) else parse_price_dollars(v)),
    PlainSerializer(lambda p: p.to_str(), return_type=str),
]
QuantityField = Annotated[
    Quantity,
    BeforeValidator(lambda v: v if isinstance(v, Quantity) else parse_quantity_fp(v)),
    PlainSerializer(lambda q: q.to_str(), return_type=str),
]
QuantityDeltaField = Annotated[
    QuantityDelta,
    BeforeValidator(lambda v: v if isinstance(v, QuantityDelta) else parse_quantity_delta_fp(v)),
    PlainSerializer(lambda q: q.to_str(), return_type=str),
]
MoneyField = Annotated[
    Money,
    BeforeValidator(lambda v: v if isinstance(v, Money) else parse_money_dollars(v)),
    PlainSerializer(lambda m: m.to_str(), return_type=str),
]
MultiplierField = Annotated[
    Decimal,
    BeforeValidator(parse_fee_multiplier),
    PlainSerializer(str, return_type=str),
]


class _WireModel(BaseModel):
    """Base for every Kalshi wire model.

    ``frozen`` because a parsed payload is a record of what was received and
    must not be edited in place; ``extra="ignore"`` per the forward-compatibility
    policy above.
    """

    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)


_ModelT = TypeVar("_ModelT", bound=BaseModel)


def unknown_top_level_fields(model: type[BaseModel], payload: Mapping[str, Any]) -> tuple[str, ...]:
    """Keys present in ``payload`` that ``model`` does not declare.

    Used for observability: a growing set here means Kalshi has added fields and
    we should decide whether any of them are financially relevant. Returns names
    in sorted order so log output is stable.
    """
    known: set[str] = set()
    for name, info in model.model_fields.items():
        known.add(name)
        if info.alias:
            known.add(info.alias)
    return tuple(sorted(set(payload) - known))


def _ensure_utc_optional(value: datetime | None) -> datetime | None:
    return None if value is None else ensure_utc(value)


# --------------------------------------------------------------------------
# Shared fragments
# --------------------------------------------------------------------------


class KalshiSettlementSource(_WireModel):
    """An official source used to determine markets in a series or event."""

    name: str | None = None
    url: str | None = None


class KalshiPriceRange(_WireModel):
    """One band of the market's valid price grid.

    Endpoints are inclusive and adjacent bands share an endpoint; see
    :class:`predarb.domain.models.PriceGrid` for the verified boundary
    semantics.
    """

    start: PriceField
    end: PriceField
    step: PriceField


class KalshiMveLeg(_WireModel):
    """One leg of a multivariate (combination) market."""

    event_ticker: str | None = None
    market_ticker: str | None = None
    side: str | None = None


# --------------------------------------------------------------------------
# Series
# --------------------------------------------------------------------------


class KalshiSeries(_WireModel):
    ticker: str
    title: str | None = None
    category: str | None = None
    categories: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    frequency: str | None = None
    contract_url: str | None = None
    contract_terms_url: str | None = None
    settlement_sources: tuple[KalshiSettlementSource, ...] = ()
    additional_prohibitions: tuple[str, ...] = ()
    fee_type: str | None = None
    """Not an enum: ``margin_market_maker_program_fees`` was observed live and is
    absent from the documented four-value set."""

    fee_multiplier: MultiplierField | None = None
    last_updated_ts: datetime | None = None
    exchange_index: int | None = None

    _utc = field_validator("last_updated_ts")(_ensure_utc_optional)


class KalshiSeriesEnvelope(_WireModel):
    """``GET /series/{series_ticker}``."""

    series: KalshiSeries


# --------------------------------------------------------------------------
# Event
# --------------------------------------------------------------------------


class KalshiEvent(_WireModel):
    event_ticker: str
    series_ticker: str
    title: str | None = None
    sub_title: str | None = None
    category: str | None = None
    mutually_exclusive: bool | None = None
    """Venue metadata meaning "at most one market here can resolve YES".

    Preserved exactly. It supports an ``AT_MOST_ONE`` relation as evidence and
    establishes nothing about exhaustiveness."""

    collateral_return_type: str | None = None
    """Observed ``"MECNET"`` and ``""``; documented values are not enumerated,
    so this stays an opaque string (A-16)."""

    strike_date: datetime | None = None
    strike_period: str | None = None
    settlement_sources: tuple[KalshiSettlementSource, ...] = ()
    fee_type_override: str | None = None
    fee_multiplier_override: MultiplierField | None = None
    last_updated_ts: datetime | None = None
    exchange_index: int | None = None

    _utc = field_validator("strike_date", "last_updated_ts")(_ensure_utc_optional)


class KalshiEventEnvelope(_WireModel):
    """``GET /events/{event_ticker}``."""

    event: KalshiEvent
    markets: tuple[KalshiMarket, ...] = ()


class KalshiEventsPage(_WireModel):
    """``GET /events``."""

    events: tuple[KalshiEvent, ...] = ()
    cursor: str | None = None


# --------------------------------------------------------------------------
# Market
# --------------------------------------------------------------------------


class KalshiMarket(_WireModel):
    ticker: str
    event_ticker: str
    market_type: str
    status: str
    title: str | None = None
    yes_sub_title: str | None = None
    no_sub_title: str | None = None

    # --- payout and grid ---
    notional_value_dollars: PriceField | None = None
    price_level_structure: str | None = None
    price_ranges: tuple[KalshiPriceRange, ...] = ()

    # --- quotes ---
    yes_bid_dollars: PriceField | None = None
    yes_ask_dollars: PriceField | None = None
    no_bid_dollars: PriceField | None = None
    no_ask_dollars: PriceField | None = None
    last_price_dollars: PriceField | None = None
    previous_yes_bid_dollars: PriceField | None = None
    previous_yes_ask_dollars: PriceField | None = None
    previous_price_dollars: PriceField | None = None
    yes_bid_size_fp: QuantityField | None = None
    yes_ask_size_fp: QuantityField | None = None
    no_bid_size_fp: QuantityField | None = None
    no_ask_size_fp: QuantityField | None = None
    liquidity_dollars: MoneyField | None = None
    volume_fp: QuantityField | None = None
    volume_24h_fp: QuantityField | None = None
    open_interest_fp: QuantityField | None = None

    # --- lifecycle ---
    open_time: datetime | None = None
    close_time: datetime | None = None
    expiration_time: datetime | None = None
    expected_expiration_time: datetime | None = None
    latest_expiration_time: datetime | None = None
    created_time: datetime | None = None
    updated_time: datetime | None = None
    settlement_timer_seconds: int | None = None
    settlement_ts: datetime | None = None
    can_close_early: bool | None = None
    early_close_condition: str | None = None
    fee_waiver_expiration_time: datetime | None = None

    # --- settlement ---
    result: str | None = None
    settlement_value_dollars: PriceField | None = None
    expiration_value: str | None = None

    # --- rules and strikes ---
    rules_primary: str | None = None
    rules_secondary: str | None = None
    strike_type: str | None = None
    floor_strike: Decimal | None = None
    cap_strike: Decimal | None = None
    functional_strike: str | None = None
    custom_strike: dict[str, str] | None = None

    # --- structure ---
    mve_collection_ticker: str | None = None
    mve_selected_legs: tuple[KalshiMveLeg, ...] = ()
    is_provisional: bool | None = None
    occurrence_datetime: datetime | None = None
    exchange_index: int | None = None

    _utc = field_validator(
        "open_time",
        "close_time",
        "expiration_time",
        "expected_expiration_time",
        "latest_expiration_time",
        "created_time",
        "updated_time",
        "settlement_ts",
        "fee_waiver_expiration_time",
        "occurrence_datetime",
    )(_ensure_utc_optional)

    @field_validator("floor_strike", "cap_strike", mode="before")
    @classmethod
    def _reject_float_strike(cls, value: object) -> object:
        """Strikes are JSON numbers; reject a float that slipped past decoding.

        A strike is a measurement threshold rather than money, but it still
        decides settlement, so it is held exactly. Reaching here as a ``float``
        means the payload was decoded without ``parse_float=Decimal``.
        """
        if isinstance(value, float):
            # ValueError, not TypeError: Pydantic only converts ValueError and
            # AssertionError into a ValidationError. A TypeError would escape
            # the validation machinery and surface as an unrelated crash.
            raise ValueError(  # noqa: TRY004
                f"strike {value!r} arrived as a float; decode the payload with "
                "predarb.venues.kalshi.models.decode_json to keep it exact"
            )
        return value


class KalshiMarketEnvelope(_WireModel):
    """``GET /markets/{ticker}``."""

    market: KalshiMarket


class KalshiMarketsPage(_WireModel):
    """``GET /markets``."""

    markets: tuple[KalshiMarket, ...] = ()
    cursor: str | None = None


# --------------------------------------------------------------------------
# Order book (REST)
# --------------------------------------------------------------------------

PriceLevelPair = tuple[PriceField, QuantityField]
"""One ``[price, count]`` pair, e.g. ``["0.4200", "13.00"]``."""


class KalshiOrderbook(_WireModel):
    """Bids only, two sides.

    There is no ask array and none is invented here: the wire model mirrors the
    venue. Ask derivation needs the contract's notional and belongs to the layer
    that has it.

    Array order is *not* normalised here either -- this type records what
    arrived. Sorting happens in normalisation, deliberately and visibly, because
    the documented and observed orderings disagree (A-07).
    """

    yes_dollars: tuple[PriceLevelPair, ...] = ()
    no_dollars: tuple[PriceLevelPair, ...] = ()


class KalshiOrderbookEnvelope(_WireModel):
    """``GET /markets/{ticker}/orderbook``."""

    orderbook_fp: KalshiOrderbook


# --------------------------------------------------------------------------
# Fee changes
# --------------------------------------------------------------------------


class KalshiSeriesFeeChange(_WireModel):
    id: str
    series_ticker: str
    fee_type: str
    fee_multiplier: MultiplierField
    scheduled_ts: datetime

    @field_validator("scheduled_ts")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class KalshiSeriesFeeChangesResponse(_WireModel):
    """``GET /series/fee_changes``.

    Returns an empty array unless ``show_historical=true`` is passed (A-12).
    """

    series_fee_change_arr: tuple[KalshiSeriesFeeChange, ...] = ()


class KalshiEventFeeChange(_WireModel):
    id: str
    event_ticker: str
    series_ticker: str | None = None
    fee_type_override: str
    fee_multiplier_override: MultiplierField
    scheduled_ts: datetime

    @field_validator("scheduled_ts")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class KalshiEventFeeChangesResponse(_WireModel):
    """``GET /events/fee_changes`` -- note the plural path segment."""

    event_fee_changes: tuple[KalshiEventFeeChange, ...] = ()
    cursor: str | None = None


# --------------------------------------------------------------------------
# Exchange status
# --------------------------------------------------------------------------


class KalshiExchangeShardStatus(_WireModel):
    """Trading status for one exchange shard.

    Shards are real: markets, events and series all carry ``exchange_index``,
    and the shards observed live are Default (0), Combos (1), Crypto &
    Commodities (2) and a sports shard (3). A shard can be halted while others
    trade, so a book from a halted shard is not a tradeable book.
    """

    exchange_index: int | None = None
    description: str | None = None
    exchange_active: bool | None = None
    trading_active: bool | None = None
    intra_exchange_transfers_active: bool | None = None


class KalshiExchangeStatus(_WireModel):
    """``GET /exchange/status``."""

    exchange_active: bool | None = None
    trading_active: bool | None = None
    intra_exchange_transfers_active: bool | None = None
    exchange_index_statuses: tuple[KalshiExchangeShardStatus, ...] = ()


# --------------------------------------------------------------------------
# Account limits (authenticated, read-only)
# --------------------------------------------------------------------------
# These two endpoints are the authoritative source for rate limiting. They are
# account-scoped but disclose no balance, position, order or fill data -- only
# the token budget and per-endpoint costs -- which is why Phase 1 calls them and
# no other /account route.


class KalshiBucketLimit(_WireModel):
    """One token bucket's configuration."""

    refill_rate: int
    """Tokens added per second."""

    bucket_capacity: int
    """Maximum tokens held; also the largest burst after idling."""


class KalshiUsageLevelGrant(_WireModel):
    """A grant raising the account's API usage level."""

    exchange_instance: str | None = None
    level: str | None = None
    expires_ts: int | None = None
    source: str | None = None


class KalshiAccountLimits(_WireModel):
    """``GET /account/limits``.

    ``usage_tier`` is the account's effective Predictions API tier (basic,
    advanced, expert, premier, paragon, prime, prestige). Read and write budgets
    are reported separately and are modelled separately.
    """

    usage_tier: str | None = None
    read: KalshiBucketLimit | None = None
    write: KalshiBucketLimit | None = None
    grants: tuple[KalshiUsageLevelGrant, ...] = ()


class KalshiEndpointTokenCost(_WireModel):
    """One endpoint whose token cost differs from the default."""

    method: str
    path: str
    cost: int


class KalshiEndpointCosts(_WireModel):
    """``GET /account/endpoint_costs``.

    ``default_cost`` is currently 10, but it is read rather than assumed: it is
    account and server configuration, not a constant.
    """

    default_cost: int
    endpoint_costs: tuple[KalshiEndpointTokenCost, ...] = ()


# --------------------------------------------------------------------------
# WebSocket
# --------------------------------------------------------------------------


class KalshiWsSnapshotMsg(_WireModel):
    """``msg`` of an ``orderbook_snapshot``.

    Field names differ from the REST book: ``yes_dollars_fp`` here versus
    ``orderbook_fp.yes_dollars`` there. Modelled separately for that reason.
    """

    market_ticker: str
    market_id: str | None = None
    yes_dollars_fp: tuple[PriceLevelPair, ...] = ()
    no_dollars_fp: tuple[PriceLevelPair, ...] = ()


class KalshiWsDeltaMsg(_WireModel):
    """``msg`` of an ``orderbook_delta``.

    ``delta_fp`` is a **signed relative** change to the resting size at
    ``price_dollars``, not a replacement value.
    """

    market_ticker: str
    market_id: str | None = None
    price_dollars: PriceField
    delta_fp: QuantityDeltaField
    side: str
    ts_ms: int | None = None
    client_order_id: str | None = None
    subaccount: int | None = None


class KalshiWsErrorMsg(_WireModel):
    code: int | None = None
    msg: str | None = None


class KalshiWsEnvelope(_WireModel):
    """The common WebSocket frame.

    ``seq`` is preserved exactly and is **not** interpreted here. Its scoping --
    connection-global, per-``sid``, per-market or per-channel -- is unverified
    (A-09) and cannot be settled from a single message pair, so nothing in this
    layer assumes an answer.
    """

    type: str
    sid: int | None = None
    seq: int | None = None
    id: int | None = None
    msg: dict[str, Any] | None = None

    def as_snapshot(self) -> KalshiWsSnapshotMsg:
        return KalshiWsSnapshotMsg.model_validate(self._require_msg("orderbook_snapshot"))

    def as_delta(self) -> KalshiWsDeltaMsg:
        return KalshiWsDeltaMsg.model_validate(self._require_msg("orderbook_delta"))

    def as_error(self) -> KalshiWsErrorMsg:
        return KalshiWsErrorMsg.model_validate(self._require_msg("error"))

    def _require_msg(self, expected_type: str) -> dict[str, Any]:
        if self.type != expected_type:
            raise ValueError(f"expected a {expected_type!r} frame, got {self.type!r}")
        if self.msg is None:
            raise ValueError(f"{expected_type!r} frame has no msg body")
        return self.msg
