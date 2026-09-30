# Prediction-Market-Arbitrage-Scanner

A research system built to decide, to a contractual standard of proof, whether
single-venue prediction-market arbitrage exists — and to refuse to call anything
arbitrage unless the governing text proves it.

The study is complete and frozen. Both results are below, and neither cancels
the other out.

## Final result

```
PROJECT_IMPLEMENTATION                             = COMPLETE
ENGINEERING_RESULT                                 = SUCCESS

ORIGINAL_SINGLE_VENUE_CONTRACTUAL_ARBITRAGE_THESIS = NOT_SUPPORTED_ON_THE_VENUES_INVESTIGATED
LIVE_PROFITABLE_CONTRACTUAL_ARBITRAGE              = NOT_DEMONSTRATED
TRADING                                            = NOT_ATTEMPTED
```

We built a system strict enough to distinguish *apparent* arbitrage from
*contractually provable* arbitrage, applied it across five U.S. prediction-market
venue architectures, and found that every investigated venue failed for a
concrete structural reason **before live executable price became the only
remaining unknown**.

The system worked. It correctly declined to label apparent price inconsistencies
as arbitrage when the governing contracts did not establish the required
worst-case payoff. That negative finding is a central result of the project, not
a byproduct of it.

This conclusion is scoped to the venues investigated and to single-venue
contractual arbitrage. It is **not** a claim that prediction-market arbitrage is
impossible, nor a claim about cross-venue arbitrage, statistical mispricing or
any other strategy — none of which were investigated here.

## Research question

> Can we find practical, executable, **single-venue** prediction-market
> opportunities whose positive worst-case profit can be **contractually proven**?

"Proven" means proven after accounting for all of:

- settlement semantics as written in the governing documents
- every reachable *normal* settlement mechanism
- alternate and cancellation paths
- executable depth, not displayed price
- fees and fee rounding
- multi-leg execution and leg collision
- logical relations between markets
- venue matching and netting rules
- point-in-time information availability, with no lookahead

The project was built on the premise that arbitrage **should not be assumed to
exist**. "We observed zero genuine executable arbitrages" was always a valid
outcome, and the analysis was never tuned toward positive findings.

## Venue findings

Five conditions must hold *simultaneously* for a venue to reach live-price
validation. No venue produced a full row.

| Venue | Useful relation | Settlement proof | Basket floor | No venue-rule elimination | Only price unknown |
| --- | --- | --- | --- | --- | --- |
| **Kalshi** | ✗ | ✗ | — | ✓ | ✗ |
| **ForecastEx** | ✓ | partial | ✗ | ✗ | ✗ |
| **Polymarket US / QCEX** | ✓ | ✗ | ✗ | ✓ | ✗ |
| **Rothera** | ✓ | ✗ (closest) | ✗ | ✓ | ✗ |
| **ProphetX** | — | ✗ | — | — | ✗ |

Four distinct failure modes, not one recurring defect.

### Kalshi

A full read-only stack was built and validated live: market data, order-book
reconstruction, executable depth, fees, detectors, replay, durable storage,
failure injection. **Settlement certification became the blocker.**

Rule 6.3(c)(b) authorises a "fair allocation" without stating what is being
allocated; no rounding model appears anywhere in the corpus; and the interaction
between Rule 7.1 and Rule 6.3(c) is unresolved, so the required guaranteed
terminal payoff cannot be proven. **36 active product families** were screened
in the strict-binary census and **no strict-two-state candidate survived** the
complete governing framework.

Result: `IMPLEMENTATION_COMPLETE, SEMANTIC_LIVE_EXECUTION_BLOCKED_BY_VENUE_GOVERNANCE`.
Live contractual detector execution remained blocked by venue governance.

### ForecastEx

Settlement semantics were materially cleaner. Rule 603(a) states both sides in
both branches, and the same-market YES+NO complement is explicitly conserved
through several mechanisms.

But Rule 401(d) inverse matching creates a same-market pair at **$1.01**, while
mandatory Rule 604 offsetting returns **$1.00**. Same-market complement
arbitrage is therefore **structurally negative by one cent before fees** — no
quote can rescue it.

Cross-market threshold and nested relations do exist, and are provable. They
fail under alternate settlement, because affected contracts can receive
independently determined settlement prices and fair allocations, and Rule 604
offsetting is same-market only.

Result: `RELATION_SEMANTICS_UNRESOLVED`. Transport implementation was not
justified.

### Polymarket US / QCEX

Useful combinatorial and related-contract structures existed. Critically,
**product-specific filings rather than the top-level Rulebook contained the
decisive settlement behaviour** — the Rulebook screened clean only because it
specifies no product's payout at all.

