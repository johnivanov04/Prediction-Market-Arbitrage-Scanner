# Phase 1 acceptance report — Kalshi prediction-market arbitrage research

**Status: IMPLEMENTATION_COMPLETE, LIVE_SEMANTIC_VALIDATION_PENDING.**
No executable arbitrage has been demonstrated, and none was required. No real
Kalshi settlement certificate has been human-approved, so the detector
economics have never run past the semantic gate on live data. Both facts are
reported as results, not as pending work.

Evidence classifications used throughout: **DOCUMENTED** (stated in current
official Kalshi documentation), **OBSERVED** (measured live, with counts),
**PROVEN** (established by argument or exhaustive test), **INFERRED**,
**UNKNOWN**.

---

## 1. Objective

Determine empirically whether riskless, executable arbitrage exists on Kalshi,
under a standard of proof strict enough that a positive finding would survive
scrutiny — and strict enough that a negative finding means something.

The project was built on the premise that **arbitrage should not be assumed to
exist**. "We observed zero genuine executable arbitrages" is a valid result, and
the analysis was never to be tuned toward positive findings.

## 2. Scope

Phase 1 is **Kalshi only, read-only**. No order submission, no balances,
positions, fills or portfolio endpoints; no second venue, no cross-venue
arbitrage, no statistical mispricing, no ML.

## 3. Architecture

One process, one append-only raw journal, one catalogue. No Kafka, no
Kubernetes, no microservices. Dependency rule: `detectors/` and `domain/`
perform **no I/O** — no HTTP, no database, no clock reads. Storage sits at the
orchestration boundary, which is what lets live and replay invoke the same pure
economic functions.

## 4. Kalshi protocol findings

53 recorded assumptions (`docs/api_assumptions.md`). The ones that changed the
design:

| | Finding | Class |
| --- | --- | --- |
| A-07 | documented and live order-book array order disagree; levels are sorted explicitly | OBSERVED |
| A-09 | `seq` is per-`sid` and dense — 2,752 adjacent pairs, all +1, zero exceptions | OBSERVED |
| A-12 | `show_historical` defaults false, so fee-change history reads as empty | DOCUMENTED |
| A-14 | the fee-multiplier column mapping is not documented | **UNKNOWN** |
| A-46/A-50 | the event market list omits archived members — 27.7% of members, on 46% of events | DOCUMENTED + OBSERVED |
| A-47 | nested markets arrive inside `event.markets`, not beside it | OBSERVED |
| A-48 | live/historical partition by a moving cutoff; **not strict in practice** | DOCUMENTED + OBSERVED |
| A-49 | `/historical/markets` returns negative top-of-book sizes on finalized markets | OBSERVED |
| A-51 | a provisional market may be **removed** entirely | DOCUMENTED |
| A-52 | `mutually_exclusive` bounds the maximum winners, never the minimum | DOCUMENTED |

## 5. Exact money

Scaled integers throughout: `PRICE_SCALE(1e-4) × QUANTITY_SCALE(1e-2) =
MONEY_SCALE(1e-6)`. Binary floats are rejected at the boundary — a `float`
reaching a price or quantity raises rather than rounding. Prices are not whole
cents, contract counts are fractional, and tick size varies with price level;
all three contradict widely repeated assumptions about Kalshi. **PROVEN** by
property tests over the arithmetic.

## 6. Sequence integrity

A-09 was established by experiment, not assumption: every candidate scope for
`seq` was tested, and only per-`sid` was dense with zero violations. Books fail
closed on a gap — `WAITING_SNAPSHOT`, `SEQUENCE_VIOLATION` and
`INTEGRITY_UNKNOWN` are all unscannable, and only `VALID` may be read.

## 7. Book reconstruction

Snapshot/delta with full provenance (epoch, sid, seq, applied frames). A
disconnect invalidates every live book, and a new epoch requires a fresh
snapshot before any delta is applied — applying deltas to an empty book would
fabricate one.

## 8. Executable depth

