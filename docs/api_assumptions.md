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

### A-09 Sequence semantics — RESOLVED by live experiment (2026-09-16)

The highest-risk unknown in Phase 1, now settled with production evidence.
**2,764 frames** across 5 connections, gathered by
`tools/ws_sequence_experiment.py`.

#### DOCUMENTED

* Two message types on `orderbook_delta`: `orderbook_snapshot`, then
  `orderbook_delta`.
* The envelope carries `sid` (server-assigned subscription id) and `seq`.
* `seq` "should be checked if you want to guarantee you received all the
  messages"; "used for snapshot/delta consistency".
* `delta_fp` is a signed relative change.
* A snapshot arrives first on subscription.

#### OBSERVED

**`seq` is scoped per `sid`, and is dense within it.** Every candidate scope was
measured over the same frames:

| Scope | Streams | Adjacent pairs | Advance by +1 | Skips | Dups | Decreases | Density |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| **SID** | 6 | **2,752** | **2,752** | **0** | **0** | **0** | **1.0000** |
| CONNECTION | 5 | 2,753 | 2,749 | 2 | 0 | 2 | 0.9985 |
| SID_MARKET | 15 | 2,741 | 2,735 | 6 | 0 | 0 | 0.9978 |
| MARKET | 14 | 2,742 | 2,732 | 8 | 0 | 2 | 0.9964 |

SID is the **only** scope with a perfect result, and every individual sid stream
is perfectly dense:

| session / sid | Frames | Range | Composition |
| --- | ---: | --- | --- |
| 1 / 1 | 289 | 1 → 289 | 4 snapshots, 285 deltas |
| 2 / 1 | 1,493 | 1 → 1,493 | 4 snapshots, 1,489 deltas |
| 2 / 2 | 6 | 1 → 6 | 6 trades |
| 3 / 1 | 753 | 1 → 753 | 2 snapshots, 749 deltas, 2 `ok` |
| 4 / 1 | 97 | 1 → 97 | 2 snapshots, 95 deltas |
| 5 / 1 | 120 | 1 → 120 | 2 snapshots, 118 deltas |

**Experiment A — one subscription, several markets.** All markets share a single
`sid`, and their frames interleave in one dense sequence. `seq` is *not*
per-market.

**Experiment B — two subscriptions, one connection.** Two subscribes to the
*same* channel do **not** create a second sid: the server merges the markets
into the existing subscription. Distinct sids require **distinct channels**
(`orderbook_delta` → sid 1, `ticker` → sid 2, `trade` → sid 3). With two live
sids on one socket, both started at `seq = 1` independently. The decisive
interleave:

```
idx=126  sid=1  seq=125  orderbook_delta
idx=127  sid=2  seq=1    trade            <- second sid opens its own sequence
idx=128  sid=1  seq=126  orderbook_delta  <- first sid continues, unaffected
```

A connection-global counter cannot produce this. It is also why the CONNECTION
row above shows *decreases*.

**Experiment C — `update_subscription`.** `add_markets` kept the same `sid` and
delivered exactly **one** snapshot for the added market, numbered **250** —
mid-stream, not at 1. `delete_markets` stopped that market's frames while the
original market continued uninterrupted. Sequence numbering was unbroken
throughout.

**Experiment D — reconnect.** Both reconnects were assigned `sid = 1` and
restarted at `seq = 1`. **Sequence state does not survive a connection.**

**Not every frame carries `seq`.** `ticker` frames were observed with no `seq`
at all, and the `subscribed` acknowledgement carries its `sid` inside `msg`
rather than on the envelope.

**Non-market-data frames consume sequence numbers.** The `ok` frame (the
documented response to `update_subscription`) occupies a slot in the sid's
sequence. A reconstructor must not assume every value in a sid's sequence is an
order-book message for a market it tracks.

#### INFERRED

* `seq` is dense per `sid` **in the general case**. 2,752 consecutive pairs with
  zero exceptions is strong, but it is a sample from one account, one venue
  shard, and roughly fifteen minutes of trading. It is a sound basis for an
  implementation invariant; it is not an exchange guarantee.
* A skip within a sid therefore indicates message loss. This follows from
  density, and density is observed rather than documented.

#### UNKNOWN

* Whether the exchange *guarantees* density, or merely exhibits it. Nothing in
  the documentation promises it.
* The expected recovery procedure after a gap. Not documented; our answer is to
  resnapshot.
* Whether `seq` ever wraps, and at what value. The longest stream observed
  reached 1,493.
* Whether a skip can occur legitimately under load, backpressure, or on a shard
  other than the one observed.
* Whether every channel numbers its sid the same way. Confirmed for
  `orderbook_delta` and `trade`; `ticker` carries no `seq` at all.

#### Invariant for Step 4

The evidence contradicts the previous tentative `(sid, market_ticker)` design,
which shows 6 skips and would fire spurious gap alerts. The corrected invariant:

> **Track `seq` per `(connection, sid)`.** The first frame of a sid establishes
> the baseline; every subsequent frame carrying a `seq` must equal
> `expected_seq`. Anything else — a skip, a repeat, or a decrease — invalidates
> **every book belonging to that sid**, not just the market named in the frame,
> because the counter is shared across markets and a hole could have carried any
> of them. Invalidated books become `INTEGRITY_UNKNOWN`, are excluded from
> scanning, and are restored only by a fresh snapshot.
>
> Frames without a `seq` are passed through without advancing the counter.
> Sequence state is per-connection and is discarded on disconnect.

Two consequences worth stating explicitly, because both are easy to get wrong:

* **A gap invalidates the whole sid, not one market.** With one counter per
  subscription, a missing frame could have belonged to any subscribed market.
  Invalidating only the market named in the *next* frame would leave the
  genuinely affected book silently stale.
* **A snapshot is not special.** It takes the next number in the sid's sequence
  like anything else — observed at 250 mid-stream after `add_markets`. Treating
  a snapshot as a sequence reset would discard a real gap.

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

#### Which column does `fee_multiplier` scale? — UNKNOWN

The fee schedule presents maker and taker multipliers in separate columns, and
the API exposes a single `fee_multiplier` per series. Whether that one field is
the taker column, the maker column, or a factor applied to both is **not stated
anywhere reachable from this environment**, and nothing in the API's own
documentation resolves it. The schedule PDF remains HTTP 429 to automated fetch.

The `GET /trade-api/v2/margin/fee_tiers` endpoint does return `maker_fee_rates`
and `taker_fee_rates` separately (changelog, 2026-06-11), but it does not
resolve this: it covers **margin** markets on a different fee model entirely
(a decimal fraction of notional, e.g. `0.0008` = 8bps), not event contracts on
the quadratic model, and it requires authenticating as a direct margin user —
account access Phase 1 does not take.

**Machine-readable consequence.** `MultiplierStatus` gates the arbitrage claim:

| Status | When | Effect |
| --- | --- | --- |
| `IDENTITY` | `fee_multiplier = 1` | Every reading of the schedule agrees; the ambiguity cannot change the number. `supports_arbitrage_claim` may be true |
| `UNRESOLVED_MAPPING` | anything else | Figures are still reported as hypotheticals; `supports_arbitrage_claim` is **false** |

This is not a hypothetical restriction. 32 listed series carry `M = 0` or
`M = 0.5`, and in the `/series/fee_changes` universe it is the common case: of
the 55 quadratic rows there, 30 are at `M = 0` and 18 at `M = 0.5`, leaving only
7 arbitrage-eligible.

#### Which fee types can actually be priced — VERIFIED (live, 2026-09-18)

`tools/validate_fees.py` swept the full live series population. `fee_type` and
the taker rate are **not** the same question: the schedule gives a rate for the
general quadratic model, not for every `fee_type` the API emits.

Counted over two separately named universes, because `/series` is not
exhaustive (A-42).

**Listed series** — everything `/series` returns: **14,168**

| State | Count | Share of listing |
| --- | --- | --- |
| `BOUNDABLE_FOR_ARB` — quadratic, `M = 1` | 13,973 | 98.62% |
| `CALCULABLE_ONLY` — quadratic, `M != 1` | 32 | 0.23% |
| `BLOCKED_UNSUPPORTED_TYPE` | 163 | 1.15% |

By fee type within that listing: `quadratic` 14,005; `quadratic_with_maker_fees`
160; `quadratic_with_combo_maker_fees` 3. (14,005 + 160 + 3 = 14,168.)

**Addressable but unlisted** — named in `/series/fee_changes`, never returned by
the listing: **23**, all `margin_market_maker_program_fees`, all blocked.

**Combined**: 13,973 boundable of **14,191**. That universe is the listing plus
the fee-change tickers it omitted, and is a *lower bound* on what exists.

A previous revision of this document reported "14,004 / 14,167 priceable" with
unsupported counts of 160 + 3 + 23. **Those numbers do not reconcile**: 23 of
them are from a different universe, and "priceable" silently included the 32
series whose multiplier semantics are unresolved. `CoverageCensus` now refuses
to hold counts that do not sum to their own total.

The maker variants are refused because the names only *imply* the quadratic
taker leg is unchanged; an implication from a name is not a specification.

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

### A-28 Account rate-limit discovery — VERIFIED (docs), UNTESTED (no credentials)

Rate limits are **discoverable**, so nothing is hardcoded:

``GET /account/limits``
    ``usage_tier`` (basic / advanced / expert / premier / paragon / prime /
    prestige), plus separate ``read`` and ``write`` objects each carrying
    ``refill_rate`` (tokens/second) and ``bucket_capacity``. Also ``grants``,
    describing volume- or manually-granted usage levels.

``GET /account/endpoint_costs``
    ``default_cost`` plus ``endpoint_costs``, an array of
    ``{method, path, cost}`` for endpoints that differ from the default.

