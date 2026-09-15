# Architecture

## 1. Shape

A **modular monolith**: one Python package, one process per job (capture, scan,
replay), one database. No Kafka, no Kubernetes, no microservices. The research
question is whether provable inconsistencies exist at all; distributed
infrastructure would add failure modes without adding evidence.

The suggested layout in the Phase 1 brief is adopted essentially unchanged. Two
deviations are noted in §7, both small and both justified there.

## 2. Dependency rule

Dependencies run strictly one way. This is the structural property that makes
live and replay share code.

```
                    ┌─────────────┐
                    │   domain    │   money, enums, models, payoff
                    └──────▲──────┘   depends on NOTHING
                           │
        ┌──────────────────┼──────────────────┐
        │                  │                  │
  ┌─────┴─────┐      ┌─────┴─────┐      ┌─────┴─────┐
  │  venues   │      │   books   │      │ semantics │
  │ (kalshi)  │      │           │      │           │
  └─────▲─────┘      └─────▲─────┘      └─────▲─────┘
        │                  │                  │
  ┌─────┴─────┐            │                  │
  │  ingest   │            └────────┬─────────┘
  └─────▲─────┘                     │
        │                    ┌──────┴──────┐
        │                    │  detectors  │  PURE. no I/O.
        │                    └──────▲──────┘
        │                           │
        │                    ┌──────┴──────┐
        └────────────────────┤opportunities│
                             └──────▲──────┘
                    ┌───────────────┴───────────────┐
              ┌─────┴─────┐                   ┌─────┴─────┐
              │  replay   │                   │    cli    │
              └───────────┘                   └───────────┘

  storage is a leaf used by ingest / opportunities / semantics.
  It is never imported by domain, books or detectors.
```

Rules enforced by review (and, later, an import-linter test):

1. `domain` imports nothing from the rest of the package.
2. `detectors` perform **no I/O** — no HTTP, no database, no clock reads of
   their own. They receive a book snapshot, a relation set, a fee schedule and
   a `Clock`.
3. Venue-specific representations never escape `venues/`. A detector cannot
   tell it is looking at Kalshi.

Rule 2 is the important one. It is what allows `scan-live` and `replay` to call
the identical function, which is an acceptance criterion rather than a
convenience.

## 3. Data flow

### Capture (write path)

```
Kalshi REST ──┐
              ├──▶ ingest ──▶ raw_journal (append-only Parquet)
Kalshi WS ────┘                    │
                                   ├──▶ books/reconstruction ──▶ BookState
                                   └──▶ storage (Postgres catalogue)
```

The raw journal is written **before or alongside** normalisation, never after.
If normalisation has a bug, the bytes are still there and the capture is not
lost.

### Detection (read path)

```
BookState ─┐
relations ─┼──▶ detectors ──▶ candidates ──▶ payoff engine ──▶ opportunities
fees ──────┘                                       │
                                                   └── worst-case payoff
```

### Replay

```
raw_journal ──▶ replay/engine ──▶ books/reconstruction ──▶ detectors ──▶ opportunities
                     │                                          ▲
                     └── FrozenClock, as-of metadata/fees/relations
```

Replay substitutes the *sources*, never the *logic*. The engine's only extra
responsibility is refusing lookahead: it resolves metadata, fee schedules and
relations as of the simulated timestamp, and it does not let future book
updates be visible.

## 4. Module responsibilities

