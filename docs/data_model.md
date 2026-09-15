# Data Model

## 1. Why the exchange listing is not the fundamental object

A Kalshi market ticker is a *venue's representation* of a claim about the
world. Two tickers can express the same claim; one ticker's meaning can change
when its rules are amended. If the listing is the fundamental object, then
"are these two markets the same?" becomes a string-similarity question, and
string similarity is exactly what must never authorise a trade.

So the model separates four layers:

```
        CanonicalEvent          "the 2026 NATO Secretary General selection"
              │                  a real-world occurrence
              ▼
         Proposition            "X is selected"
              │                  a truth-apt claim, normalised
              ▼
       SettlementSpec           "per these rules, per this source, as of this version"
              │                  how the claim is adjudicated, and when
              ▼
       VenueInstrument          KXNEXTNATOSECGEN-99-X on Kalshi
                                 a tradeable contract with a book
```

Relations are asserted between **propositions**, not between tickers. That is
what makes them portable across venues in later phases, and what forces the
question "do these mean the same thing?" to be answered explicitly.

---

## 2. Entities

### 2.1 CanonicalEvent

A real-world occurrence with a measurement window.

| Field | Notes |
| --- | --- |
| `event_id` | Internal UUID. Never a venue ticker. |
| `category`, `event_type` | Our taxonomy, not the venue's |
| `canonical_entities` | Normalised entity references (people, teams, tickers) |
| `start_time`, `end_time` | Aware UTC |
| `measurement_window_start/end` | Often narrower than start/end |
| `timezone` | The *contract's* timezone — a "daily high in NYC" market settles on local calendar days, and using UTC would silently shift the window |

### 2.2 Proposition

A truth-apt claim, normalised where possible.

| Field | Notes |
| --- | --- |
| `proposition_id` | Internal UUID |
| `event_id` | → CanonicalEvent |
| `subject` | Canonical entity |
| `predicate` / `metric` | e.g. `high_temperature` |
| `comparator` | `>`, `>=`, `<`, `<=`, `between`, `custom` |
| `threshold`, `unit` | Exact values; never floats |
| `measurement_window` | May narrow the event's window |
| `normalized_form` | Structured representation where mechanically derivable |
| `normalization_status` | `NORMALIZED` / `MANUAL` / `UNNORMALIZABLE` |

Kalshi's `strike_type` (A-19) maps onto `comparator`. `functional` and `custom`
strikes are not mechanically normalisable and are marked `UNNORMALIZABLE`; they
may still be traded manually but cannot be auto-matched.

### 2.3 SettlementSpec

**Versioned.** How a proposition is adjudicated. This is where fail-closed
lives.

| Field | Notes |
| --- | --- |
| `settlement_spec_id`, `version` | |
| `settlement_kind` | `BINARY` / `SCALAR` / `UNKNOWN`. Only `BINARY` is arbitrage-eligible |
| `settlement_sources`, `source_urls` | From the venue's `settlement_sources` |
| `measurement_start`, `measurement_end` | |
| `determination_time`, `expected_payout_time` | |
| `resolver` | Who determines it |
| `dispute_mechanism` | Kalshi has `disputed` and `amended` statuses (A-17) |
| `tie_behavior` | |
| `cancellation_behavior`, `postponement_behavior`, `void_behavior` | |
| `revision_behavior` | What happens if the source revises its data |
| `possible_settlement_values` | Explicit enumeration. Empty ⇒ `UNKNOWN` |
| `payout_currency`, `maximum_payout` | `maximum_payout` from `notional_value_dollars` (A-18) |
| `raw_rules_hash` | SHA-256 of the exact rules text |
| `valid_from`, `valid_to` | Bitemporal |

**Default is `UNKNOWN`.** A settlement spec becomes `BINARY` only when someone
has read the rules and enumerated the possible settlement values. A market
whose spec is `UNKNOWN` cannot appear in a contractual-arbitrage claim.

