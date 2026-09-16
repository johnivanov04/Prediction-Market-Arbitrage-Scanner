# Kalshi API Assumptions and Verified Findings

**Verification date:** 2026-09-15
**Environment checked:** production, `https://external-api.kalshi.com/trade-api/v2`
**Method:** current official Kalshi documentation (`docs.kalshi.com`), cross-checked
against live unauthenticated responses from the production API.

Every entry has a status:

| Status | Meaning |
| --- | --- |
| **VERIFIED** | Confirmed in current official docs *and*, where possible, observed live |
| **DOCS-ONLY** | Stated in current official docs; not yet observed live |
| **OBSERVED-ONLY** | Seen in live responses; not found in the docs |
| **DISCREPANCY** | Docs and observed behaviour disagree |
| **UNRESOLVED** | Could not be verified; the system must fail closed |

> **Rule.** No entry below may be promoted from UNRESOLVED to a trading-relevant
> assumption without a documentation citation or a reproducible live
> observation recorded here. Detectors read these as data, never as constants
> baked into code.

---

## Summary of findings that contradict common assumptions

These matter enough to state up front. Each one, if assumed wrongly, produces
fake arbitrage.

1. **Kalshi prices are no longer whole cents.** They are dollar strings with
   four decimals, and tick size varies by price level (down to `$0.0001` near
   the book edges). A cent-based integer model is lossy. (A-04)
2. **Contract quantities are fractional.** Sizes are fixed-point strings with
   two decimals — `"1596.82"` contracts is a real, observed book level. An
   integer-contract model is wrong. (A-05)
3. **Contract payout is metadata, not a constant.** `notional_value_dollars`
   carries it. Hardcoding $1.00 defeats the settlement-safety rule. (A-18)
4. **The order book contains bids only.** Every ask is derived. (A-06)
5. **Documented level ordering does not match observed ordering.** (A-07)
6. **`mutually_exclusive` means AT_MOST_ONE and nothing more.** The official
   field description says so explicitly. (A-11)

---

## Connectivity and authentication

### A-01 REST base URLs — VERIFIED

| Environment | Base URL |
| --- | --- |
| Production | `https://external-api.kalshi.com/trade-api/v2` |
| Demo | `https://external-api.demo.kalshi.co/trade-api/v2` |

Note the different TLDs (`.com` for production, `.co` for demo). Credentials
are **not** shared between environments.

Older hosts (`trading-api.kalshi.com`, `api.elections.kalshi.com`) appear
throughout third-party tutorials and in model training data. We use the hosts
above, which are what the current docs specify.

Source: `docs.kalshi.com/getting_started/quick_start_market_data`,
`docs.kalshi.com/getting_started/demo_env`.

### A-02 WebSocket URLs — VERIFIED

| Environment | URL |
| --- | --- |
| Production | `wss://external-api-ws.kalshi.com/trade-api/ws/v2` |
| Demo | `wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2` |

**Resolved 2026-09-15** by independent review of the current official docs. The
earlier uncertainty came from the WebSocket reference rendering the production
host without its path; the documented production URL carries `/trade-api/ws/v2`
exactly as demo does.

`KalshiEndpoints` already used these values, so no code change was required.
The value stays overridable via `PREDARB_KALSHI_WS_URL`, and the connector logs
the resolved URL on connect.

### A-03 Request signing — VERIFIED (docs), UNTESTED (no credentials yet)

Three headers on every private request:

- `KALSHI-ACCESS-KEY` — the API key ID
- `KALSHI-ACCESS-TIMESTAMP` — current time in **milliseconds**
- `KALSHI-ACCESS-SIGNATURE` — base64 signature

The signed message is the concatenation of `timestamp_ms`, the **uppercase**
HTTP method, and the request path **with the query string stripped**. The doc's
example: `/trade-api/v2/portfolio/orders?limit=5` is signed as
`/trade-api/v2/portfolio/orders`.

Signature: RSA-PSS, SHA-256 digest, MGF1-SHA256, salt length equal to the
digest length, base64-encoded.

**WebSocket handshake — VERIFIED.** The handshake uses the same three headers
and the same RSA-PSS scheme. The signed message is:

