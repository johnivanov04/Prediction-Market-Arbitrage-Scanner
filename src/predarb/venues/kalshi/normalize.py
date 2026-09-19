"""Kalshi wire objects -> domain objects.

Every function here is **deterministic and side-effect free**: same input, same
output, no I/O, no clock reads. That is what lets normalisation run identically
in live capture and in replay.

What normalisation will not do
------------------------------
* It does not create semantic relations. ``mutually_exclusive`` is carried
  through as venue metadata; turning it into an ``AT_MOST_ONE`` relation is a
  reviewed act in the semantics layer, and it can never produce ``EXACTLY_ONE``.
* It does not invent missing data. An absent notional or price grid becomes
  ``None`` plus an exclusion reason, never a default.
* It does not decide that a contract settles binary. It records what the venue's
  ``market_type`` claims; a proven ``SettlementSpec`` still requires reading the
  contract rules.
* It does not derive asks. That needs the notional and belongs to the book
  layer.
"""

from __future__ import annotations

import hashlib
from datetime import datetime

from predarb.books.levels import (
    BookLevel,
    BookSource,
    BookTransport,
    QuotedBook,
    sort_levels_best_first,
)
from predarb.domain.enums import MarketSide, SettlementKind, VenueId
from predarb.domain.fees import FeeConfiguration, FeeScope, FeeTimeline, ScheduledFeeChange
from predarb.domain.models import (
    EXCLUSION_INCONSISTENT_GRID,
    EXCLUSION_MULTIVARIATE,
    EXCLUSION_NO_GRID,
    EXCLUSION_NO_NOTIONAL,
    EXCLUSION_NON_BINARY,
    EXCLUSION_PROVISIONAL,
    PriceBand,
    PriceGrid,
    VenueEvent,
    VenueInstrument,
    VenueSeries,
)
from predarb.domain.money import Price, Quantity
from predarb.venues.kalshi.models import (
    KalshiEvent,
    KalshiEventFeeChange,
    KalshiMarket,
    KalshiOrderbook,
    KalshiPriceRange,
    KalshiSeries,
    KalshiSeriesFeeChange,
    KalshiWsSnapshotMsg,
)

__all__ = [
    "EXCLUSION_INCONSISTENT_GRID",
    "EXCLUSION_MULTIVARIATE",
    "EXCLUSION_NON_BINARY",
    "EXCLUSION_NO_GRID",
    "EXCLUSION_NO_NOTIONAL",
    "EXCLUSION_PROVISIONAL",
    "book_source_from_rest",
    "build_fee_timeline",
    "exclusion_reasons_for",
    "normalize_event",
    "normalize_market",
    "normalize_orderbook",
    "normalize_price_grid",
    "normalize_series",
    "normalize_ws_snapshot",
    "rules_hash_for",
    "settlement_kind_for",
]

# Re-exported from the domain so existing callers keep working; the constants
# themselves live beside VenueInstrument, because they describe the contract
# rather than anything Kalshi-specific.

_MARKET_TYPE_TO_SETTLEMENT = {
    "binary": SettlementKind.BINARY,
    "scalar": SettlementKind.SCALAR,
}


def settlement_kind_for(market_type_raw: str | None) -> SettlementKind:
    """Map the venue's ``market_type`` to a settlement kind, failing closed.

    An unrecognised or absent value becomes :attr:`SettlementKind.UNKNOWN`, which
    is ineligible for contractual-arbitrage classification. A new venue value
    must never be silently treated as a standard binary.
    """
    if market_type_raw is None:
        return SettlementKind.UNKNOWN
    return _MARKET_TYPE_TO_SETTLEMENT.get(market_type_raw.strip().lower(), SettlementKind.UNKNOWN)


def rules_hash_for(primary: str | None, secondary: str | None) -> str | None:
    """Stable SHA-256 over a market's rules text.

    Relations record this at proof time; when it changes, the proof is
    invalidated. The two parts are joined with a NUL separator so that moving
    text between them changes the hash rather than producing a collision.
    Returns ``None`` when there is no rules text at all, which is itself a
    reason not to trust the instrument.
    """
    if primary is None and secondary is None:
        return None
    payload = f"{primary or ''}\x00{secondary or ''}".encode()
    return hashlib.sha256(payload).hexdigest()


def normalize_price_grid(
    ranges: tuple[KalshiPriceRange, ...], structure_name: str | None = None
) -> PriceGrid | None:
    """Build a :class:`PriceGrid` from ``price_ranges``.

    Returns ``None`` when the venue supplied no bands. Bands are sorted by start
    price rather than trusting array order, consistent with how book levels are
    handled.
    """
    if not ranges:
        return None
    bands = tuple(
        sorted(
            (PriceBand(start=r.start, end=r.end, step=r.step) for r in ranges),
            key=lambda b: b.start.units,
        )
    )
    return PriceGrid(bands=bands, structure_name=structure_name)