Depth comes **only** from resting opposite-side bids: to buy YES you cross NO
bids, so `yes_ask = notional − no_bid`. Never a midpoint, last trade or
displayed probability. Multi-leg cost is gross by leg with collision detection
across all legs, because distinct tickers do not prove distinct source
liquidity.

## 9. Fee model

Exact where the venue's arithmetic is reproducible, bounded where it is not:

`ceil_6dp(F) ≤ net ≤ ceil_6dp(F) + k_max·B − μ`

The bound is **PROVEN**, and derived only after an earlier "unbounded"
conclusion was corrected: 0.01 quantity granularity bounds the fill count.
Two claims were withdrawn under challenge rather than defended — direct-member
invariance (an exhaustive sweep found 114 counterexamples) and one-fill-per-level.

**A-14 remains UNKNOWN.** The fee-multiplier column mapping is undocumented, so
an unresolved multiplier yields `FeeQuote | UnavailableFee` at the type level.
Unknown is never zero: a leg whose fee cannot support a contractual claim blocks
the whole basket.

## 10. Payoff engine

Exact per-state payoff with worst-case extraction. `AT_MOST_ONE` uses an n+1
state space rather than 2ⁿ filtered — enumerating states the certificate says
cannot exist, then filtering, would build the wrong thing and rely on the filter.

## 11. Settlement certification

A human reads the contract, answers a checklist, and types an exact confirmation
phrase. Certificates are content-addressed, append-only and bound to an evidence
fingerprint; when evidence drifts the certificate is **not** edited to pretend
the review never happened — it stays as issued and becomes inapplicable.

Unknown settlement semantics **fail closed**. There is no `--skip-certificates`,
`--assume-binary` or `--trust-mutually-exclusive` flag, and adding one would
make every gate decorative.

## 12. AT_MOST_ONE

Relation certificates over an **exact canonical member set**, separate from the
members' payoff certificates — they are different proof obligations from
different evidence, and a basket needs both. Licenses a NO basket.

## 13. AT_LEAST_ONE

Licenses a YES basket. The floor is `q · min_i(N_i)`, **PROVEN** symbolically
with preconditions checked per member; `2ⁿ − 1` permitted states are counted and
never built (one live event has 300 members). Cross-checked against full
enumeration for n ≤ 6 and property-tested over random inputs.

Only ALL-NO is forbidden. Multiple winners are permitted and only *raise* a
long-YES basket's payoff, so the detector requires no mutual exclusion and stays
valid on the 69% of sampled events that are not mutually exclusive.

## 14. Derived EXACTLY_ONE

`AT_MOST_ONE(S) ∧ AT_LEAST_ONE(S)`, composed at evaluation time from two
independently reviewed certificates over the **same canonical set**, and never
stored. A third human review could approve the conjunction without either half;
a derivation recomputed per decision cannot outlive a parent. No EXACTLY_ONE
economic detector exists.

## 15. Replay and no-lookahead

Valid time and knowledge time are never conflated. Resolution is two-phase:
discard everything observed after the horizon, *then* apply effective time.
Capture ordinals — not timestamps — are authoritative for ordering.

`LiveSession` and `ReplayEngine` drive one `EvaluationCoordinator`. The live
side is fed one observation at a time and cannot see forward; the replay side
indexes the finished stream and *can*. Agreement therefore means a run holding
every future observation declined to use it. Deliberately breaking the horizon
filter fails all four equivalence tests — the comparison has teeth.

Absence is separated from ignorance throughout: only an explicit registry
snapshot licenses "we knew there was no certificate".

## 16. Durable storage

See `docs/storage.md`. Source evidence persisted before state mutation (fatal on
failure); derived decisions after (recoverable). Content-derived identities make
every write idempotent. Bounded backpressure stops the collector rather than
sampling the feed.

## 17. Soak methodology

One read-only session against production Kalshi: discovery, metadata, resolved
fee context, explicit settlement- and relation-registry snapshots, then a live
WebSocket feed driving all three detectors with durable persistence throughout.
No gate weakened, no market selected to produce a finding.

## 18. Soak results

Production Kalshi, read-only, **45 minutes** continuous
(2702s), session `4b95cc363ed3`.

