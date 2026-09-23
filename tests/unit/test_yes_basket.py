"""AT_LEAST_ONE BUY-YES basket detector.

Book arithmetic reminder: buying YES crosses NO bids, so
``yes_ask = notional - no_bid``. A basket of n YES legs costs
``sum(N_i - no_bid_i)`` and pays at least ``q * min_i(N_i)`` -- so it is
profitable when the NO bids are collectively rich, the mirror image of Step 9's
NO basket.

The distinction these tests exist to protect: AT_LEAST_ONE forbids only the
all-NO state. Several members settling YES is permitted and only *increases* a
long-YES basket's payoff, so this detector stays valid on events that are not
mutually exclusive.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from predarb.books.execution import ExecutionContext, ExecutionQuote
from predarb.books.levels import BookLevel
from predarb.books.liquidity import BookLiquidityId
from predarb.books.orderbook import BookView
from predarb.books.state import BookIntegrity, BookProvenance
from predarb.detectors import yes_basket as detector_module
from predarb.detectors.binary_complement import LegFeeQuoter
from predarb.detectors.yes_basket import (
    YesBasketMember,
    YesBasketResult,
    YesBasketSearch,
    evaluate_yes_basket_quantity,
    search_yes_basket,
)
from predarb.domain.at_least_one_payoff import PayoffTheorem
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
from predarb.semantics.relation import (
    ALL_SELECTED_MEMBERS_LOSE,
    RelationCertificate,
    RelationClaim,
    RelationStatus,
)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
NOTIONAL = Price.from_value("1.0000")
CONTEXT = ExecutionContext(current_connection_epoch=1)
EVENT = "EVT"

# NO bids at 0.70 mean YES costs 0.30 a leg. Three legs cost 0.90 against a
# guaranteed 1.00 floor, so the basket is profitable before fees.
RICH = [("0.7000", "50.00")]
# NO bids at 0.60 mean YES costs 0.40 a leg: 1.20 for three, above the floor.
POOR = [("0.6000", "50.00")]


def q(value: str) -> Quantity:
    return Quantity.from_value(value)


def money(value: str) -> Money:
    return Money.from_value(value)


def member_fingerprint(ticker: str, salt: str = "v1") -> SettlementEvidenceFingerprint:
    return synthetic_fingerprint(market=ticker, rules=salt)


def instrument(ticker: str, notional: Price = NOTIONAL) -> VenueInstrument:
    return VenueInstrument(
        venue=VenueId.KALSHI,
        ticker=ticker,
        event_ticker=EVENT,
        title="",
        status_raw="active",
        settlement_kind=SettlementKind.BINARY,
        market_type_raw="binary",
        notional_value=notional,
        price_grid=None,
        yes_sub_title=None,
        no_sub_title=None,
        rules_primary=None,
        rules_secondary=None,
        rules_hash="h" * 64,
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


def book(
    ticker: str,
    no_levels: list[tuple[str, str]],
    *,
    integrity: BookIntegrity = BookIntegrity.VALID,
    epoch: int = 1,
) -> BookView:
    """A book with NO bids only -- the liquidity a YES buyer crosses."""
    levels = tuple(
        BookLevel(price=Price.from_value(p), quantity=q(size), side=MarketSide.NO)
        for p, size in sorted(no_levels, key=lambda r: Decimal(r[0]), reverse=True)
    )
    return BookView(
        market_ticker=ticker,
        integrity=integrity,
        yes_bids=(),
        no_bids=levels,
        provenance=BookProvenance(
            connection_epoch=epoch, sid=1, latest_seq=10, latest_raw_id=f"raw-{ticker}"
        ),
    )


def settlement_certificate(
    ticker: str,
    *,
    fingerprint: SettlementEvidenceFingerprint | None = None,
    notional: Price = NOTIONAL,
    status: CertificateStatus = CertificateStatus.VERIFIED,
) -> SettlementCertificate:
    return standard_binary_complement(
        market_ticker=ticker,
        evidence_fingerprint=fingerprint or member_fingerprint(ticker),
        rules_hash="h" * 64,
        notional=notional,
        evidence=f"Synthetic fixture for {ticker}.",
        verified_by="test",
        verification_method="fixture",
        verified_at=T0,
        valid_from=T0,
        status=status,
    )


def relation_certificate(
    members: list[str],
    *,
    claim: RelationClaim = RelationClaim.AT_LEAST_ONE,
    fingerprint: SettlementEvidenceFingerprint | None = None,
    member_fingerprints: dict[str, str] | None = None,
    status: RelationStatus = RelationStatus.VERIFIED,
    issued_at: datetime = T0,
) -> RelationCertificate:
    prints = member_fingerprints or {t: member_fingerprint(t).digest for t in members}
    return RelationCertificate(
        certificate_id="relcert-" + "-".join(members),
        claim=claim,
        event_ticker=EVENT,
        selected_members=tuple(members),
        snapshot_id="snap",
        evidence_fingerprint=fingerprint or synthetic_fingerprint(event=EVENT, members=members),
        member_settlement_fingerprints=prints,
        status=status,
        reviewer="test",
        reviewed_at=T0,
        issued_at=issued_at,
        valid_from=issued_at,
        valid_to=None,
        policy_schema_version="relation-evidence/1",
        evidence="Synthetic relation fixture.",
    )


def basket_member(
    ticker: str,
    no_levels: list[tuple[str, str]],
    *,
    notional: Price = NOTIONAL,
    cert_status: CertificateStatus = CertificateStatus.VERIFIED,
    current_salt: str | None = "v1",
    integrity: BookIntegrity = BookIntegrity.VALID,
    epoch: int = 1,
) -> YesBasketMember:
    current = member_fingerprint(ticker, current_salt) if current_salt else None
    return YesBasketMember(
        instrument=instrument(ticker, notional),
        view=book(ticker, no_levels, integrity=integrity, epoch=epoch),
        certificate=settlement_certificate(ticker, notional=notional, status=cert_status),
        current_settlement_fingerprint=current,
    )


def flat_fees(
    lower: str,
    upper: str | None = None,
    *,
    supports: bool = True,
    per_leg: dict[str, str] | None = None,
) -> LegFeeQuoter:
    top = upper if upper is not None else lower

    def quoter(quote: ExecutionQuote) -> LegFees:
        if per_leg and quote.market_ticker in per_leg:
            value = per_leg[quote.market_ticker]
            if value == "unavailable":
                return FeesUnavailable(reason="fee type unsupported", provenance="stub")
            if value == "unresolved":
                return FeeBounds(
                    lower=money(lower),
                    upper=money(top),
                    exact=False,
                    supports_arbitrage_claim=False,
                    provenance="stub: multiplier unresolved",
                )
        return FeeBounds(
            lower=money(lower),
            upper=money(top),
            exact=(lower == top),
            supports_arbitrage_claim=supports,
            provenance="stub",
        )

    return quoter


def evaluate(
    *,
    members: list[YesBasketMember] | None = None,
    relation: RelationCertificate | None = None,
    relation_fingerprint: SettlementEvidenceFingerprint | None = None,
    relation_unavailable: bool = False,
    quoter: LegFeeQuoter | None = None,
    quantity: str = "1.00",
    context: ExecutionContext = CONTEXT,
) -> YesBasketResult:
    legs = members if members is not None else [basket_member(t, RICH) for t in ("A", "B", "C")]
    cert = relation or relation_certificate([m.ticker for m in legs])
    current = None if relation_unavailable else (relation_fingerprint or cert.evidence_fingerprint)
    return evaluate_yes_basket_quantity(
        relation=cert,
        relation_evidence_fingerprint=current,
        members=legs,
        context=context,
        fee_quoter=quoter or flat_fees("0.010000"),
        quantity=q(quantity),
        at=T0,
    )


class TestClearArbitrage:
    """Scenario 1: three members, NO bids rich enough that YES is cheap."""

    def test_it_is_a_proven_contractual_arbitrage(self):
        result = evaluate()
        assert result.classification is Classification.PROVEN_CONTRACTUAL_ARBITRAGE
        assert result.is_arbitrage_claim

    def test_the_floor_is_the_smallest_winning_payout(self):
        result = evaluate()
        assert result.guaranteed_payoff == money("1.000000")
        assert result.payoff_proof is not None
        assert result.payoff_proof.theorem is PayoffTheorem.SINGLETON_WITNESS_MINIMUM

    def test_the_cost_is_gross_by_leg_and_never_netted(self):
        result = evaluate()
        assert result.total_gross_cost == money("0.900000")
        assert set(result.gross_cost_by_leg) == {"A", "B", "C"}
        assert any("no collateral-netting" in w for w in result.warnings)

    def test_the_profit_floor_uses_the_fee_upper_bound(self):
        result = evaluate(quoter=flat_fees("0.010000", "0.020000"))
        assert result.profit is not None
        assert result.profit.fee_upper == money("0.060000")
        assert result.profit.profit_lower_bound == money("0.040000")

    def test_all_four_status_axes_are_reported(self):
        result = evaluate()
        assert result.semantic_status is SemanticStatus.VERIFIED
        assert result.payoff_status is PayoffStatus.STATE_INDEPENDENT_PROFIT
        assert result.cost_status is CostStatus.BOUNDED
        assert result.execution_status is ExecutionStatus.RACE_EXPOSED


class TestMultipleYesIsPermitted:
    """Scenarios 2, 3, 4: the distinction from mutual exclusion.

    AT_LEAST_ONE forbids only all-NO. More winners pay more, so the detector is
    valid on events that are not mutually exclusive at all -- 65 of 94 sampled
    events carry ``mutually_exclusive = false``.
    """

    def test_the_forbidden_state_is_all_no(self):
        assert evaluate().forbidden_state == ALL_SELECTED_MEMBERS_LOSE
        proof = evaluate().payoff_proof
        assert proof is not None
        assert proof.forbidden_state == ALL_SELECTED_MEMBERS_LOSE

    def test_the_permitted_state_count_is_two_to_the_n_minus_one(self):
        proof = evaluate().payoff_proof
        assert proof is not None
        assert proof.permitted_state_count == 2**3 - 1

    def test_the_best_case_has_every_member_winning(self):
        """All-YES is permitted, and pays the most. AT_MOST_ONE would forbid it."""
        proof = evaluate().payoff_proof
        assert proof is not None
        assert proof.best_case_payoff == money("3.000000")
        assert proof.best_case_payoff > proof.worst_case_payoff

    def test_the_result_warns_that_this_is_not_mutual_exclusion(self):
        assert any("does NOT assert mutual exclusion" in w for w in evaluate().warnings)

    def test_no_mutual_exclusion_input_is_required_anywhere(self):
        """The detector takes no AT_MOST_ONE certificate and no exclusivity flag.

        Checked on the signature rather than by grepping the source, which would
        also match the docstring explaining why none is needed.
        """
        parameters = set(inspect.signature(evaluate_yes_basket_quantity).parameters)
        assert not any("mutual" in p or "exclusive" in p for p in parameters)
        assert not any("at_most_one" in p for p in parameters)


class TestSingletonWitness:
    """Scenarios 5 and 6: where the minimum sits, and with unequal notionals."""

    def uneven(self) -> list[YesBasketMember]:
        """B has half the notional, so its NO bid is scaled to match."""
        return [
            basket_member("A", RICH),
            basket_member("B", [("0.3500", "50.00")], notional=Price.from_value("0.5000")),
            basket_member("C", RICH),
        ]

    def test_the_witness_is_the_single_minimising_member(self):
        result = evaluate(members=self.uneven(), relation=relation_certificate(["A", "B", "C"]))
        assert result.minimising_members == ("B",)

    def test_differing_notionals_use_the_smallest_winning_payout(self):
        result = evaluate(members=self.uneven(), relation=relation_certificate(["A", "B", "C"]))
        assert result.guaranteed_payoff == money("0.500000")

    def test_ties_report_every_witness(self):
        result = evaluate()
        assert result.minimising_members == ("A", "B", "C")

    def test_the_floor_scales_with_quantity(self):
        assert evaluate(quantity="2.00").guaranteed_payoff == money("2.000000")


class TestBlockedPaths:
    """Scenarios 7, 10-17: every way this must refuse."""

    def test_a_shallow_leg_blocks_on_depth(self):
        members = [
            basket_member("A", RICH),
            basket_member("B", [("0.7000", "0.50")]),
            basket_member("C", RICH),
        ]
        result = evaluate(members=members, quantity="1.00")
        assert result.classification is Classification.INSUFFICIENT_DEPTH
        assert result.blocking_legs == ("B",)

    def test_an_unverified_relation_blocks(self):
        relation = relation_certificate(["A", "B", "C"], status=RelationStatus.REVIEW_REQUIRED)
        result = evaluate(relation=relation)
        assert result.classification is Classification.BLOCKED_SETTLEMENT_SEMANTICS
        assert result.semantic_status is SemanticStatus.BLOCKED

    def test_a_stale_relation_certificate_blocks(self):
        result = evaluate(relation_fingerprint=synthetic_fingerprint(event=EVENT, v="moved"))
        assert result.classification is Classification.BLOCKED_SETTLEMENT_SEMANTICS
        assert "evidence has changed" in (result.blocking_reason or "")

    def test_unavailable_relation_evidence_blocks(self):
        result = evaluate(relation_unavailable=True)
        assert result.classification is Classification.BLOCKED_SETTLEMENT_SEMANTICS
        assert "unavailable" in (result.blocking_reason or "")

    def test_a_missing_member_certificate_blocks(self):
        members = [
            basket_member("A", RICH),
            basket_member("B", RICH, cert_status=CertificateStatus.REVIEW_REQUIRED),
            basket_member("C", RICH),
        ]
        result = evaluate(members=members)
        assert result.classification is Classification.BLOCKED_SETTLEMENT_SEMANTICS
        assert "B" in result.blocking_legs

    def test_stale_member_settlement_evidence_blocks(self):
        members = [
            basket_member("A", RICH),
            basket_member("B", RICH, current_salt="drifted"),
            basket_member("C", RICH),
        ]
        result = evaluate(members=members)
        assert result.classification is Classification.BLOCKED_SETTLEMENT_SEMANTICS

    def test_an_at_most_one_certificate_is_refused_not_repriced(self):
        """The two claims forbid opposite states; neither substitutes."""
        relation = relation_certificate(["A", "B", "C"], claim=RelationClaim.AT_MOST_ONE)
        result = evaluate(relation=relation)
        assert result.classification is Classification.BLOCKED_SETTLEMENT_SEMANTICS
        assert "AT_MOST_ONE" in (result.blocking_reason or "")
        assert "not interchangeable" in (result.blocking_reason or "")
        assert result.payoff_proof is None

    def test_a_certificate_over_a_different_member_set_blocks(self):
        result = evaluate(relation=relation_certificate(["A", "B"]))
        assert result.classification is Classification.BLOCKED_SETTLEMENT_SEMANTICS
        assert "not" in (result.blocking_reason or "")

    def test_an_invalid_book_blocks(self):
        members = [
            basket_member("A", RICH),
            basket_member("B", RICH, integrity=BookIntegrity.INTEGRITY_UNKNOWN),
            basket_member("C", RICH),
        ]
        result = evaluate(members=members)
        assert result.classification is Classification.BLOCKED_BOOK_INTEGRITY
        assert result.execution_status is ExecutionStatus.STALE_OR_INVALID

    def test_a_liquidity_collision_blocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Distinct tickers do not prove distinct source liquidity.

        Two legs reaching the same aggregate level would consume the same
        resting orders twice, manufacturing depth that does not exist. The
        detector must refuse rather than price it.
        """
        collision = BookLiquidityId(
            connection_epoch=1,
            sid=1,
            market_ticker="A",
            source_outcome=MarketSide.NO,
            source_price=Price.from_value("0.7000"),
        )
        monkeypatch.setattr(
            detector_module, "detect_liquidity_collisions", lambda *_a, **_k: (collision,)
        )
        result = evaluate()
        assert result.classification is Classification.BLOCKED_LIQUIDITY_COLLISION
        assert result.profit is None

    def test_collision_detection_spans_all_legs(self):
        """Three legs normally consume three distinct sources."""
        ids = evaluate().liquidity_ids
        assert len(ids) == 3
        assert len(set(ids)) == 3

    def test_an_unavailable_fee_blocks_rather_than_counting_as_zero(self):
        result = evaluate(quoter=flat_fees("0.010000", per_leg={"B": "unavailable"}))
        assert result.classification is Classification.BLOCKED_FEE_SEMANTICS
        assert "unavailable" in (result.blocking_reason or "")

    def test_an_unresolved_multiplier_blocks(self):
        result = evaluate(quoter=flat_fees("0.010000", per_leg={"C": "unresolved"}))
        assert result.classification is Classification.BLOCKED_FEE_SEMANTICS
        assert "cannot support a contractual claim" in (result.blocking_reason or "")

    def test_a_single_member_basket_is_refused(self):
        """Scenario 19, and the choice is made twice over.

        "At least one of {A} must settle YES" is "A must settle YES" -- a
        statement about that contract's payoff, which belongs in a settlement
        certificate. So a one-member relation certificate cannot be built at
        all, and the detector refuses n = 1 independently in case one is
        constructed some other way.
        """
        with pytest.raises(ValueError, match="asserts nothing useful"):
            relation_certificate(["A"])

        # And independently in the detector, in case one is built another way.
        result = evaluate(
            members=[basket_member("A", RICH)], relation=relation_certificate(["A", "B"])
        )
        assert result.classification is Classification.BLOCKED_SETTLEMENT_SEMANTICS
        assert "at least 2 members" in (result.blocking_reason or "")


