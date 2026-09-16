"""Tests for the price grid: band validity, boundaries, snapping."""

from __future__ import annotations

import pytest

from predarb.domain.models import PriceBand, PriceGrid
from predarb.domain.money import Price

pytestmark = pytest.mark.unit


def band(start: str, end: str, step: str) -> PriceBand:
    return PriceBand(
        start=Price.from_value(start), end=Price.from_value(end), step=Price.from_value(step)
    )


def p(value: str) -> Price:
    return Price.from_value(value)


# The three structures observed live, verbatim from captured payloads.
LINEAR_CENT = PriceGrid(bands=(band("0.0000", "1.0000", "0.0100"),), structure_name="linear_cent")
TAPERED = PriceGrid(
    bands=(
        band("0.0000", "0.1000", "0.0010"),
        band("0.1000", "0.9000", "0.0100"),
        band("0.9000", "1.0000", "0.0010"),
    ),
    structure_name="tapered_deci_cent",
)
CENTER_DECI = PriceGrid(
    bands=(
        band("0.0000", "0.0100", "0.0001"),
        band("0.0100", "0.9900", "0.0010"),
        band("0.9900", "1.0000", "0.0001"),
    ),
    structure_name="center_deci_edge_centi_cent",
)
ALL_GRIDS = [LINEAR_CENT, TAPERED, CENTER_DECI]


class TestSingleBandGrid:
    @pytest.mark.parametrize("price", ["0.0000", "0.0100", "0.5000", "0.9900", "1.0000"])
    def test_on_grid_prices_valid(self, price):
        assert LINEAR_CENT.is_valid_price(p(price))

    @pytest.mark.parametrize("price", ["0.0001", "0.0050", "0.5005", "0.9999"])
    def test_off_grid_prices_invalid(self, price):
        assert not LINEAR_CENT.is_valid_price(p(price))

    def test_endpoints_are_inclusive(self):
        assert LINEAR_CENT.is_valid_price(p("0.0000"))
        assert LINEAR_CENT.is_valid_price(p("1.0000"))


class TestMultiBandGrid:
    @pytest.mark.parametrize("price", ["0.0000", "0.0010", "0.0990", "0.1000"])
    def test_fine_lower_band(self, price):
        assert TAPERED.is_valid_price(p(price))

    @pytest.mark.parametrize("price", ["0.1100", "0.5000", "0.8900", "0.9000"])
    def test_coarse_middle_band(self, price):
        assert TAPERED.is_valid_price(p(price))

    @pytest.mark.parametrize("price", ["0.9010", "0.9990", "1.0000"])
    def test_fine_upper_band(self, price):
        assert TAPERED.is_valid_price(p(price))

    def test_fine_tick_invalid_inside_coarse_band(self):
        # 0.1010 is on the 0.0010 grid but the middle band steps by 0.0100.
        assert not TAPERED.is_valid_price(p("0.1010"))

    def test_sub_cent_prices_valid_at_the_edges(self):
        # This is the whole point of the tiered structures.
        assert CENTER_DECI.is_valid_price(p("0.0001"))
        assert CENTER_DECI.is_valid_price(p("0.9999"))

    def test_sub_cent_price_invalid_in_the_middle(self):
        assert not CENTER_DECI.is_valid_price(p("0.5001"))

    def test_observed_live_book_prices_are_on_grid(self):
        # Taken from the captured center_deci order book.
        assert CENTER_DECI.is_valid_price(p("0.7410"))
        assert CENTER_DECI.is_valid_price(p("0.2180"))