def exclusion_reasons_for(
    market: KalshiMarket, grid: PriceGrid | None, settlement: SettlementKind
) -> tuple[str, ...]:
    """Why Phase 1 must not scan this instrument.

    Reasons are accumulated rather than short-circuited so an audit shows every
    problem with an instrument, not just the first one found.
    """
    reasons: list[str] = []
    if market.mve_collection_ticker or market.mve_selected_legs:
        reasons.append(EXCLUSION_MULTIVARIATE)
    if market.is_provisional:
        reasons.append(EXCLUSION_PROVISIONAL)
    if settlement is not SettlementKind.BINARY:
        reasons.append(EXCLUSION_NON_BINARY)
    if market.notional_value_dollars is None:
        reasons.append(EXCLUSION_NO_NOTIONAL)
    if grid is None:
        reasons.append(EXCLUSION_NO_GRID)
    elif not (grid.has_consistent_boundaries() and grid.is_contiguous()):
        reasons.append(EXCLUSION_INCONSISTENT_GRID)
    return tuple(reasons)


def normalize_market(market: KalshiMarket) -> VenueInstrument:
    """Wire market -> :class:`VenueInstrument`."""
    grid = normalize_price_grid(market.price_ranges, market.price_level_structure)
    settlement = settlement_kind_for(market.market_type)
    return VenueInstrument(
        venue=VenueId.KALSHI,
        ticker=market.ticker,
        event_ticker=market.event_ticker,
        title=market.title or "",
        status_raw=market.status,
        settlement_kind=settlement,
        market_type_raw=market.market_type,
        notional_value=market.notional_value_dollars,
        price_grid=grid,
        yes_sub_title=market.yes_sub_title,
        no_sub_title=market.no_sub_title,
        rules_primary=market.rules_primary,
        rules_secondary=market.rules_secondary,
        rules_hash=rules_hash_for(market.rules_primary, market.rules_secondary),
        open_time=market.open_time,
        close_time=market.close_time,
        expected_expiration_time=market.expected_expiration_time,
        latest_expiration_time=market.latest_expiration_time,
        settlement_timer_seconds=market.settlement_timer_seconds,
        result_raw=market.result,
        settlement_value=market.settlement_value_dollars,
        can_close_early=market.can_close_early,
        yes_bid=market.yes_bid_dollars,
        yes_ask=market.yes_ask_dollars,
        no_bid=market.no_bid_dollars,
        no_ask=market.no_ask_dollars,
        yes_bid_size=market.yes_bid_size_fp,
        yes_ask_size=market.yes_ask_size_fp,
        exclusion_reasons=exclusion_reasons_for(market, grid, settlement),
    )


def normalize_series(series: KalshiSeries) -> VenueSeries:
    """Wire series -> :class:`VenueSeries`."""
    return VenueSeries(
        venue=VenueId.KALSHI,
        ticker=series.ticker,
        title=series.title or "",
        category=series.category,
        frequency=series.frequency,
        settlement_source_names=tuple(s.name or "" for s in series.settlement_sources),
        settlement_source_urls=tuple(s.url or "" for s in series.settlement_sources),
        contract_url=series.contract_url,
        contract_terms_url=series.contract_terms_url,
        fee_type_raw=series.fee_type,
        last_updated=series.last_updated_ts,
    )


def normalize_event(event: KalshiEvent) -> VenueEvent:
    """Wire event -> :class:`VenueEvent`.

    ``mutually_exclusive`` and ``collateral_return_type`` are copied verbatim.
    No relation is created here.
    """
    return VenueEvent(
        venue=VenueId.KALSHI,
        ticker=event.event_ticker,
        series_ticker=event.series_ticker,
        title=event.title or "",
        sub_title=event.sub_title,
        mutually_exclusive=event.mutually_exclusive,
        collateral_return_type=event.collateral_return_type,
        settlement_source_names=tuple(s.name or "" for s in event.settlement_sources),
        settlement_source_urls=tuple(s.url or "" for s in event.settlement_sources),
        last_updated=event.last_updated_ts,
    )


