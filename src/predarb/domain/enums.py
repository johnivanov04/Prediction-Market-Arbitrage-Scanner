"""Closed vocabularies for the domain.

Every enum here encodes a distinction the system is required to keep separate.
In particular, the three status axes on an opportunity (:class:`SemanticStatus`,
:class:`PayoffStatus`, :class:`ExecutionStatus`) are independent on purpose: a
portfolio can have a provably state-independent profit while still being
exposed to race risk during execution, and collapsing those into one "is it
arbitrage?" flag is how false claims get made.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "ExecutionStatus",
    "FeeType",
    "Liquidity",
    "MarketSide",
    "OpportunityKind",
    "PayoffStatus",
    "RelationType",
    "SemanticStatus",
    "SettlementKind",
    "VenueId",
    "VerificationStatus",
]


class VenueId(StrEnum):
    """Venues. Phase 1 implements KALSHI only."""

    KALSHI = "KALSHI"


class MarketSide(StrEnum):
    """Which side of a binary contract a leg holds."""

    YES = "YES"
    NO = "NO"


class SettlementKind(StrEnum):
    """How a contract's terminal payoff is determined.

    Only :attr:`BINARY` is eligible for contractual-arbitrage classification in
    Phase 1. Everything else, including :attr:`UNKNOWN`, fails closed: the
    payoff engine refuses to enumerate states it cannot prove.
    """

    BINARY = "BINARY"
    """Pays the full notional to exactly one of YES/NO. The only provable case."""

    SCALAR = "SCALAR"
    """Settles somewhere on a range. Kalshi exposes ``market_type == "scalar"``
    and ``result == "scalar"``; such a contract has no two-state payoff."""

    UNKNOWN = "UNKNOWN"
    """Settlement semantics have not been verified. Fails closed."""


class RelationType(StrEnum):
    """Logical relationships between propositions.

    Phase 1 detects the first four. ``IMPLIES``/``DISJOINT``/``PARTITION`` are
    declared now so the storage schema and registry do not need migrating when
    later phases add them, but no detector consumes them yet.
    """

    EQUIVALENT = "EQUIVALENT"
    AT_MOST_ONE = "AT_MOST_ONE"
    AT_LEAST_ONE = "AT_LEAST_ONE"
    EXACTLY_ONE = "EXACTLY_ONE"

    IMPLIES = "IMPLIES"
    DISJOINT = "DISJOINT"
    PARTITION = "PARTITION"


class VerificationStatus(StrEnum):
    """Review state of a :class:`RelationType` assertion.

    Only :attr:`VERIFIED` relations may feed a contractual-arbitrage detector.
    """

    VERIFIED = "VERIFIED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    REJECTED = "REJECTED"


class SemanticStatus(StrEnum):
    """Whether the logical claim underpinning an opportunity is proven."""

    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"


class PayoffStatus(StrEnum):
    """What the payoff engine could prove about the portfolio."""

    STATE_INDEPENDENT_PROFIT = "STATE_INDEPENDENT_PROFIT"
    """Minimum payoff over all valid settlement states exceeds all-in cost."""

    PROBABILISTIC = "PROBABILISTIC"
    """Profitable in some states, not guaranteed in all. Not arbitrage."""

    UNKNOWN = "UNKNOWN"
    """States could not be enumerated. Never an arbitrage claim."""


class ExecutionStatus(StrEnum):
    """What is known about actually acquiring the portfolio."""

    DISPLAY_ONLY = "DISPLAY_ONLY"
    """Derived from top-of-book display prices; depth not verified."""

    DEPTH_VERIFIED = "DEPTH_VERIFIED"
    """Every leg's quantity is backed by real resting depth."""

    RACE_EXPOSED = "RACE_EXPOSED"
    """Depth-verified but multi-leg: legs can move before all fills complete."""

    STALE = "STALE"
    """At least one input book exceeded the staleness budget."""

    LOCKED = "LOCKED"
    """All legs filled. Unreachable in Phase 1, which places no orders."""


class OpportunityKind(StrEnum):
    """The structure a detector claims to have found."""

    BINARY_COMPLEMENT = "BINARY_COMPLEMENT"
    AT_MOST_ONE_NO_BASKET = "AT_MOST_ONE_NO_BASKET"
    AT_LEAST_ONE_YES_BASKET = "AT_LEAST_ONE_YES_BASKET"
    EXACTLY_ONE_YES_BASKET = "EXACTLY_ONE_YES_BASKET"
    EXACTLY_ONE_NO_BASKET = "EXACTLY_ONE_NO_BASKET"


class FeeType(StrEnum):
    """Kalshi ``fee_type`` values.

    Taken from the live OpenAPI enum rather than assumed. The formula behind
    each is carried as versioned metadata, never hardcoded -- see
    ``docs/api_assumptions.md`` (A-08) for the open question about the exact
    constants.
    """

    QUADRATIC = "quadratic"
    QUADRATIC_WITH_MAKER_FEES = "quadratic_with_maker_fees"
    QUADRATIC_WITH_COMBO_MAKER_FEES = "quadratic_with_combo_maker_fees"
    FLAT = "flat"


class Liquidity(StrEnum):
    """Whether a leg is modelled as crossing the spread or resting.

    Phase 1 evaluates :attr:`TAKER` only; a resting order is not an executable
    portfolio at detection time.
    """

    TAKER = "TAKER"
    MAKER = "MAKER"