Both are authenticated but disclose **no** balance, position, order or fill
data — only rate-limit configuration. That is why Phase 1 calls these two
``/account`` routes and no others.

`10` is the *current* default cost, not a constant: it is read from the server.
An account on a different tier, or a future change to a specific endpoint's
cost, would silently invalidate a hardcoded value.

**Open:** the exact format of the `path` field in `endpoint_costs` (whether it
includes the `/trade-api/v2` prefix) is unverified until a live call is made.
The registry looks up whatever template the client passes, so a format mismatch
degrades to the default cost rather than failing — conservative, but it would
mean an override was missed.

### A-29 Anonymous rate limits are not documented — UNRESOLVED

The discovered budget describes the **authenticated account**. Nothing states
that it also describes anonymous public requests.

Rather than model a token bucket whose numbers would be fiction, unauthenticated
access uses bounded concurrency plus a politeness interval, and relies on
backoff if a 429 arrives. This is recorded as a deliberate gap, not an oversight.

### A-30 429 responses carry no retry metadata — DOCUMENTED

Current documentation states 429 responses contain neither ``Retry-After`` nor
``X-RateLimit-*`` headers, and that a 429 carries no penalty: the bucket simply
keeps refilling.

So the client uses bounded exponential backoff with jitter. It still *reads* a
``Retry-After`` if one ever appears, and prefers it when present.

### A-31 Demo and production hold different data — OBSERVED

The same public call returns different results per environment: 147 historical
series fee changes on production versus 72 on demo (2026-09-15). Demo is not a
mirror, so a fixture captured there is not interchangeable with a production
one. All committed fixtures are from production.

### A-32 The default `/markets` listing is ~100% combo markets — VERIFIED (live)

Measured 2026-09-16 on production, paging `GET /markets?status=open`:

* **29,998 of 30,000** markets returned carry `mve_collection_ticker` (combo).
* The first non-combo market appears at position **16,772**.
* In the first 6,000, exactly **one** market had any resting size.

Enumerating series and querying per series instead found **1,222** quoting
non-combo markets out of 1,872 examined.

**Consequence.** Any scan that pages the default listing a few thousand deep
and skips combos evaluates *zero* real candidates, and will report "no active
markets" — a statement about listing order, not about the exchange. Market
discovery must go through `GET /series` and then per-series market queries.

### A-33 `status` query filter and `status` field use different vocabularies — VERIFIED (live)

| Filter value | Result |
| --- | --- |
| `open` | markets whose `status` field is `active` |
| `unopened` | `status` field is `initialized` |
| `closed` | `status` field is `closed` or `determined` |
| `active` | **HTTP 400** `"invalid status filter"` |

Passing the field's own value as a filter is rejected. The two vocabularies
overlap enough to be confusing and must not be used interchangeably.

### A-34 `no_bid_size_fp` is routinely absent from list responses — VERIFIED (live)

Of 1,222 quoting non-combo markets found, **every one** quoted only the YES
side in the listing; `no_bid_size_fp` was absent or zero throughout, including
on markets with six-figure 24-hour volume.

A selector requiring both sides therefore matches nothing. The order book
itself often *does* have NO-side levels — the field is a summary that is
frequently not populated, not a statement that the side is empty.

### A-35 Collection fields arrive as JSON `null` — VERIFIED (live)

Sampling 14,098 series and 500 markets:

| Field | `null` occurrences |
| --- | ---: |
| `series.tags` | 2,777 (20%) |
| `series.settlement_sources` | 7 |
| `series.additional_prohibitions` | 1 |

A model declaring these as non-nullable arrays **crashes on real data**; this
was found by a live metadata scan failing, not in review. Every collection
field now coerces `null` to an empty tuple.

Note this is the opposite judgement to the one made for prices: an absent price
stays `None` because "no bid" is not "$0.00", whereas a null list has no second
reading.

### A-36 `orderbook_delta` carries both `ts` and `ts_ms` — DOCUMENTED (`ts` deprecated)

Every real `orderbook_delta` observed carries both:

```json
"ts": "2026-09-16T17:54:15.469939Z",
"ts_ms": 1789581255469
```

**Current documentation documents both**, and marks `ts` explicitly:

> `ts` (string, optional, **deprecated**) — "Deprecated - Optional timestamp for
> when the orderbook change was recorded (RFC3339). Use `ts_ms` instead"

**Use `ts_ms`.** An earlier revision of this document recorded `ts` as
undocumented and preferred it over `ts_ms` on the grounds that it carries
microseconds. Both halves of that were wrong: it is documented, and it is
deprecated. The extra resolution is not worth depending on a field the venue
has said to stop using. `ts` is still parsed when present, so a recorded frame
round-trips, but nothing derives from it.

*Historical note:* both fields were first noticed empirically in captured
frames, before the documentation was re-checked — which is why the earlier
revision misclassified them.

Deltas were also observed arriving in same-timestamp pairs of equal magnitude
and opposite sign (`-1568.00` then `+1568.00`) — resting size moving between
price levels.