Six markets, chosen by ordinary discovery rather than selected for an outcome:
four of them form one real event (`KXPREMIERLEAGUE-27`) monitored as **both** an
AT_MOST_ONE and an AT_LEAST_ONE basket, plus two standalone markets.

### Transport

| | |
| --- | --- |
| connections | 1 |
| reconnects | 0 |
| ping/liveness failures | 0 |
| idle polls (quiet-market pings) | 446 |
| WebSocket frames | 683 |
| lifecycle events | 3 |
| sequence violations | 0 |
| unknown frames | 0 |

### Books and context

All books reconstructed from snapshot/delta with sequence integrity enforced;
no invariant failure and no invalidation outside the closing disconnect. Context
was captured once at session start — 17 observations
covering metadata, resolved fee configuration and both registry snapshots. Both
registries were **explicitly empty**, which is what makes the resulting blocks
determinate rather than gaps.

### Decisions

| Detector | Decisions |
| --- | --- |
| `BINARY_COMPLEMENT` | 689 |
| `AT_MOST_ONE_BASKET` | 191 |
| `AT_LEAST_ONE_BASKET` | 191 |
| **total** | **1071** |

684 triggers produced 1071 decisions.
`detector_did_run` was **false for all of them**, and every one classified
`BLOCKED_SETTLEMENT_SEMANTICS`. Zero missing-knowledge decisions: the registries
had been consulted, so absence was known rather than unknown.

**Proven contractual arbitrage candidates: 0.** Nothing reached the economics,
because nothing passed the semantic gate.

### Storage

| | |
| --- | --- |
| observations persisted | 703 |
| duplicate writes | 0 |
| decisions persisted | 1071 |
| derived write failures | 0 |
| catalogue size | 3.7 MB |
| raw journal | 462 KB |
| queue high-water mark | 1 |

Persistence sits in the receive path, so the high-water mark is 1 by
construction. The bound is a guard against a future async buffer, not something
this run exercised under load — see §21.

## 19. Replay equivalence

The full session replayed offline from its stored source observations:

| | |
| --- | --- |
| observations replayed | 703 |
| decisions persisted | 1071 |
| decisions regenerated | 1071 |
| **fingerprint matches** | **1071** |
| missing from catalogue | 0 |
| present without source | 0 |
| fingerprint mismatches | 0 |
| **network calls during replay** | **0** |

**Exact match.** Every persisted decision was regenerated from source evidence
with an identical economic fingerprint, and the replay reached no network.

This is the Step-10 no-lookahead property holding on real data at scale: the
live side held only what it had been handed, the replay side had all
703 observations indexed and declined to use any of
them early.

## 20. Performance and resources

Latency from local frame receipt, in milliseconds. **Measurement only** — this
describes this collector on this machine and predicts nothing about trading.

| Stage | p50 | p95 | p99 | max |
| --- | --- | --- | --- | --- |
| receive → persisted | 1.166 | 4.324 | 11.356 | 614.701 |
| receive → book mutated | 2.09 | 7.686 | 11.73 | 614.778 |
| receive → decision | 3.336 | 10.666 | 12.066 | 18.11 |

The 614.701 ms maximum is a single outlier at
session start, when SQLite was creating its WAL; p99 is under 12 ms.

| Resource | Observed | Per hour |
| --- | --- | --- |
| CPU (user + system) | 6.5 s | 8.7 s |
| peak RSS | 251 MB | — |
| catalogue growth | 3.7 MB | 5.0 MB |
| raw journal growth | 462 KB | 615 KB |
| decisions | 1071 | 1427 |

Sizing a longer collection: roughly **6 MB/hour**
of durable data at this market count and activity level, on well under 1% of one
core. A 24-hour run is ~134 MB.

## 21. Failure injection

24 controlled local faults, none directed at Kalshi. Every case failed closed.

