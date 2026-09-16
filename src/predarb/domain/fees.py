"""Fee *configuration* as a point-in-time resolvable structure.

This module deliberately does **not** compute fees. It answers one question:

    what fee configuration was in effect for this instrument at time T?

Computing a fee from that configuration is the later fee engine's job. The
split matters because the two have different failure modes: a wrong formula is
an arithmetic bug, while a wrong *effective configuration* is a point-in-time
bug that silently applies today's fees to last week's book, which is exactly
the lookahead a replay must not have.

Three sources, kept distinct
----------------------------
Flattening these into one value would lose the ability to answer "why this
multiplier?", which the audit command has to be able to do.

``series base``
    ``fee_type`` / ``fee_multiplier`` on the series object.
``scheduled changes``
    ``GET /series/fee_changes`` and ``GET /events/fee_changes`` rows, each with
    a ``scheduled_ts``. Note these endpoints return an empty array unless
    ``show_historical=true`` is passed (``docs/api_assumptions.md`` A-12).
``event override``
    ``fee_type_override`` / ``fee_multiplier_override`` on the event, which
    takes precedence over the series configuration.

Precedence, most specific first: event scheduled change, event override, series
scheduled change, series base.

On fee types
------------
``fee_type`` is **not** modelled as a closed enum. The documented enum has four
values, but ``margin_market_maker_program_fees`` was observed live on 24 series.
An unrecognised fee type is carried verbatim and marked unresolvable rather than
crashing ingestion or being silently coerced to ``quadratic``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from predarb.clock import ensure_utc
from predarb.domain.enums import FeeType

__all__ = [
    "FeeConfiguration",
    "FeeScope",
    "FeeTimeline",
    "ResolvedFeeConfiguration",
    "ScheduledFeeChange",
]


def _require_decimal(value: object, *, field: str) -> Decimal:
    """Runtime guard for an untyped caller.

    The field is annotated ``Decimal``, but nothing stops a dict-driven caller
    handing over a ``float`` decoded from JSON. A float multiplier is exactly
    the silent-precision-loss path this codebase refuses, so it raises. The
    parameter is ``object`` so the check is a real boundary rather than code the
    type checker can prove unreachable.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        raise TypeError(
            f"{field}: fee multiplier must not be a float ({value!r}); "
            "decode JSON with parse_float=Decimal to keep the exact value"
        )
    if isinstance(value, int) and not isinstance(value, bool):
        return Decimal(value)
    raise TypeError(f"{field}: fee multiplier must be a Decimal, got {type(value).__name__}")


class FeeScope(StrEnum):
    """Which level of the venue hierarchy a fee configuration came from."""

    SERIES = "SERIES"
    EVENT = "EVENT"


@dataclass(frozen=True, slots=True)
class FeeConfiguration:
    """A fee type and multiplier, with the scope it came from."""

    fee_type_raw: str
    multiplier: Decimal
    scope: FeeScope
    scope_ticker: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "multiplier", _require_decimal(self.multiplier, field="FeeConfiguration")
        )

    @property
    def fee_type(self) -> FeeType | None:
        """The documented enum member, or ``None`` if the venue sent a new value."""
        try:
            return FeeType(self.fee_type_raw)
        except ValueError:
            return None

    @property
    def is_recognised(self) -> bool:
        return self.fee_type is not None


@dataclass(frozen=True, slots=True)
class ScheduledFeeChange:
    """A fee change with a known effective timestamp."""

    change_id: str
    scope: FeeScope
    scope_ticker: str
    fee_type_raw: str
    multiplier: Decimal
    scheduled_ts: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "scheduled_ts", ensure_utc(self.scheduled_ts))
        object.__setattr__(
            self, "multiplier", _require_decimal(self.multiplier, field="ScheduledFeeChange")
        )

    def as_configuration(self) -> FeeConfiguration:
        return FeeConfiguration(
            fee_type_raw=self.fee_type_raw,
            multiplier=self.multiplier,
            scope=self.scope,
            scope_ticker=self.scope_ticker,
        )


@dataclass(frozen=True, slots=True)
class ResolvedFeeConfiguration:
    """The configuration in effect at an instant, plus why.

    ``provenance`` is human-readable and ends up in the audit output, so a
    reviewer can see whether a multiplier came from the series default or from
    a change that took effect three days before the book snapshot.
    """

    configuration: FeeConfiguration
    effective_from: datetime | None
    provenance: str

    @property
    def is_resolvable(self) -> bool:
        """Whether the later fee engine can actually compute from this.

        False when the venue sent a fee type we do not recognise. Fee
        computation must fail closed in that case rather than guess a formula.
        """
        return self.configuration.is_recognised


@dataclass(frozen=True, slots=True)
class FeeTimeline:
    """Everything known about one instrument's fees, resolvable at any instant.

    Construct once from metadata, then query with :meth:`resolve_at`. The inputs
    are kept as supplied so that a replay at an earlier timestamp sees the same
    structure and simply resolves to an earlier answer.
    """

    series_ticker: str
    event_ticker: str | None = None
    series_base: FeeConfiguration | None = None
    event_override: FeeConfiguration | None = None
    series_changes: tuple[ScheduledFeeChange, ...] = ()
    event_changes: tuple[ScheduledFeeChange, ...] = ()

    def resolve_at(self, instant: datetime) -> ResolvedFeeConfiguration | None:
        """Return the configuration in effect at ``instant``, or ``None``.

        ``None`` means no configuration is known for that time -- for example a
        replay timestamp before any metadata we hold. The caller must fail
        closed; there is no default fee configuration.

        A scheduled change applies only once ``scheduled_ts <= instant``. This is
        the point-in-time guarantee: a change scheduled for tomorrow is
        invisible to a book from today, in live scanning and in replay alike.
        """
        moment = ensure_utc(instant)

        latest = self._latest_change(self.event_changes, moment)
        if latest is not None:
            return ResolvedFeeConfiguration(
                configuration=latest.as_configuration(),
                effective_from=latest.scheduled_ts,
                provenance=f"event scheduled change {latest.change_id} on {self.event_ticker}",
            )
        if self.event_override is not None:
            return ResolvedFeeConfiguration(
                configuration=self.event_override,
                effective_from=None,
                provenance=f"event override on {self.event_ticker}",
            )
        latest = self._latest_change(self.series_changes, moment)
        if latest is not None:
            return ResolvedFeeConfiguration(
                configuration=latest.as_configuration(),
                effective_from=latest.scheduled_ts,
                provenance=f"series scheduled change {latest.change_id} on {self.series_ticker}",
            )
        if self.series_base is not None:
            return ResolvedFeeConfiguration(
                configuration=self.series_base,
                effective_from=None,
                provenance=f"series base configuration on {self.series_ticker}",
            )
        return None

    @staticmethod
    def _latest_change(
        changes: tuple[ScheduledFeeChange, ...], moment: datetime
    ) -> ScheduledFeeChange | None:
        """Most recent change already in effect at ``moment``.

        Ties on ``scheduled_ts`` are broken by ``change_id`` so resolution is
        deterministic -- replaying the same data twice must give the same answer.
        """
        applicable = [c for c in changes if c.scheduled_ts <= moment]
        if not applicable:
            return None
        return max(applicable, key=lambda c: (c.scheduled_ts, c.change_id))