### A-37 `ok` control frames carry `sid` and `seq` — DOCUMENTED

`update_subscription` is answered with an `ok` frame. Current documentation
shows it verbatim, including both fields:

```json
{"type": "ok", "id": 123, "sid": 456, "seq": 222, "msg": { ... }}
```

Observed live, confirming the shape:

```json
{"type":"ok","id":2,"sid":1,"seq":1,"msg":{"market_tickers":[...]}}
```

**A control frame therefore consumes a sequence number.** This is the single
most consequential detail for reconstruction: sequence validation must happen
*before* type routing, because a reconstructor that filters to order-book
messages and only then checks `seq` will see a phantom gap every time a control
frame passes.

*Historical note:* first discovered empirically in a captured frame; the
documentation was then re-checked and does describe it.

#### `update_subscription` actions — DOCUMENTED

`add_markets`, `delete_markets`, `get_snapshot` (plus index/underlying variants
for the CF Benchmarks and Pyth channels, which Phase 1 does not use).

Observed: `add_markets` keeps the same `sid` and emits one snapshot for the
added market, numbered mid-stream; `delete_markets` stops that market's frames
while others continue.

### A-38 A snapshot may contain no levels — VERIFIED (live)

Two captured `orderbook_snapshot` frames had empty `yes_dollars_fp` *and*
`no_dollars_fp`: the markets had finished trading. An empty snapshot is valid
real data, not a malformed frame, and must not be treated as a parse failure.

### A-39 WebSocket keep-alive — DOCUMENTED

There is a dedicated page, `docs.kalshi.com/websockets/connection-keep-alive`:

* Kalshi sends **Ping frames every 10 seconds**, with the body `heartbeat`.
* Clients are expected to **respond with Pong frames (`0xA`)**.
* Clients **may initiate their own Ping frames**, and Kalshi responds with Pong.
* No consequence for non-response is documented.

*Historical note:* an earlier revision recorded this as UNRESOLVED because the
WebSocket overview, the connection reference and the quick-start none of them
state a cadence — the quick-start only says the Python `websockets` library
handles ping/pong automatically. The cadence lives on its own page, which the
earlier search missed.

**The liveness design is unchanged, and this confirms it.** Two points matter:

1. The documented mechanism is Ping/Pong at the *protocol* level, not
   application JSON. That is exactly the separation already implemented: book
   change time is not connection health, sid JSON activity is not necessarily
   connection health, and Ping/Pong is.
2. Our client-initiated probe is explicitly sanctioned — "clients may initiate
   their own Ping frames, to which Kalshi will respond with Pong". It is kept,
   because it yields a *positive* answer on demand rather than inferring health
   from the absence of something.

The 10-second cadence is now a documented fact rather than a guess, but our own
liveness window remains **our local safety policy**: the documentation states no
consequence for a missed Pong, so how long to tolerate silence is still our call.

### A-40 `get_snapshot` preserves the sid and numbers in-stream — VERIFIED (live)

`update_subscription` with `action: "get_snapshot"` was exercised against
production on 2026-09-18. Findings:

| Question | Answer |
| --- | --- |
| Does it preserve the `sid`? | **Yes** — every subsequent frame stayed on the same sid |
| What `seq` do the snapshots get? | Mid-stream and consecutive: `18`, `19` |
| Does each requested market produce one? | **Yes** — two markets requested, two snapshots |
| Do deltas interleave afterwards? | **Yes** — 10 deltas arrived among them |
| Does the sequence stay dense? | **Yes** — contiguous across the whole observation |

So a requested snapshot behaves exactly like the automatic one: it takes the
next number in the sid's sequence rather than resetting it, and the stream
continues uninterrupted.

**Not yet used for recovery.** This is a viable future optimisation — it would
avoid a full reconnect after a gap — but Phase 1 keeps reconnect as the default
recovery boundary. What is verified here is behaviour on a *healthy* sid; how
the venue responds to `get_snapshot` on a sid whose sequence we have already
lost is untested, and that is precisely the case recovery has to handle.
Correctness before avoiding a reconnect.

### A-41 Application silence is not a liveness signal — VERIFIED (live)

Kalshi's keep-alive is at the *protocol* level (A-39), and a quiet market
genuinely sends no application frames for minutes at a time.

Measured consequence: a collector that inferred health from application traffic
dropped and rebuilt the connection **every 30 seconds** on quiet markets. After
switching to a protocol-level Ping/Pong probe on idle, the same code ran a full
240-second session on **one connection with zero reconnects**.

The protocol's own Ping/Pong is the correct signal, and the `websockets` library
exposes it directly: `connection.ping()` returns a waiter that resolves with the
round-trip time when the Pong arrives. A Pong proves the socket works; silence
proves nothing. Kalshi explicitly supports client-initiated Pings (A-39). Any
timeout layered on top is **our local safety policy** — the documentation states
no consequence for a missed Pong.

### A-42 The `/series` listing is not exhaustive — VERIFIED (live, 2026-09-18)