| Injected | Result |
| --- | --- |
| collector process restart | resumes from durable ordinal; no duplicates |
| re-ingesting a replayed tail | 9 proven-present, 0 inserted |
| dropped recorded frame | book differs and the digest changes — visible, not silent |
| duplicated frame | sequence integrity rejects it |
| reordered frames | stream construction refuses; nothing re-sorts by timestamp |
| raw/source write failure | `SourcePersistenceError`, collector marked unhealthy and stops |
| derived decision write failure | collection continues; replay regenerates all 6 |
| storage transaction failure | rolled back; earlier rows intact |
| stale context | reported `INCOMPLETE_STALE_CONTEXT`, not treated as current |
| missing certificate snapshot | `MISSING_POINT_IN_TIME_KNOWLEDGE` |
| explicitly empty registry | determinate `BLOCKED_SETTLEMENT_SEMANTICS` |
| reconnect / new epoch | requires a fresh snapshot; no book inherited |
| truncated final journal record | `TRUNCATED_TAIL`, usable prefix |
| corrupted middle record | refused |
| edited record | hash mismatch, refused |
| 429 rate limit (fixture) | classified retryable; a write method never is |
| backpressure bound exceeded | stops rather than sampling |

The backpressure case is the one worth reading carefully. Persistence is in the
receive path, so the collector **cannot** silently fall behind — pressure shows
up as slower socket consumption, never a dropped frame. The bound exists so that
introducing an async buffer later cannot quietly turn "cannot keep up" into
"drops messages"; it is tested directly, but the soak did not exercise it under
load and the report does not claim otherwise.

## 22. Human certification status

**No real Kalshi settlement certificate has been human-approved.**

A curated review queue was prepared: markets are screened *out* on structural
signals that the payoff is not the simple two-state one — scalar type, non-binary
result, `functional`/`structured` strikes, or rules advertising fair-price or
scalar settlement. Surviving candidates are not thereby certifiable; it means
nothing obvious disqualified them and a human must still read the contract.
Cancellation, void, DNP, tie and discretion clauses are surfaced verbatim rather
than scored.

Screened 20 market(s): 12 eligible, 8 screened out
on structural grounds. 4 review request(s) opened, all
`AWAITING_REVIEW` with **COMPLETE** evidence:

| Market | Request | Evidence | Clauses flagged for reading |
| --- | --- | --- | --- |
| `KXBTC15M-26SEP231645-45` | `54f59f39b008` | COMPLETE | none |
| `KXTIME-26-ZOH` | `07964b122b49` | COMPLETE | none |
| `KXBOND-30-CAL` | `cd64ad0dba4a` | COMPLETE | none |
| `KXFEDERALCHARGE-27JAN01-AFAU` | `eb2e1dfa62f9` | COMPLETE | none |

Every request has both governing documents retrieved but **requiring manual
reading at source** — the Step-8 hash-bound acknowledgement, so approving
version A cannot satisfy version B. The flagged clauses are surfaced for a
reader, not scored.

Claude did not and will not approve these. Issuance requires an APPROVED review
recorded against an exact evidence fingerprint, with a hash-bound acknowledgement
that each governing document was read at source.

## 23. Opportunities observed

**Zero proven contractual arbitrages**, on any live market, across every
session. Every live decision is `BLOCKED_SETTLEMENT_SEMANTICS` with
`detector_did_run = False`.

This is neither a disappointment nor a finding about prices. It is the semantic
gate working: no market has a reviewed settlement certificate, the registries
were consulted and found empty, so the system *knows* it cannot prove anything
about these markets. It is a determinate conclusion, not a gap. Measured on a real
Step-10 capture: stripping only the two registry snapshots turned all 147 of
that session's decisions from `BLOCKED_SETTLEMENT_SEMANTICS` into
`MISSING_POINT_IN_TIME_KNOWLEDGE`, and correctly weakened the absence claim
from "no proven candidate existed on complete inputs" to "inputs are incomplete
for a definitive claim". Same books, same fees, same code — the only difference
is whether the system can show it looked.

The synthetic paths prove the economics work when a certificate exists. What has
not been shown is that any *real* market's contract supports one.

## 24. Unresolved assumptions

