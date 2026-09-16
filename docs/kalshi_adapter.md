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

---

# Transport layer (Step 3)

## 10. Read-only by construction

Phase 1 must be structurally incapable of submitting an order, not merely
missing the code. Three independent layers enforce it:

1. **No public write surface.** `KalshiReadOnlyClient` exposes named GET
   operations only. There is no public `request(method, path, ...)` that would
   make a POST to `/portfolio/orders` a one-liner. A test enumerates every
   public method and fails on anything that is not a read, a constructor or
   `aclose`.
2. **The transport refuses non-read methods.** `_Transport` is private and
   raises `ReadOnlyViolationError` for anything outside `{GET, HEAD}`. Adding a
   write path requires deliberately disabling that guard — a visible, reviewable
   act rather than an oversight.
3. **Retries know the difference.** `RetryPolicy.is_retryable` checks the HTTP
   method, not just the error type. It will not become a generic "retry
   anything" primitive that a future write path inherits by accident.

A key with **write scope changes none of this**. Read scope is sufficient and
preferred; write scope simply goes unused.

## 11. Authentication

One signing primitive serves REST and the WebSocket, differing only in the path
they sign, so the two cannot drift apart.

```
message = str(timestamp_ms) + METHOD + path      # query stripped, no hostname
signature = base64(RSA-PSS-SHA256-MGF1(message)) # salt length = digest length
```

Headers: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP`,
`KALSHI-ACCESS-SIGNATURE`.

`signing_path()` is the **only** place a signing path is built, because getting
it wrong yields a signature valid for a message the server never computes — and
the resulting 401 is indistinguishable from a bad key:

```
https://external-api.kalshi.com/trade-api/v2/markets?limit=100&cursor=abc
  signs as  /trade-api/v2/markets
  NOT       /markets
  NOT       /trade-api/v2/markets?limit=100&cursor=abc