```
timestamp_ms + "GET" + "/trade-api/ws/v2"
```

That is, the method is literally `GET` and the path is the fixed WebSocket path
regardless of environment — it is not derived from the connection URL's host.
Resolved 2026-09-15; previously listed as an open question.

Source: `docs.kalshi.com/getting_started/api_keys`.

### A-04 Prices: dollar strings, tiered tick sizes — VERIFIED (live)

Prices are strings in **dollars with four decimal places**: `"0.4200"`.

Tick size is **not uniform**. Markets carry `price_level_structure` and a
`price_ranges` array. Observed live on a market with
`price_level_structure: "center_deci_edge_centi_cent"`:

```json
"price_ranges": [
  {"start": "0.0000", "end": "0.0100", "step": "0.0001"},
  {"start": "0.0100", "end": "0.9900", "step": "0.0010"},
  {"start": "0.9900", "end": "1.0000", "step": "0.0001"}
]
```

So the tick is a tenth of a cent through the middle of the range and a
hundredth of a cent at the edges.

**Three structures observed live** (Step 2 survey, ~19,000 markets):

| `price_level_structure` | Bands |
| --- | --- |
| `linear_cent` | `0.0000–1.0000` step `0.0100` (uniform cent) |
| `tapered_deci_cent` | `0.0000–0.1000` @ `0.0010`, `0.1000–0.9000` @ `0.0100`, `0.9000–1.0000` @ `0.0010` |
| `center_deci_edge_centi_cent` | `0.0000–0.0100` @ `0.0001`, `0.0100–0.9900` @ `0.0010`, `0.9900–1.0000` @ `0.0001` |

**Boundary semantics — RESOLVED (verified computationally).** Adjacent bands
share an endpoint, so which band owns it was an open question. Checked across
all three structures:

* every band is contiguous with the next (`band[i].end == band[i+1].start`);
* every band's span is a whole number of its own steps, so its endpoint is on
  its own grid;
* therefore at every interior boundary **both** adjacent bands accept the price.

Because the bands agree, ownership does not affect validity: a price is valid if
**any** band accepts it. This is structural rather than coincidental — given
contiguity and whole-step spans it cannot be otherwise — so the remaining risk
is a future structure whose bands *overlap* over a range rather than meeting at
a point. `PriceGrid.has_consistent_boundaries()` detects exactly that case, and
an instrument failing it is excluded rather than guessed at.

**Consequences.** A cent-based integer representation cannot hold these prices.
Our `Price` type uses units of `$0.0001`. Tick validity is a per-market property
read from `price_ranges`; `price_level_structure` is retained as metadata only
and no business logic branches on its name.

**Distribution note.** `center_deci_edge_centi_cent` — the finest grid — was
observed **only** on multivariate combo markets (7,787 of 7,787 in one sample),
and almost none of those quote anything. Sub-cent pricing is therefore largely
confined to instruments Phase 1 excludes.

**A fourth structure name exists.** A live validation run on 2026-09-15 observed
`deci_cent` on 2 of 515 markets. A subsequent sweep of all 14,092 series did not
find it again, so its band layout is **not captured** and the name is recorded
here as an observation rather than a documented structure.

This is worth more than a footnote: the run that encountered it **passed all
checks**. Nothing branches on `price_level_structure`, so an unseen structure
name costs nothing — the grid is read from `price_ranges` and validated on its
own terms. Had the code carried a `structure_name -> bands` table, that run
would have failed on a name nobody had heard of. Treat the list of structure
names as open.

### A-05 Quantities: fixed-point, fractional — VERIFIED (live)

Contract counts are strings with **two decimal places**. Field names carry an
`_fp` suffix (`volume_fp`, `open_interest_fp`, `yes_bid_size_fp`, `delta_fp`).

Observed live book levels include `"1596.82"` and `"14.29"` contracts.
Fractional positions are real and must not be truncated to integers.

Our `Quantity` type uses units of `0.01` contracts.

---

## Order book

### A-06 Bids only; two sides; asks are derived — VERIFIED (live)

`GET /markets/{ticker}/orderbook` returns:

```json
{"orderbook_fp": {"yes_dollars": [[price, count], ...],
                  "no_dollars":  [[price, count], ...]}}
```

The docs state plainly: *"Kalshi's orderbook only returns bids, not asks."*
There is no independent YES ask book. The best YES ask is **derived** from the
best NO bid:

```
best_yes_ask = notional - best_no_bid
```

The docs' worked example: highest NO bid `$0.5600` implies a best YES ask of
`$0.44`, against a best YES bid of `$0.4200`, for a two-cent spread.

**Our handling.** Derived levels are tagged `derived=True` and keep a reference
to the quoted level they came from. The derivation uses the instrument's
`notional_value_dollars`, never a hardcoded `$1.00` — which is why
`Price.complement()` requires the notional as an explicit argument.

### A-07 Level ordering — DISCREPANCY

The API reference describes levels as *"organized from best to worst prices"*.
Observed live, `no_dollars` is ascending by price:

```
["0.0100","1596.82"], ["0.0200","33.00"], ... ["0.1700","176.00"], ...
```

For a **bid** book, best means *highest*, so the observed array is ordered
worst-to-best — the opposite of the prose. The separate orderbook-responses
guide agrees with the observation, instructing readers to take *"the last
element in the `no_dollars` array"* as the highest NO bid.

**Our handling.** The parser sorts explicitly by price and never relies on
array order. Cheap, and it removes a whole class of silent inversion bug —
which on this book would turn the worst price into the "best" and manufacture
an edge.

### A-08 `orderbook_delta` websocket channel — VERIFIED (docs)

Two message types on the `orderbook_delta` channel:

| Type | Payload |
| --- | --- |
| `orderbook_snapshot` | `market_ticker`, `market_id`, `yes_dollars_fp`, `no_dollars_fp` (arrays of `[price, count]`) |
| `orderbook_delta` | `market_ticker`, `market_id`, `price_dollars`, `delta_fp`, `side` (`"yes"`/`"no"`), optional `ts_ms`, optional `client_order_id` |

Envelope fields: `type`, `sid` (subscription id), `seq` (sequence number),
`msg`.

`delta_fp` is a **signed relative change** to the resting quantity at that
price level, not a replacement value. Documented example: `"-54.00"`.

A snapshot arrives first on subscription; deltas follow.

### A-09 Sequence gaps — UNRESOLVED (docs are silent on recovery)

The docs say `seq` should be *"checked if you want to guarantee you received
all the messages"* and is *"used for snapshot/delta consistency"*. They do not
document:

- whether `seq` is scoped per subscription (`sid`) or per market
- whether `seq` resets on resubscribe
- what recovery procedure the exchange expects after a gap

This is the highest-risk unknown in Phase 1: a mishandled gap produces a book
that looks tradeable and is not.

**Our handling — fail closed.** On any gap, duplicate-with-conflict, or
out-of-order arrival, the affected book is marked `INTEGRITY_UNKNOWN` and
becomes immediately ineligible for scanning. It only becomes eligible again
after a fresh snapshot. We track `seq` both per `sid` and per
`(sid, market_ticker)` during capture and record which one is actually
contiguous, which resolves the scoping question empirically from our own
captured data. Until it is resolved, the stricter interpretation governs.

### A-10 Subscription limits — UNRESOLVED

The WS reference documents error code 26, *"Subscription market limit exceeded
— Adding markets would exceed the per-subscription market limit"*, but states
no number. The collector must handle 26 by splitting subscriptions, and should
record the limit it discovers.

Subscribe/unsubscribe command shapes are documented:

```json
{"id": 1, "cmd": "subscribe",
 "params": {"channels": ["orderbook_delta"], "market_tickers": ["..."]}}
```

Client ids start at 1 and increment; `id: 0` is treated as absent. The server
returns a `sid` per subscription.

---

## Fees

### A-11 Fee model is per-series metadata — VERIFIED (live)

There is **no universal Kalshi fee percentage**. The series object carries:

- `fee_type` — enum: `quadratic`, `quadratic_with_maker_fees`,
  `quadratic_with_combo_maker_fees`, `flat`
- `fee_multiplier` — a numeric multiplier applied to the fee calculation