| | Status |
| --- | --- |
| A-14 fee-multiplier column mapping | **UNKNOWN**; bounded, never guessed |
| union completeness of venue membership | unprovable from the API (A-51) |
| the documented live/historical partition | observed overlapping (A-48) |
| `custom` / `structured` strike grammar | 463 of 935 members, opaque |
| `product_metadata` | typed `object`, no schema, unpopulated |
| boundary landings on strike edges | a tick-size question the helper does not answer |
| whole-degree settlement domains | asserted in one live request, **not attested** |

## 25. What Phase 1 explicitly does NOT prove

- that arbitrage exists on Kalshi, or that it does not;
- that any real market's settlement semantics support a contractual claim;
- that `live + historical` enumeration is complete membership;
- that a detected candidate would fill — all legs are non-atomic and
  `RACE_EXPOSED`, never `LOCKED`;
- anything about latency in a trading context: the measured percentiles describe
  this collector on this machine;
- anything about any venue other than Kalshi.

## 26. Readiness

See the status matrix in §27 and the freeze proposal below. The implementation
is complete and deterministically tested; the live *semantic* path is
unexercised because no certificate exists, and that distinction is not hidden
behind an overall green label.

## 27. Status matrix

Reported per dimension. An unexercised semantic path is **not** hidden behind an
overall green label.

| Dimension | Status |
| --- | --- |
| IMPLEMENTATION | **COMPLETE** |
| DETERMINISTIC_TESTING | **PASS** |
| LIVE_TRANSPORT_VALIDATION | **PASS** |
| LIVE_BOOK_VALIDATION | **PASS** |
| REPLAY_EQUIVALENCE | **PASS** |
| DURABLE_STORAGE | **PASS** |
| FAILURE_INJECTION | **PASS** |
| SECURITY_SCAN | **PASS** |
| LIVE_INDIVIDUAL_SEMANTIC_PATH | **NOT_YET_EXERCISED** |
| LIVE_RELATION_SEMANTIC_PATH | **NOT_YET_EXERCISED** |
| REAL_DETECTOR_EXECUTION | **NOT_YET_EXERCISED** |
| 24H_SOAK | **NOT_YET_RUN** (45 minutes completed) |
| TRADING | **NOT_IN_SCOPE** |

**Overall: IMPLEMENTATION_COMPLETE, LIVE_SEMANTIC_VALIDATION_PENDING.**

Every mechanical guarantee Phase 1 set out to establish holds and is tested. The
one thing not shown is that any *real* Kalshi market's contract supports a
settlement certificate — and that is a human judgement nobody has made yet, not
a defect in the system. Four candidates are queued for that decision.

## 28. Proposed Phase-1 freeze

Not merged, not tagged. Awaiting review.

| | |
| --- | --- |
| freeze candidate | the Step-13 working tree on `phase-1`, on top of `c543af3` |
| catalogue schema | `phase1-catalogue/1` |
| replay bundle schema | `replay-bundle/1` |
| decision fingerprint | `economic-decision/1` |
| settlement evidence | `settlement-evidence/1` |
| relation evidence | `relation-evidence/1` |
| AT_LEAST_ONE policy | `at-least-one-evidence/1` |
| membership evidence | `venue-membership-evidence/1` |
| strike partition | `strike-partition/1` |
| domain constraint | `domain-constraint/1` |
| detectors | binary complement, AT_MOST_ONE NO basket, AT_LEAST_ONE YES basket |
| derived proofs | EXACTLY_ONE (composed, not stored) |

**Outstanding semantic-validation limitations**: no human-approved settlement
certificate; no relation certificate; the one live AT_LEAST_ONE request has an
undischarged domain condition. See §22 and §24.

**Phase-2 prerequisites**: §29.

## 29. Phase-2 entry criteria

1. At least one human-approved settlement certificate, with the binary
   complement detector observed running on that live market — any classification.
2. A relation path exercised end to end, or an explicit statement that no
   straightforward relation can be safely certified.
3. A longer soak (≥ 24h) with zero unexplained replay mismatches.
4. A-14 resolved, or an explicit decision to keep operating on bounds.
5. Phase-1 freeze reviewed and accepted.

Phase 2 should not begin while the live semantic path is unexercised: a second
venue would multiply an untested gate rather than validate it.
