# Kalshi Adapter

How Kalshi JSON becomes domain objects, and what the boundary guarantees.

```
Kalshi JSON bytes
      │  decode_json  (parse_float=Decimal)
      ▼
raw Python payload
      │  Pydantic v2 wire models  (venues/kalshi/models.py)
      ▼
validated wire objects          <- mirror the venue, field for field
      │  normalisation          (venues/kalshi/normalize.py)
      ▼
domain objects                  <- mean what the application means
```

The split exists so venue naming never reaches a detector. A wire model may be
called `KalshiMarket` and carry `yes_bid_dollars`; a detector sees a
`VenueInstrument` with a `yes_bid` of type `Price`.

## 1. Decoding: why not plain `json.loads`

Kalshi encodes prices and sizes as **strings** (`"0.4200"`, `"1596.82"`), which
is good — a string cannot lose precision in transit. But three financially
relevant fields are JSON **numbers**:

- `fee_multiplier` / `fee_multiplier_override`
- `floor_strike`, `cap_strike`

`json.loads` converts a JSON number to a `float` before any validator runs, so
the exact digits are gone before we can object. Every payload is therefore
decoded with `decode_json`, which passes `parse_float=Decimal`.

Observed multipliers are `0`, `0.5` and `1`, all binary-exact, so a float would
survive today. A future `0.1` would not, and the loss would be silent and small
— the worst kind in a fee calculation.

## 2. Fixed-point parsing

`venues/kalshi/fixed_point.py` is the only place wire strings become exact
types.

| Wire pattern | Example | Type | Scale |
| --- | --- | --- | --- |
| `*_dollars` (price) | `"0.4200"` | `Price` | $0.0001 |
| `*_dollars` (money) | `"0.003639"` | `Money` | $0.000001 |
| `*_fp` | `"1596.82"` | `Quantity` | 0.01 contracts |
| `delta_fp` | `"-54.00"` | `QuantityDelta` | 0.01, **signed** |

Three rules:

**Never through `float`.** Measured against the real grids, the naive path
corrupts **573 of the 10,001** representable prices and **9,174 of the first
200,000** quantities. `int(float("0.0003") * 10000)` gives `2`, not `3` — a full
tick on the `$0.0001` edge grid. `int(float("0.29") * 100)` gives `28`, not
`29`. A test walks the AST of `fixed_point.py` and `money.py` and fails if a
`float(...)` call appears in either.

**Excess precision raises, never rounds.** A five-decimal price or
three-decimal quantity is rejected. Kalshi documents no rounding at this
boundary, so rounding would be us inventing a value the venue never sent.

**Absent is not zero.** A missing quote stays `None`. A market with no bid is
not a market bidding $0.00, and conflating the two puts a free contract at the
top of the book.

`QuantityDelta` is separate from `Quantity` because `delta_fp` is signed and a
resting size cannot be. Applying a delta that would take a level negative raises
rather than clamping to zero: a negative result means a message was missed, and
clamping would hide exactly the gap the reconstructor must detect.

## 3. Forward-compatibility policy

Kalshi changes this API actively, so strictness is chosen per field rather than
globally.

| Category | Policy | Why |
| --- | --- | --- |
| Unknown extra fields | **Ignored**, and reported by `unknown_top_level_fields()` | A harmless new field must not break ingestion. Nothing is lost: the raw journal keeps the bytes, and the report surfaces additions in observability. |
| Known financial fields | **Strict.** Malformed or re-typed values raise at parse time | A plausible-looking wrong price is worse than a crash. |
| Enum-like fields | **Open.** Typed `str`, mapped later with an explicit unknown case | `fee_type` carries `margin_market_maker_program_fees` live, absent from the documented enum. A closed enum would crash on real data. |
| Optional fields | **Stay `None`** | See "absent is not zero" above. |

This is not theoretical on either side. `GET /events` returns an unmodelled
`milestones` key, which is ignored and reported (A-27); and the undocumented
`fee_type` would have been fatal under a closed enum (A-11).

Mapping an open string to a closed vocabulary happens in normalisation and fails
closed: `settlement_kind_for()` maps anything it does not recognise to
`SettlementKind.UNKNOWN`, which is ineligible for a contractual-arbitrage claim.
A new venue value can never be silently treated as a standard binary.

## 4. The order book

Kalshi publishes **bids only**, on two sides. There is no ask array, and the
wire model does not invent one — deriving an ask needs the contract's notional,
which belongs to the layer that holds instrument metadata.