Observed live: series `KXHIGHNY` reports `fee_type: "quadratic"`,
`fee_multiplier: 1`.

**The documented `fee_type` enum is incomplete — VERIFIED (live).** Across 147
historical series fee changes:

| `fee_type` | Count |
| --- | --- |
| `quadratic_with_maker_fees` | 64 |
| `quadratic` | 55 |
| `margin_market_maker_program_fees` | 24 |
| `quadratic_with_combo_maker_fees` | 4 |

`margin_market_maker_program_fees` is **absent from the documented four-value
enum** (it appears on margin/perps series such as `KXGOLDPERP`). A closed enum
would have crashed ingestion on real data, so `fee_type` is modelled as a plain
string and mapped to our vocabulary with an explicit unrecognised case that
fails closed.

**`fee_multiplier` is not always 1 — VERIFIED (live).** Observed values: `0`,
`0.5`, `1`. A multiplier of `0` means fees are genuinely waived for that series.
Assuming `M = 1` would overstate costs on some series and understate nothing —
but it would still be wrong, and on a `0` series it would hide a real edge.

**`fee_multiplier` is a JSON *number*, not a string.** This is the one
financially relevant field Kalshi does not encode as a decimal string, so
`json.loads` converts it to a `float` before any validation can object. Payloads
are therefore decoded with `parse_float=Decimal`. `0.5` happens to be
binary-exact so a float round-trip would survive today; a future `0.1` would
not, and the loss would be silent. The same decoding protects `floor_strike` and
`cap_strike`, which are also JSON numbers.

Events can override both, via `fee_type_override` and
`fee_multiplier_override`, which "take precedence over series-level fees".

Markets additionally carry `fee_waiver_expiration_time`.

### A-12 Scheduled fee changes are a first-class API concept — VERIFIED (docs)

`GET /series/fee_changes` (query: `series_ticker`, `show_historical`) returns
`SeriesFeeChange` records:

| Field | Meaning |
| --- | --- |
| `id` | Fee change identifier |
| `series_ticker` | Series it applies to |
| `fee_type` | New fee type |
| `fee_multiplier` | New multiplier |
| `scheduled_ts` | When it takes effect |

There is an event-level equivalent. **This is the authoritative source for
point-in-time fee correctness**, and is exactly what replay needs to avoid
applying today's fees to last week's book.

**Two path gotchas, both verified live:**

* `/series/{ticker}/fee_changes` returns **404**. The real path is
  `/series/fee_changes` with the ticker as a *query parameter*.
* The event-level endpoint is `/events/fee_changes` (**plural**).
  `/event/fee_changes` returns 404.

**`show_historical=true` is required — VERIFIED (live).** Without it the
endpoint returns an empty array:

```
GET /series/fee_changes                      -> {"series_fee_change_arr":[]}
GET /series/fee_changes?show_historical=true -> 147 records
```

This is a trap for point-in-time correctness: a collector that omits the flag
gets a successful 200 with no data and would silently conclude that no fee
change has ever happened.

`/events/fee_changes` returns `event_fee_changes` with `fee_type_override`,
`fee_multiplier_override`, `event_ticker`, `series_ticker`, `id`,
`scheduled_ts`, plus a `cursor` for pagination.

### A-13 Fee rounding — VERIFIED (docs)

Net fee = trade fee + rounding fee − rebate, floored at zero. The documented
algorithm:

1. `trade_fee = ceil_6dp(model_fee)` — round **up** to the nearest `$0.000001`
2. `aligned_change = floor_precision(revenue - trade_fee)`
3. `rounding_fee = (revenue - trade_fee) - aligned_change`
4. add the rounding fee to the order's accumulator
5. rebate accumulated rounding in increments of the user's balance precision

Balance precision targets: `$0.0001` for direct members, `$0.01` for
non-direct members. The accumulator persists across fills within one order.

Documented worked example, now a golden test in
`tests/unit/test_money.py::TestKalshiDocumentedRoundingExample`:

```
trade fee      = ceil_6dp($0.00363825) = $0.003639
aligned change = floor_cent(-$0.055 - $0.003639) = -$0.060000
rounding fee   = (-$0.055 - $0.003639) - (-$0.06) = $0.001361
```