Athletic Event Contracts (Rule 9.101) include a **fractional tie settlement**
paying $0.50 to each side, and Rule 9.101(K) cancellation permits discretionary
valuation — "last-traded prices, $0.50 per contract, or other fair and equitable
valuation". Rule 1.5's precedence is scoped to "trading in" a Contract, leaving
rule precedence and settlement closure unresolved.

CAOC combinatorial contracts supply the strongest logical combination language
found anywhere in the study — "resolves to $1.00 if and only if every leg is
satisfied" — but reachable fractional leg settlement destroys the guaranteed
basket floor, dropping it to half the notional.

Conservation is **`NOT_PROVEN`, not `DISPROVEN`**: no Polymarket US text
authorises a non-conserving state.

### Rothera

The closest venue to satisfying the semantic requirements. Its product
certifications explicitly use a **residual construction** for fair-market
settlement:

```
long  = p
short = $1 − p
```

present in all fourteen families read. This establishes an important
distinction: **discretion over one settlement price can preserve the notional
exactly**, at every price and every precision. Clearing preserves the payoff by
rule (DCO Rule 5.1(D)(3)).

Two things still block it. Ordinary contingency language reserves settlement
determinations without fully constraining the output — the Contingencies clause
in all fourteen certifications reserves "the right to make settlement
determinations" on ordinary source delay with no stated output.

More importantly, **related contracts may receive independent fair-market
settlement prices.** A genuine three-way soccer partition — home win, away win,
tie, proven mutually exclusive and exhaustive over regulation-time goals — can
settle at:

```
p_home + p_away + p_tie
```

with no contractual coupling and no floor. Every individual contract conserves
perfectly while the cross-contract basket has **no guaranteed payoff floor**.

Result: Phase 2D failed; live-price validation was not justified.

### ProphetX

Rule 5.2 provides broad settlement discretion. Combined payout is capped at the
maximum Settlement Value — "in no case shall the combined payout across
positions **exceed**" it — but no corresponding conservation floor is
established. **An upper bound is not a complement proof**: the cap is satisfied
by `$0.40 + $0.40` and by `$0 + $0`.

Result: `COMPLEMENT_CONSERVATION_NOT_PROVEN`. Screen closed.

## What the research established

**1. Price identities are not arbitrage proofs.**
Tests like `YES + NO < $1` or `sum(outcome prices) < $1` are insufficient. The
relevant quantity is the **minimum contractual payoff across every reachable
settlement state**, compared with executable acquisition cost plus fees. Every
venue in this study would have produced apparent opportunities under a price-sum
screen; none survived contact with the settlement text.

**2. Ordinary outcomes are not settlement states.**
`AT_MOST_ONE` / `AT_LEAST_ONE` / `EXACTLY_ONE` relations may hold under ordinary
resolution while failing under cancellation, fair-price settlement, ties,
outcome review, source failure, indeterminate settlement, or other
contract-specific paths.

**3. Single-contract conservation does not imply basket conservation.**
Rothera is the clearest example. Even where every individual market guarantees
`long = p` and `short = 1 − p`, an apparently exhaustive group of related
contracts can receive **independent** `p` values, so the basket
`p₁ + p₂ + … + pₙ` may have no contractual lower bound. The two properties are
independent and both can hold at once. This was a major final finding.

**4. Discretion over a price is not discretion over a payout.**
If the venue chooses one `p` and the opposite side is mechanically `1 − p`,
complementarity survives however the price is chosen. If two payouts are
independently chosen, or their relationship is simply unstated, complementarity
is not proven.

**5. Venue mechanics can eliminate the edge before prices matter.**
ForecastEx is canonical: acquisition of a complementary pair costs $1.01 while
the mandatory offset value is $1.00. No amount of live quote collection can
reveal a positive same-market complement edge.

**6. Product-specific filings matter more than top-level rulebooks.**
Rulebooks were repeatedly insufficient. Critical settlement rules lived in
product certifications, terms and conditions, amendments, clearing rules and
incorporated documents. This is why the evidence system models **transitive
governing-document closure** rather than treating a citation as a leaf.

**7. Unknown is not disproven.**
The project preserves `PROVEN` / `DISPROVEN` / `NOT_PROVEN` / `UNRESOLVED` /
`EVIDENCE_INCOMPLETE` as distinct verdicts, and does not convert missing
language into a negative factual claim. Two verdicts in this study were
corrected from `DISPROVEN` to `NOT_PROVEN` for exactly this reason.