def _levels_from_pairs(
    pairs: tuple[tuple[Price, Quantity], ...], side: MarketSide
) -> tuple[BookLevel, ...]:
    """Build sorted, deduplicated levels from wire ``[price, count]`` pairs.

    Zero-size levels are dropped: they carry no executable depth, and keeping
    them would put a price point in the book that nothing can be traded against.
    A repeated price is an error rather than something to merge -- the venue
    publishes aggregated levels, so a duplicate means the payload is malformed
    and quietly summing it would fabricate depth.
    """
    seen: set[int] = set()
    levels: list[BookLevel] = []
    for price, quantity in pairs:
        if quantity.is_zero:
            continue
        if price.units in seen:
            raise ValueError(
                f"duplicate {side} price level at {price}; the venue publishes "
                "aggregated levels, so a repeat means the payload is malformed"
            )
        seen.add(price.units)
        levels.append(BookLevel(price=price, quantity=quantity, side=side))
    return sort_levels_best_first(levels)


def normalize_orderbook(orderbook: KalshiOrderbook, source: BookSource) -> QuotedBook:
    """REST order book -> :class:`QuotedBook`.

    Levels are sorted explicitly, best (highest) bid first. The venue's array
    order is not trusted: its documentation and its live responses disagree
    (``docs/api_assumptions.md`` A-07).
    """
    return QuotedBook(
        source=source,
        yes_bids=_levels_from_pairs(orderbook.yes_dollars, MarketSide.YES),
        no_bids=_levels_from_pairs(orderbook.no_dollars, MarketSide.NO),
    )


def normalize_ws_snapshot(snapshot: KalshiWsSnapshotMsg, source: BookSource) -> QuotedBook:
    """WebSocket ``orderbook_snapshot`` -> :class:`QuotedBook`.

    Same result as :func:`normalize_orderbook` from different field names
    (``yes_dollars_fp`` rather than ``orderbook_fp.yes_dollars``).
    """
    return QuotedBook(
        source=source,
        yes_bids=_levels_from_pairs(snapshot.yes_dollars_fp, MarketSide.YES),
        no_bids=_levels_from_pairs(snapshot.no_dollars_fp, MarketSide.NO),
    )


def build_fee_timeline(
    *,
    series: KalshiSeries,
    event: KalshiEvent | None = None,
    series_changes: tuple[KalshiSeriesFeeChange, ...] = (),
    event_changes: tuple[KalshiEventFeeChange, ...] = (),
) -> FeeTimeline:
    """Assemble a point-in-time resolvable :class:`FeeTimeline`.

    The three sources stay separate; nothing is flattened into a single
    timeless multiplier. Changes are filtered to the relevant tickers so a
    whole-exchange fee-change feed can be passed in directly.
    """
    base: FeeConfiguration | None = None
    if series.fee_type is not None and series.fee_multiplier is not None:
        base = FeeConfiguration(
            fee_type_raw=series.fee_type,
            multiplier=series.fee_multiplier,
            scope=FeeScope.SERIES,
            scope_ticker=series.ticker,
        )

    override: FeeConfiguration | None = None
    if (
        event is not None
        and event.fee_type_override is not None
        and event.fee_multiplier_override is not None
    ):
        override = FeeConfiguration(
            fee_type_raw=event.fee_type_override,
            multiplier=event.fee_multiplier_override,
            scope=FeeScope.EVENT,
            scope_ticker=event.event_ticker,
        )

    scheduled_series = tuple(
        ScheduledFeeChange(
            change_id=change.id,
            scope=FeeScope.SERIES,
            scope_ticker=change.series_ticker,
            fee_type_raw=change.fee_type,
            multiplier=change.fee_multiplier,
            scheduled_ts=change.scheduled_ts,
        )
        for change in series_changes
        if change.series_ticker == series.ticker
    )
    # A change with both overrides null is a clearing record, and is carried
    # through as one: the event falls back to its parent series from that
    # moment. Dropping it would leave a superseded override in force forever.
    scheduled_event = tuple(
        ScheduledFeeChange(
            change_id=change.id,
            scope=FeeScope.EVENT,
            scope_ticker=change.event_ticker,
            fee_type_raw=change.fee_type_override,
            multiplier=change.fee_multiplier_override,
            scheduled_ts=change.scheduled_ts,
        )
        for change in event_changes
        if event is not None and change.event_ticker == event.event_ticker
    )

    return FeeTimeline(
        series_ticker=series.ticker,
        event_ticker=event.event_ticker if event is not None else None,
        series_base=base,
        event_override=override,
        series_changes=scheduled_series,
        event_changes=scheduled_event,
    )


def book_source_from_rest(
    *, instrument_ticker: str, received_at: datetime, raw_message_id: str | None = None
) -> BookSource:
    """Provenance for a book built from a REST response."""
    return BookSource(
        transport=BookTransport.REST_SNAPSHOT,
        instrument_ticker=instrument_ticker,
        received_at=received_at,
        raw_message_id=raw_message_id,
    )