This is why `Money` is denominated in micro-dollars: `ceil_6dp` is exactly
"round up to one `Money` unit".

### A-14 The fee formulas — VERIFIED (fee schedule effective 2026-07-07)

**Resolved 2026-09-15.** The general event-contract model-fee formulas from the
current official Kalshi Fee Schedule are:

```
taker_model_fee = M * 0.07   * C * P * (1 - P)
maker_model_fee = M * 0.0175 * C * P * (1 - P)
```

where `P` is the contract price in dollars, `C` the number of contracts, and
`M` the applicable series/contract multiplier (`fee_multiplier`, or the event's
`fee_multiplier_override` where present — A-11).

The quadratic `P * (1 - P)` term peaks at `P = 0.50`, so fees are most
expensive on coin-flip contracts and fall toward both price extremes. Phase 1
evaluates taker execution, so the `0.07` form is the one that gates
profitability.

#### The model fee is not the net fee

This is the correction that matters most, and the two are easy to conflate. The
fee schedule gives the **model fee**; the API's fee-rounding documentation (A-13)
then defines a separate account-level process on top of it. Five distinct
quantities, modelled separately and never collapsed:

| Concept | Definition |
| --- | --- |
| `raw_model_fee` | `M * rate * C * P * (1 - P)`. A real-valued intermediate, not money |
| `trade_fee` | `ceil_6dp(raw_model_fee)` — the fee actually charged on the fill |
| `rounding_fee` | `(revenue - trade_fee) - floor_precision(revenue - trade_fee)`; realigns the balance to the member's precision grid |
| `rebate` | Drawn from the per-order accumulator, returned in increments of the member's balance precision |
| `net_fee` | `trade_fee + rounding_fee - rebate`, floored at zero |

Balance-alignment precision: `$0.0001` for direct members, `$0.01` for
non-direct members.

`raw_model_fee` is deliberately typed as `Decimal`, not `Money`: it routinely
carries more than six decimal places (the documented worked example uses
`$0.00363825`), and it becomes money exactly once, at the `ceil_6dp` step.
`Money.ceil_from_decimal()` is the only sanctioned conversion.

#### Versioning is still required

Knowing the general formulas does **not** make fees a constant. Versioned,
data-driven fee schedules remain mandatory because:

- individual series carry different multipliers and `fee_type` configurations
- event-level overrides exist (`fee_type_override`, `fee_multiplier_override`)
- fee changes are scheduled with an effective timestamp (A-12)
- the general fee schedule itself changes — the current one is dated
  2026-07-07 and supersedes earlier versions

So `FeeSchedule` rows still carry their formula parameters, source and
effective window. What changes is that `constants_verified` can now be **true**
for the general schedule, which unblocks profitability claims for series on the
standard multiplier.

#### Superseded note

An earlier revision of this document recorded the constants as UNRESOLVED
because `kalshi.com/docs/kalshi-fee-schedule.pdf` returned HTTP 429 to every
automated fetch. It remains fetch-blocked from this environment; the values
above come from independent review of the current official schedule. The
per-series non-standard multiplier table in that PDF is still **not**
transcribed — see the open questions.

#### Rounding: docs vs third-party sources — RESOLVED

Third-party summaries describe the fee as rounded up **to the cent**. The
official API documentation specifies `ceil_6dp`. The official documentation
governs; the blog figures appear to describe an older schedule, or to be
simplifying. Our implementation rounds to six decimals per A-13.

## Market, event and series semantics

### A-15 `mutually_exclusive` is AT_MOST_ONE — VERIFIED (docs + live)

The official field description:

> *"If true, only one market in this event can resolve to 'yes'. If false,
> multiple markets can resolve to 'yes'."*

This is precisely AT_MOST_ONE. It says nothing about whether *any* market must
resolve YES, so it does **not** establish AT_LEAST_ONE and therefore **cannot**
establish EXACTLY_ONE.

Observed live: `KXNEXTNATOSECGEN-99` and `KXNEWPOPE-70` both report
`mutually_exclusive: true`. Neither is exhaustive in any obvious sense — "none
of the listed candidates" is a real outcome in both.