**Array order is not trusted.** The API reference says levels run "best to
worst"; live responses are ascending by price, which for a bid book is
worst-to-best. Normalisation sorts explicitly, descending, so `yes_bids[0]` is
the best bid by construction, and `QuotedBook` *rejects* ascending input rather
than accepting it quietly. A property test shuffles the wire array and asserts
the normalised book is identical either way.

Two other normalisation decisions:

- **Zero-size levels are dropped.** They carry no executable depth, and keeping
  them would put a price point in the book that nothing can trade against.
- **A duplicate price raises.** Kalshi publishes aggregated levels, so a repeat
  means the payload is malformed; summing them would fabricate depth.

REST and WebSocket disagree on names for the same data — `orderbook_fp.yes_dollars`
versus `yes_dollars_fp` in `msg` (A-25) — so they have separate wire classes and
separate normalisers that produce the same `QuotedBook`.

## 5. Price grids

`price_ranges` is the source of truth for valid prices. `price_level_structure`
is retained as metadata; no business logic branches on its name.

Four structure *names* have been observed (A-04), three with captured band
layouts. Nothing branches on the name — a run that encountered the fourth,
previously unseen, name passed every check because the grid is read from
`price_ranges` and validated on its own terms.

Validity is the **union** over bands with
inclusive endpoints, which is correct rather than a guess: bands are contiguous
and each spans a whole number of its own steps, so every shared endpoint is on
both grids. `PriceGrid.has_consistent_boundaries()` guards the one case that
could still break this — bands that overlap over a *range* with different steps
— and such an instrument is excluded rather than resolved arbitrarily.

## 6. Fees

Normalisation builds a `FeeTimeline`, not a number. Three sources stay
separate — series base, scheduled changes, event override — and
`resolve_at(t)` answers *what configuration was in effect at `t`*. A change
scheduled for tomorrow is invisible to a book from today, in live scanning and
replay alike.

Computing a fee from a configuration is the later fee engine's job. The split
matters because the failure modes differ: a wrong formula is an arithmetic bug,
while a wrong effective configuration is a point-in-time bug that silently
applies today's fees to last week's book.

Two collection gotchas, both verified live (A-12):

- `/series/fee_changes` returns an **empty array** unless `show_historical=true`
  is passed. A collector that omits it gets a 200 with no data and would
  conclude no fee change has ever happened.
- The event path is `/events/fee_changes` (plural).

## 7. What normalisation deliberately does not do

- **No relations.** `mutually_exclusive` is copied through as venue metadata.
  Turning it into an `AT_MOST_ONE` relation is a reviewed act in the semantics
  layer, and it can never produce `EXACTLY_ONE`.
- **No invented data.** An absent notional or price grid becomes `None` plus an
  exclusion reason.
- **No settlement claim.** `settlement_kind` records what `market_type` says. A
  proven `SettlementSpec` still requires reading contract rules.
- **No derived asks.** That needs the notional and belongs to the book layer.
- **No `collateral_return_type` interpretation.** `MECNET` is carried as an
  opaque string; its capital-netting semantics are unresolved (A-16).

## 8. Exclusions

`exclusion_reasons_for()` accumulates *every* reason, not just the first, so an
audit can explain fully why an instrument was skipped.

| Reason | Trigger |
| --- | --- |
| `MULTIVARIATE_COMBINATION_MARKET` | `mve_collection_ticker` or `mve_selected_legs` present |
| `PROVISIONAL_MARKET` | `is_provisional` |
| `NON_BINARY_SETTLEMENT` | `settlement_kind` is not `BINARY` |
| `NOTIONAL_VALUE_ABSENT` | no `notional_value_dollars` |
| `PRICE_GRID_ABSENT` | no `price_ranges` |
| `PRICE_GRID_INCONSISTENT` | bands overlap and disagree, or are non-contiguous |

## 9. Fixtures

`tests/fixtures/kalshi/` holds real captured payloads plus a `manifest.json`
recording endpoint, capture time, tickers, HTTP status, byte length and SHA-256
for each. Bodies are stored as **raw bytes, never re-serialised**, so raw-byte
regression tests are meaningful.

WebSocket fixtures are **synthetic and labelled as such** in the manifest, the
filenames and a README, because the production socket rejects unauthenticated
connections (A-24) and no key is configured. They are derived from the official
documentation's examples; the `subscribed` acknowledgement is flagged as the
lowest-confidence shape.

Regenerate with `uv run python tools/capture_fixtures.py`. Validate the layer
against production with `uv run python tools/validate_live.py`, which proves
every financial string round-trips byte-for-byte.
