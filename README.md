# Prediction-Market-Arbitrage-Scanner

A research system for finding and **proving** mechanically provable pricing
inconsistencies on prediction markets.

Phase 1 is **Kalshi only** and **read-only**. It exists to answer an empirical
question, not to assert an answer:

> Do simple, mechanically provable pricing inconsistencies appear on Kalshi at
> genuinely executable prices, after fees, depth and settlement rules?

"We observed zero genuine executable arbitrages" is a valid and acceptable
result. The system is built so that such a finding would be trustworthy.

## What this system will not do

- It does not place, modify or cancel orders. There is no execution code path
  and no brokerage dependency. This is a hard Phase 1 boundary.
- It does not infer that two markets are related because their titles look
  similar. Logical relations are human-verified and version-pinned.
- It does not treat mutual exclusivity as exhaustiveness. They are different
  properties and are modelled separately.
- It does not label something arbitrage unless the portfolio's worst-case
  payoff, over every valid settlement state, exceeds the all-in executable
  acquisition cost including fees.

## Core distinctions

The system keeps four categories apart, and never blurs them:

| Category | Requires |
| --- | --- |
| **True contractual arbitrage** | Verified relation + provable payoff in every state + executable depth + fees |
| **Statistical mispricing** | A model. Out of scope in Phase 1. |
| **Stale-market opportunity** | A book we can show was stale. Reported as a data-quality finding, not an edge. |
| **Relative value** | A view. Out of scope in Phase 1. |

It also keeps *payoff certainty* separate from *execution certainty*. A
portfolio can have a guaranteed profit once all legs are filled and still be
exposed to race risk while those legs are being acquired. These are two
independent status axes on every opportunity record.

## Documentation

| Document | Contents |
| --- | --- |
| [docs/architecture.md](docs/architecture.md) | Module layout, dependency rules, data flow, live/replay symmetry |
| [docs/data_model.md](docs/data_model.md) | Canonical event → proposition → settlement spec → venue instrument, relations, versioning |
| [docs/arbitrage_definitions.md](docs/arbitrage_definitions.md) | Formal definitions, the four opportunity structures, and the traps |
| [docs/api_assumptions.md](docs/api_assumptions.md) | Verified Kalshi API findings, discrepancies, and open questions |
| [docs/kalshi_adapter.md](docs/kalshi_adapter.md) | Wire schema, fixed-point parsing, forward-compatibility policy, normalisation |
| [docs/reconstruction.md](docs/reconstruction.md) | Sequence invariant, book state machine, recovery, raw journal |
| [docs/execution.md](docs/execution.md) | Derived asks, executable depth, VWAP, liquidity identity |
| [docs/fees.md](docs/fees.md) | Taker fee mechanics, fill-fragmentation limits, exactness classification |
| [docs/detection.md](docs/detection.md) | Payoff states, settlement certification, profit intervals, the binary-complement canary |
| [docs/certification.md](docs/certification.md) | Settlement evidence, fingerprinting, human review, drift and historical validity |
| docs/replay.md | *(planned)* Replay and point-in-time guarantees |

**Start with `docs/api_assumptions.md`.** Several findings there contradict
widely repeated assumptions about Kalshi (prices are no longer whole cents,
contract counts are fractional, tick size varies with price level).

## Status

Phase 1, step 2 complete: foundations plus the Kalshi wire and normalisation
layer, built against real captured payloads.

**Step 1 — foundations**

- `predarb.domain.money` — exact `Price` / `Quantity` / `Money` / `QuantityDelta`
  scaled-integer arithmetic, floats rejected at the boundary
- `predarb.domain.enums` — closed vocabularies, including the three independent
  opportunity status axes
- `predarb.clock` — UTC-only time, three-timestamp provenance, injectable clock
- `predarb.config`, `predarb.logging`

**Step 2 — venue boundary**

- `predarb.venues.kalshi.fixed_point` — exact string → fixed-point parsing
- `predarb.venues.kalshi.models` — Pydantic v2 wire schema with a documented
  per-field strictness policy
- `predarb.venues.kalshi.normalize` — deterministic wire → domain conversion
- `predarb.domain.models` — price grid and venue instrument/event/series
- `predarb.domain.fees` — point-in-time resolvable fee configuration
- `predarb.books.levels` — normalised book levels, ordering, quoted/derived split

**Step 3 — transport (read-only)**

- `predarb.venues.kalshi.auth` — one RSA-PSS signing primitive for REST and WS
- `predarb.venues.kalshi.client` — async REST client with **no write surface**
- `predarb.venues.kalshi.rate_limit` — budgets discovered from the venue
- `predarb.venues.kalshi.pagination` — streaming cursor pagination
- `predarb.venues.kalshi.websocket` — authenticated handshake and subscriptions
- `predarb.venues.kalshi.errors`, `session` — typed errors, config wiring

**Step 5 — executable depth**

- `predarb.books.execution` — derived asks, breakpoint curves, gross cost
- `predarb.books.liquidity` — liquidity identity shared by direct and derived views
- `predarb.books.multileg` — multi-leg gross cost with collision detection
- `predarb.domain.average_price` — exact VWAP as a ratio, not a price

**A-09 resolved** (2026-09-16): `seq` is scoped per `sid` and is dense —
2,752 adjacent pairs, all advancing by exactly one, zero exceptions. Every
other candidate scope shows violations. See `docs/api_assumptions.md` A-09 for
the evidence and the resulting Step 4 invariant.

Next: book reconstruction. Detectors, storage and replay remain unbuilt — see
`docs/architecture.md` §6 for the build order.

## Development

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                  # create .venv and install runtime + dev dependencies
uv run ruff check .      # lint
uv run ruff format --check .
uv run mypy              # type check (strict)
uv run pytest            # tests
```

Copy `.env.example` to `.env`. Most settings use a `PREDARB_` prefix;
credentials additionally accept the plain `KALSHI_` names.

**Public market data needs no credentials.** Series, events, markets and order
books are all reachable unauthenticated:

```bash
uv run python tools/validate_transport.py     # exercises the public REST client live
```

**Credentials are needed only** for the WebSocket (it rejects unauthenticated
connections with HTTP 401) and for `GET /account/limits` /
`GET /account/endpoint_costs`. **Read scope is sufficient and preferred** —
Phase 1 submits no orders, and a write-scoped key changes nothing.

Store the private key outside the repository and point at it by path:

```bash
mkdir -p ~/.config/predarb/kalshi
mv ~/Downloads/kalshi-key.pem ~/.config/predarb/kalshi/kalshi-key.pem
chmod 600 ~/.config/predarb/kalshi/kalshi-key.pem
```

then set `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH` and
`KALSHI_ENVIRONMENT` in `.env`. Kalshi credentials are **environment-specific**:
a production key does not work against demo, and the 401 looks identical to a
bad key.

`.env`, `*.pem`, `secrets/` and `data/` are gitignored. Never paste a private
key into a file in this repository.