```

The WebSocket handshake signs the **fixed** path `/trade-api/ws/v2` with method
`GET`, regardless of environment.

**Clock.** The signer takes a `Clock` rather than reading the wall clock, which
makes signing deterministic in tests and leaves a seam for future skew handling.

**Secrets.** The private key loads from a filesystem path and is never
serialised, printed, or placed in an exception. `KalshiCredentials`,
`KalshiSigner`, `AuthHeaders` and the client all override `__repr__`.
`redact_headers()` masks the three auth headers plus `Authorization`/`Cookie`,
and errors never carry request headers at all. Key-file permissions are checked
and **warned** about, never enforced — permission bits mean different things
across platforms, and a false refusal to start would be worse than a missing
warning.

## 12. Authentication failure causes

Separated because the remedies are completely different:

| Error | Meaning | Fix |
| --- | --- | --- |
| `KalshiAuthenticationError` | 401, cause unclear | Check key id, signing path, environment |
| `KalshiClockSkewError` | 401 where the venue's text mentions the timestamp | Check local clock sync |
| `KalshiAuthorizationError` | 403 | Key lacks read scope for this endpoint |

No tolerance window is asserted anywhere: current documentation states none, and
claiming a number would be inventing a guarantee. Classification is driven only
by the venue's own wording.

## 13. Rate limiting — discovered, not hardcoded

`GET /account/limits` gives `usage_tier` and separate read/write buckets
(`refill_rate`, `bucket_capacity`); `GET /account/endpoint_costs` gives
`default_cost` plus per-endpoint overrides. Both are authoritative and
per-account.

`10` is the *current* default cost, not a constant. An account on another tier,
or a change to one endpoint's cost, would silently invalidate a hardcoded value.

Read and write budgets are modelled separately because the venue separates them.
Phase 1 only ever spends from the read bucket, but representing both prevents a
future write path from quietly drawing on the read budget.

Timing uses the clock's **monotonic** reading. The wall clock can step backwards
under NTP correction, which would make the bucket believe it had refilled.

**Unauthenticated access does not get an invented budget.** The discovered
numbers describe the authenticated account, and nothing documents that they
describe anonymous traffic (A-29). `ConservativePolicy` uses bounded concurrency
plus a politeness interval and relies on backoff — honest about what is unknown.

## 14. Retries

Bounded exponential backoff with jitter. Kalshi's 429 carries no penalty and no
`Retry-After` (A-30), so backing off and retrying is the documented-correct
response; a `Retry-After` is honoured if one ever appears.

**Every authenticated retry re-signs.** The timestamp is inside the signed
message, so reusing headers would present the server with a stale — eventually
replayed — timestamp. This is tested by advancing a clock across a retry and
asserting both the timestamp and the signature changed.

## 15. Pagination

Streams by default: a metadata sync covers tens of thousands of markets, and
materialising every page first wastes memory and delays all work.

Safety properties, because an opaque cursor is untrusted input:

- a repeated cursor raises `CursorLoopError` rather than looping;
- `max_pages` / `max_items` **raise** rather than truncating — a partial
  metadata sync that reports success would leave the catalogue quietly
  incomplete;
- an empty-string cursor terminates, exactly as an absent one does. Kalshi has
  been observed returning `""`, and treating that as a page would re-request the
  first page forever.

## 16. Logging

Logged: method, endpoint **template** (`/markets/{ticker}/orderbook`, so errors
aggregate), status, latency, retry count, correlation id, public tickers.

Never logged: the private key, signatures, auth headers, or raw
credential-bearing requests. Note that enabling `httpx`/`httpcore` DEBUG logging
*directly* can still print request headers — predarb's own logging paths never
do, and `safe_request_log_fields()` is the sanctioned way to log a request.

---

# WebSocket sequencing (A-09 resolved)

## 17. Market discovery

Finding markets that emit order-book traffic is not a one-liner. The default
`GET /markets` listing is **~100% combo markets** (29,998 of 30,000 measured;
first non-combo at position 16,772), and combos almost never quote. A scan that
pages the listing and skips combos evaluates zero candidates and concludes "no
active markets" — a statement about listing order, not about the exchange.

`market_discovery.py` enumerates series and queries markets per series instead,
ranking by 24-hour volume, then open interest, then currently quoted size, with
a penalty for markets minutes from close unless they are very busy. The same
scan that found nothing before finds **1,222** quoting markets.

A single quoted side is sufficient: `no_bid_size_fp` is routinely absent from
list responses even on heavily traded markets (A-34), so requiring both sides
selects nothing.

## 18. What `seq` actually is

Resolved by live experiment over 2,764 frames (A-09):

**`seq` is scoped per `sid`, and is dense within it.** 2,752 adjacent pairs, all
advancing by exactly one, zero skips, duplicates or decreases. Every other
candidate scope — connection-global, per-market, per-(sid, market) — shows
violations on the same data.

Facts a reconstructor has to respect:

| Fact | Consequence |
| --- | --- |
| One subscription covers many markets under one `sid` | A gap could belong to any of them |
| Two subscribes to the same channel **merge** into one sid | Distinct sids need distinct channels |
| Each sid starts at `seq = 1` independently | Never compare `seq` across sids |
| `seq` restarts at 1 after reconnect | Sequence state is per-connection; discard it on disconnect |
| Snapshots take the next number in the stream | A snapshot at seq 250 was observed after `add_markets`; a snapshot is not a reset |
| `ok` control frames carry `sid`/`seq` (documented) | Sequence must be checked **before** type routing, or control frames read as phantom gaps |
| `ticker` frames carry no `seq` | Pass through without advancing the counter |

## 19. The Step 4 invariant

> Track `seq` per `(connection, sid)`. Every frame carrying a `seq` must equal
> `expected_seq`. A skip, repeat or decrease invalidates **every book belonging
> to that sid** — not just the market named in the frame — because the counter
> is shared and the hole could have carried any market. Invalidated books become
> `INTEGRITY_UNKNOWN`, are excluded from scanning, and are restored only by a
> fresh snapshot.

This replaces the earlier tentative `(sid, market_ticker)` design, which the
evidence contradicts: per-market grouping shows 6 skips on healthy data and
would fire spurious gap alerts.

Where it still fails closed: density is **observed, not guaranteed**. Nothing in
the documentation promises it, wrap-around behaviour is unknown, and only two
channels were confirmed to number their sids this way. The invariant treats any
deviation as loss rather than assuming the exchange is well-behaved.

## 20. Fixtures: real vs synthetic

| Directory | Contents |
| --- | --- |
| `rest/` | REAL — unmodified production REST bodies |
| `websocket_real/` | REAL — unmodified production WebSocket frames from the A-09 run |
| `websocket/` | SYNTHETIC — documentation-derived, retained only for cases not observed live |

Real frames are captured by `tools/ws_sequence_experiment.py`, which clears the
directory at the start of each run: two runs writing the same filenames would
interleave frames from different sessions into one apparent stream, which looks
exactly like a sequence violation and would corrupt the evidence. Only message
bodies are stored; handshake headers never reach the writer.

The synthetic `subscribed` acknowledgement was the lowest-confidence guess in
Step 2. The real frames confirm its shape — `sid` does live inside `msg` rather
than on the envelope.