Note that `revision_behavior` is not hypothetical: the live `KXHIGHNY` series
carries product metadata describing exactly what happens when the source
publishes a materially erroneous value and later revises it.

### 2.4 VenueInstrument

The tradeable contract.

| Field | Notes |
| --- | --- |
| `venue`, `instrument_ticker` | |
| `venue_event_id`, `venue_series_id` | |
| `proposition_id` | **Nullable.** Set only when the mapping is verified |
| `yes_semantics`, `no_semantics` | From `yes_sub_title` / `no_sub_title` |
| `price_ranges` | Tiered tick sizes (A-04) — per-market, never assumed |
| `minimum_quantity` | |
| `notional_value_dollars` | Per-contract payout (A-18) |
| `open_time`, `close_time`, `expiration_time` | |
| `fee_schedule_id` | → FeeSchedule effective at a point in time |
| `settlement_spec_id` | → SettlementSpec version |
| `status` | Venue status (A-17) |
| `is_excluded`, `exclusion_reason` | MVE, provisional, non-binary (A-20) |

`proposition_id` being nullable is the point: an instrument with no verified
proposition can be captured, stored and charted, but cannot participate in a
relation and therefore cannot produce an arbitrage claim.

### 2.5 Relation

**Versioned.** An assertion about a set of propositions.

| Field | Notes |
| --- | --- |
| `relation_id`, `version` | |
| `relation_type` | `EQUIVALENT`, `AT_MOST_ONE`, `AT_LEAST_ONE`, `EXACTLY_ONE` (+ reserved `IMPLIES`, `DISJOINT`, `PARTITION`) |
| `members` | Ordered proposition ids |
| `verification_status` | `VERIFIED` / `REVIEW_REQUIRED` / `REJECTED` |
| `evidence` | Free text: the quoted rules language that justifies it |
| `rules_hashes` | Hash per member at proof time |
| `verified_by`, `verification_method` | Human identifier; `MANUAL` in Phase 1 |
| `verified_at` | |
| `valid_from`, `valid_to` | Bitemporal |

**Invalidation.** Each member's rules hash is stored at proof time. If any
member's current hash differs, the relation is automatically demoted to
`REVIEW_REQUIRED`. Proof was against specific rules text; when the text
changes, the proof has not been shown to survive.

### 2.6 FeeSchedule

**Versioned.** Fee parameters, never constants in code.

| Field | Notes |
| --- | --- |
| `fee_schedule_id`, `version` | |
| `scope` | `SERIES` or `EVENT` (events override series — A-11) |
| `scope_ticker` | |
| `fee_type` | `quadratic`, `quadratic_with_maker_fees`, `quadratic_with_combo_maker_fees`, `flat` |
| `fee_multiplier` | Exact decimal |
| `formula_parameters` | The constants for `fee_type` — taker `0.07`, maker `0.0175` for the quadratic family (A-14) |
| `rounding_rule` | `ceil_6dp` plus the accumulator/rebate mechanics (A-13) |
| `source`, `source_hash` | Provenance for the constants (currently: fee schedule effective 2026-07-07) |
| `constants_verified` | Boolean. False blocks any profit claim |
| `effective_from`, `effective_to` | From `scheduled_ts` (A-12) |

`constants_verified` is **true** for the general schedule as of 2026-09-15
(A-14). It remains a field rather than an assumption because a series on a
non-standard multiplier, or a future schedule revision, must be able to mark
itself unverified and re-block profit claims without a code change.

#### Fees are five concepts, not one

The fee schedule defines a *model fee*; the API defines an account-level
process on top of it. Flattening them loses the ability to reconcile against a
real fill:

| Concept | Type | Meaning |
| --- | --- | --- |
| `raw_model_fee` | `Decimal` | `M * rate * C * P * (1 - P)`. A real-valued intermediate — **not** money |
| `trade_fee` | `Money` | `ceil_6dp(raw_model_fee)`; charged on the fill |
| `rounding_fee` | `Money` | Realigns the balance to the member's precision grid |
| `rebate` | `Money` | Returned from the per-order accumulator |
| `net_fee` | `Money` | `trade_fee + rounding_fee - rebate`, floored at zero |

`raw_model_fee` is `Decimal` because it routinely exceeds six decimal places;
it becomes `Money` exactly once, at the documented ceiling step, via
`Money.ceil_from_decimal()`. The accumulator is **per order**, so `rounding_fee`
and `rebate` are properties of a fill sequence, not of a single leg — which is
why they cannot be folded into a per-leg fee number.

### 2.7 RawMessage (append-only)

| Field | Notes |
| --- | --- |
| `raw_message_id` | |
| `venue`, `endpoint_or_channel` | |
| `exchange_timestamp` | Nullable — Kalshi omits `ts_ms` on some messages |
| `received_timestamp`, `processed_timestamp` | Always present |
| `ws_session_id`, `subscription_id` | `sid` |
| `sequence_number` | `seq`, nullable |
| `payload` | Exact bytes |
| `payload_hash` | SHA-256 |
| `collector_version`, `schema_version` | |

Partitioned Parquet by `(venue, date, channel)`. **Append-only**: no update or
delete path exists in the repository layer, so a correction is a new record.

### 2.8 BookState and levels

| Field | Notes |
| --- | --- |
| `instrument_ticker`, `sid` | |
| `last_sequence` | |
| `integrity` | `VALID` / `GAP_DETECTED` / `AWAITING_SNAPSHOT` / `STALE` |
| `yes_bid_levels`, `no_bid_levels` | Quoted |
| `derived_ask_levels` | `derived=True` + source level reference |
| `snapshot_message_id`, `last_update_message_id` | Audit trail |
| `last_update_received_at` | Drives book age |

Only `integrity == VALID` and age within budget is scannable. The quoted vs
derived distinction is never lost (A-06): a derived YES ask is an arithmetic
consequence of a NO bid, and consuming it consumes that bid's depth.

### 2.9 Opportunity

Stores everything needed to recompute the claim exactly. Per §14 of the brief:
identifiers, relation and rules-version ids, fee-schedule ids, source message
ids per book, every leg with levels consumed and VWAP, fees per leg, all-in
cost, payoff in **every** valid state, worst-case payoff, contractual profit,
capital required (netted and un-netted — A-16), return on capital, settlement
timing, oldest input age, warnings, and the three independent status axes.

---

## 3. Versioning and point-in-time reads

Every version-bearing table is bitemporal. All reads on the replay path go
through an as-of accessor:

```python
repo.get_fee_schedule(series_ticker, as_of=simulated_time)
repo.get_relation(relation_id, as_of=simulated_time)
repo.get_settlement_spec(instrument, as_of=simulated_time)
```

There is deliberately **no** "current" accessor available to detector code.
Removing the convenient wrong call is more reliable than remembering not to
make it, and the lookahead tests assert that a record created after the
simulated timestamp is invisible.

---

## 4. Storage split

| Store | Holds | Why |
| --- | --- | --- |
| **PostgreSQL** | Catalogue and state: instruments, propositions, settlement specs, relations, fee schedules, opportunities | Relational integrity, bitemporal queries, Alembic migrations |
| **Parquet** (partitioned) | Raw journal, book snapshots | Volume; columnar scan for research |
| **DuckDB** | Offline analysis over Parquet | Zero-copy SQL for the research questions |

The split is by access pattern, not by service. Everything runs in one process
group.

---

## 5. Identifier conventions

- Internal ids are UUIDv7 — time-ordered, so they sort usefully in the journal.
- Venue tickers are stored verbatim, and are never used as primary keys: they
  are a venue's namespace, and a ticker can be reused.
- Hashes are SHA-256 hex.
- All timestamps are `TIMESTAMPTZ`, always UTC.
