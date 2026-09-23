"""Read membership facts from a raw market payload, without financial validation.

The question this answers is narrow:

    Does ticker X belong to event E?

Nothing about bids, asks, sizes, volume or last price bears on it. So none of
those fields is parsed here, and a malformed one cannot remove a market from an
event it plainly belongs to.

Why this is a separate path, not a looser validator
----------------------------------------------------
``KalshiMarket`` enforces financial invariants deliberately: a negative contract
count is not a quantity, and code that prices books must never see one. Those
invariants are right, and relaxing them to make enumeration work would weaken
the executable path to serve the semantic one.

A-49 is what forced the split. ``/historical/markets`` returns negative
``yes_bid_size_fp`` on finalized markets, and before this existed, one bad page
failed an entire walk -- so two of forty sampled events reported *zero members*,
which is indistinguishable from an event that genuinely has none. A financial
anomaly silently became a semantic conclusion.

So: strict financial normalisation may reject a record while membership
normalisation still accepts its semantic projection. Both readings of the same
payload are correct, because they are answering different questions.

What is required, and what is not
----------------------------------
**Required**: a ticker, and an ``event_ticker`` matching the event asked about.
Without those there is no membership fact to record, and nothing is guessed --
the evidence becomes incomplete instead.

**Recorded when readable**: status, lifecycle timestamps, result, settlement
value, ``is_provisional``, ``market_type``, and the documented structured strike
fields the partition helper reads.

**Never required**: anything financial. An unreadable one is attached as a
``DATA_QUALITY_ANOMALY`` and excluded from semantic reasoning.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from predarb.clock import ensure_utc
from predarb.semantics.membership import MembershipMember, MembershipSource

__all__ = [
    "FINANCIAL_FIELDS",
    "MembershipIdentityError",
    "payload_digest",
    "project_membership_member",
]

FINANCIAL_FIELDS: tuple[str, ...] = (
    "yes_bid_dollars",
    "yes_ask_dollars",
    "no_bid_dollars",
    "no_ask_dollars",
    "yes_bid_size_fp",
    "yes_ask_size_fp",
    "no_bid_size_fp",
    "no_ask_size_fp",
    "last_price_dollars",
    "previous_price_dollars",
    "previous_yes_bid_dollars",
    "previous_yes_ask_dollars",
    "volume_fp",
    "volume_24h_fp",
    "open_interest_fp",
    "liquidity_dollars",
)
"""Fields membership never consults. Checked only so an unusable one is
*reported* rather than passing unnoticed -- the market stays either way."""


class MembershipIdentityError(ValueError):
    """A field membership genuinely depends on is missing or contradictory."""


def payload_digest(payload: Mapping[str, Any]) -> str:
    """A stable digest of the raw record, so the original stays recoverable."""
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _text(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _timestamp(payload: Mapping[str, Any], key: str) -> tuple[datetime | None, str | None]:
    """Parse a timestamp, reporting rather than raising on a bad one.

    A malformed ``settlement_ts`` is worth knowing about and is not grounds for
    denying that a market belongs to its event.
    """
    raw = payload.get(key)
    if raw in (None, ""):
        return None, None
    try:
        if isinstance(raw, datetime):
            return ensure_utc(raw), None
        return ensure_utc(datetime.fromisoformat(str(raw).replace("Z", "+00:00"))), None
    except (ValueError, TypeError):
        return None, f"{key}={raw!r} is not a readable timestamp"


def _decimal_text(payload: Mapping[str, Any], key: str) -> tuple[str | None, str | None]:
    """Strike bounds, kept as exact text. A float here is a decoding bug."""
    raw = payload.get(key)
    if raw is None:
        return None, None
    if isinstance(raw, float):
        return None, f"{key} arrived as a float; the payload was decoded inexactly"
    try:
        return str(Decimal(str(raw))), None
    except (InvalidOperation, ValueError):
        return None, f"{key}={raw!r} is not a readable number"


def _financial_anomalies(payload: Mapping[str, Any]) -> dict[str, str]:
    """Unusable financial fields, recorded and then set aside.

    Negative sizes are the observed case (A-49). A negative *price* would be
    equally unusable, and both are reported here rather than either crashing the
    walk or being silently normalised into a plausible number.
    """
    anomalies: dict[str, str] = {}
    for name in FINANCIAL_FIELDS:
        raw = payload.get(name)
        if raw is None:
            continue
        if isinstance(raw, float):
            anomalies[name] = f"{raw!r} (decoded as float; exactness already lost)"
            continue
        text = str(raw).strip()
        if text.startswith("-"):
            anomalies[name] = text
            continue
        try:
            Decimal(text)
        except (InvalidOperation, ValueError):
            anomalies[name] = f"{text!r} (unreadable)"
    return anomalies


def project_membership_member(
    payload: Mapping[str, Any],
    *,
    expected_event_ticker: str,
    source: MembershipSource,
) -> MembershipMember:
    """Project one raw market payload into a membership fact.

    Raises :class:`MembershipIdentityError` only when identity itself is
    unusable. Every other defect is attached to the record and the market is
    kept, because a market with a bad price is still a market in the event.
    """
    ticker = _text(payload, "ticker")
    if not ticker:
        raise MembershipIdentityError(
            "market payload has no ticker; there is no membership fact to record "
            f"(digest {payload_digest(payload)[:12]})"
        )

    event_ticker = _text(payload, "event_ticker")
    if not event_ticker:
        raise MembershipIdentityError(
            f"{ticker}: no event_ticker, so its membership cannot be established"
        )
    if event_ticker != expected_event_ticker:
        raise MembershipIdentityError(
            f"{ticker}: returned under event_ticker {event_ticker!r} when "
            f"{expected_event_ticker!r} was requested; the venue answered a "
            "different question and nothing is inferred from that"
        )

    anomalies = _financial_anomalies(payload)

    settled_ts, settled_note = _timestamp(payload, "settlement_ts")
    created_time, created_note = _timestamp(payload, "created_time")
    close_time, close_note = _timestamp(payload, "close_time")
    floor_strike, floor_note = _decimal_text(payload, "floor_strike")
    cap_strike, cap_note = _decimal_text(payload, "cap_strike")
    for note in (settled_note, created_note, close_note, floor_note, cap_note):
        if note is not None:
            key = note.split("=")[0].split(" ")[0]
            anomalies[key] = note

    settlement_value = payload.get("settlement_value_dollars")
    custom_strike = payload.get("custom_strike")

    return MembershipMember(
        ticker=ticker,
        source=source,
        event_ticker=event_ticker,
        status=_text(payload, "status"),
        market_type=_text(payload, "market_type"),
        result=_text(payload, "result"),
        settlement_value=None if settlement_value is None else str(settlement_value),
        settled_ts=settled_ts,
        created_time=created_time,
        close_time=close_time,
        is_provisional=payload.get("is_provisional"),
        strike_type=_text(payload, "strike_type"),
        floor_strike=floor_strike,
        cap_strike=cap_strike,
        functional_strike=_text(payload, "functional_strike"),
        custom_strike=dict(custom_strike) if isinstance(custom_strike, dict) else None,
        primary_participant_key=_text(payload, "primary_participant_key"),
        title=_text(payload, "title"),
        yes_sub_title=_text(payload, "yes_sub_title"),
        data_quality_anomalies=anomalies,
    )