**8. Extraordinary intervention is a separate risk class.**
Broad venue-wide emergency and regulatory intervention is tracked apart from
normal contractual settlement, because a proof against sovereign or exchange
intervention is unobtainable on any regulated venue and would reject every venue
for a property none can have. The strongest claim the system produces is
`CONTRACTUALLY_GUARANTEED_UNDER_NORMAL_GOVERNING_SETTLEMENT`, with
`RESIDUAL_VENUE_INTERVENTION_RISK` disclosed separately. **Extraordinary venue
or regulatory intervention remains explicitly disclosed as residual intervention
risk.** The exclusion is narrow and machine-enforced: product
cancellation, settlement discretion, fair-market-price settlement, tie/push
rules, source failure, outcome review, scheduled contingencies and
product-specific discretionary valuation all remain inside the proof.

## Engineering result

**SUCCESS.** The system built to test the thesis worked, and its rejection of
every venue is evidence the gate functioned.

Built and validated:

- exact scaled-integer money arithmetic, with floats rejected at every economic
  boundary
- sequence-safe live order-book reconstruction, with a proven per-`sid` density
  invariant
- executable multi-level depth and a liquidity identity shared by direct and
  derived views
- multi-leg gross cost with collision handling
- exact and bounded venue fee models, including venue-specific rounding modes
- generic payoff-state evaluation and exact profit intervals
- human-reviewed settlement certification; unknown semantics fail closed
- relation certification over exact member sets
- `AT_MOST_ONE` / `AT_LEAST_ONE` / derived `EXACTLY_ONE` research
- governing-document dependency closure, cycle-safe and canonically ordered
- PDF evidence extraction with raw-byte identity preserved separately from
  extraction provenance
- historical rule-reference lineage through filed amendments
- claim- and context-scoped dependency materiality
- bitemporal point-in-time replay with a no-lookahead proof
- deterministic economic fingerprints
- durable restart-safe storage, crash recovery and fault injection
- live/replay equivalence through one shared coordinator
- multi-venue semantic research models covering five venues
- security scanning
- read-only Kalshi transport boundaries, with no write surface

Final verification at freeze:

```
pytest                 2,946 passed · 2 skipped · 8 deselected
ruff check .           clean
ruff format --check .  clean
mypy (strict)          clean
security scan          clean — 0 hits across all five scopes
```

No orders were submitted. No live trading was attempted. No credentials were
used in Phase 2.

## Architecture and core distinctions

The system keeps four categories apart and never blurs them:

| Category | Requires |
| --- | --- |
| **True contractual arbitrage** | Verified relation + provable payoff in every reachable settlement state + executable depth + fees |
| **Statistical mispricing** | A model. Out of scope. |
| **Stale-market opportunity** | A book we can show was stale. A data-quality finding, not an edge. |
| **Relative value** | A view. Out of scope. |

It also keeps *payoff certainty* separate from *execution certainty*. A
portfolio can have a guaranteed profit once all legs are filled and still be
exposed to race risk while those legs are acquired. These are independent status
axes on every opportunity record.

Further invariants the system enforces rather than documents:

- it does not infer that two markets are related because their titles look
  similar — relations are human-verified and version-pinned
- it does not treat mutual exclusivity as exhaustiveness
- it does not label something arbitrage unless the worst-case payoff over every
  valid settlement state exceeds the all-in executable acquisition cost
  including fees
- an unreadable governing document fails closed, never open

## Final project status

```
PROJECT_IMPLEMENTATION                             = COMPLETE

ORIGINAL_SINGLE_VENUE_CONTRACTUAL_ARBITRAGE_THESIS = NOT_SUPPORTED_ON_INVESTIGATED_VENUES

LIVE_PROFITABLE_CONTRACTUAL_ARBITRAGE              = NOT_DEMONSTRATED

TRADING                                            = NOT_ATTEMPTED
```

Phase 1 is frozen at `f77569f` with one acceptance criterion unmet; the
criterion is left standing in the report rather than rewritten. Phase 2 is
frozen. Neither is a pending roadmap.

## Documentation

### Final reports — start here

| Document | Contents |
| --- | --- |
| [docs/phase2_report.md](docs/phase2_report.md) | **Phase 2 freeze and final venue study.** Thesis conclusion, all five venue outcomes, research lessons, emergency-scope decision |
| [docs/phase1_report.md](docs/phase1_report.md) | **Phase 1 acceptance report.** Kalshi implementation, the settlement census, the unmet criterion, status matrix |
| [docs/phase2d_report.md](docs/phase2d_report.md) | Rothera qualification: residual construction, partition floors, ProphetX close-out |
| [docs/phase2c_report.md](docs/phase2c_report.md) | Polymarket US / QCEX qualification: product-filing precedence, tie settlement, CAOC |
| [docs/api_assumptions.md](docs/api_assumptions.md) | Every verified venue finding, A-01 … A-72, with classifications and sources |

