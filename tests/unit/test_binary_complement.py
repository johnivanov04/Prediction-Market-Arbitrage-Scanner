"""Edge cases for the same-market YES+NO complement detector.

Fees arrive through a stub quoter in most tests. That is deliberate: the
detector must be provably venue-agnostic, so its own tests should not need a
Kalshi fee engine to express "fees are high" or "fees are unknown". Integration
with the real Kalshi adapter is covered separately.

Book arithmetic reminder: buying YES crosses NO bids, so
``yes_ask = notional - no_bid``. A YES+NO pair therefore costs
``2N - yes_bid - no_bid``, which only falls below the guaranteed payout ``N``
when ``yes_bid + no_bid > N`` -- the bids cross. That is why this detector is a
canary: a matching engine that mints complementary pairs should not leave
crossed bids lying around.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from predarb.books.execution import ExecutionContext
from predarb.books.levels import BookLevel
from predarb.books.liquidity import BookLiquidityId
from predarb.books.orderbook import BookView
from predarb.books.state import BookIntegrity, BookProvenance
from predarb.detectors import binary_complement as detector_module
from predarb.detectors.binary_complement import (
    BinaryComplementResult,
    BinaryComplementSearch,
    LegFeeQuoter,
    evaluate_quantity,
    require_tradeable_quantity,
    search_binary_complement,
)
from predarb.domain.costs import FeeBounds, FeesUnavailable, LegFees
from predarb.domain.enums import MarketSide, SettlementKind, VenueId
from predarb.domain.models import VenueInstrument
from predarb.domain.money import Money, Price, Quantity
from predarb.opportunities.models import (
    Classification,
    CostStatus,
    ExecutionStatus,
    PayoffStatus,
    SemanticStatus,
)
from predarb.semantics.certificate import (
    CertificateStatus,
    SettlementCertificate,
    standard_binary_complement,
)
from predarb.semantics.fingerprint import SettlementEvidenceFingerprint, synthetic_fingerprint

pytestmark = pytest.mark.unit

T0 = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
NOTIONAL = Price.from_value("1.0000")
HASH = "a" * 64
CONTEXT = ExecutionContext(current_connection_epoch=1)

# Synthetic evidence: the rules component plus two other settlement-relevant
# components, so a test can move one without touching the rules.
FINGERPRINT = synthetic_fingerprint(
    market_rules_hash=HASH, market_notional="1.0000", series_settlement_sources=("official",)
)
OTHER_FINGERPRINT = synthetic_fingerprint(
    market_rules_hash=HASH, market_notional="1.0000", series_settlement_sources=("changed",)
)


def q(value: str) -> Quantity:
    return Quantity.from_value(value)


def money(value: str) -> Money:
    return Money.from_value(value)


def book(
    *,
    yes: list[tuple[str, str]],
    no: list[tuple[str, str]],
    integrity: BookIntegrity = BookIntegrity.VALID,
    epoch: int = 1,
    ticker: str = "MKT",
) -> BookView:
    def levels(rows: list[tuple[str, str]], side: MarketSide) -> tuple[BookLevel, ...]:
        return tuple(
            BookLevel(price=Price.from_value(p), quantity=q(qty), side=side)
            for p, qty in sorted(rows, key=lambda r: Decimal(r[0]), reverse=True)
        )

    return BookView(
        market_ticker=ticker,
        integrity=integrity,
        yes_bids=levels(yes, MarketSide.YES),
        no_bids=levels(no, MarketSide.NO),
        provenance=BookProvenance(
            connection_epoch=epoch, sid=1, latest_seq=10, latest_raw_id="raw-10"
        ),
    )


def instrument(
    *,
    ticker: str = "MKT",
    rules_hash: str | None = HASH,
    settlement: SettlementKind = SettlementKind.BINARY,
    notional: Price | None = NOTIONAL,
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
        rules_hash=rules_hash,
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
        exclusion_reasons=(),
    )


def certificate(
    *,
    rules_hash: str = HASH,
    fingerprint: SettlementEvidenceFingerprint | None = None,
    status: CertificateStatus = CertificateStatus.VERIFIED,
    ticker: str = "MKT",
    notional: Price = NOTIONAL,
) -> SettlementCertificate:
    return standard_binary_complement(
        market_ticker=ticker,
        evidence_fingerprint=fingerprint or FINGERPRINT,
        rules_hash=rules_hash,
        notional=notional,
        evidence="Contract terms rev 3 read in full: two states, no DNP clause.",
        verified_by="research",
        verification_method="manual rules review",
        verified_at=T0,
        valid_from=T0,
        status=status,
    )


def flat_fees(lower: str, upper: str | None = None, *, supports: bool = True) -> LegFeeQuoter:
    """A stub quoter returning a fixed interval for every leg."""
    top = upper if upper is not None else lower

    def quoter(quote: object) -> LegFees:  # noqa: ARG001 - protocol signature
        return FeeBounds(
            lower=money(lower),
            upper=money(top),
            exact=(lower == top),
            supports_arbitrage_claim=supports,
            provenance="stub",
        )

    return quoter


def unavailable_fees(
    reason: str = "fee type 'flat' has no established taker formula",
) -> LegFeeQuoter:
    def quoter(quote: object) -> LegFees:  # noqa: ARG001 - protocol signature
        return FeesUnavailable(reason=reason, provenance="stub")

    return quoter


def evaluate(
    *,
    view: BookView | None = None,
    inst: VenueInstrument | None = None,
    cert: SettlementCertificate | None = None,
    quoter: LegFeeQuoter | None = None,
    quantity: str = "1.00",
    context: ExecutionContext = CONTEXT,
    current_fingerprint: SettlementEvidenceFingerprint | None = FINGERPRINT,
) -> BinaryComplementResult:
    return evaluate_quantity(
        instrument=inst or instrument(),
        view=view if view is not None else CROSSED_BOOK,
        certificate=cert or certificate(),
        current_evidence_fingerprint=current_fingerprint,
        context=context,
        fee_quoter=quoter or flat_fees("0.010000"),
        quantity=q(quantity),
        at=T0,
    )


# yes_bid 0.60 + no_bid 0.50 = 1.10 > notional, so the bids cross:
# a YES+NO pair costs (1-0.50) + (1-0.60) = 0.90 against a $1.00 payout.
CROSSED_BOOK = book(yes=[("0.6000", "50.00")], no=[("0.5000", "50.00")])

# yes_bid 0.40 + no_bid 0.50 = 0.90: the ordinary, non-crossed case.
NORMAL_BOOK = book(yes=[("0.4000", "50.00")], no=[("0.5000", "50.00")])


class TestScenario01ClearProvenArbitrage:
    """Crossed bids, a wide margin, and fees far below it."""

    @pytest.fixture
    def result(self) -> BinaryComplementResult:
        return evaluate(quoter=flat_fees("0.020000", "0.030000"))

    def test_classified_as_proven_contractual_arbitrage(self, result):
        assert result.classification is Classification.PROVEN_CONTRACTUAL_ARBITRAGE
        assert result.is_arbitrage_claim

    def test_the_guaranteed_payoff_is_quantity_times_notional(self, result):
        assert result.worst_case_payoff == money("1.000000")

    def test_the_payoff_is_the_same_in_every_state(self, result):
        assert result.portfolio_payoff is not None
        assert result.portfolio_payoff.is_state_independent

    def test_gross_cost_comes_from_executable_depth(self, result):
        # yes_ask = 1 - no_bid = 0.50; no_ask = 1 - yes_bid = 0.40
        assert result.gross_yes_cost == money("0.500000")
        assert result.gross_no_cost == money("0.400000")
        assert result.total_gross_cost == money("0.900000")

    def test_profit_lower_bound_uses_the_fee_upper_bound(self, result):
        assert result.profit is not None
        # 1.00 - (0.90 + 0.03 + 0.03)
        assert result.profit.profit_lower_bound == money("0.040000")

    def test_profit_upper_bound_uses_the_fee_lower_bound(self, result):
        assert result.profit is not None
        # 1.00 - (0.90 + 0.02 + 0.02)
        assert result.profit.profit_upper_bound == money("0.060000")

    def test_status_axes_are_reported_separately(self, result):
        assert result.semantic_status is SemanticStatus.VERIFIED
        assert result.payoff_status is PayoffStatus.STATE_INDEPENDENT_PROFIT
        assert result.cost_status is CostStatus.BOUNDED


class TestScenario02GrossArbErasedByFees:
    def test_fees_above_the_margin_make_it_unprofitable(self):
        """$0.10 of gross margin cannot survive $0.30 of fees."""
        result = evaluate(quoter=flat_fees("0.150000"))
        assert result.classification is Classification.PROVEN_NOT_PROFITABLE
        assert result.payoff_status is PayoffStatus.NON_POSITIVE
        assert not result.is_arbitrage_claim

    def test_an_uncrossed_book_is_never_profitable(self):
        """The ordinary case: a pair costs more than it can ever pay."""
        result = evaluate(view=NORMAL_BOOK, quoter=flat_fees("0.000000"))
        assert result.total_gross_cost == money("1.100000")
        assert result.classification is Classification.PROVEN_NOT_PROFITABLE

    def test_the_profit_bounds_are_reported_even_when_negative(self):
        result = evaluate(quoter=flat_fees("0.150000"))
        assert result.profit is not None
        assert result.profit.profit_upper_bound.units < 0


class TestScenario03FeeIntervalStraddlesZero:
    """Profitable on optimistic fees, unprofitable on pessimistic ones."""

    @pytest.fixture
    def result(self) -> BinaryComplementResult:
        # margin is 0.10; fees per leg in [0.02, 0.08] -> total in [0.04, 0.16]
        return evaluate(quoter=flat_fees("0.020000", "0.080000"))

    def test_classified_indeterminate(self, result):
        assert result.classification is Classification.INDETERMINATE_COST_BOUNDS
        assert result.payoff_status is PayoffStatus.INDETERMINATE

    def test_it_is_not_an_arbitrage_claim(self, result):
        assert not result.is_arbitrage_claim

    def test_the_bounds_actually_straddle_zero(self, result):
        assert result.profit is not None
        assert result.profit.profit_lower_bound.units <= 0
        assert result.profit.profit_upper_bound.units > 0


class TestScenario04DisplayedLooksGoodDepthDoesNot:
    """A thin crossed top-of-book over a thick uncrossed rest."""

    BOOK = book(
        yes=[("0.6000", "0.10"), ("0.4000", "50.00")],
        no=[("0.5000", "0.10"), ("0.3000", "50.00")],
    )

    def test_the_top_level_alone_is_profitable(self):
        result = evaluate(view=self.BOOK, quantity="0.10", quoter=flat_fees("0.000100"))
        assert result.classification is Classification.PROVEN_CONTRACTUAL_ARBITRAGE

    def test_the_same_market_is_not_profitable_at_size(self):
        """Depth, not the displayed best price, decides."""
        result = evaluate(view=self.BOOK, quantity="1.00", quoter=flat_fees("0.000100"))
        assert result.classification is Classification.PROVEN_NOT_PROFITABLE
        assert result.total_gross_cost is not None
        assert result.total_gross_cost > money("1.000000")

    def test_the_deeper_quote_crosses_worse_levels(self):
        result = evaluate(view=self.BOOK, quantity="1.00", quoter=flat_fees("0.000100"))
        assert result.yes_quote is not None
        assert len(result.yes_quote.slices) == 2


class TestScenario05InsufficientDepth:
    def test_a_leg_that_cannot_fill_blocks_evaluation(self):
        thin = book(yes=[("0.6000", "0.50")], no=[("0.5000", "50.00")])
        result = evaluate(view=thin, quantity="1.00")
        assert result.classification is Classification.INSUFFICIENT_DEPTH
        assert result.payoff_status is PayoffStatus.NOT_EVALUATED

    def test_no_profit_interval_is_produced(self):
        thin = book(yes=[("0.6000", "0.50")], no=[("0.5000", "50.00")])
        result = evaluate(view=thin, quantity="1.00")
        assert result.profit is None
        assert result.total_gross_cost is None

    def test_the_shortfall_is_explained(self):
        thin = book(yes=[("0.6000", "0.50")], no=[("0.5000", "50.00")])
        result = evaluate(view=thin, quantity="1.00")
        assert result.blocking_reason is not None
        assert "displayed depth fills" in result.blocking_reason

    def test_a_partial_fill_is_never_used_to_claim_the_requested_size(self):
        """Half a complement has a state-dependent payoff."""
        thin = book(yes=[("0.6000", "0.50")], no=[("0.5000", "50.00")])
        result = evaluate(view=thin, quantity="1.00")
        assert not result.is_arbitrage_claim


class TestScenario06UnverifiedSettlement:
    def test_review_required_blocks_despite_good_economics(self):
        result = evaluate(cert=certificate(status=CertificateStatus.REVIEW_REQUIRED))
        assert result.classification is Classification.BLOCKED_SETTLEMENT_SEMANTICS
        assert result.semantic_status is SemanticStatus.BLOCKED

    def test_no_economics_are_computed_at_all(self):
        """Semantics are checked before any number exists to be quoted."""
        result = evaluate(cert=certificate(status=CertificateStatus.REVIEW_REQUIRED))
        assert result.profit is None
        assert result.total_gross_cost is None
        assert result.yes_quote is None

    @pytest.mark.parametrize("status", [CertificateStatus.REJECTED, CertificateStatus.INVALIDATED])
    def test_every_non_verified_status_blocks(self, status):
        result = evaluate(cert=certificate(status=status))
        assert result.classification is Classification.BLOCKED_SETTLEMENT_SEMANTICS

    def test_a_certificate_for_another_market_blocks(self):
        result = evaluate(cert=certificate(ticker="OTHER"))
        assert result.classification is Classification.BLOCKED_SETTLEMENT_SEMANTICS
        assert result.blocking_reason is not None
        assert "OTHER" in result.blocking_reason


class TestScenario07EvidenceDrift:
    def test_changed_evidence_blocks(self):
        result = evaluate(current_fingerprint=OTHER_FINGERPRINT)
        assert result.classification is Classification.BLOCKED_SETTLEMENT_SEMANTICS
        assert result.blocking_reason is not None
        assert "settlement evidence has changed" in result.blocking_reason

    def test_the_reason_names_the_component_that_moved(self):
        """ "The evidence changed" would not tell a reviewer where to look."""
        result = evaluate(current_fingerprint=OTHER_FINGERPRINT)
        assert result.blocking_reason is not None
        assert "series_settlement_sources" in result.blocking_reason

    def test_unavailable_current_evidence_blocks(self):
        """If we cannot show the evidence is unchanged, we must not assume it."""
        result = evaluate(current_fingerprint=None)
        assert result.classification is Classification.BLOCKED_SETTLEMENT_SEMANTICS
        assert result.blocking_reason is not None
        assert "unavailable" in result.blocking_reason

    def test_matching_rules_but_drifted_evidence_still_blocks(self):
        """The whole reason the binding is wider than the rules hash.

        Both fingerprints carry the identical rules component; only a
        settlement source moved. A rules-hash binding would have called this
        unchanged.
        """
        assert FINGERPRINT.component("market_rules_hash") == OTHER_FINGERPRINT.component(
            "market_rules_hash"
        )
        result = evaluate(current_fingerprint=OTHER_FINGERPRINT)
        assert result.classification is Classification.BLOCKED_SETTLEMENT_SEMANTICS

    def test_both_fingerprints_are_retained_for_audit(self):
        result = evaluate(current_fingerprint=OTHER_FINGERPRINT)
        assert result.certificate_evidence_fingerprint == FINGERPRINT.digest
        assert result.current_evidence_fingerprint == OTHER_FINGERPRINT.digest
        assert result.certificate_rules_hash == HASH


class TestScenario08ScalarOrUnknownSettlement:
    def test_a_scalar_instrument_blocks(self):
        result = evaluate(inst=instrument(settlement=SettlementKind.SCALAR))
        assert result.classification is Classification.BLOCKED_SETTLEMENT_SEMANTICS

    def test_an_unknown_settlement_kind_blocks(self):
        result = evaluate(inst=instrument(settlement=SettlementKind.UNKNOWN))
        assert result.classification is Classification.BLOCKED_SETTLEMENT_SEMANTICS

    def test_a_notional_disagreement_blocks(self):
        """The certified payout must match what the instrument claims."""
        result = evaluate(inst=instrument(notional=Price.from_value("2.0000")))
        assert result.classification is Classification.BLOCKED_SETTLEMENT_SEMANTICS
        assert result.blocking_reason is not None
        assert "notional" in result.blocking_reason


class TestScenario09UnknownFeeType:
    def test_unavailable_fees_block_rather_than_counting_as_zero(self):
        result = evaluate(quoter=unavailable_fees())
        assert result.classification is Classification.BLOCKED_FEE_SEMANTICS
        assert result.cost_status is CostStatus.UNAVAILABLE

    def test_the_reason_survives_into_the_result(self):
        result = evaluate(quoter=unavailable_fees("fee type 'flat' is unsupported"))
        assert result.blocking_reason is not None
        assert "flat" in result.blocking_reason

    def test_no_profit_interval_is_fabricated(self):
        result = evaluate(quoter=unavailable_fees())
        assert result.profit is None

    def test_the_payoff_is_still_recorded_for_audit(self):
        """It got as far as the payoff; the block came afterwards."""
        result = evaluate(quoter=unavailable_fees())
        assert result.portfolio_payoff is not None
        assert result.total_gross_cost is None


class TestScenario10UnresolvedMultiplier:
    def test_a_fee_that_cannot_support_a_claim_blocks(self):
        result = evaluate(quoter=flat_fees("0.010000", supports=False))
        assert result.classification is Classification.BLOCKED_FEE_SEMANTICS

    def test_it_blocks_even_when_the_economics_look_excellent(self):
        result = evaluate(quoter=flat_fees("0.000001", supports=False))
        assert result.classification is Classification.BLOCKED_FEE_SEMANTICS
        assert not result.is_arbitrage_claim

    def test_the_reason_names_the_leg(self):
        result = evaluate(quoter=flat_fees("0.010000", supports=False))
        assert result.blocking_reason is not None
        assert "YES leg" in result.blocking_reason


class TestScenario11LiquidityCollision:
    """Opposite sides should never collide, but the guard is not optional."""

    def test_the_two_legs_normally_consume_distinct_liquidity(self):
        result = evaluate()
        ids = result.liquidity_ids
        assert len(ids) == len(set(ids))
        assert {i.source_outcome for i in ids} == {MarketSide.YES, MarketSide.NO}

    def test_a_collision_blocks_the_candidate(self, monkeypatch):
        """Forced, because the book structure cannot produce one naturally."""
        collision = BookLiquidityId(
            connection_epoch=1,
            sid=1,
            market_ticker="MKT",
            source_outcome=MarketSide.YES,
            source_price=Price.from_value("0.6000"),
        )
        monkeypatch.setattr(
            detector_module, "detect_liquidity_collisions", lambda *_a, **_k: (collision,)
        )
        result = evaluate()
        assert result.classification is Classification.BLOCKED_LIQUIDITY_COLLISION
        assert result.profit is None

    def test_the_colliding_level_is_named(self, monkeypatch):
        collision = BookLiquidityId(
            connection_epoch=1,
            sid=1,
            market_ticker="MKT",
            source_outcome=MarketSide.YES,
            source_price=Price.from_value("0.6000"),
        )
        monkeypatch.setattr(
            detector_module, "detect_liquidity_collisions", lambda *_a, **_k: (collision,)
        )
        result = evaluate()
        assert result.blocking_reason is not None
        assert "consumed by both legs" in result.blocking_reason


class TestScenario12InvalidBook:
    @pytest.mark.parametrize(
        "integrity",
        [
            BookIntegrity.WAITING_SNAPSHOT,
            BookIntegrity.INTEGRITY_UNKNOWN,
        ],
    )
    def test_a_non_valid_book_blocks(self, integrity):
        result = evaluate(
            view=book(yes=[("0.6000", "50.00")], no=[("0.5000", "50.00")], integrity=integrity)
        )
        assert result.classification is Classification.BLOCKED_BOOK_INTEGRITY
        assert result.execution_status is ExecutionStatus.STALE_OR_INVALID

    def test_a_stale_connection_epoch_blocks(self):
        stale = book(yes=[("0.6000", "50.00")], no=[("0.5000", "50.00")], epoch=7)
        result = evaluate(view=stale)
        assert result.classification is Classification.BLOCKED_BOOK_INTEGRITY

    def test_an_unhealthy_journal_blocks(self):
        result = evaluate(
            context=ExecutionContext(current_connection_epoch=1, journal_healthy=False)
        )
        assert result.classification is Classification.BLOCKED_BOOK_INTEGRITY


class TestScenario13RaceRisk:
    def test_a_proven_arbitrage_is_still_race_exposed(self):
        """Two acquisitions are not atomic, whatever the payoff proves."""
        result = evaluate(quoter=flat_fees("0.020000"))
        assert result.classification is Classification.PROVEN_CONTRACTUAL_ARBITRAGE
        assert result.execution_status is ExecutionStatus.RACE_EXPOSED

    def test_the_warning_says_so_in_words(self):
        result = evaluate(quoter=flat_fees("0.020000"))
        assert any("not atomic" in w for w in result.warnings)

    def test_no_result_can_report_a_locked_execution(self):
        """Nothing here places an order, so nothing may claim one is locked."""
        assert not hasattr(ExecutionStatus, "LOCKED")
        assert "LOCKED" not in {member.value for member in ExecutionStatus}


class TestScenario14ExactZeroProfit:
    def test_zero_guaranteed_profit_is_not_arbitrage(self):
        """Arbitrage requires strictly positive guaranteed profit."""
        # margin is exactly 0.10; exact fees of 0.05 per leg consume all of it
        result = evaluate(quoter=flat_fees("0.050000"))
        assert result.profit is not None
        assert result.profit.profit_lower_bound == Money.zero()
        assert result.profit.profit_upper_bound == Money.zero()
        assert result.classification is Classification.PROVEN_NOT_PROFITABLE
        assert not result.is_arbitrage_claim

    def test_exact_fees_are_reported_as_an_exact_cost(self):
        result = evaluate(quoter=flat_fees("0.050000"))
        assert result.cost_status is CostStatus.EXACT

    def test_one_micro_dollar_of_margin_is_enough(self):
        """The boundary is strict, and it is exactly one micro-dollar wide."""
        result = evaluate(quoter=flat_fees("0.049999"))
        assert result.profit is not None
        assert result.profit.profit_lower_bound == Money.from_units(2)
        assert result.classification is Classification.PROVEN_CONTRACTUAL_ARBITRAGE


class TestQuantityGrid:
    def test_zero_quantity_is_refused(self):
        with pytest.raises(ValueError, match=re.escape("at least 0.01")):
            require_tradeable_quantity(Quantity.zero())

    def test_more_than_two_decimals_cannot_be_constructed(self):
        """The grid is enforced by the type; nothing is silently rounded."""
        with pytest.raises(ValueError):
            Quantity.from_value("0.005")

    def test_the_smallest_tradeable_quantity_is_accepted(self):
        require_tradeable_quantity(q("0.01"))
        result = evaluate(quantity="0.01", quoter=flat_fees("0.000100"))
        assert result.classification is Classification.PROVEN_CONTRACTUAL_ARBITRAGE


class TestAuditability:
    def test_every_result_carries_book_coordinates(self):
        result = evaluate()
        assert result.identity.connection_epoch == 1
        assert result.identity.sid == 1
        assert result.identity.book_seq == 10
        assert result.identity.latest_raw_id == "raw-10"

    def test_the_certificate_and_states_are_retained(self):
        result = evaluate()
        assert result.certificate_identity == f"MKT@{FINGERPRINT.short}"
        assert {s.name for s in result.allowed_states} == {"YES", "NO"}

    def test_fill_slices_and_liquidity_ids_are_retained(self):
        result = evaluate()
        assert result.yes_quote is not None
        assert result.no_quote is not None
        assert len(result.liquidity_ids) == 2

    def test_the_detection_instant_is_the_one_supplied(self):
        """Pure: the detector never reads a clock of its own."""
        assert evaluate().identity.detected_at == T0

    def test_payoff_in_every_state_is_retained(self):
        result = evaluate()
        assert result.portfolio_payoff is not None
        assert len(result.portfolio_payoff.per_state) == 2


class TestPurity:
    def test_the_detector_imports_no_venue_ingest_or_storage_module(self):
        """Structural, not aspirational: a detector cannot tell it is Kalshi."""
        assert detector_module.__file__ is not None
        source = Path(detector_module.__file__).read_text()
        for forbidden in (
            "predarb.venues",
            "predarb.ingest",
            "predarb.storage",
            "httpx",
            "websockets",
            "asyncio",
        ):
            assert forbidden not in source, f"detector must not import {forbidden}"

    def test_the_detector_exposes_no_trading_surface(self):
        for forbidden in ("place_order", "submit", "create_order", "cancel", "preview_order"):
            assert not hasattr(detector_module, forbidden)

    def test_repeated_evaluation_is_deterministic(self):
        first, second = evaluate(), evaluate()
        assert first.classification is second.classification
        assert first.profit is not None
        assert second.profit is not None
        assert first.profit.profit_lower_bound == second.profit.profit_lower_bound


class TestBoundedSearch:
    """A sweep is exhaustive within its interval and says nothing beyond it."""

    def search(self, **kwargs: object) -> BinaryComplementSearch:
        params: dict[str, object] = {
            "instrument": instrument(),
            "view": CROSSED_BOOK,
            "certificate": certificate(),
            "current_evidence_fingerprint": FINGERPRINT,
            "context": CONTEXT,
            "fee_quoter": flat_fees("0.020000"),
            "min_quantity": q("0.01"),
            "max_quantity": q("0.05"),
            "at": T0,
        }
        params.update(kwargs)
        return search_binary_complement(**params)  # type: ignore[arg-type]

    def test_every_increment_in_the_interval_is_evaluated(self):
        result = self.search()
        assert result.evaluated_quantity_count == 5
        assert result.search_step == q("0.01")

    def test_the_interval_is_reported_with_the_answer(self):
        result = self.search()
        assert result.search_min_quantity == q("0.01")
        assert result.search_max_quantity == q("0.05")
        assert result.search_complete

    def test_a_barren_sweep_says_only_that_the_interval_was_barren(self):
        """Never 'no arbitrage exists'."""
        result = self.search(fee_quoter=flat_fees("5.000000"))
        assert result.proven_candidates == ()
        assert "no proven candidate in" in result.summary()
        assert "[0.01, 0.05]" in result.summary()

    def test_candidates_are_found_across_the_interval(self):
        result = self.search(fee_quoter=flat_fees("0.000100"))
        assert len(result.proven_candidates) == 5

    def test_the_best_candidate_maximises_the_guaranteed_floor(self):
        result = self.search(fee_quoter=flat_fees("0.000100"))
        best = result.best_candidate
        assert best is not None
        assert best.quantity == q("0.05")

    def test_depth_caps_the_reported_interval(self):
        """Quantities beyond displayed depth are not silently claimed as searched."""
        thin = book(yes=[("0.6000", "0.03")], no=[("0.5000", "0.03")])
        result = self.search(view=thin, max_quantity=q("1.00"))
        assert result.search_max_quantity == q("0.03")
        assert result.evaluated_quantity_count == 3
        assert any("displayed depth supports only" in w for w in result.warnings)

    def test_blocked_semantics_prevent_the_sweep_entirely(self):
        result = self.search(certificate=certificate(status=CertificateStatus.REVIEW_REQUIRED))
        assert result.evaluated_quantity_count == 0
        assert not result.search_complete
        assert result.results[0].classification is Classification.BLOCKED_SETTLEMENT_SEMANTICS

    def test_an_unquotable_book_prevents_the_sweep(self):
        stale = book(yes=[("0.6000", "50.00")], no=[("0.5000", "50.00")], epoch=9)
        result = self.search(view=stale)
        assert result.evaluated_quantity_count == 0
        assert not result.search_complete

    def test_an_inverted_interval_is_refused(self):
        with pytest.raises(ValueError, match="search interval is empty"):
            self.search(min_quantity=q("1.00"), max_quantity=q("0.50"))

    def test_a_zero_minimum_is_refused(self):
        with pytest.raises(ValueError, match=re.escape("at least 0.01")):
            self.search(min_quantity=Quantity.zero())

    def test_keep_all_retains_every_evaluation(self):
        result = self.search(fee_quoter=flat_fees("5.000000"), keep="all")
        assert len(result.results) == result.evaluated_quantity_count

    def test_keep_candidates_retains_one_explanatory_result(self):
        """Enough to explain a barren sweep without holding every quote."""
        result = self.search(fee_quoter=flat_fees("5.000000"))
        assert len(result.results) == 1
        assert result.results[0].classification is Classification.PROVEN_NOT_PROFITABLE

    def test_an_unknown_keep_mode_is_refused(self):
        with pytest.raises(ValueError, match="keep must be"):
            self.search(keep="some")

    def test_the_curves_are_built_once_not_per_quantity(self, monkeypatch):
        """Rebuilding per quantity would be waste, and would risk evaluating
        different quantities against subtly different inputs."""
        calls = []
        original = detector_module.build_execution_curve  # type: ignore[attr-defined]

        def counting(*args: object, **kwargs: object) -> object:
            calls.append(1)
            return original(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(detector_module, "build_execution_curve", counting)
        self.search(max_quantity=q("0.20"))
        assert len(calls) == 2  # one YES curve, one NO curve

    def test_search_and_direct_evaluation_agree(self):
        """The sweep must be the same function, not a parallel implementation."""
        swept = self.search(fee_quoter=flat_fees("0.000100"), keep="all")
        for result in swept.results:
            direct = evaluate(quantity=str(result.quantity), quoter=flat_fees("0.000100"))
            assert direct.classification is result.classification
            assert direct.profit is not None
            assert result.profit is not None
            assert direct.profit.profit_lower_bound == result.profit.profit_lower_bound


class TestSearchTally:
    """The funnel must not be shaped by what the search chose to retain."""

    def search(self, **kwargs: object) -> BinaryComplementSearch:
        params: dict[str, object] = {
            "instrument": instrument(),
            "view": CROSSED_BOOK,
            "certificate": certificate(),
            "current_evidence_fingerprint": FINGERPRINT,
            "context": CONTEXT,
            "fee_quoter": flat_fees("5.000000"),
            "min_quantity": q("0.01"),
            "max_quantity": q("0.05"),
            "at": T0,
        }
        params.update(kwargs)
        return search_binary_complement(**params)  # type: ignore[arg-type]

    def test_counts_cover_every_evaluation_not_just_retained_ones(self):
        result = self.search()
        assert len(result.results) == 1  # retention discarded the rest
        assert sum(result.classification_counts.values()) == 5
        assert result.classification_counts[Classification.PROVEN_NOT_PROFITABLE] == 5

    def test_counts_match_under_either_retention_mode(self):
        kept = self.search(keep="candidates").classification_counts
        everything = self.search(keep="all").classification_counts
        assert kept == everything

    def test_a_tally_that_disagrees_with_the_count_is_refused(self):
        """Guards against a future change updating one and not the other."""
        with pytest.raises(ValueError, match="classification counts sum to"):
            BinaryComplementSearch(
                market_ticker="MKT",
                search_min_quantity=q("0.01"),
                search_max_quantity=q("0.02"),
                search_step=q("0.01"),
                evaluated_quantity_count=2,
                search_complete=True,
                results=(),
                classification_counts={Classification.PROVEN_NOT_PROFITABLE: 1},
            )

    def test_mixed_outcomes_are_tallied_separately(self):
        thin = book(yes=[("0.6000", "0.03")], no=[("0.5000", "0.03")])
        result = self.search(view=thin, max_quantity=q("0.03"), fee_quoter=flat_fees("0.000100"))
        assert result.classification_counts[Classification.PROVEN_CONTRACTUAL_ARBITRAGE] == 3


class TestSearchWithNoExecutableQuantity:
    """Depth below the minimum must not print an inverted interval."""

    def search(self, view: BookView, **kwargs: object) -> BinaryComplementSearch:
        params: dict[str, object] = {
            "instrument": instrument(),
            "view": view,
            "certificate": certificate(),
            "current_evidence_fingerprint": FINGERPRINT,
            "context": CONTEXT,
            "fee_quoter": flat_fees("0.000100"),
            "min_quantity": q("0.01"),
            "max_quantity": q("1.00"),
            "at": T0,
        }
        params.update(kwargs)
        return search_binary_complement(**params)  # type: ignore[arg-type]

    def test_an_empty_book_evaluates_nothing(self):
        result = self.search(book(yes=[], no=[]))
        assert result.evaluated_quantity_count == 0
        assert result.results == ()

    def test_the_interval_is_never_reported_inverted(self):
        """[0.01, 0.00] would read as a sweep that ran backwards."""
        result = self.search(book(yes=[], no=[]))
        assert result.search_min_quantity <= result.search_max_quantity

    def test_it_does_not_claim_a_completed_search(self):
        result = self.search(book(yes=[], no=[]))
        assert not result.search_complete
        assert any("is executable" in w for w in result.warnings)

    def test_depth_below_the_minimum_is_explained(self):
        thin = book(yes=[("0.6000", "0.50")], no=[("0.5000", "0.50")])
        result = self.search(thin, min_quantity=q("1.00"), max_quantity=q("2.00"))
        assert result.evaluated_quantity_count == 0
        assert any("displayed depth supports only 0.50" in w for w in result.warnings)

    def test_counts_stay_consistent_with_the_evaluation_count(self):
        result = self.search(book(yes=[], no=[]))
        assert sum(result.classification_counts.values()) == 0