class TestFeeSensitivity:
    """Scenarios 8, 9, 18."""

    def test_a_large_fee_erases_the_edge(self):
        result = evaluate(quoter=flat_fees("0.050000"))
        assert result.classification is Classification.PROVEN_NOT_PROFITABLE
        assert result.payoff_status is PayoffStatus.NON_POSITIVE

    def test_a_fee_interval_straddling_zero_is_indeterminate(self):
        result = evaluate(quoter=flat_fees("0.010000", "0.050000"))
        assert result.classification is Classification.INDETERMINATE_COST_BOUNDS
        assert result.profit is not None
        assert result.profit.profit_lower_bound.units <= 0 < result.profit.profit_upper_bound.units

    def test_exactly_zero_guaranteed_profit_is_not_arbitrage(self):
        """Strictly positive means strictly positive. Zero is not an edge.

        Two legs with NO bids at 0.5000 cost 0.5000 each, so gross is exactly
        1.000000 against a floor of exactly 1.000000. With zero fees the
        guaranteed profit is exactly zero, and that is not a claim.
        """
        members = [basket_member(t, [("0.5000", "50.00")]) for t in ("A", "B")]
        result = evaluate(
            members=members,
            relation=relation_certificate(["A", "B"]),
            quoter=flat_fees("0.000000"),
        )
        assert result.total_gross_cost == money("1.000000")
        assert result.guaranteed_payoff == money("1.000000")
        assert result.profit is not None
        assert result.profit.profit_lower_bound == money("0.000000")
        assert result.classification is Classification.PROVEN_NOT_PROFITABLE
        assert not result.is_arbitrage_claim