ForecastEx findings (Phases 2A–2B) are recorded in
[`src/predarb/venues/forecastex/`](src/predarb/venues/forecastex/) and
summarised in the Phase 2 report.

### Design and implementation

| Document | Contents |
| --- | --- |
| [docs/architecture.md](docs/architecture.md) | Module layout, dependency rules, data flow, live/replay symmetry |
| [docs/data_model.md](docs/data_model.md) | Event → proposition → settlement spec → venue instrument, relations, versioning |
| [docs/arbitrage_definitions.md](docs/arbitrage_definitions.md) | Formal definitions, the four opportunity structures, and the traps |
| [docs/kalshi_adapter.md](docs/kalshi_adapter.md) | Wire schema, fixed-point parsing, forward-compatibility policy, normalisation |
| [docs/reconstruction.md](docs/reconstruction.md) | Sequence invariant, book state machine, recovery, raw journal |
| [docs/execution.md](docs/execution.md) | Derived asks, executable depth, VWAP, liquidity identity |
| [docs/fees.md](docs/fees.md) | Taker fee mechanics, fill-fragmentation limits, exactness classification |
| [docs/detection.md](docs/detection.md) | Payoff states, settlement certification, profit intervals, the binary-complement canary |
| [docs/certification.md](docs/certification.md) | Settlement evidence, fingerprinting, human review, drift and historical validity |
| [docs/relations.md](docs/relations.md) | AT_MOST_ONE relations, the n+1 state space, NO baskets |
| [docs/exhaustiveness.md](docs/exhaustiveness.md) | Venue membership vs outcome exhaustiveness, and why `mutually_exclusive` is insufficient |
| [docs/at_least_one.md](docs/at_least_one.md) | AT_LEAST_ONE BUY-YES baskets, the worst-case payoff theorem, derived EXACTLY_ONE |
| [docs/storage.md](docs/storage.md) | Durable catalogue, the source/derived boundary, idempotency, retention |
| [docs/replay.md](docs/replay.md) | Point-in-time replay, valid vs knowledge time, bundle integrity, no-lookahead proof |

Several findings in `docs/api_assumptions.md` contradict widely repeated
assumptions about these venues — Kalshi prices are not whole cents, contract
counts are fractional, tick size varies with price level, and two venues'
rulebooks specify no product payouts at all.

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

**Public Kalshi market data needs no credentials.** Series, events, markets and
order books are reachable unauthenticated:

```bash
uv run python tools/validate_transport.py     # exercises the public REST client live
```

**Credentials are needed only** for the Kalshi WebSocket (it rejects
unauthenticated connections with HTTP 401) and for `GET /account/limits` /
`GET /account/endpoint_costs`. **Read scope is sufficient and preferred** — the
system submits no orders, and a write-scoped key changes nothing. No credentials
of any kind were used for the Phase 2 venue research, which is documentary.

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
key into a file in this repository. Downloaded rulebooks and regulatory filings
are kept under a gitignored local research path and referenced by sha256 only.

## Phase history

### Phase 1 — Kalshi (frozen at `f77569f`)

Implementation and live validation against one venue: exact money arithmetic and
the venue wire boundary; read-only REST and WebSocket transport; sequence-safe
book reconstruction; executable depth and multi-leg cost; a bounded fee engine;
payoff evaluation and both detector families behind human-reviewed settlement
certification; relation certificates and NO baskets; bitemporal point-in-time
replay with a no-lookahead proof; durable storage, crash recovery and fault
injection.

Every mechanical guarantee the phase set out to establish holds and is tested.
The phase ended blocked by the venue's governing settlement semantics, not by an
incomplete build — 36 product families screened, none strictly two-state, and
complement conservation unprovable even for the cleanest contract on the
exchange.

### Phase 2 — venue qualification before transport (frozen)

Phase 1 built a complete adapter for a venue whose settlement semantics then
blocked it. Phase 2 inverted the order and qualified each venue's governing
documents *before* writing any transport, rejecting four venues without a line
of adapter code.

- **2A–2B — ForecastEx**: settlement qualification, then cross-market relation
  feasibility
- **2C — Polymarket US / QCEX**: settlement, precedence and combinatorial
  structures
- **2D — Rothera**: full governing closure, partition semantics, fee model; and
  the ProphetX screen closed on Rule 5.2

Conclusion: no investigated venue reached the state where live executable price
was the only remaining unknown.

Cross-venue arbitrage, statistical mispricing and other strategies were not
investigated and are deliberately excluded from every conclusion above. They
would be separate projects if explicitly chosen later.