**Our handling.** `mutually_exclusive: true` may seed an AT_MOST_ONE relation
at `REVIEW_REQUIRED`. Promotion to `VERIFIED`, and any AT_LEAST_ONE or
EXACTLY_ONE claim, requires a human reading `rules_primary` and recording the
evidence.

### A-16 `collateral_return_type` — OBSERVED-ONLY

Observed values: `"MECNET"` on mutually-exclusive events, `""` otherwise. The
docs describe the field as *"how collateral is returned when markets settle
(e.g. 'binary' for standard yes/no markets)"* without enumerating values.

`MECNET` reads as mutually-exclusive collateral netting: collateral for a set
of mutually exclusive positions nets rather than summing. If so it materially
reduces the **capital required** for a NO basket, which changes return on
deployed capital — though not the arbitrage classification itself, which
depends only on worst-case payoff versus all-in cost.

**Our handling.** Capital required is computed conservatively (un-netted)
until netting behaviour is confirmed, and the record carries both figures.
Under-stating capital would over-state return; over-stating it cannot create a
false arbitrage.

### A-17 Market type and settlement result — VERIFIED (docs + live)

- `market_type`: `"binary"` | `"scalar"`
- `result`: `"yes"` | `"no"` | `"scalar"` | `""` (unsettled)
- `settlement_value_dollars`: nullable, the YES settlement value once determined
- `status`: `initialized`, `inactive`, `active`, `closed`, `determined`,
  `disputed`, `amended`, `finalized`

The settlement page also references *"sub-cent scalar settlement"*,
confirming that non-binary terminal payoffs genuinely exist.

The `disputed` and `amended` statuses are direct evidence that settlement is
not always final on determination.

**Our handling.** Only `market_type == "binary"` maps to
`SettlementKind.BINARY`. `"scalar"` maps to `SettlementKind.SCALAR`, and
anything unrecognised maps to `SettlementKind.UNKNOWN`. Only `BINARY` is
eligible for a contractual-arbitrage claim.

### A-18 Payout is metadata — VERIFIED (live)

`notional_value_dollars` carries the per-contract settlement value.

**Survey (Step 2).** Sampled **~19,100 markets** across three populations:

| Population | Markets | `notional_value_dollars` | `market_type` |
| --- | --- | --- | --- |
| Default `/markets` listing (open) | 12,000 | all `"1.0000"` | all `binary` |
| Settled markets | 6,000 | all `"1.0000"` | all `binary` |
| Sampled across ~400 distinct series | 1,089 | all `"1.0000"` | all `binary` |

Settled markets reported `settlement_value_dollars` of exactly `"0.0000"` or
`"1.0000"`, and `result` of `yes` or `no` — consistent with $1 binary payout.

**Conclusion: no market with a notional other than `$1.0000` was observed, and
no `scalar` market was observed at all.** That is evidence, not proof. The field
is still read per instrument and `Price.complement()` still requires an explicit
notional, because the cost of reading the field is zero and the cost of a wrong
assumption is every payoff silently rescaled. `market_type: "scalar"` and
`result: "scalar"` remain documented, so the type exists even if it is not
currently listed on this endpoint.

### A-19 Strike structure — VERIFIED (live)

`strike_type`: `greater`, `greater_or_equal`, `less`, `less_or_equal`,
`between`, `functional`, `custom`, `structured`, with `floor_strike`,
`cap_strike`, `functional_strike`, `custom_strike`.

These are the raw material for proposition normalisation (threshold,
comparator, unit). `functional` and `custom` strikes are not mechanically
normalisable and go to manual review.

### A-20 Multivariate events (MVE) — OBSERVED-ONLY

Live markets exist with `mve_collection_ticker`, `mve_selected_legs` and
`is_provisional: true`. These are combination contracts whose payoff is a
conjunction of legs in *other* events — for example a single contract on
"Texas wins first 5 innings **and** Houston wins".

This is a genuine source of provable relations (an MVE contract logically
implies its legs) and also a trap: an MVE market looks like an ordinary binary
market in `GET /markets`.