Paginating `/series` to exhaustion returns 14,168 series (2026-09-18; the count drifts as series are added). It does **not** return
every series the API will serve: 23 perpetual-futures series (`KXBTCPERP`,
`KXETHPERP`, `KXAAVEPERP`, ...) are absent from that listing yet resolve
normally through `GET /series/{series_ticker}`.

They were found by taking the series tickers appearing in
`/series/fee_changes` and subtracting the listing. All 23 carry
`fee_type = "margin_market_maker_program_fees"` and `fee_multiplier = 0`.

Two consequences:

1. **A census built on `/series` under-reports.** Surveying only the listing
   would conclude `margin_market_maker_program_fees` is extinct, when in fact
   it is live on every one of those series. `tools/validate_fees.py` probes the
   gap explicitly rather than assuming the listing is complete, and counts the
   two populations as **separate universes** with their own denominators —
   folding them into one fraction is what made the earlier coverage figures
   fail to reconcile (A-14).
2. **Absence from a listing is not absence from the venue.** Any later
   universe-selection step must treat the listing as a lower bound on what
   exists, not as the set of what exists.

Why these particular series are excluded is not documented. They are margin
products rather than event contracts, so plausibly the listing is scoped to
event contracts — but that is inference, not documentation, and is recorded as
INFERRED. Phase 1 does not trade them either way.

### A-43 Quantity granularity bounds fill fragmentation — DOCUMENTED

> "Minimum granularity is 0.01 contracts"
> — docs.kalshi.com/getting_started/fixed_point_migration

Quantities accept 0–2 decimal places on input and are always emitted with 2.
Combined with 4dp prices, "intermediate calculations can reach up to 6 decimal
places (for example, in fee rounding math)" — the same scale invariant this
codebase enforces.

The consequence matters well beyond parsing. Every positive fill is a whole
number of 0.01-contract increments, so an order of quantity `Q` cannot be split
into more than `Q / 0.01` fills. Fill fragmentation is therefore **finite and
computable**, which is what makes a pre-trade upper bound on fees derivable at
all — see `docs/fees.md` §3.1 and §3.4.

An earlier revision of this work described the non-direct member's fee exposure
as *unbounded*. That was incorrect: unknown is not the same as unbounded, and
the bound follows directly from this documented granularity.

### A-44 The fee accumulator may retain value at order end — DOCUMENTED

The fee-rounding page states that the accumulator "applies across all fills of
an order so that the total fee converges to what a single equivalent fill would
cost". That wording invites a stronger reading than the mechanics support.

What the documentation actually specifies, and what we implement verbatim:

1. `trade_fee = ceil_6dp(model_fee)`
2. `aligned_change = floor_precision(revenue - trade_fee)`
3. `rounding_fee = (revenue - trade_fee) - aligned_change`
4. add the rounding fee to the order's accumulator
5. rebate in increments of the user's target balance precision, "capped so the
   fill's net fee cannot be negative"

Step 5's cap is per-fill, so a fill whose own fee is below one balance step can
never trigger a rebate no matter how much has accrued. The documentation's own
worked example already ends with **$0.002 still carried** after the order's
final fill, so residue at order end is documented behaviour, not our invention.
What it does not address is residue of a *whole step or more*, which dust-sized
fills produce.

Searched and not found in any reachable official source: an end-of-order
reconciliation, a final accumulator refund, any definition of what becomes of
stranded accumulator value on order termination, or any documented fill
aggregation that would prevent the fragmentation in the first place (the fills
endpoint defines a fill only as "when a trade you have is matched").

Status: the five steps are DOCUMENTED; the dust-fill residue is INFERRED from
them; the intended scope of "converges" is UNKNOWN. We implement the explicit
steps and do not assume the stronger invariance. Note this is the safe
direction: if an undocumented refund does exist, our published fee interval
already extends down to `ceil_6dp(F)` and remains correct.

### A-45 `market_type == "binary"` does not establish a two-state payoff — DOCUMENTED

Kalshi's rules acknowledge markets that resolve to a **fair-market value** or
carry a **did-not-play** adjustment, governed by the per-series contract terms
rather than by any API field. A contract can therefore carry
`market_type == "binary"` and still have a terminal value that `{0, notional}`
does not describe.

Consequence for Phase 1: `market_type` is a description, never evidence. A
contractual-arbitrage claim requires an explicit
`SettlementCertificate` whose payoff tables were read from the rules text and
bound to its `rules_hash` (`docs/detection.md` §2-3). The same applies to the
title, ticker, event category and yes/no subtitles — none of them specify
settlement, and none may mint a certificate.

This is the gate that makes the difference between "these two prices sum to
less than a dollar" and "this portfolio pays a dollar in every state it can
reach". Without it the first sentence gets mistaken for the second.

Status: the existence of non-standard resolutions is DOCUMENTED; which specific
markets carry them is UNKNOWN per market until their terms are read, so every
market starts at `REVIEW_REQUIRED`.

