"""Read-only live validation of the Kalshi wire/normalisation layer.

Run with::

    uv run python tools/validate_live.py

What it proves
--------------
For a live sample of markets and order books, every financially relevant value
survives the round trip

    raw JSON string  ->  exact fixed-point type  ->  rendered string

byte for byte. If any price or size were passing through a float, or being
rounded, or losing a trailing zero, the rendered string would differ from the
original substring and this tool would report it.

It also re-runs normalisation twice on the same payload to confirm determinism,
and reports observed notional values and market types.

Safety
------
Public, unauthenticated GETs only. No credentials are read or sent, and no
account-changing operation exists in this file. Nothing is written anywhere
except stdout.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from decimal import Decimal
from typing import Any

import httpx

from predarb.clock import SystemClock
from predarb.domain.money import Money, Price, Quantity
from predarb.venues.kalshi.models import (
    KalshiMarketsPage,
    KalshiOrderbookEnvelope,
    decode_json,
    unknown_top_level_fields,
)
from predarb.venues.kalshi.normalize import book_source_from_rest as make_source
from predarb.venues.kalshi.normalize import normalize_market, normalize_orderbook

API_BASE = "https://external-api.kalshi.com/trade-api/v2"
PUBLIC_HEADERS = {"Accept": "application/json", "User-Agent": "predarb-live-validate/0.1"}

# Field-name suffix -> the exact type that field must round-trip through.
_PRICE_KEY = re.compile(r"_dollars$")
_COUNT_KEY = re.compile(r"_fp$")
_MONEY_KEYS = {"liquidity_dollars"}


def _walk_strings(node: Any, path: str = "") -> list[tuple[str, str, str]]:
    """Yield ``(path, key, value)`` for every string-valued financial field.

    Order-book levels need their own case. They arrive as
    ``"yes_dollars": [["0.4200", "13.00"], ...]`` -- the key names the array, not
    the values, so a plain key-walk skips the level strings entirely. Those are
    the most precision-sensitive values in the payload, so each pair is unpacked
    as (price, count) explicitly.
    """
    found: list[tuple[str, str, str]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            is_financial_key = bool(_PRICE_KEY.search(key) or _COUNT_KEY.search(key))
            if isinstance(value, str) and is_financial_key:
                found.append((f"{path}.{key}", key, value))
            elif is_financial_key and isinstance(value, list) and _looks_like_levels(value):
                for index, pair in enumerate(value):
                    price_raw, count_raw = pair[0], pair[1]
                    found.append((f"{path}.{key}[{index}][0]", "level_dollars", price_raw))
                    found.append((f"{path}.{key}[{index}][1]", "level_fp", count_raw))
            else:
                found += _walk_strings(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found += _walk_strings(value, f"{path}[{index}]")
    return found


def _looks_like_levels(value: list[Any]) -> bool:
    """Whether a list is an array of ``[price, count]`` string pairs."""
    return all(
        isinstance(item, list)
        and len(item) == 2
        and isinstance(item[0], str)
        and isinstance(item[1], str)
        for item in value
    )


def _check_roundtrip(key: str, raw: str) -> str | None:
    """Return a failure description, or ``None`` if the value survived intact.

    Two different standards, on purpose:

    * **Prices and counts** are byte-compared. Their wire precision (4 dp and
      2 dp) is exactly the scale of :class:`Price` and :class:`Quantity`, so a
      correct parse must render the identical string. A trailing-zero or tick
      difference here is a real defect.
    * **Money fields** are value-compared. ``liquidity_dollars`` arrives at 4 dp
      while :class:`Money` canonically renders 6 dp, so ``"0.0000"`` correctly
      becomes ``"0.000000"``. That is a representation difference, not a loss,
      and comparing bytes here would flag correct behaviour as a failure.
    """
    if key in _MONEY_KEYS:
        parsed = Money.from_value(raw)
        if parsed.as_decimal() != Decimal(raw):
            return f"value changed: raw={raw!r} parsed={parsed.to_str()!r}"
        return None
    rendered = (
        Quantity.from_value(raw).to_str()
        if _COUNT_KEY.search(key)
        else Price.from_value(raw).to_str()
    )
    if rendered != raw:
        return f"raw={raw!r} rendered={rendered!r}"
    return None


def main() -> None:
    clock = SystemClock()
    failures: list[str] = []
    checked = 0
    notionals: Counter[str] = Counter()
    market_types: Counter[str] = Counter()
    structures: Counter[str] = Counter()
    unknown_fields: Counter[str] = Counter()

    with httpx.Client(base_url=API_BASE, headers=PUBLIC_HEADERS, timeout=60) as client:
        print("Fetching live markets ...")
        payload = decode_json(
            client.get("/markets", params={"limit": 500, "status": "open"}).content
        )
        for name in unknown_top_level_fields(KalshiMarketsPage, payload):
            unknown_fields[name] += 1
        page = KalshiMarketsPage.model_validate(payload)
        print(f"  {len(page.markets)} markets")

        # 1. every financial string in the raw payload round-trips exactly
        for path, key, raw in _walk_strings(payload):
            checked += 1
            problem = _check_roundtrip(key, raw)
            if problem:
                failures.append(f"markets{path}: {problem}")

        # 2. normalisation is deterministic, and metadata is preserved
        for market in page.markets:
            notionals[
                market.notional_value_dollars.to_str()
                if market.notional_value_dollars
                else "ABSENT"
            ] += 1
            market_types[market.market_type] += 1
            structures[market.price_level_structure or "ABSENT"] += 1
            first = normalize_market(market)
            second = normalize_market(market)
            if first != second:
                failures.append(f"normalize_market not deterministic for {market.ticker}")
            if first.notional_value != market.notional_value_dollars:
                failures.append(f"notional altered by normalisation for {market.ticker}")

        # 3. order books: round-trip plus sort-order correctness
        # The default /markets ordering is dominated by combo markets that quote
        # nothing, so books are sampled from real single-market series instead.
        candidates: list[str] = []
        series_page = decode_json(client.get("/series", params={"limit": 60}).content)
        for entry in series_page.get("series", [])[:60]:
            if len(candidates) >= 15:
                break
            time.sleep(0.08)
            listing = decode_json(
                client.get(
                    "/markets",
                    params={"series_ticker": entry["ticker"], "limit": 20, "status": "open"},
                ).content
            )
            sub_page = KalshiMarketsPage.model_validate(listing)
            for market in sub_page.markets:
                has_size = (market.yes_bid_size_fp and not market.yes_bid_size_fp.is_zero) or (
                    market.no_bid_size_fp and not market.no_bid_size_fp.is_zero
                )
                if has_size:
                    candidates.append(market.ticker)
                    notionals[
                        market.notional_value_dollars.to_str()
                        if market.notional_value_dollars
                        else "ABSENT"
                    ] += 1
                    market_types[market.market_type] += 1
                    structures[market.price_level_structure or "ABSENT"] += 1
                    break
        print(f"Fetching {len(candidates)} live order books ...")
        books_checked = 0
        for ticker in candidates:
            time.sleep(0.1)
            response = client.get(f"/markets/{ticker}/orderbook")
            if response.status_code != 200:
                continue
            raw_payload = decode_json(response.content)
            for path, key, raw in _walk_strings(raw_payload):
                checked += 1
                problem = _check_roundtrip(key, raw)
                if problem:
                    failures.append(f"{ticker}{path}: {problem}")
            book = KalshiOrderbookEnvelope.model_validate(raw_payload).orderbook_fp
            source = make_source(instrument_ticker=ticker, received_at=clock.now())
            normalized = normalize_orderbook(book, source)
            books_checked += 1
            # best bid must be the highest price on each side
            for name, wire, levels in (
                ("yes", book.yes_dollars, normalized.yes_bids),
                ("no", book.no_dollars, normalized.no_bids),
            ):
                non_zero = [p for p, q in wire if not q.is_zero]
                if not non_zero:
                    continue
                expected_best = max(non_zero, key=lambda p: p.units)
                actual_best = levels[0].price if levels else None
                if actual_best != expected_best:
                    failures.append(
                        f"{ticker} {name}: best bid {actual_best} != highest wire price "
                        f"{expected_best}"
                    )
                total_wire = sum(q.units for _, q in wire)
                total_norm = sum(level.quantity.units for level in levels)
                if total_wire != total_norm:
                    failures.append(
                        f"{ticker} {name}: depth changed in normalisation "
                        f"({total_wire} -> {total_norm} units)"
                    )

    print("\n================ RESULTS ================")
    print(f"financial strings round-tripped : {checked}")
    print(f"order books normalised          : {books_checked}")
    print(f"notional_value_dollars observed : {dict(notionals)}")
    print(f"market_type observed            : {dict(market_types)}")
    print(f"price_level_structure observed  : {dict(structures)}")
    print(f"unknown top-level fields        : {dict(unknown_fields) or 'none'}")
    if failures:
        print(f"\nFAILURES ({len(failures)}):")
        for failure in failures[:40]:
            print(f"  {failure}")
        raise SystemExit(1)
    print("\nAll checks passed: no precision lost, ordering correct, normalisation deterministic.")


if __name__ == "__main__":
    main()