| Module | Owns | Phase 1 status |
| --- | --- | --- |
| `domain/money` | `Price`, `Quantity`, `Money`; exact arithmetic | **Implemented** |
| `domain/enums` | Closed vocabularies, status axes | **Implemented** |
| `domain/models` | Canonical event, proposition, settlement spec, instrument, relation | Planned |
| `domain/payoff` | Settlement-state enumeration, worst-case payoff | Planned |
| `clock` | UTC time, three-timestamp provenance, injectable clock | **Implemented** |
| `config`, `logging` | Settings, structured logging | **Implemented** |
| `venues/base` | Venue protocol | Planned |
| `venues/kalshi/*` | Auth, REST, WS, wire models, normalisation, fees | Planned |
| `ingest/*` | Metadata sync, raw journal, book collector | Planned |
| `books/*` | Levels, book state, sequence-correct reconstruction, execution depth | Planned |
| `semantics/*` | Propositions, settlement specs, relations, registry | Planned |
| `detectors/*` | `binary_complement`, `group_basket` | Planned |
| `opportunities/*` | Opportunity record, persistence, audit rendering | Planned |
| `storage/*` | SQLAlchemy models, repositories, Parquet writers | Planned |
| `replay/engine` | Point-in-time replay | Planned |
| `cli/main` | Typer commands | Planned |

## 5. Decisions worth explaining

### 5.1 Scaled integers rather than `Decimal` for money

To be clear about what this is *not*: `Decimal` is exact, and with explicit
contexts it is perfectly reproducible. This is not a claim that `Decimal` is
nondeterministic.

The choice is driven by the shape of the data. Kalshi documents prices to four
decimal places and quantities to two, so their product naturally lands at six.
Those three scales are fixed and known ahead of time, which makes scaled
integers the closer fit:

- **Exactness is structural, not contextual.** Every operation is plain integer
  arithmetic, so there is no precision or rounding mode to configure correctly
  at each call site.
- **Invalid precision is easy to reject.** A five-decimal price fails to convert
  to an integer number of `$0.0001` units, so it raises instead of silently
  rounding to a valid-looking off-tick price. With `Decimal` the same check is
  possible but must be remembered and written out every time.
- **Serialisation is unambiguous.** One integer has exactly one wire
  representation; `Decimal("0.42")` and `Decimal("0.4200")` compare equal but
  render differently.

`Decimal` is still used at the parse/serialise boundary to turn an exact decimal
string into an exact integer, and for genuinely real-valued intermediates such
as `raw_model_fee` (§5.7).

The scales were chosen against the *observed* API (see `docs/api_assumptions.md`
A-04, A-05, A-13):

```
price     4 dp  →  units of $0.0001
quantity  2 dp  →  units of 0.01 contracts
money     6 dp  →  units of $0.000001
```

with the invariant

```
PRICE_SCALE * QUANTITY_SCALE == MONEY_SCALE
```

This is the load-bearing detail. Because `1e-4 * 1e-2 == 1e-6` exactly,
`price * quantity` is one integer multiplication with **no rounding at all**,
and Kalshi's documented `ceil_6dp` fee rounding is exactly "round up to one
`Money` unit". Gross notional is never approximated, and the only rounding
anywhere in the pipeline is rounding the exchange itself documents.

Floats are rejected at the boundary with a loud, specific exception rather than
being coerced: a float arriving here means someone did inexact arithmetic
upstream, and the value can no longer support an arbitrage claim.

### 5.2 Detectors are pure functions

Required by the acceptance criterion that live and replay share logic. It also
makes the deterministic test suite possible: a detector's output is a function
of its inputs, so synthetic fixtures are exhaustive rather than indicative.

The cost is that all I/O moves to the edges, and detectors take more arguments.
That is the right trade here.

### 5.3 Fail-closed defaults

Every unknown resolves to the choice that *cannot* produce a false positive:

- unknown settlement → `SettlementKind.UNKNOWN`, excluded
- sequence gap → book marked integrity-unknown, excluded
- unverified relation → cannot reach a contractual-arbitrage detector
- unverified fee constants → no `STATE_INDEPENDENT_PROFIT` claim at all
- unknown collateral netting → capital computed un-netted (conservative)

The asymmetry is deliberate. A missed opportunity costs nothing; a false one
costs real money on a trade that was never riskless.

### 5.4 Bitemporal versioning everywhere