### A-46 The event market list is not a complete outcome universe — DOCUMENTED

> "historical markets settled before the historical cutoff will not be included"
> — docs.kalshi.com, Get Event and Get Events (`with_nested_markets`)

Markets that settled before the cutoff live only behind `GET /historical/markets`;
the boundary itself is published at `GET /historical/cutoff`. So:

    the markets returned for an event  !=  every outcome the event ever had

This is a **completeness** limitation, and it cuts differently for different
logical claims.

#### Safe: `AT_MOST_ONE` over a selected subset

"At most one of these selected markets may settle YES" needs no proof that the
list is exhaustive. Its state space is: none of the selected markets settles
YES, or exactly one does. A winner *outside* the selected subset — including a
historical market the API never returned — is economically identical, from the
basket's point of view, to "all selected markets settle NO", which is already an
enumerated state.

So a selected-subset `AT_MOST_ONE` claim survives incomplete membership intact.

#### Unsafe without further evidence: `AT_LEAST_ONE`, `EXACTLY_ONE`, `PARTITION`

Each of these asserts that *some* outcome must occur among a known set. That is
precisely a claim about completeness, and the endpoint that enumerates the set
explicitly does not guarantee it. A missing settled market is a missing outcome,
and a basket priced on "one of these must win" would be unhedged against it.

None of these is implemented.

#### Exhaustiveness may not be inferred from

- `mutually_exclusive` — that is a statement about *conflict*, not coverage:
  "no two can both win" says nothing about whether one must
- the current event market list, or how many markets it returned
- titles, subtitles or category
- the absence of an obvious missing outcome

Recorded so the distinction survives into later phases: mutual exclusion and
exhaustiveness are different propositions requiring different evidence, and only
the first is reachable from the metadata we have.

### A-47 Nested markets arrive inside the event object — VERIFIED (live, 2026-09-21)

``GET /events/{event_ticker}?with_nested_markets=true`` returns:

```
{ "event": { ..., "markets": [ 7 markets ] }, "markets": [] }
```

The markets are nested **inside** the event object. The envelope's own
top-level ``markets`` key is present and **empty**.

This is a quiet trap: reading the top-level key yields zero members, which is
indistinguishable from an event that genuinely has none. Our first relation
discovery run reported "57 mutually-exclusive events, 0 with at least two
members", which looked like a plausible finding about the venue and was in fact
a bug in our own reading.

``KalshiEventEnvelope.member_markets`` checks both, event-nested first, so a
caller cannot pick the wrong one. The top-level field is retained rather than
deleted: if the venue starts populating it, that is a change we want to see
rather than one we have already discarded.

Note this is a *shape* finding and does not soften A-46: however the markets are
delivered, the list still omits markets settled before the historical cutoff.

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

1. **A-09 follow-ups.** Scope and density are resolved (per-`sid`, dense over
   2,752 consecutive pairs). What remains unknown is whether density is
   *guaranteed* rather than merely exhibited, whether `seq` wraps, and what
   recovery the exchange expects after a gap. The Step 4 invariant fails closed
   on all three.
2. **A-14 — the per-series non-standard multiplier table.** The general
   formulas are now verified, but the fee schedule PDF also carries a table of
   series with non-standard maker/taker multipliers, which is not yet
   transcribed. A series on a non-standard multiplier would have its fees
   mis-stated if we assumed `M` from the general schedule. Mitigated because
   `fee_multiplier` is read per series from the live API; the open part is
   whether the PDF's table ever disagrees with that field.
3. **A-14 — does `fee_multiplier` scale the taker column, the maker column, or
   both?** The API exposes one field; the schedule has two columns. Not
   load-bearing while 14,134/14,167 series carry `M = 1`, where every reading
   agrees, but unresolved for the 33 series on `0` or `0.5`.
4. **Fee segmentation.** The net fee depends on how the venue splits an order
   into fills, which L2 depth does not reveal. It is nonetheless **bounded**:
   quantity granularity caps the fill count (A-43), giving a proven interval
   `[ceil_6dp(F), ceil_6dp(F) + k_max·B − µ]`. What remains open is how *tight*
   that bound can be made — for a non-direct member it is wide enough to exceed
   the position's notional, so marginal opportunities will still fail a
   risklessness test. Narrowing it would need either observed fill segmentation
   or a documented aggregation rule (A-44), neither of which we have.
5. **A-16 — does `MECNET` net collateral across a mutually-exclusive basket,
   and by how much?** Affects capital and return, not classification.
6. **A-10 — the numeric per-subscription market limit.** Low priority; to be
   learned from normal operation rather than by probing production.
7. **A-28 — the `path` format in `endpoint_costs`.** A mismatch silently falls
   back to the default cost.
8. **A-18 — does any live market have a notional other than `$1.0000`?**
   ~19,100 markets sampled, all `$1.0000` and all `binary`; no `scalar` market
   observed. Downgraded from unknown to *unobserved*, not closed.