class TestBoundaryBehaviour:
    """Boundary ownership was resolved empirically, not assumed."""

    @pytest.mark.parametrize("grid", ALL_GRIDS)
    def test_all_observed_grids_have_consistent_boundaries(self, grid):
        assert grid.has_consistent_boundaries()

    @pytest.mark.parametrize("grid", ALL_GRIDS)
    def test_all_observed_grids_are_contiguous(self, grid):
        assert grid.is_contiguous()

    @pytest.mark.parametrize(("grid", "boundary"), [(TAPERED, "0.1000"), (TAPERED, "0.9000")])
    def test_both_adjacent_bands_accept_the_boundary(self, grid, boundary):
        covering = grid.bands_covering(p(boundary))
        assert len(covering) == 2
        assert all(b.accepts(p(boundary)) for b in covering)

    @pytest.mark.parametrize(
        ("grid", "boundary"), [(CENTER_DECI, "0.0100"), (CENTER_DECI, "0.9900")]
    )
    def test_center_deci_boundaries_agree(self, grid, boundary):
        covering = grid.bands_covering(p(boundary))
        assert len(covering) == 2
        assert all(b.accepts(p(boundary)) for b in covering)

    def test_contiguous_bands_always_agree_at_the_shared_endpoint(self):
        # Structural, not coincidental: a band spans a whole number of its own
        # steps, so its endpoint is on its grid, and the next band starts there.
        for grid in ALL_GRIDS:
            for index in range(len(grid.bands) - 1):
                boundary = grid.bands[index].end
                assert grid.bands[index].accepts(boundary)
                assert grid.bands[index + 1].accepts(boundary)

    def test_overlapping_bands_that_disagree_are_detected(self):
        # Both bands are individually well formed, but they overlap over a
        # range and step differently, so they disagree about 0.0600: the first
        # accepts it, the second's grid runs 0.0500, 0.0800, 0.1100 ...
        # If Kalshi ever ships such a structure the instrument is excluded
        # rather than silently resolved one way.
        overlapping = PriceGrid(
            bands=(band("0.0000", "0.1000", "0.0100"), band("0.0500", "0.9500", "0.0300")),
        )
        assert not overlapping.has_consistent_boundaries()
        assert not overlapping.is_contiguous()


class TestSnapping:
    def test_snap_down_within_band(self):
        assert LINEAR_CENT.snap_down(p("0.5050")) == p("0.5000")

    def test_snap_up_within_band(self):
        assert LINEAR_CENT.snap_up(p("0.5050")) == p("0.5100")

    def test_snap_is_identity_on_grid(self):
        assert LINEAR_CENT.snap_down(p("0.5000")) == p("0.5000")
        assert LINEAR_CENT.snap_up(p("0.5000")) == p("0.5000")

    def test_snap_across_band_boundary_uses_finest_available(self):
        # Just below the tapered grid's 0.1000 boundary the fine band applies.
        assert TAPERED.snap_down(p("0.0995")) == p("0.0990")
        assert TAPERED.snap_up(p("0.0995")) == p("0.1000")

    def test_snap_up_in_coarse_band(self):
        assert TAPERED.snap_up(p("0.1005")) == p("0.1100")

    def test_snap_results_are_always_valid(self):
        for grid in ALL_GRIDS:
            for raw in ("0.0000", "0.0007", "0.3333", "0.9995", "1.0000"):
                for snapped in (grid.snap_down(p(raw)), grid.snap_up(p(raw))):
                    if snapped is not None:
                        assert grid.is_valid_price(snapped)

    def test_snap_up_above_range_is_none(self):
        assert LINEAR_CENT.snap_up(Price.from_value("2.0000")) is None


class TestBandValidation:
    def test_band_must_span_whole_steps(self):
        with pytest.raises(ValueError, match="whole number"):
            band("0.0000", "0.0050", "0.0030")

    def test_band_end_before_start_rejected(self):
        with pytest.raises(ValueError, match="precedes start"):
            band("0.5000", "0.1000", "0.0100")

    def test_zero_step_rejected(self):
        with pytest.raises(ValueError, match="step must be positive"):
            band("0.0000", "1.0000", "0.0000")

    def test_grid_requires_a_band(self):
        with pytest.raises(ValueError, match="at least one band"):
            PriceGrid(bands=())

    def test_grid_requires_ascending_bands(self):
        with pytest.raises(ValueError, match="ascending"):
            PriceGrid(
                bands=(band("0.5000", "1.0000", "0.0100"), band("0.0000", "0.5000", "0.0100"))
            )
