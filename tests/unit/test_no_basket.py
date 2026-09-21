"""AT_MOST_ONE NO-basket detector.

Book arithmetic reminder: buying NO crosses YES bids, so
``no_ask = notional - yes_bid``. A basket of n NO legs costs
``sum(N_i - yes_bid_i)`` and pays ``sum(N) - max(N)`` in the worst joint state,
so it is profitable when the YES bids are collectively rich -- the multi-market
analogue of a crossed complement.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from predarb.books.execution import ExecutionContext, ExecutionQuote
from predarb.books.levels import BookLevel
from predarb.books.liquidity import BookLiquidityId
from predarb.books.orderbook import BookView
from predarb.books.state import BookIntegrity, BookProvenance
from predarb.detectors import no_basket as detector_module
from predarb.detectors.binary_complement import LegFeeQuoter
from predarb.detectors.no_basket import (
    BasketMember,
    BasketResult,
    BasketSearch,
    JointMemberNoPayoff,
    evaluate_basket_quantity,
    search_no_basket,
)
from predarb.domain.costs import FeeBounds, FeesUnavailable, LegFees
from predarb.domain.enums import MarketSide, SettlementKind, VenueId
from predarb.domain.models import VenueInstrument
from predarb.domain.money import Money, Price, Quantity
from predarb.domain.payoff import IncompletePayoffSpecificationError, SettlementState
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
    NO_SELECTED_MEMBER_WINS,
    RelationCertificate,
    RelationClaim,
    RelationStatus,
)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
NOTIONAL = Price.from_value("1.0000")
CONTEXT = ExecutionContext(current_connection_epoch=1)
EVENT = "EVT"


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
    yes_levels: list[tuple[str, str]],
    *,
    integrity: BookIntegrity = BookIntegrity.VALID,
    epoch: int = 1,
) -> BookView:
    """A book with YES bids only -- the liquidity a NO buyer crosses."""
    levels = tuple(
        BookLevel(price=Price.from_value(p), quantity=q(size), side=MarketSide.YES)
        for p, size in sorted(yes_levels, key=lambda r: Decimal(r[0]), reverse=True)
    )
    return BookView(
        market_ticker=ticker,
        integrity=integrity,
        yes_bids=levels,
        no_bids=(),
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
    fingerprint: SettlementEvidenceFingerprint | None = None,
    member_fingerprints: dict[str, str] | None = None,
    status: RelationStatus = RelationStatus.VERIFIED,
    issued_at: datetime = T0,
) -> RelationCertificate:
    prints = member_fingerprints or {t: member_fingerprint(t).digest for t in members}
    return RelationCertificate(
        certificate_id="relcert-" + "-".join(members),
        claim=RelationClaim.AT_MOST_ONE,
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
    yes_levels: list[tuple[str, str]],
    *,
    notional: Price = NOTIONAL,
    cert_status: CertificateStatus = CertificateStatus.VERIFIED,
    current_salt: str | None = "v1",
    integrity: BookIntegrity = BookIntegrity.VALID,
    epoch: int = 1,
) -> BasketMember:
    """``current_salt`` of ``None`` means current evidence is unavailable; a
    different salt means it drifted."""
    current = member_fingerprint(ticker, current_salt) if current_salt else None
    return BasketMember(
        instrument=instrument(ticker, notional),
        view=book(ticker, yes_levels, integrity=integrity, epoch=epoch),
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

    def quoter(quote: object) -> LegFees:  # noqa: ARG001 - protocol signature
        return FeeBounds(
            lower=money(lower),
            upper=money(top),
            exact=(lower == top),
            supports_arbitrage_claim=supports,
            provenance="stub",
        )

    def per_leg_quoter(quote: ExecutionQuote) -> LegFees:
        ticker = quote.market_ticker
        if per_leg and ticker in per_leg:
            value = per_leg[ticker]
            if value == "unavailable":
                return FeesUnavailable(reason="fee type unsupported", provenance="stub")
            if value == "unresolved":
                return FeeBounds(
                    lower=money(lower),
                    upper=money(top),
                    exact=False,
                    supports_arbitrage_claim=False,
                    provenance="stub A-14",
                )
        return FeeBounds(
            lower=money(lower),
            upper=money(top),
            exact=(lower == top),
            supports_arbitrage_claim=supports,
            provenance="stub",
        )

    return per_leg_quoter if per_leg else quoter


# Three markets, each with a rich YES bid at 0.40: a NO leg costs
# 1.00 - 0.40 = 0.60, so three legs cost 1.80 and pay 2.00 in every joint state.
RICH = [("0.4000", "50.00")]
RICHER = [("0.6000", "50.00")]


def evaluate(
    *,
    members: list[BasketMember] | None = None,
    relation: RelationCertificate | None = None,
    relation_fingerprint: SettlementEvidenceFingerprint | None = None,
    relation_unavailable: bool = False,
    quoter: LegFeeQuoter | None = None,
    quantity: str = "1.00",
    context: ExecutionContext = CONTEXT,
) -> BasketResult:
    """``relation_fingerprint`` of ``None`` uses the certificate's own, unless
    ``relation_unavailable`` says current evidence could not be established."""
    legs = members if members is not None else [basket_member(t, RICH) for t in ("A", "B", "C")]
    cert = relation or relation_certificate([m.ticker for m in legs])
    current = None if relation_unavailable else (relation_fingerprint or cert.evidence_fingerprint)
    return evaluate_basket_quantity(
        relation=cert,
        relation_evidence_fingerprint=current,
        members=legs,
        context=context,
        fee_quoter=quoter or flat_fees("0.010000"),
        quantity=q(quantity),
        at=T0,
    )


class TestScenario01ThreeMarketProvenArb:
    @pytest.fixture
    def result(self) -> BasketResult:
        return evaluate(quoter=flat_fees("0.010000", "0.020000"))

    def test_classified_proven(self, result):
        assert result.classification is Classification.PROVEN_CONTRACTUAL_ARBITRAGE
        assert result.is_arbitrage_claim

    def test_worst_case_is_n_minus_one_notionals(self, result):
        """Emerges from the generic engine, not from a hardcoded formula."""
        assert result.worst_case_payoff == money("2.000000")

    def test_gross_cost_is_the_sum_of_leg_costs(self, result):
        assert result.total_gross_cost == money("1.800000")
        assert set(result.gross_cost_by_leg) == {"A", "B", "C"}

    def test_profit_lower_uses_fee_upper_bounds(self, result):
        # 2.00 - (1.80 + 3 x 0.02)
        assert result.profit is not None
        assert result.profit.profit_lower_bound == money("0.140000")

    def test_profit_upper_uses_fee_lower_bounds(self, result):
        assert result.profit is not None
        assert result.profit.profit_upper_bound == money("0.170000")

    def test_statuses_are_reported_separately(self, result):
        assert result.semantic_status is SemanticStatus.VERIFIED
        assert result.payoff_status is PayoffStatus.STATE_INDEPENDENT_PROFIT
        assert result.cost_status is CostStatus.BOUNDED
        assert result.execution_status is ExecutionStatus.RACE_EXPOSED


class TestScenario02to04StateSpace:
    def test_the_all_no_state_is_included(self):
        result = evaluate()
        names = {s.name for s in result.joint_states}
        assert NO_SELECTED_MEMBER_WINS in names

    def test_there_is_one_state_per_member(self):
        result = evaluate()
        names = {s.name for s in result.joint_states}
        assert {"A", "B", "C"} <= names

    def test_there_are_exactly_n_plus_one_states(self):
        """Not 2**n filtered: the forbidden combinations are never built."""
        result = evaluate()
        assert len(result.joint_states) == 4

    def test_no_two_yes_state_exists(self):
        """A state where two members both win is not in the space at all."""
        result = evaluate()
        names = {s.name for s in result.joint_states}
        assert "A+B" not in names
        assert not any("," in name or "+" in name for name in names)

    def test_every_state_pays_at_least_the_worst_case(self):
        result = evaluate()
        assert result.portfolio_payoff is not None
        worst = result.worst_case_payoff
        assert worst is not None
        for entry in result.portfolio_payoff.per_state:
            assert entry.total >= worst

    def test_the_all_no_state_pays_the_most(self):
        """Nothing lost: every NO leg wins."""
        result = evaluate()
        assert result.portfolio_payoff is not None
        best = result.portfolio_payoff.payoff_in(
            next(s for s in result.joint_states if s.name == NO_SELECTED_MEMBER_WINS)
        )
        assert best == money("3.000000")


class TestScenario05to06IncompleteMembership:
    def test_all_selected_no_is_a_valid_state(self):
        """The claim never asserts that one member must win."""
        result = evaluate()
        assert result.portfolio_payoff is not None
        assert any(s.name == NO_SELECTED_MEMBER_WINS for s in result.joint_states)

    def test_a_subset_of_a_larger_event_still_proves(self):
        """A winner outside the subset maps to all-selected-NO.

        The basket is certified over {A, B} while the event also contains C.
        An outside winner is economically identical to "neither A nor B won",
        which is already enumerated -- so the proof holds regardless.
        """
        # A basket profits exactly when the YES bids sum above the notional:
        # cost is sum(N - yes_bid_i) and the worst case pays (n-1) x N. Two legs
        # therefore need richer bids than three do.
        members = [basket_member(t, RICHER) for t in ("A", "B")]
        relation = relation_certificate(["A", "B"])
        result = evaluate(members=members, relation=relation, quoter=flat_fees("0.010000"))
        assert result.classification is Classification.PROVEN_CONTRACTUAL_ARBITRAGE
        # 2 legs at 1.00 - 0.60 = 0.40 -> 0.80 cost; worst case pays 1 x 1.00
        assert result.total_gross_cost == money("0.800000")
        assert result.worst_case_payoff == money("1.000000")
        assert len(result.joint_states) == 3

    def test_the_warning_refuses_completeness_language(self):
        result = evaluate()
        assert any(
            "not a claim that the event's outcome set is complete" in w for w in result.warnings
        )
        assert any("selected certified mutually-exclusive subset" in w for w in result.warnings)


class TestScenario07to08Fees:
    def test_fees_can_erase_the_edge(self):
        result = evaluate(quoter=flat_fees("0.100000"))
        assert result.classification is Classification.PROVEN_NOT_PROFITABLE
        assert result.payoff_status is PayoffStatus.NON_POSITIVE

    def test_a_straddling_fee_interval_is_indeterminate(self):
        # margin is 0.20 gross; fees per leg in [0.01, 0.10] -> total [0.03, 0.30]
        result = evaluate(quoter=flat_fees("0.010000", "0.100000"))
        assert result.classification is Classification.INDETERMINATE_COST_BOUNDS
        assert result.profit is not None
        assert result.profit.profit_lower_bound.units <= 0
        assert result.profit.profit_upper_bound.units > 0

    def test_each_leg_is_quoted_independently(self):
        result = evaluate()
        assert set(result.fees_by_leg) == {"A", "B", "C"}


class TestScenario09ShallowLeg:
    def test_one_shallow_leg_blocks_the_basket(self):
        members = [
            basket_member("A", RICH),
            basket_member("B", [("0.4000", "0.50")]),
            basket_member("C", RICH),
        ]
        result = evaluate(members=members, quantity="1.00")
        assert result.classification is Classification.INSUFFICIENT_DEPTH
        assert result.blocking_legs == ("B",)

    def test_no_profit_interval_is_produced(self):
        members = [
            basket_member("A", RICH),
            basket_member("B", [("0.4000", "0.50")]),
            basket_member("C", RICH),
        ]
        result = evaluate(members=members)
        assert result.profit is None


class TestScenario10to12Certificates:
    def test_a_stale_member_settlement_certificate_blocks(self):
        members = [
            basket_member("A", RICH),
            basket_member("B", RICH, current_salt="v2"),
            basket_member("C", RICH),
        ]
        result = evaluate(members=members)
        assert result.classification is Classification.BLOCKED_SETTLEMENT_SEMANTICS
        assert result.semantic_status is SemanticStatus.BLOCKED

    def test_an_unavailable_member_fingerprint_blocks(self):
        members = [
            basket_member("A", RICH),
            basket_member("B", RICH, current_salt=None),
            basket_member("C", RICH),
        ]
        assert evaluate(members=members).classification is (
            Classification.BLOCKED_SETTLEMENT_SEMANTICS
        )

    def test_an_uncertified_member_blocks(self):
        members = [
            basket_member("A", RICH),
            basket_member("B", RICH, cert_status=CertificateStatus.REVIEW_REQUIRED),
            basket_member("C", RICH),
        ]
        result = evaluate(members=members)
        assert result.classification is Classification.BLOCKED_SETTLEMENT_SEMANTICS
        assert "B" in result.blocking_legs

    def test_stale_relation_evidence_blocks(self):
        result = evaluate(relation_fingerprint=synthetic_fingerprint(event=EVENT, members="other"))
        assert result.classification is Classification.BLOCKED_SETTLEMENT_SEMANTICS
        assert result.blocking_reason is not None
        assert "relation evidence has changed" in result.blocking_reason

    def test_unavailable_relation_evidence_blocks(self):
        result = evaluate(relation_unavailable=True)
        assert result.classification is Classification.BLOCKED_SETTLEMENT_SEMANTICS

    @pytest.mark.parametrize(
        "status",
        [RelationStatus.REVIEW_REQUIRED, RelationStatus.REJECTED, RelationStatus.INVALIDATED],
    )
    def test_an_unverified_relation_blocks(self, status):
        relation = relation_certificate(["A", "B", "C"], status=status)
        result = evaluate(relation=relation)
        assert result.classification is Classification.BLOCKED_SETTLEMENT_SEMANTICS

    def test_no_economics_are_computed_when_semantics_block(self):
        result = evaluate(relation_unavailable=True)
        assert result.profit is None
        assert result.total_gross_cost is None
        assert result.quotes == {}


class TestScenario13to15MembershipAndDrift:
    def test_mutually_exclusive_does_not_appear_in_the_detector(self):
        """A metadata flag can never stand in for a reviewed certificate."""
        source = detector_module.__file__
        assert source is not None
        assert "mutually_exclusive" not in Path(source).read_text()

    def test_a_new_member_does_not_join_an_existing_certificate(self):
        """The certificate covers an exact set, not a superset."""
        relation = relation_certificate(["A", "B"])
        members = [basket_member(t, RICH) for t in ("A", "B", "C")]
        result = evaluate(members=members, relation=relation)
        assert result.classification is Classification.BLOCKED_SETTLEMENT_SEMANTICS
        assert result.blocking_reason is not None
        assert "not what was reviewed" in result.blocking_reason

    def test_a_dropped_member_also_fails_coverage(self):
        relation = relation_certificate(["A", "B", "C"])
        members = [basket_member(t, RICH) for t in ("A", "B")]
        result = evaluate(members=members, relation=relation)
        assert result.classification is Classification.BLOCKED_SETTLEMENT_SEMANTICS

    def test_member_evidence_drift_recorded_in_the_relation_blocks(self):
        """The relation was reviewed against those payoff tables."""
        relation = relation_certificate(
            ["A", "B", "C"],
            member_fingerprints={
                "A": member_fingerprint("A").digest,
                "B": member_fingerprint("B", "OLD").digest,
                "C": member_fingerprint("C").digest,
            },
        )
        result = evaluate(relation=relation)
        assert result.classification is Classification.BLOCKED_SETTLEMENT_SEMANTICS
        assert result.blocking_reason is not None
        assert "settlement evidence for B has changed" in result.blocking_reason


class TestScenario16to18ExecutionAndFees:
    def test_a_liquidity_collision_blocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        collision = BookLiquidityId(
            connection_epoch=1,
            sid=1,
            market_ticker="A",
            source_outcome=MarketSide.YES,
            source_price=Price.from_value("0.4000"),
        )
        monkeypatch.setattr(
            detector_module, "detect_liquidity_collisions", lambda *_a, **_k: (collision,)
        )
        result = evaluate()
        assert result.classification is Classification.BLOCKED_LIQUIDITY_COLLISION
        assert result.profit is None

    def test_collision_detection_spans_all_legs(self):
        """Three legs normally consume three distinct sources."""
        result = evaluate()
        ids = result.liquidity_ids
        assert len(ids) == 3
        assert len(set(ids)) == 3

    @pytest.mark.parametrize(
        "integrity", [BookIntegrity.WAITING_SNAPSHOT, BookIntegrity.INTEGRITY_UNKNOWN]
    )
    def test_an_invalid_book_on_one_leg_blocks(self, integrity):
        members = [
            basket_member("A", RICH),
            basket_member("B", RICH, integrity=integrity),
            basket_member("C", RICH),
        ]
        result = evaluate(members=members)
        assert result.classification is Classification.BLOCKED_BOOK_INTEGRITY
        assert result.execution_status is ExecutionStatus.STALE_OR_INVALID
        assert result.blocking_legs == ("B",)

    def test_an_unresolved_multiplier_on_one_leg_blocks(self):
        result = evaluate(quoter=flat_fees("0.010000", per_leg={"B": "unresolved"}))
        assert result.classification is Classification.BLOCKED_FEE_SEMANTICS
        assert result.blocking_legs == ("B",)

    def test_an_unavailable_fee_on_one_leg_blocks(self):
        result = evaluate(quoter=flat_fees("0.010000", per_leg={"C": "unavailable"}))
        assert result.classification is Classification.BLOCKED_FEE_SEMANTICS
        assert result.blocking_legs == ("C",)
        assert result.profit is None

    def test_the_blocking_leg_is_named_in_the_reason(self):
        result = evaluate(quoter=flat_fees("0.010000", per_leg={"B": "unresolved"}))
        assert result.blocking_reason is not None
        assert "B:" in result.blocking_reason


class TestScenario19ZeroProfit:
    def test_exact_zero_guaranteed_profit_is_not_arbitrage(self):
        # gross 1.80, payout 2.00 -> margin 0.20; exact fees of 0.20/3... use
        # a per-leg fee that sums to exactly 0.20
        result = evaluate(quoter=flat_fees("0.066667"))
        assert result.profit is not None
        assert result.profit.profit_lower_bound == Money.from_units(-1)
        assert result.classification is Classification.PROVEN_NOT_PROFITABLE

    def test_the_boundary_is_one_micro_dollar_wide(self):
        result = evaluate(quoter=flat_fees("0.066666"))
        assert result.profit is not None
        assert result.profit.profit_lower_bound == Money.from_units(2)
        assert result.classification is Classification.PROVEN_CONTRACTUAL_ARBITRAGE


class TestScenario20SingleMember:
    def test_a_one_member_basket_is_refused(self):
        """ "At most one of {A} settles YES" is true of any binary market."""
        with pytest.raises(ValueError, match="asserts nothing"):
            relation_certificate(["A"])

    def test_the_detector_refuses_a_single_leg_too(self):
        relation = relation_certificate(["A", "B"])
        result = evaluate(members=[basket_member("A", RICH)], relation=relation)
        assert result.classification is Classification.BLOCKED_SETTLEMENT_SEMANTICS
        assert result.blocking_reason is not None
        assert "at least 2 members" in result.blocking_reason


class TestJointPayoffLifting:
    def test_a_member_loses_only_in_its_own_state(self):
        certificate = settlement_certificate("A")
        lifted = JointMemberNoPayoff(
            ticker="A",
            certificate=certificate,
            joint_states=(NO_SELECTED_MEMBER_WINS, "A", "B"),
        )
        assert lifted.payoff_per_contract(SettlementState("A")) == Price.from_units(0)
        assert lifted.payoff_per_contract(SettlementState("B")) == NOTIONAL
        assert lifted.payoff_per_contract(SettlementState(NO_SELECTED_MEMBER_WINS)) == NOTIONAL

    def test_a_state_outside_the_relation_raises(self):
        lifted = JointMemberNoPayoff(
            ticker="A",
            certificate=settlement_certificate("A"),
            joint_states=(NO_SELECTED_MEMBER_WINS, "A"),
        )
        with pytest.raises(IncompletePayoffSpecificationError):
            lifted.payoff_per_contract(SettlementState("Z"))

    def test_unequal_notionals_lift_correctly(self):
        """The generic identity is q x (sum(N) - max(N))."""
        members = [
            basket_member("A", RICH, notional=Price.from_value("1.0000")),
            basket_member("B", RICH, notional=Price.from_value("2.0000")),
        ]
        result = evaluate(
            members=members,
            relation=relation_certificate(["A", "B"]),
            quoter=flat_fees("0.000100"),
        )
        # sum = 3.00, max = 2.00 -> worst case 1.00
        assert result.worst_case_payoff == money("1.000000")


class TestNoNettingAssumption:
    def test_cash_is_reported_gross_by_leg(self):
        result = evaluate()
        total = sum(c.units for c in result.gross_cost_by_leg.values())
        assert result.total_gross_cost is not None
        assert result.total_gross_cost.units == total

    def test_the_absence_of_netting_is_stated(self):
        result = evaluate()
        assert any("no collateral-netting" in w for w in result.warnings)


class TestRaceRisk:
    def test_a_proven_basket_is_race_exposed(self):
        result = evaluate(quoter=flat_fees("0.010000"))
        assert result.classification is Classification.PROVEN_CONTRACTUAL_ARBITRAGE
        assert result.execution_status is ExecutionStatus.RACE_EXPOSED
        assert result.non_atomic
        assert result.leg_count == 3

    def test_the_warning_names_the_leg_count(self):
        result = evaluate(quoter=flat_fees("0.010000"))
        assert any("ALL 3 legs" in w for w in result.warnings)

    def test_no_locked_status_exists(self):
        assert "LOCKED" not in {member.value for member in ExecutionStatus}


class TestPurity:
    def test_the_detector_performs_no_io(self):
        assert detector_module.__file__ is not None
        source = Path(detector_module.__file__).read_text()
        for forbidden in (
            "predarb.venues",
            "predarb.ingest",
            "predarb.storage",
            "CertificateRegistry",
            "httpx",
            "asyncio",
            "open(",
        ):
            assert forbidden not in source, f"detector must not use {forbidden}"

    def test_no_exhaustiveness_relation_is_referenced(self):
        assert detector_module.__file__ is not None
        source = Path(detector_module.__file__).read_text()
        for forbidden in ("AT_LEAST_ONE", "EXACTLY_ONE", "PARTITION"):
            assert forbidden not in source

    def test_the_evaluation_instant_is_supplied(self):
        assert evaluate().detected_at == T0


class TestBoundedSearch:
    def search(self, **kwargs: object) -> BasketSearch:
        members = [basket_member(t, RICH) for t in ("A", "B", "C")]
        relation = relation_certificate(["A", "B", "C"])
        params: dict[str, object] = {
            "relation": relation,
            "relation_evidence_fingerprint": relation.evidence_fingerprint,
            "members": members,
            "context": CONTEXT,
            "fee_quoter": flat_fees("0.010000"),
            "min_quantity": q("0.01"),
            "max_quantity": q("0.05"),
            "at": T0,
        }
        params.update(kwargs)
        return search_no_basket(**params)  # type: ignore[arg-type]

    def test_every_increment_is_evaluated(self):
        result = self.search()
        assert result.evaluated_quantity_count == 5

    def test_the_scope_travels_with_the_answer(self):
        result = self.search()
        assert result.search_min_quantity == q("0.01")
        assert result.search_max_quantity == q("0.05")
        assert result.search_complete

    def test_a_barren_sweep_claims_only_its_interval(self):
        result = self.search(fee_quoter=flat_fees("5.000000"))
        assert result.proven_candidates == ()
        assert "no proven candidate in" in result.summary()

    def test_counts_cover_every_evaluation(self):
        result = self.search(fee_quoter=flat_fees("5.000000"))
        assert sum(result.classification_counts.values()) == 5

    def test_common_depth_caps_the_interval(self):
        members = [
            basket_member("A", RICH),
            basket_member("B", [("0.4000", "0.03")]),
            basket_member("C", RICH),
        ]
        result = self.search(members=members, max_quantity=q("1.00"))
        assert result.search_max_quantity == q("0.03")
        assert any("common depth" in w for w in result.warnings)

    def test_blocked_semantics_prevent_the_sweep(self):
        result = self.search(relation_evidence_fingerprint=None)
        assert result.evaluated_quantity_count == 0
        assert not result.search_complete

    def test_an_inverted_interval_is_refused(self):
        with pytest.raises(ValueError, match="search interval is empty"):
            self.search(min_quantity=q("1.00"), max_quantity=q("0.50"))

    def test_search_agrees_with_direct_evaluation(self):
        swept = self.search(fee_quoter=flat_fees("0.000100"), keep="all")
        for result in swept.results:
            direct = evaluate(quantity=str(result.quantity), quoter=flat_fees("0.000100"))
            assert direct.classification is result.classification

    def test_curves_are_built_once_per_leg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[int] = []
        original = detector_module.build_execution_curve  # type: ignore[attr-defined]

        def counting(*args: object, **kwargs: object) -> object:
            calls.append(1)
            return original(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(detector_module, "build_execution_curve", counting)
        self.search(max_quantity=q("0.20"))
        # One probe evaluation (3) plus one build per leg for the sweep (3).
        assert len(calls) == 6