9. **Maker fees.** Phase 1 models taker execution only, so this is deferred,
   but `quadratic_with_maker_fees` exists and the `0.0175` rate is recorded.
10. **Settlement edge cases.** Void, cancellation, postponement and tie
   behaviour are not enumerated in the API docs. Per-series contract terms
   (`contract_terms_url`) are the real source. Until read for a given series,
   its settlement spec stays `UNKNOWN` and its markets are excluded.

### Resolved since the first revision

| Was | Now |
| --- | --- |
| Does `get_snapshot` preserve the sid? (A-40) | Resolved: yes, and its snapshot takes the next seq in-stream |
| Is application silence a liveness signal? (A-41) | Resolved: no — use protocol Ping/Pong |
| Is there a documented ping cadence? (A-39) | Resolved: yes — every 10s, body `heartbeat`, client responds with Pong |


| Was | Now |
| --- | --- |
| Production WebSocket path unknown (A-02) | Verified: `wss://external-api-ws.kalshi.com/trade-api/ws/v2` |
| What the WS handshake signs (A-03) | Verified: `timestamp + "GET" + "/trade-api/ws/v2"` |
| `model_fee` formula unknown (A-14) | Verified: taker `M*0.07*C*P*(1-P)`, maker `M*0.0175*C*P*(1-P)` |
| Fee rounding: cent or 6 dp? (A-13) | Resolved: 6 dp per official API docs; third-party "to the cent" is not authoritative |
| `price_ranges` boundary ownership (A-04) | Resolved: bands are contiguous and whole-step, so adjacent bands always agree; validity is the union |
| Does the WebSocket need auth? (A-24) | Resolved: yes — HTTP 401 unauthenticated, verified empirically |
| What is `seq` scoped to? (A-09) | Resolved: per `sid`, dense; 2,752/2,752 pairs advance by exactly one |
| Is `seq` dense or merely monotonic? (A-09) | Resolved: dense within a sid; zero skips, duplicates or decreases observed |
| Does `seq` survive reconnect? (A-09) | Resolved: no — both reconnects restarted at `seq = 1` |

### A-48 Live and historical markets are partitioned by a moving cutoff — DOCUMENTED, partition not strict in practice

Current official documentation states the boundary explicitly.

`GET /markets`:

> Markets that settled before the historical cutoff are only available via
> `GET /historical/markets`.

`GET /events/{event_ticker}`, on `with_nested_markets`:

> **Historical markets settled before the historical cutoff will not be
> included.**

`GET /historical/cutoff` returns the boundary itself, as
`market_settled_ts`, `trades_created_ts`, `orders_updated_ts` and
`market_positions_last_updated_ts`. The Historical Data guide adds:

> The cutoff timestamps will be regularly updated, advancing forward over time.

`GET /historical/markets` accepts `event_ticker`, `series_ticker`, `tickers`,
`limit`, `cursor` and `mve_filter`, needs no authentication, and states that
**filters are mutually exclusive** — so supplying two is refused locally rather
than sent, because a venue that honoured one and ignored the other would return
a plausible page answering a different question.

**OBSERVED (live, 2026-09-22, 94 stratified events):** the cutoff was
`2026-07-24T00:00:00Z`. Both paginated paths were exhausted for **94/94**
events, covering 1,108 distinct markets.

The partition is **not strict**. `KXKNESSET-27` returned two markets from the
live tier that reported settlement times *before* the cutoff — markets the
documentation says are "only available via `GET /historical/markets`".
`KXG7LEADEROUT-45JAN01` showed the same on an earlier run, with all seven
members returned by both tiers.

So the tiers **overlap** rather than partition, in the safe direction: the live
tier can still carry an archived market, but nothing observed suggests the
reverse. Deduplication by ticker is mandatory, and enumeration queries the
**live tier first and the historical tier second** — if a market crosses the
boundary mid-enumeration, that order returns it twice (detectable) rather than
never (invisible).

### A-49 `/historical/markets` returns negative top-of-book sizes — OBSERVED (live, 2026-09-22)

Archived markets carry **negative** `yes_bid_size_fp` and `yes_ask_size_fp`:

```
"status": "finalized", "result": "no",
"yes_bid_size_fp": "-19.00", "yes_ask_size_fp": "-389.00",
"volume_fp": "2561.00", "open_interest_fp": "0.00"
```

Residual book-state fields on a market that no longer has a book. Never observed
from the live `/markets` endpoint, including with `status=settled` (200 markets
sampled, zero negatives).

A negative contract count is not a quantity, so it cannot be parsed as one — and
it must not be coerced to `0`, because zero is a real, tradeable answer that
would make an archived market look like a live one with an empty book. The raw
value is moved to `KalshiMarket.quote_size_anomalies` and the field itself reads
absent, which is what it truthfully is. The carve-out is scoped to the four
top-of-book size fields; a negative `volume_fp` or `open_interest_fp` is still
fatal.