class TestLargeBasket:
    """Scenario 20: no exponential enumeration anywhere on the live path."""

    def test_a_forty_member_basket_evaluates_without_enumerating_states(self):
        tickers = [f"M{i:02d}" for i in range(40)]
        members = [basket_member(t, RICH) for t in tickers]
        result = evaluate(members=members, relation=relation_certificate(tickers))

        assert result.payoff_proof is not None
        assert result.payoff_proof.permitted_state_count == 2**40 - 1
        assert result.guaranteed_payoff == money("1.000000")
        # 40 legs at 0.30 = 12.00 gross against a 1.00 floor.
        assert result.classification is Classification.PROVEN_NOT_PROFITABLE

    def test_the_proof_object_holds_no_materialised_state_set(self):
        proof = evaluate().payoff_proof
        assert proof is not None
        assert isinstance(proof.permitted_state_count, int)
        assert not hasattr(proof, "states")


class TestSearch:
    def search(self, **kwargs: Any) -> YesBasketSearch:
        relation = kwargs.pop("relation", None) or relation_certificate(["A", "B", "C"])
        params: dict[str, Any] = {
            "relation": relation,
            "relation_evidence_fingerprint": relation.evidence_fingerprint,
            "members": [basket_member(t, RICH) for t in ("A", "B", "C")],
            "context": CONTEXT,
            "fee_quoter": flat_fees("0.010000"),
            "min_quantity": q("0.01"),
            "max_quantity": q("1.00"),
            "at": T0,
        }
        return search_yes_basket(**{**params, **kwargs})

    def test_it_evaluates_every_hundredth(self):
        search = self.search()
        assert search.evaluated_quantity_count == 100
        assert search.search_step == q("0.01")

    def test_the_scope_travels_with_the_result(self):
        search = self.search(max_quantity=q("0.50"))
        assert search.search_max_quantity == q("0.50")
        assert "no proven candidate in [0.01, 0.50]" in search.summary() or search.proven_candidates

    def test_depth_limits_are_reported_not_hidden(self):
        members = [basket_member(t, [("0.7000", "0.20")]) for t in ("A", "B", "C")]
        search = self.search(members=members, max_quantity=q("1.00"))
        assert search.search_complete is False
        assert any("common depth" in w for w in search.warnings)

    def test_a_blocked_basket_reports_that_no_search_ran(self):
        search = self.search(
            relation=relation_certificate(["A", "B", "C"], status=RelationStatus.REJECTED)
        )
        assert search.evaluated_quantity_count == 0
        assert search.search_complete is False
        assert any("search not performed" in w for w in search.warnings)

    def test_counts_reconcile_with_evaluations(self):
        search = self.search(keep="all")
        assert sum(search.classification_counts.values()) == search.evaluated_quantity_count


