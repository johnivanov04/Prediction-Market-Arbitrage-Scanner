"""Tests for executable depth and gross cost."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from predarb.books.execution import (
    BookNotExecutableError,
    ExecutionContext,
    ExecutionCurve,
    InsufficientDepthError,
    UnsupportedExecutionSemanticsError,
    build_execution_curve,
)
from predarb.books.levels import BookLevel
from predarb.books.orderbook import BookView
from predarb.books.state import BookIntegrity, BookProvenance
from predarb.domain.average_price import AveragePrice
from predarb.domain.enums import MarketSide, SettlementKind, VenueId
from predarb.domain.models import (
    EXCLUSION_MULTIVARIATE,
    EXCLUSION_NO_GRID,
    EXCLUSION_PROVISIONAL,
    VenueInstrument,
)
from predarb.domain.money import Money, Price, Quantity

pytestmark = pytest.mark.unit

T0 = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)
NOTIONAL = Price.from_value("1.0000")


def q(value: str) -> Quantity:
    return Quantity.from_value(value)


def p(value: str) -> Price:
    return Price.from_value(value)


def level(price: str, quantity: str, side: MarketSide) -> BookLevel:
    return BookLevel(price=p(price), quantity=q(quantity), side=side)


def book(
    *,
    yes: list[tuple[str, str]] | None = None,
    no: list[tuple[str, str]] | None = None,
    integrity: BookIntegrity = BookIntegrity.VALID,
    epoch: int = 1,
    ticker: str = "MKT",
    seq: int | None = 10,
) -> BookView:
    """A book view with levels already in best-first order."""
    yes_levels = tuple(
        level(price, quantity, MarketSide.YES)
        for price, quantity in sorted(yes or [], key=lambda r: Decimal(r[0]), reverse=True)
    )
    no_levels = tuple(
        level(price, quantity, MarketSide.NO)
        for price, quantity in sorted(no or [], key=lambda r: Decimal(r[0]), reverse=True)
    )
    return BookView(
        market_ticker=ticker,
        integrity=integrity,
        yes_bids=yes_levels,
        no_bids=no_levels,
        provenance=BookProvenance(connection_epoch=epoch, sid=1, latest_seq=seq),
    )


def instrument(
    *,
    ticker: str = "MKT",
    notional: Price | None = NOTIONAL,
    settlement: SettlementKind = SettlementKind.BINARY,
    exclusions: tuple[str, ...] = (),
) -> VenueInstrument:
    return VenueInstrument(
        venue=VenueId.KALSHI,
        ticker=ticker,
        event_ticker="EVT",
        title="",
        status_raw="active",
        settlement_kind=settlement,
        market_type_raw="binary",
        notional_value=notional,
        price_grid=None,
        yes_sub_title=None,
        no_sub_title=None,
        rules_primary=None,
        rules_secondary=None,
        rules_hash=None,
        open_time=None,
        close_time=None,
        expected_expiration_time=None,
        latest_expiration_time=None,
        settlement_timer_seconds=None,
        result_raw=None,
        settlement_value=None,
        can_close_early=None,
        yes_bid=None,
        yes_ask=None,
        no_bid=None,
        no_ask=None,
        yes_bid_size=None,
        yes_ask_size=None,
        exclusion_reasons=exclusions,
    )


CONTEXT = ExecutionContext(current_connection_epoch=1)


def curve_for(view: BookView, outcome: MarketSide, **kwargs: Any) -> ExecutionCurve:
    return build_execution_curve(view, instrument(**kwargs), CONTEXT, outcome, at=T0)


# ---------------------------------------------------------------------------
# Derived asks
# ---------------------------------------------------------------------------


class TestDerivedAsks:
    def test_buy_yes_derives_from_no_bids(self) -> None:
        curve = curve_for(book(no=[("0.5600", "13.00")]), MarketSide.YES)
        assert len(curve.levels) == 1
        assert curve.levels[0].execution_price == p("0.4400")
        assert curve.levels[0].available_quantity == q("13.00")

    def test_buy_no_derives_from_yes_bids(self) -> None:
        curve = curve_for(book(yes=[("0.4200", "7.00")]), MarketSide.NO)
        assert curve.levels[0].execution_price == p("0.5800")
        assert curve.levels[0].available_quantity == q("7.00")

    def test_buy_yes_ignores_yes_bids_entirely(self) -> None:
        """YES depth comes only from NO bids. A YES bid is not buyable depth."""
        view = book(yes=[("0.4200", "999.00")], no=[("0.5600", "1.00")])
        curve = curve_for(view, MarketSide.YES)
        assert curve.max_fillable_quantity == q("1.00")

    def test_buy_no_ignores_no_bids_entirely(self) -> None:
        view = book(yes=[("0.4200", "2.00")], no=[("0.5600", "999.00")])
        assert curve_for(view, MarketSide.NO).max_fillable_quantity == q("2.00")

    def test_notional_is_used_explicitly(self) -> None:
        """The complement must come from metadata, never a hardcoded $1.00."""
        curve = build_execution_curve(
            book(no=[("1.5000", "4.00")]),
            instrument(notional=p("2.0000")),
            CONTEXT,
            MarketSide.YES,
            at=T0,
        )
        assert curve.levels[0].execution_price == p("0.5000")

    def test_sub_cent_prices_are_exact(self) -> None:
        curve = curve_for(book(no=[("0.9999", "3.00")]), MarketSide.YES)
        assert curve.levels[0].execution_price == p("0.0001")

    def test_fractional_quantities_preserved(self) -> None:
        curve = curve_for(book(no=[("0.5600", "14.29")]), MarketSide.YES)
        assert curve.levels[0].available_quantity == q("14.29")

    def test_every_level_is_marked_derived(self) -> None:
        curve = curve_for(book(no=[("0.5600", "1.00"), ("0.5500", "2.00")]), MarketSide.YES)
        assert all(level.derived for level in curve.levels)

    def test_provenance_points_back_to_the_source_bid(self) -> None:
        curve = curve_for(book(no=[("0.5600", "13.00")]), MarketSide.YES)
        level_ = curve.levels[0]
        assert level_.source_outcome is MarketSide.NO
        assert level_.source_bid_price == p("0.5600")
        assert level_.liquidity.source_price == p("0.5600")
        assert level_.liquidity.market_ticker == "MKT"
        assert level_.liquidity.connection_epoch == 1
        assert level_.liquidity.sid == 1
        assert level_.liquidity.book_seq == 10

    def test_empty_source_side_gives_zero_depth(self) -> None:
        """A valid empty book is legitimate, not an error."""
        curve = curve_for(book(yes=[("0.4200", "5.00")]), MarketSide.YES)
        assert curve.is_empty
        assert curve.max_fillable_quantity == q("0.00")
        assert curve.best_execution_price is None

    def test_zero_quantity_levels_are_skipped(self) -> None:
        curve = curve_for(book(no=[("0.5600", "0.00"), ("0.5500", "2.00")]), MarketSide.YES)
        assert len(curve.levels) == 1
        assert curve.levels[0].execution_price == p("0.4500")

    def test_derived_and_source_share_one_liquidity_identity(self) -> None:
        """The two are views of the same resting contracts."""
        view = book(no=[("0.5600", "13.00")])
        yes_curve = curve_for(view, MarketSide.YES)
        identity = yes_curve.levels[0].liquidity
        assert identity.source_outcome is MarketSide.NO
        assert identity.source_price == view.no_bids[0].price


class TestOrdering:
    def test_cheapest_executable_ask_first(self) -> None:
        curve = curve_for(
            book(no=[("0.1000", "1.00"), ("0.9000", "2.00"), ("0.5000", "3.00")]),
            MarketSide.YES,
        )
        prices = [lv.execution_price.to_str() for lv in curve.levels]
        assert prices == ["0.1000", "0.5000", "0.9000"]

    def test_best_bid_maps_to_cheapest_ask(self) -> None:
        """The orders are inverses, so relying on book order would invert the curve."""
        view = book(no=[("0.9000", "2.00"), ("0.1000", "1.00")])
        assert view.no_bids[0].price == p("0.9000")  # best bid is highest
        curve = curve_for(view, MarketSide.YES)
        assert curve.levels[0].execution_price == p("0.1000")  # cheapest ask

    def test_equal_prices_are_ordered_deterministically(self) -> None:
        # Two different notionals cannot collide here, so construct a tie via
        # repeated construction and assert stability.
        view = book(no=[("0.5000", "1.00"), ("0.4000", "2.00")])
        first = [lv.execution_price.units for lv in curve_for(view, MarketSide.YES).levels]
        second = [lv.execution_price.units for lv in curve_for(view, MarketSide.YES).levels]
        assert first == second


# ---------------------------------------------------------------------------
# Quoting
# ---------------------------------------------------------------------------


class TestSingleLevelExecution:
    def _curve(self) -> ExecutionCurve:
        return curve_for(book(no=[("0.6000", "10.00")]), MarketSide.YES)

    def test_exact_fill(self) -> None:
        quote = self._curve().quote_up_to(q("10.00"))
        assert quote.fully_fillable
        assert quote.filled_quantity == q("10.00")
        assert quote.gross_cost == Money.from_value("4.000000")

    def test_partial_of_one_level(self) -> None:
        quote = self._curve().quote_up_to(q("3.00"))
        assert quote.filled_quantity == q("3.00")
        assert quote.gross_cost == Money.from_value("1.200000")
        assert len(quote.slices) == 1

    def test_zero_quantity(self) -> None:
        quote = self._curve().quote_up_to(q("0.00"))
        assert quote.filled_quantity.is_zero
        assert quote.gross_cost.is_zero
        assert quote.vwap is None
        assert quote.slices == ()
        assert quote.fully_fillable

    def test_request_beyond_the_level(self) -> None:
        quote = self._curve().quote_up_to(q("15.00"))
        assert not quote.fully_fillable
        assert quote.filled_quantity == q("10.00")
        assert quote.unfilled_quantity == q("5.00")


class TestMultiLevelExecution:
    def _curve(self) -> ExecutionCurve:
        # NO bids 0.60/0.50/0.40 -> YES asks 0.40/0.50/0.60
        return curve_for(
            book(no=[("0.6000", "10.00"), ("0.5000", "20.00"), ("0.4000", "30.00")]),
            MarketSide.YES,
        )

    def test_consumes_cheapest_first(self) -> None:
        quote = self._curve().quote_up_to(q("15.00"))
        assert [s.execution_price.to_str() for s in quote.slices] == ["0.4000", "0.5000"]
        assert [s.quantity.to_str() for s in quote.slices] == ["10.00", "5.00"]

    def test_crosses_several_levels(self) -> None:
        quote = self._curve().quote_up_to(q("45.00"))
        assert len(quote.slices) == 3
        # 10*0.40 + 20*0.50 + 15*0.60 = 4 + 10 + 9 = 23
        assert quote.gross_cost == Money.from_value("23.000000")

    def test_exact_exhaustion_of_all_levels(self) -> None:
        quote = self._curve().quote_up_to(q("60.00"))
        assert quote.fully_fillable
        # 10*0.40 + 20*0.50 + 30*0.60 = 4 + 10 + 18 = 32
        assert quote.gross_cost == Money.from_value("32.000000")

    def test_beyond_total_depth_fills_what_exists(self) -> None:
        quote = self._curve().quote_up_to(q("100.00"))
        assert not quote.fully_fillable
        assert quote.filled_quantity == q("60.00")
        assert quote.unfilled_quantity == q("40.00")

    def test_best_and_worst_execution_prices(self) -> None:
        quote = self._curve().quote_up_to(q("45.00"))
        assert quote.best_execution_price == p("0.4000")
        assert quote.worst_execution_price == p("0.6000")

    def test_strict_mode_raises_on_shortfall(self) -> None:
        with pytest.raises(InsufficientDepthError, match="only"):
            self._curve().require_full_quantity(q("100.00"))

    def test_strict_mode_succeeds_when_available(self) -> None:
        assert self._curve().require_full_quantity(q("60.00")).fully_fillable


class TestCostExactness:
    def test_gross_cost_is_the_sum_of_slices(self) -> None:
        curve = curve_for(book(no=[("0.6000", "3.00"), ("0.5000", "7.00")]), MarketSide.YES)
        quote = curve.quote_up_to(q("10.00"))
        assert quote.gross_cost == Money.from_units(sum(s.gross_cost.units for s in quote.slices))

    def test_six_decimal_precision_is_preserved(self) -> None:
        curve = curve_for(book(no=[("0.9999", "0.01")]), MarketSide.YES)
        quote = curve.quote_up_to(q("0.01"))
        # 0.0001 * 0.01 = 0.000001 exactly
        assert quote.gross_cost == Money.from_value("0.000001")

    def test_no_rounding_of_gross_notional(self) -> None:
        curve = curve_for(book(no=[("0.9997", "14.29")]), MarketSide.YES)
        quote = curve.quote_up_to(q("14.29"))
        # 0.0003 * 14.29 = 0.004287
        assert quote.gross_cost.to_str() == "0.004287"

    def test_fees_are_never_included(self) -> None:
        quote = curve_for(book(no=[("0.6000", "1.00")]), MarketSide.YES).quote_up_to(q("1.00"))
        assert quote.fees_included is False


class TestVwap:
    def test_vwap_is_an_exact_ratio(self) -> None:
        curve = curve_for(book(no=[("0.5800", "3.00"), ("0.5700", "1.00")]), MarketSide.YES)
        quote = curve.quote_up_to(q("4.00"))
        # 3 @ 0.42 + 1 @ 0.43 = 1.69 over 4 -> 0.4225
        assert quote.vwap == AveragePrice(Money.from_value("1.690000"), q("4.00"))
        assert quote.vwap.as_decimal(4) == Decimal("0.4225")

    def test_vwap_need_not_land_on_the_price_grid(self) -> None:
        """Which is exactly why it is not stored as a Price."""
        curve = curve_for(book(no=[("0.9999", "3.00"), ("0.9998", "1.00")]), MarketSide.YES)
        vwap = curve.quote_up_to(q("4.00")).vwap
        assert vwap is not None
        assert not vwap.is_exact_on_price_grid
        assert vwap.as_decimal(8) == Decimal("0.00012500")

    def test_vwap_is_none_for_an_empty_fill(self) -> None:
        assert curve_for(book(), MarketSide.YES).quote_up_to(q("5.00")).vwap is None

    def test_vwap_times_quantity_recovers_gross_cost(self) -> None:
        curve = curve_for(book(no=[("0.6000", "3.00"), ("0.5000", "7.00")]), MarketSide.YES)
        quote = curve.quote_up_to(q("10.00"))
        assert quote.vwap is not None
        recovered = quote.vwap.cost_for(quote.filled_quantity)
        assert recovered == quote.gross_cost.as_decimal()


# ---------------------------------------------------------------------------
# Book safety
# ---------------------------------------------------------------------------


class TestBookSafety:
    @pytest.mark.parametrize(
        "integrity",
        [
            BookIntegrity.WAITING_SNAPSHOT,
            BookIntegrity.INTEGRITY_UNKNOWN,
            BookIntegrity.UNSUBSCRIBED,
        ],
    )
    def test_non_valid_books_are_rejected(self, integrity: BookIntegrity) -> None:
        with pytest.raises(BookNotExecutableError, match="integrity"):
            curve_for(book(no=[("0.6000", "1.00")], integrity=integrity), MarketSide.YES)

    def test_stale_epoch_is_rejected(self) -> None:
        """Sequence authority does not cross connections."""
        stale = book(no=[("0.6000", "1.00")], epoch=1)
        context = ExecutionContext(current_connection_epoch=2)
        with pytest.raises(BookNotExecutableError, match="epoch"):
            build_execution_curve(stale, instrument(), context, MarketSide.YES, at=T0)

    def test_unhealthy_journal_is_rejected(self) -> None:
        context = ExecutionContext(current_connection_epoch=1, journal_healthy=False)
        with pytest.raises(BookNotExecutableError, match="journal"):
            build_execution_curve(
                book(no=[("0.6000", "1.00")]), instrument(), context, MarketSide.YES, at=T0
            )

    def test_unhealthy_connection_is_rejected(self) -> None:
        context = ExecutionContext(current_connection_epoch=1, connection_healthy=False)
        with pytest.raises(BookNotExecutableError, match="connection"):
            build_execution_curve(
                book(no=[("0.6000", "1.00")]), instrument(), context, MarketSide.YES, at=T0
            )

    def test_there_is_no_bypass_flag(self) -> None:
        """A replay caller must construct authority, not disable the check."""
        signature = inspect.signature(build_execution_curve)
        for forbidden in ("skip_validation", "force", "unsafe", "allow_invalid"):
            assert forbidden not in signature.parameters


class TestUnsupportedSemantics:
    def test_scalar_settlement_is_rejected(self) -> None:
        with pytest.raises(UnsupportedExecutionSemanticsError, match="binary"):
            curve_for(
                book(no=[("0.6000", "1.00")]), MarketSide.YES, settlement=SettlementKind.SCALAR
            )

    def test_unknown_settlement_is_rejected(self) -> None:
        with pytest.raises(UnsupportedExecutionSemanticsError):
            curve_for(
                book(no=[("0.6000", "1.00")]), MarketSide.YES, settlement=SettlementKind.UNKNOWN
            )

    def test_missing_notional_is_rejected(self) -> None:
        with pytest.raises(UnsupportedExecutionSemanticsError, match="notional"):
            curve_for(book(no=[("0.6000", "1.00")]), MarketSide.YES, notional=None)

    @pytest.mark.parametrize("reason", [EXCLUSION_MULTIVARIATE, EXCLUSION_PROVISIONAL])
    def test_execution_blocking_exclusions_are_rejected(self, reason: str) -> None:
        with pytest.raises(UnsupportedExecutionSemanticsError, match="cannot be quoted"):
            curve_for(book(no=[("0.6000", "1.00")]), MarketSide.YES, exclusions=(reason,))

    def test_missing_price_grid_does_not_block_execution(self) -> None:
        """The grid validates quoted prices; it says nothing about resting depth.

        A resting bid is liquidity the venue published. Refusing to price
        against it because tick metadata is missing would reject genuine depth
        on a technicality.
        """
        curve = curve_for(
            book(no=[("0.6000", "1.00")]), MarketSide.YES, exclusions=(EXCLUSION_NO_GRID,)
        )
        assert curve.max_fillable_quantity == q("1.00")

    def test_instrument_must_match_the_book(self) -> None:
        with pytest.raises(UnsupportedExecutionSemanticsError, match="does not describe"):
            curve_for(book(no=[("0.6000", "1.00")], ticker="OTHER"), MarketSide.YES)