**Our handling.** Phase 1 **excludes** any market carrying
`mve_collection_ticker` or `is_provisional: true` from detection. `IMPLIES` is
already reserved in `RelationType` for the phase that models them properly.
`is_provisional` markets can be *removed* after determination, which is its own
settlement-risk category.

### A-21 Rate limits — VERIFIED (docs)

Token-bucket, continuously refilling, no fixed windows and no penalty on 429.
Default cost 10 tokens per request. Basic tier: 200 read tokens/sec, 100 write
tokens/sec, rising through named tiers to 10,000/8,000. Buckets hold one to two
seconds of budget depending on tier, allowing a burst of up to twice the
per-second budget after idling.

Phase 1 is read-only, so only the read bucket matters. `GET
/account/endpoint_costs` lists non-default costs.

### A-22 Exchange sharding — OBSERVED-ONLY

`exchange_index` appears on markets, events and series (observed `0` and `1`).
Rate-limit docs tie shard-scoped budgets to `exchange_index >= 1`. Relevance to
read-only market data is unclear; recorded because it may affect connection
routing.

### A-23 Settlement timing — VERIFIED (live)

`settlement_timer_seconds` (observed `5`) is the delay from determination to
settlement. Combined with `expected_expiration_time` and
`latest_expiration_time` this drives the capital-lockup estimate. Note the
docs warn settlement timing "can vary based on market type, data source
availability, and manual review requirements", so this is an estimate, not a
guarantee.

---

### A-24 WebSocket requires authentication — VERIFIED (empirically)

Confirmed by attempting an unauthenticated connection to the production socket:

```
wss://external-api-ws.kalshi.com/trade-api/ws/v2
-> websockets.exceptions.InvalidStatus: server rejected WebSocket connection: HTTP 401
```

Public market data over REST needs no credentials, but the WebSocket does, even
for public order-book channels. **Consequence for Phase 1:** with no API key
configured, no real WebSocket message can be captured. The WebSocket fixtures in
`tests/fixtures/kalshi/websocket/` are therefore **synthetic and labelled as
such** in the manifest, the filenames and a README. Resolving A-09 is blocked on
credentials.

### A-25 REST and WebSocket name the book differently — VERIFIED

| Transport | Shape |
| --- | --- |
| REST `GET /markets/{ticker}/orderbook` | `{"orderbook_fp": {"yes_dollars": [...], "no_dollars": [...]}}` |
| WebSocket `orderbook_snapshot` | `{"msg": {"yes_dollars_fp": [...], "no_dollars_fp": [...]}}` |

Same data, different wrapper and different field names — the `_fp` suffix moves
from the wrapper to the arrays. Modelled by two separate wire classes so neither
can be validated with the other's schema.

### A-26 Exchange shards — VERIFIED (live)

`GET /exchange/status` enumerates the shards referenced by `exchange_index`:

| Index | Description |
| --- | --- |
| 0 | Default |
| 1 | Combos |
| 2 | Crypto & Commodities |
| 3 | Tennis, Baseball, Basketball |

Each reports `exchange_active`, `trading_active` and
`intra_exchange_transfers_active` independently, so one shard can be halted
while others trade. A book from a halted shard is not a tradeable book, which
makes this a scan-eligibility input, not just diagnostics.

### A-27 Undocumented response fields appear without notice — OBSERVED

`GET /events` returns a top-level `milestones` key that is not in the models
here and was not looked for. It is ignored safely. This is the concrete
justification for the forward-compatibility policy: unknown fields are ignored
but *reported*, so additions surface in observability instead of either crashing
ingestion or vanishing silently.

## Differences from the assumptions in the Phase 1 brief

The brief is accurate on the points that matter most (bids-only books, no
universal fee rate, mutual exclusion ≠ exhaustiveness). Three of its incidental
assumptions need adjusting:

| Brief says | Actually | Effect |
| --- | --- | --- |
| "Buy `q` YES / `q` NO" with `q` implicitly an integer | Quantities are fractional to 2 dp | `Quantity` is fixed-point; depth breakpoints can be fractional |
| Binary complement "pays `q` dollars" | Pays `q x notional_value_dollars` | Payoff engine reads the notional per instrument |
| Fee "rounding" (unspecified precision) | Documented as ceiling to 6 dp, plus an accumulator/rebate mechanism | `Money` is micro-dollar denominated; rounding is modelled, not approximated |