Every version-bearing table carries `valid_from` / `valid_to`. Replay asks
"what did we know at time T", which is a different question from "what do we
know now", and the second question is the one that produces lookahead bias.

Rules content is hashed. A relation stores the rules hashes it was proven
against; when a hash changes, the relation's proof is invalidated and it
returns to `REVIEW_REQUIRED` automatically rather than quietly continuing to
authorise alerts.

### 5.5 Append-only raw journal

Historical source messages are never mutated. Corrections are new records.
This is what makes an opportunity auditable years later: the claim can be
recomputed from the bytes that produced it.

High-volume book data goes to partitioned Parquet (read back with Polars and
DuckDB); the structured catalogue goes to Postgres. Splitting by access pattern
rather than by service keeps the operational surface small.

### 5.6 Monotonic clock for staleness

Staleness gates safety decisions, so it is measured with a monotonic source.
The wall clock can step backwards under NTP correction, which would
*under-report* book age and let a stale book through. The `Clock` protocol
exposes both readings and replay supplies a `FrozenClock` driven by journal
timestamps.

### 5.7 Fees are modelled as five quantities, not one

The fee schedule defines a **model fee**; the API defines a separate
account-level process on top of it (A-13, A-14). The pipeline keeps
`raw_model_fee`, `trade_fee`, `rounding_fee`, `rebate` and `net_fee` distinct.

`raw_model_fee` is the one value in the system that is deliberately `Decimal`
rather than `Money`: `M * 0.07 * C * P * (1 - P)` routinely produces more than
six decimal places, and the documented worked example uses eight. It becomes
money exactly once, at the documented `ceil_6dp` step, through
`Money.ceil_from_decimal()` — which is why `Money.from_value()` still rejects
over-precise input everywhere else.

The rounding accumulator is **per order**, not per fill, so `rounding_fee` and
`rebate` are properties of a fill sequence. A per-leg fee number cannot
represent them, which is the concrete reason the five are not collapsed.

## 6. Build order

Each step is independently testable, and the ordering is chosen so that
correctness infrastructure exists before anything that could produce a claim.

1. ✅ **Foundations** — money, enums, clock, config, logging, tooling
2. **Wire models + normalisation** — Kalshi Pydantic models mirroring the
   observed schema exactly; golden tests against captured real payloads
3. **REST client + auth** — signing, rate-limit budgeting, metadata sync
4. **Raw journal** — append-only Parquet with full provenance
5. **Book reconstruction** — snapshot/delta, sequence integrity, derived levels
6. **Execution-depth engine** — VWAP, breakpoints, limiting leg
7. **Fee engine** — versioned schedules; *blocked on A-14*
8. **Settlement specs + relations registry** — with the verification workflow
9. **Payoff engine** — state enumeration, worst-case
10. **Detectors** — binary complement, then baskets
11. **Opportunity record + audit CLI**
12. **Replay engine + lookahead tests**
13. **Live capture, then the research questions**

Steps 2–6 are the ones that determine whether any later number is trustworthy,
which is why the fee engine — the most visible piece — is deliberately seventh.

## 7. Deviations from the brief's suggested layout

Both are small; the brief invited alternatives with justification.

1. **`domain/payoff.py` is a package boundary, not just a module.** The brief
   asks that enumeration be replaceable by LP/MILP/SAT/SMT later without
   changing the rest of the application. That means the payoff engine's public
   surface must be a protocol (`PayoffEngine`) with an enumeration
   implementation behind it, rather than a module of functions. Same file, but
   the interface is designed for substitution from the start.

2. **`books/execution.py` returns a breakpoint *schedule*, not a single fill.**
   The brief asks for max profitable size, max fillable size and the limiting
   leg. Those are properties of a curve, not of one quantity, so the engine's
   primary return type is the schedule of quantity breakpoints and the
   single-quantity query is derived from it. This avoids re-walking the book
   once per candidate size.

Everything else follows the suggested structure.
