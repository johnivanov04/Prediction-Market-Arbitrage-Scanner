"""Venue-independent domain types produced by normalisation.

This module holds the subset of the data model that Step 2 can populate
honestly. The full canonical model described in ``docs/data_model.md``
(``CanonicalEvent`` -> ``Proposition`` -> ``SettlementSpec`` -> ``VenueInstrument``)
is built out in later steps; what exists here is the venue-instrument layer plus
the price grid, because those are what a wire payload can actually establish.

Nothing here infers meaning. A :class:`VenueInstrument` records what the venue
says about a contract. It deliberately carries no proposition link and no
settlement *spec*: both require reading contract rules, and inventing them from
metadata is precisely the failure this system exists to avoid.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from predarb.domain.enums import SettlementKind, VenueId
from predarb.domain.money import Price, Quantity

__all__ = [
    "EXCLUSION_INCONSISTENT_GRID",
    "EXCLUSION_MULTIVARIATE",
    "EXCLUSION_NON_BINARY",
    "EXCLUSION_NO_GRID",
    "EXCLUSION_NO_NOTIONAL",
    "EXCLUSION_PROVISIONAL",
    "PriceBand",
    "PriceGrid",
    "VenueEvent",
    "VenueInstrument",
    "VenueSeries",
]


# Reasons an instrument is excluded from Phase 1. Domain vocabulary rather than
# venue detail: they describe properties of a contract, and every consumer of
# VenueInstrument needs to reason about them without importing a venue module.
EXCLUSION_MULTIVARIATE: Final = "MULTIVARIATE_COMBINATION_MARKET"
EXCLUSION_PROVISIONAL: Final = "PROVISIONAL_MARKET"
EXCLUSION_NON_BINARY: Final = "NON_BINARY_SETTLEMENT"
EXCLUSION_NO_NOTIONAL: Final = "NOTIONAL_VALUE_ABSENT"
EXCLUSION_NO_GRID: Final = "PRICE_GRID_ABSENT"
EXCLUSION_INCONSISTENT_GRID: Final = "PRICE_GRID_INCONSISTENT"


@dataclass(frozen=True, slots=True)
class PriceBand:
    """A contiguous price range with a single tick size.

    Endpoints are **inclusive at both ends**; see :class:`PriceGrid` for why
    that is the correct reading rather than a guess.
    """

    start: Price
    end: Price
    step: Price

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(f"price band end {self.end} precedes start {self.start}")
        if self.step.units <= 0:
            raise ValueError(f"price band step must be positive, got {self.step}")
        span = self.end.units - self.start.units
        if span % self.step.units != 0:
            raise ValueError(
                f"price band [{self.start}, {self.end}] is not a whole number of "
                f"{self.step} steps; the venue's grid definition is inconsistent"
            )

    def contains(self, price: Price) -> bool:
        """Whether ``price`` lies within the band, endpoints included."""
        return self.start.units <= price.units <= self.end.units

    def is_on_grid(self, price: Price) -> bool:
        """Whether ``price`` sits exactly on this band's tick grid."""
        return (price.units - self.start.units) % self.step.units == 0

    def accepts(self, price: Price) -> bool:
        return self.contains(price) and self.is_on_grid(price)

    def snap_down(self, price: Price) -> Price | None:
        """Largest grid price in this band that is ``<= price``."""
        if price.units < self.start.units:
            return None
        capped = min(price.units, self.end.units)
        offset = (capped - self.start.units) // self.step.units * self.step.units
        return Price(self.start.units + offset)

    def snap_up(self, price: Price) -> Price | None:
        """Smallest grid price in this band that is ``>= price``."""
        if price.units > self.end.units:
            return None
        floored = max(price.units, self.start.units)
        span = floored - self.start.units
        steps = -(-span // self.step.units)
        candidate = self.start.units + steps * self.step.units
        if candidate > self.end.units:
            return None
        return Price(candidate)


@dataclass(frozen=True, slots=True)
class PriceGrid:
    """The set of prices an instrument can trade at.

    Kalshi markets carry a ``price_ranges`` array, and tick size is **not**
    uniform: three structures were observed live, two of them multi-band with
    finer ticks at the book edges (``docs/api_assumptions.md`` A-04).

    Boundary semantics -- resolved, not assumed
    -------------------------------------------
    Adjacent bands share an endpoint (``band[i].end == band[i+1].start``), which
    raises the question of which band owns it. Checked against all three
    observed structures:

    * every band is contiguous with the next;
    * every band's grid lands exactly on its own endpoint;
    * at every interior boundary, **both** adjacent bands accept the price.

    Since the bands agree, ownership does not affect validity, and a price is
    valid if **any** band accepts it. :meth:`has_consistent_boundaries` asserts
    that agreement; it is exercised against every captured fixture, so a future
    structure whose bands disagree fails a test rather than silently changing
    which prices are considered tradeable.

    ``structure_name`` (Kalshi's ``price_level_structure``) is retained as
    metadata only. Business logic uses the bands, never the name.
    """

    bands: tuple[PriceBand, ...]
    structure_name: str | None = None

    def __post_init__(self) -> None:
        if not self.bands:
            raise ValueError("price grid must have at least one band")
        ordered = sorted(self.bands, key=lambda b: b.start.units)
        if list(ordered) != list(self.bands):
            raise ValueError("price grid bands must be supplied in ascending order")

    def is_valid_price(self, price: Price) -> bool:
        """Whether ``price`` is on the instrument's tradeable grid."""
        return any(band.accepts(price) for band in self.bands)

    def bands_covering(self, price: Price) -> tuple[PriceBand, ...]:
        """Every band whose range contains ``price`` (two at a shared boundary)."""
        return tuple(band for band in self.bands if band.contains(price))

    def has_consistent_boundaries(self) -> bool:
        """Whether every price covered by more than one band is judged the same way.

        For the observed structures this holds *structurally* rather than by
        luck: each band's span is a whole number of its own steps (enforced by
        :class:`PriceBand`), and the bands are contiguous, so a shared endpoint
        is necessarily on both bands' grids. Contiguous grids can therefore
        never fail this check.

        It is not vacuous, though. It is a guard against a future structure
        whose bands **overlap** over a range rather than meeting at a point. Two
        overlapping bands with different steps would disagree about prices
        inside the overlap, and the union reading used by
        :meth:`is_valid_price` would then be genuinely ambiguous -- at which
        point the instrument is excluded rather than guessed at.
        """
        for index, lower in enumerate(self.bands):
            for upper in self.bands[index + 1 :]:
                overlap_start = max(lower.start.units, upper.start.units)
                overlap_end = min(lower.end.units, upper.end.units)
                if overlap_start > overlap_end:
                    continue
                step = min(lower.step.units, upper.step.units)
                for units in range(overlap_start, overlap_end + 1, step):
                    candidate = Price(units)
                    if lower.accepts(candidate) != upper.accepts(candidate):
                        return False
        return True

    def is_contiguous(self) -> bool:
        """Whether the bands tile their span with no gaps or overlaps."""
        return all(
            self.bands[i].end.units == self.bands[i + 1].start.units
            for i in range(len(self.bands) - 1)
        )

    def snap_down(self, price: Price) -> Price | None:
        """Largest valid grid price ``<= price``, across all bands."""
        candidates = [
            snapped for band in self.bands if (snapped := band.snap_down(price)) is not None
        ]
        return max(candidates, default=None)

    def snap_up(self, price: Price) -> Price | None:
        """Smallest valid grid price ``>= price``, across all bands."""
        candidates = [
            snapped for band in self.bands if (snapped := band.snap_up(price)) is not None
        ]
        return min(candidates, default=None)


@dataclass(frozen=True, slots=True)
class VenueSeries:
    """A venue series: the template a family of events is issued from."""

    venue: VenueId
    ticker: str
    title: str
    category: str | None
    frequency: str | None
    settlement_source_names: tuple[str, ...]
    settlement_source_urls: tuple[str, ...]
    contract_url: str | None
    contract_terms_url: str | None
    fee_type_raw: str | None
    """The venue's fee type string, kept verbatim. Undocumented values occur."""
    last_updated: datetime | None


@dataclass(frozen=True, slots=True)
class VenueEvent:
    """A venue event: a group of markets issued together."""

    venue: VenueId
    ticker: str
    series_ticker: str
    title: str
    sub_title: str | None
    mutually_exclusive: bool | None
    """Venue metadata, preserved exactly.

    ``True`` is *evidence supporting* an ``AT_MOST_ONE`` relation and nothing
    more. It never establishes exhaustiveness, so it can never imply
    ``EXACTLY_ONE``. ``None`` means the venue did not say.
    """

    collateral_return_type: str | None
    """Raw venue value (``"MECNET"``, ``""``). Capital-netting semantics are
    unresolved -- see ``docs/api_assumptions.md`` A-16 -- so this is carried as
    an opaque string rather than interpreted."""

    settlement_source_names: tuple[str, ...]
    settlement_source_urls: tuple[str, ...]
    last_updated: datetime | None


@dataclass(frozen=True, slots=True)
class VenueInstrument:
    """A tradeable contract as the venue currently describes it.

    This is *venue metadata*, not a proven settlement model. ``settlement_kind``
    reflects only what the venue's ``market_type`` claims; a full
    ``SettlementSpec`` still requires reading the contract rules, and until that
    exists the instrument cannot support a contractual-arbitrage claim.
    """

    venue: VenueId
    ticker: str
    event_ticker: str
    title: str
    status_raw: str
    settlement_kind: SettlementKind
    market_type_raw: str
    notional_value: Price | None
    """Per-contract payout, read from metadata. Never assumed to be $1.00.

    ``None`` when the venue did not supply it. That is represented rather than
    defaulted, because a guessed notional silently rescales every payoff; an
    instrument without one is excluded instead."""

    price_grid: PriceGrid | None
    """``None`` when ``price_ranges`` was absent or self-inconsistent."""

    yes_sub_title: str | None
    no_sub_title: str | None
    rules_primary: str | None
    rules_secondary: str | None
    rules_hash: str | None
    """SHA-256 over the exact rules text, for relation invalidation."""

    open_time: datetime | None
    close_time: datetime | None
    expected_expiration_time: datetime | None
    latest_expiration_time: datetime | None
    settlement_timer_seconds: int | None
    result_raw: str | None
    settlement_value: Price | None
    can_close_early: bool | None
    yes_bid: Price | None
    yes_ask: Price | None
    no_bid: Price | None
    no_ask: Price | None
    yes_bid_size: Quantity | None
    yes_ask_size: Quantity | None
    exclusion_reasons: tuple[str, ...] = field(default_factory=tuple)
    """Why Phase 1 must not scan this instrument (combo, provisional, ...).

    Non-empty means excluded. The reasons are recorded rather than reduced to a
    boolean so an audit can say *why* an instrument was skipped.
    """

    @property
    def is_excluded(self) -> bool:
        return bool(self.exclusion_reasons)