The brief's instruction to use `Decimal` and/or fixed-point integers is
followed with a preference for fixed-point integers; see
`src/predarb/domain/money.py` for why.

---

## Open questions

Ordered by how much damage a wrong guess would do.

1. **A-09 — `seq` scoping and the expected gap-recovery procedure.** Currently
   handled by failing closed and resnapshotting. **Blocked on credentials**
   (A-24): the socket rejects unauthenticated connections, so the controlled
   observation needed to settle this — one subscription covering several
   markets, then several subscriptions — cannot be run yet. The synthetic
   fixtures deliberately do **not** encode an answer, and nothing in the wire
   layer interprets `seq`.
2. **A-14 — the per-series non-standard multiplier table.** The general
   formulas are now verified, but the fee schedule PDF also carries a table of
   series with non-standard maker/taker multipliers, which is not yet
   transcribed. A series on a non-standard multiplier would have its fees
   mis-stated if we assumed `M` from the general schedule. Mitigated because
   `fee_multiplier` is read per series from the live API; the open part is
   whether the PDF's table ever disagrees with that field.
3. **A-16 — does `MECNET` net collateral across a mutually-exclusive basket,
   and by how much?** Affects capital and return, not classification.
4. **A-10 — the numeric per-subscription market limit.**
5. **A-18 — does any live market have a notional other than `$1.0000`?**
   ~19,100 markets sampled, all `$1.0000` and all `binary`; no `scalar` market
   observed. Downgraded from unknown to *unobserved*, not closed.
6. **Maker fees.** Phase 1 models taker execution only, so this is deferred,
   but `quadratic_with_maker_fees` exists and the `0.0175` rate is recorded.
7. **Settlement edge cases.** Void, cancellation, postponement and tie
   behaviour are not enumerated in the API docs. Per-series contract terms
   (`contract_terms_url`) are the real source. Until read for a given series,
   its settlement spec stays `UNKNOWN` and its markets are excluded.

### Resolved since the first revision

| Was | Now |
| --- | --- |
| Production WebSocket path unknown (A-02) | Verified: `wss://external-api-ws.kalshi.com/trade-api/ws/v2` |
| What the WS handshake signs (A-03) | Verified: `timestamp + "GET" + "/trade-api/ws/v2"` |
| `model_fee` formula unknown (A-14) | Verified: taker `M*0.07*C*P*(1-P)`, maker `M*0.0175*C*P*(1-P)` |
| Fee rounding: cent or 6 dp? (A-13) | Resolved: 6 dp per official API docs; third-party "to the cent" is not authoritative |
| `price_ranges` boundary ownership (A-04) | Resolved: bands are contiguous and whole-step, so adjacent bands always agree; validity is the union |
| Does the WebSocket need auth? (A-24) | Resolved: yes — HTTP 401 unauthenticated, verified empirically |

## Sources

- https://docs.kalshi.com/
- https://docs.kalshi.com/llms.txt
- https://docs.kalshi.com/getting_started/quick_start_market_data
- https://docs.kalshi.com/getting_started/demo_env
- https://docs.kalshi.com/getting_started/api_keys
- https://docs.kalshi.com/getting_started/rate_limits
- https://docs.kalshi.com/getting_started/orderbook_responses
- https://docs.kalshi.com/getting_started/fee_rounding
- https://docs.kalshi.com/getting_started/market_settlement
- https://docs.kalshi.com/websockets/websocket-connection
- https://docs.kalshi.com/websockets/orderbook-updates
- https://docs.kalshi.com/api-reference/market/get-market
- https://docs.kalshi.com/api-reference/market/get-series
- https://docs.kalshi.com/api-reference/market/get-market-orderbook
- https://docs.kalshi.com/api-reference/events/get-event
- https://docs.kalshi.com/api-reference/exchange/get-series-fee-changes
- Live production API responses, 2026-09-15 (see inline quotes above)
- https://kalshi.com/docs/kalshi-fee-schedule.pdf — effective 2026-07-07; still
  HTTP 429 to automated fetch from this environment, values above verified by
  independent review