class TestPurity:
    def test_the_detector_reaches_for_nothing(self):
        source = Path(detector_module.__file__ or "").read_text()
        for forbidden in (
            "requests",
            "httpx",
            "asyncio",
            "open(",
            "datetime.now",
            "CertificateRegistry",
            "predarb.storage",
        ):
            assert forbidden not in source, f"detector must not use {forbidden}"

    def test_no_exactly_one_detector_exists(self):
        """Step 12 composes EXACTLY_ONE logically and prices nothing with it."""
        source = Path(detector_module.__file__ or "").read_text()
        assert "def detect_exactly_one" not in source
        assert "EXACTLY_ONE" not in source

    def test_the_evaluation_instant_is_supplied(self):
        assert evaluate().detected_at == T0

    def test_member_identities_are_recorded_for_audit(self):
        result = evaluate()
        assert set(result.member_certificate_ids) == {"A", "B", "C"}
        assert set(result.member_evidence_fingerprints) == {"A", "B", "C"}

    def test_execution_risk_is_separate_from_the_contractual_proof(self):
        result = evaluate()
        assert result.non_atomic is True
        assert result.leg_count == 3
        assert result.execution_status is ExecutionStatus.RACE_EXPOSED
        assert any("race-exposed" in w for w in result.warnings)

    def test_there_is_no_single_profit_field(self):
        assert not hasattr(YesBasketResult, "profit_value")
        assert not hasattr(YesBasketResult, "confidence")