This was not a theoretical concern: before the carve-out, two of 40 sampled
events could not be enumerated at all, because one bad page failed the whole
walk and a failed walk is indistinguishable from an empty event.

### A-50 The nested event view returns exactly the live tier — OBSERVED (live, 2026-09-22)

Across 94 stratified events, the market set from
`GET /events/{E}?with_nested_markets=true` equalled the live-tier set from
`GET /markets?event_ticker=E` in **94/94** cases, exactly.

| Measure | Count |
| --- | --- |
| events sampled (stratified by category and status) | 94 |
| both paths exhausted | 94 |
| union members | 1,108 |
| members the nested view omitted | 307 (27.7%) |
| events with at least one omission | 43 (46%) |
| members the nested view had but neither tier did | **0** |

So the nested view's omissions are precisely the archived markets, and reading
it alone under-reports membership on nearly half of events. This upgrades the
mechanism behind A-46 from inference to measurement — and A-46's *conclusion*
is unchanged, because completeness of a market list was never the same question
as exhaustiveness of outcomes.

### A-51 A provisional market may be removed entirely — DOCUMENTED

`Market.is_provisional`:

> If true, the market may be removed after determination if there is no
> activity on it.

Membership can therefore **shrink**, not only grow. A removed market is in
neither the live tier nor the historical tier, so no amount of successful
enumeration proves that `live + historical` is every market the event ever had.

This is the structural reason the membership object is named
`CombinedVenueMembershipEvidence` and not a completeness certificate. It records
which paths were exhausted under which cutoff; it does not claim the result is
complete.

### A-52 `mutually_exclusive` is an upper bound only — DOCUMENTED

`EventData.mutually_exclusive`:

> If true, only one market in this event can resolve to 'yes'. If false,
> multiple markets can resolve to 'yes'.

The contrast between the two halves fixes the meaning: the flag bounds the
**maximum** number of YES resolutions. It says nothing about the minimum, and
the word is "can", not "must".

`mutually_exclusive = true` is therefore evidence toward `AT_MOST_ONE` and
**no evidence at all** toward `AT_LEAST_ONE`. An event may be mutually exclusive
and still settle with every market NO — no candidate qualifying, the event not
occurring, cancellation, or an outcome Kalshi never listed a market for.

OBSERVED: 29 of 94 sampled events carry `mutually_exclusive = true`, and all 29
have at least two members.

### A-53 Structured fields that bear on exhaustiveness — DOCUMENTED meanings, OBSERVED values

Documented in the current OpenAPI spec and preserved verbatim:

| Field | Documented meaning | Supports membership? | Mutual exclusion? | Exhaustiveness? |
| --- | --- | --- | --- | --- |
| `mutually_exclusive` | "only one market … can resolve to 'yes'" | no | **yes** | **no** (A-52) |
| `strike_type` | enum `greater`, `greater_or_equal`, `less`, `less_or_equal`, `between`, `functional`, `custom`, `structured` | no | no | **supports a structural argument** |
| `floor_strike` | "Minimum expiration value that leads to a YES settlement" | no | no | **supports a structural argument** |
| `cap_strike` | "Maximum expiration value that leads to a YES settlement" | no | no | **supports a structural argument** |
| `functional_strike` | "Mapping from expiration values to settlement values" | no | no | no — opaque string |
| `custom_strike` | "Expiration value for each target that leads to a YES settlement" | no | no | no — opaque object |
| `market_type` | enum `binary`, `scalar` | no | no | no (A-45) |
| `collateral_return_type` | "how collateral is returned when markets settle" | no | no | no — values not enumerated |
| `product_metadata` | "Additional metadata for the event" — no schema | no | no | **no** |
| `primary_participant_key` | undocumented meaning | no | no | no |
| `is_provisional` | market may be removed | **negatively** (A-51) | no | no |

Only `strike_type` with `floor_strike`/`cap_strike` carries documented,
machine-readable semantics that can *support* an exhaustiveness argument, by
showing a set of intervals is gap-free over a domain. It cannot complete one:
the domain itself, and whether the underlying can be undefined, cancelled or
void, live in the contract prose. The interval helper therefore reports coverage
and never issues anything.

`product_metadata` is typed `object` with no schema at all. An undocumented
field cannot carry a settlement guarantee, so nothing is inferred from its keys.

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
- https://docs.kalshi.com/api-reference/events/get-events
- https://docs.kalshi.com/api-reference/historical/get-historical-markets
- https://docs.kalshi.com/api-reference/historical/get-historical-cutoff-timestamps
- https://docs.kalshi.com/getting_started/historical_data
- https://docs.kalshi.com/openapi.yaml — spec version 3.30.0, retrieved 2026-09-22
- https://docs.kalshi.com/api-reference/exchange/get-series-fee-changes
- Live production API responses, 2026-09-15 (see inline quotes above)
- https://kalshi.com/docs/kalshi-fee-schedule.pdf — effective 2026-07-07; still
  HTTP 429 to automated fetch from this environment, values above verified by
  independent review
