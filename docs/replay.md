# Point-in-time replay and the no-lookahead proof

Replay reproduces the decisions the system would have been justified in making
at a historical instant, using only what it had observed by then.

It substitutes **data sources and the clock**. It does not substitute economic
logic: the books, execution curves, fee bounds, payoff engine and detectors are
the same code the live path runs. If replay needed its own version of any of
them, the equivalence claim would be circular.

---

## 1. Two clocks, and why one is not enough

**Valid time** is when the venue says something takes effect — a fee change's
`scheduled_ts`, a certificate's `valid_from`.

**Knowledge time** is when *we* learned it — a payload fetched, a frame
arrived, evidence captured, a certificate issued.

A decision at horizon `T` may use a fact only if we had observed it by `T`,
**and** its valid time permits use at `T`. Checking valid time alone is the
classic backtest leak:

```
download historical fee changes today
-> replay last month
-> price with a schedule we did not possess then
```

The effective timestamp says "Wednesday", so a valid-time-only filter happily
admits that record into a Tuesday replay. Only knowledge time rejects it.

Resolution is strictly two-phase, and the order matters:

1. **discard** everything observed after the horizon — we did not have it;
2. **then** pick, from what remains, the version effective at the instant.

The worked case, both directions:

| Observed | Effective | Replay Tuesday | Replay Thursday |
| --- | --- | --- | --- |
| Monday | Wednesday | old config (change known, correctly unapplied) | **new config** |
| Friday | Wednesday | old config | **old config** — we had not seen it |

The second row is the one that matters. A backfilled schedule must not reprice
a decision that predates our knowledge of it.

### Missing knowledge is a result

When the horizon contains nothing applicable the resolver returns `None`, the
evaluation is recorded as `MISSING_POINT_IN_TIME_KNOWLEDGE`, and the
completeness report says so. It is never skipped, because a skipped decision is
invisible in the report and reads exactly like a decision that was made and
found nothing.

### The mode has a name

`AS_KNOWN_AT_TIME` — *what would the system have been justified in concluding
using only information it had then?* Deliberately not "objective historical
truth". A later research mode could apply corrections learned afterwards, and
that is a different and equally legitimate analysis; mixing them would produce
results nobody can interpret.

---

## 2. Ordinals, not timestamps

Every observation carries a monotonically increasing **capture ordinal**
assigned locally at record time. Knowledge cut-offs are expressed as ordinals.

Two observations can share a microsecond. Ordering between them is still
defined, and a detector triggered by `#100` must not see `#101` merely because
their clocks round the same. An `ObservationStream` refuses out-of-order or
duplicate ordinals at construction, and nothing anywhere re-sorts by timestamp —
sorting by exchange time cannot recreate sub-millisecond live ordering, and
pretending otherwise would silently reorder a frame stream.

---

## 3. Connection lifecycle is recorded, not inferred

Application frames alone cannot reproduce scan eligibility. A disconnect leaves
no trace in the frame stream, so a replay built from frames alone would keep a
book alive through an outage the live system correctly treated as fatal.

So the stream carries `CONNECTION_OPENED`, `SUBSCRIBED`, `FRAME_RECEIVED`,
`CONNECTION_CLOSED` and `CONNECTION_FAILED`, and replay drives the production
`OrderBookReconstructor` through the same API the collector uses. A close
invalidates books exactly as it did live; a new epoch resets sequence state and
returns books to `WAITING_SNAPSHOT`.

The reconstructor is given a write-nothing journal
(`ReplayFrameJournal`) rather than a modified reconstructor: the frames are
already recorded in the bundle, and re-journalling would write a second copy of
the same evidence under fresh ids. It is a different collaborator, not a
different code path.

---

## 4. Absence is not ignorance

This is the distinction the whole completeness story rests on.

> "No certificate observation exists in the bundle"
> is **not**
> "the system knew no certificate existed".

The first is a gap in capture. The second is a fact the system held. Only an
explicit snapshot — *we queried this authoritative source at ordinal N, and here
is what it held, possibly nothing* — licenses the second reading.

Four snapshot kinds record that a source was consulted:

| Kind | Records |
| --- | --- |
| `METADATA_SNAPSHOT` | which markets `/markets` was asked about, and which resolved |
| `FEE_KNOWLEDGE_SNAPSHOT` | which scopes were resolved, which were not, and every scheduled change known but not yet effective |
| `SETTLEMENT_REGISTRY_SNAPSHOT` | the local settlement-certificate registry as at that ordinal — **including an explicitly empty list** |
| `RELATION_REGISTRY_SNAPSHOT` | the same, per event, for relation certificates |

The resolver reports three outcomes, never two:

| Outcome | Meaning | Decision |
| --- | --- | --- |
| `resolved` | we had it | detector runs |
| `known_absent` | we checked; the source held nothing | determinate block, `detector_did_run = False` |
| `missing` | we never checked | `MISSING_POINT_IN_TIME_KNOWLEDGE` |

A determinate block is a **conclusion**, not a gap, and is fingerprinted over
*what was found absent* — so "no certificate for A" and "none for A, B and C"
do not share a digest merely because they share a verdict.

Measured on the real bundle from §10, by removing only the two registry
snapshots and changing nothing else:

| Stream | Classifications | Strongest available claim |
| --- | --- | --- |
| as captured | 147 × `BLOCKED_SETTLEMENT_SEMANTICS` | "No proven `BINARY_COMPLEMENT` candidate existed … across 75 decision(s) on complete inputs." |
| registry snapshots stripped | 147 × `MISSING_POINT_IN_TIME_KNOWLEDGE` | "… replay inputs are incomplete for a definitive claim (`SETTLEMENT_KNOWLEDGE=INCOMPLETE_MISSING_OBSERVATION`; 75 decision(s) lacked point-in-time knowledge)." |

Same books, same fees, same code. The only difference is whether the system can
show it looked.

---

## 5. The bundle

Self-describing and hash-verified: an observation stream plus a manifest with
per-file hashes, the schema version, the observation window, markets, connection
epochs, per-kind counts, the **detector plan**, context-snapshot counts, and a
`supports_economic_replay` flag so it is knowable *before* running anything
whether the bundle carries what a decision needs.

**Bundle creation time is not knowledge time.** A bundle may be assembled long
after the events it describes. Every observation keeps the `observed_at` and
ordinal it was captured with; the manifest records assembly time separately, and
says so in the payload. Stamping assembly time onto old observations would make
the entire dataset "known" at assembly — the most complete form of lookahead
available.

**Decisions are not bundle contents.** A bundle holds observations; a decision
derived from observations is output. The capture tool writes its live
`DecisionRecord`s to a sibling `*.oracle.json`, never inside the bundle, because
a replay that was handed the verdicts could "agree" with them for free.

### Integrity is checked before anything is replayed

| Condition | Result |
| --- | --- |
| all hashes match, schema understood | `VERIFIED` |
| final record incomplete | `TRUNCATED_TAIL` — usable prefix, ends early |
| edited record, hash mismatch | **refused** |
| corrupt middle record | **refused** |
| missing manifest or observations file | **refused** |
| unknown schema version | **refused** |

Only the first two permit replay. A damaged bundle fails closed rather than
producing "no opportunities", which is indistinguishable from a clean run that
genuinely found nothing and reads as reassurance. A truncated *tail* is
different: a capture killed mid-write is ordinary, and Step 4's tolerant reader
policy applies.

---

## 6. The detector plan travels with the session

A replay evaluates only what the session said it was monitoring. Inferring
basket groups from event metadata at replay time would let a replay consider
work the live system never did, and the two would then diverge for reasons that
have nothing to do with economics.

So `DetectorPlan` — complement markets, basket member sets, quantities, balance
precision and the refresh policy — is captured with the session and stored in
the manifest. `predarb replay run` on a bundle without one exits non-zero and
says what to re-capture rather than guessing.

Balance precision is in the plan because it changes every fee **upper** bound
(A-14): a replay that assumed a different member class would produce different
economics from identical books.

### Context freshness is our policy, not a venue guarantee

`ContextRefreshPolicy` gives each knowledge dimension a maximum age — 30 minutes
for metadata and fees, 15 for settlement and relation knowledge by default.
Nothing in the API promises a market's notional or a series' fee configuration
cannot change mid-session, so treating an old observation as still current past
that window would be an assumption dressed as knowledge. Past it, the dimension
reports `INCOMPLETE_STALE_CONTEXT`, which is distinct from never having looked.

---

## 7. Completeness, judged per detector

Six dimensions: `BOOK_STREAM`, `LIFECYCLE`, `MARKET_METADATA`, `FEE_KNOWLEDGE`,
`SETTLEMENT_KNOWLEDGE`, `RELATION_KNOWLEDGE`.

Five statuses: `COMPLETE`, `INCOMPLETE_MISSING_OBSERVATION`,
`INCOMPLETE_STALE_CONTEXT`, `CORRUPT`, `NOT_REQUIRED`.

Completeness is **per detector**. A same-market complement never consults
relation knowledge, so a bundle with no relation snapshots reports
`RELATION_KNOWLEDGE=NOT_REQUIRED` for it and its absence claim stands. An
AT_MOST_ONE basket does consult it, and the same bundle blocks that claim.
Without this split every absence claim would degrade to the weakest dimension
present, and a complement result would be poisoned by a dimension it never read.

The strongest sentence a run may print is checked against three separate ways
"we found nothing" could be wrong — something *was* found, nothing was ever
evaluated, or evaluation ran on knowledge the bundle did not contain:

> No proven `BINARY_COMPLEMENT` candidate existed within `<scope>`, across N
> decision(s) on complete inputs.

Otherwise it degrades, naming the blocking dimensions and the count of decisions
that lacked point-in-time knowledge.

---

## 8. One orchestration, driven from two ends

`LiveSession` and `ReplayEngine` are mirror images, and the difference between
them **is** the no-lookahead argument.

Both drive the same `EvaluationCoordinator` over the same observations through
the same production detectors. What differs is the knowledge base behind the
context provider:

| | Knowledge base | Can it see forward? |
| --- | --- | --- |
| `LiveSession` | fed one observation at a time | no — it has not been handed the future |
| `ReplayEngine` | indexed from the finished stream | **yes**, and must decline |

So when a replay reproduces a live run's decisions exactly, that is not two
copies of a loop agreeing. It is a run that *held* every later observation
declining to use any of them. Deliberately breaking the horizon filter makes all
four equivalence tests fail with field-level detail, which is how we know the
comparison has teeth.

There is no `ReplayOrderBookReconstructor`, no `detect_binary_replay`, no
`detect_no_basket_replay`, and no replay-only pricing shortcut.

### Shared trigger policy

Equivalence is only meaningful if both paths evaluate at the same moments. A
replay sweeping every millisecond while live scanned only on book changes would
produce a different decision set and could not honestly claim to reproduce live
behaviour, even if every individual evaluation agreed.

`ScanTriggerPolicy` is one object consumed by both. Context changes are triggers
too — a fee change taking effect, a certificate arriving, evidence drifting —
because a book that has not moved can still become newly evaluable, and omitting
those would make a live scanner miss the moment an opportunity became provable.

One trigger touching three members of the same basket produces **one** basket
decision, not three: the basket is a single subject, and evaluating per member
would inflate every count derived from the decision list.

The **decision instant** is the local receive time of the observation that
triggered the evaluation, not an exchange timestamp we may not have possessed.

### `DecisionRecord`

Every evaluation produces one, and it preserves the distinction that matters:

```
detector_did_run = False      we lacked the context to ask
detector ran -> BLOCKED_…     the detector asked and said no
```

Collapsing those would let an under-captured replay report that a detector
examined a market and rejected it, when in truth it never ran. The converse is
deliberately *not* an error: a decision can be determinate without the detector
running, because an explicitly empty registry settles the question by itself.

A record carries the resolved `context_ids` — metadata hash, fee provenance,
multiplier, fee type, certificate identity, evidence fingerprint — so live and
replay are compared **component by component**, not by final verdict alone.

### A certificate may not vouch for itself

When a certificate exists but no *current* evidence was observed by the horizon,
the resolver returns `None` rather than substituting the certificate's own
fingerprint. That substitution would compare a certificate against itself, which
always matches, silently converting "nobody checked whether the rules moved"
into "the rules have not moved". Instead the detector runs and reports current
settlement evidence as unavailable — fail-closed, and visibly so.

---

## 9. Economic decision fingerprint

Comparing live and replay by equality would fail on incidental runtime detail —
a run id, a wall-clock execution time, log formatting — none of which says
anything about whether the economics matched.

The comparison is a digest over **material** inputs and results: decision time,
market or member set, quantity, book epoch/sid/seq, the exact source liquidity
ids a quote would consume, certificate and evidence fingerprints, fee context,
payoff per state, cost and profit intervals, and classification. Incidental
detail is never fed in rather than filtered out afterwards.

Mismatches are reported by **component name**, not as two opaque digests,
because "these hashes differ" is not something anyone can act on.

---

## 10. Results

### Synthetic full paths

No live market is certified, so the only way to exercise the *positive* path is
to construct history in which a certificate exists. Both detector families walk
their full transition sets through the real engine and the real Kalshi resolver.

**Binary complement** — 14 observations, 6 triggers, 6 decisions (5 ran):

| Transition | Classification | Detector ran |
| --- | --- | --- |
| T0 book valid, registry explicitly empty | `BLOCKED_SETTLEMENT_SEMANTICS` | no — known absent |
| T1 certificate issued | `PROVEN_CONTRACTUAL_ARBITRAGE` | yes |
| T2 book moves | `PROVEN_CONTRACTUAL_ARBITRAGE`, different fingerprint | yes |
| T3 fee change becomes effective | `BLOCKED_FEE_SEMANTICS` (A-14 gate) | yes |
| T4 evidence drift observed | `BLOCKED_SETTLEMENT_SEMANTICS` | yes |

**AT_MOST_ONE basket** — 27 observations, 11 triggers, 11 decisions (5 ran):

| Transition | Classification | Detector ran |
| --- | --- | --- |
| T0 both registries explicitly empty | `BLOCKED_SETTLEMENT_SEMANTICS` | no — known absent |
| T1 relation certificate issued, members uncertified | `BLOCKED_SETTLEMENT_SEMANTICS` | no — members known absent |
| T2 member certificates issued | `PROVEN_CONTRACTUAL_ARBITRAGE` | yes |
| T3 one member's book worsens | `PROVEN_NOT_PROFITABLE` | yes |
| T4 a member's evidence drifts | `BLOCKED_SETTLEMENT_SEMANTICS` | yes |

A relation certificate alone does not unblock the basket: the relation claim and
each member's payoff claim are **separate proof obligations**.

### Lookahead attacks

Every attempt is refused:

| Attack | Result |
| --- | --- |
| future book update | invisible before its ordinal |
| future metadata | unavailable; recorded as missing |
| fee change effective earlier, observed later | **not applied** |
| certificate issued later | does not unblock earlier decisions |
| relation certificate issued later | invisible |
| evidence drift observed later | does not invalidate earlier decisions |
| same-timestamp ordering | ordinal decides, not the clock |
| missing data tempting a live fetch | stays missing; no client is importable |
| certificate with no observed current evidence | blocks; no self-comparison |

### Real prospective session

A read-only capture recording the full context set, processed live through the
coordinator, then replayed entirely from disk with the network hard-blocked:

| | Session A (4 markets, 90 s) | Session B (3 markets, 60 s) |
| --- | --- | --- |
| observations | 28 | 86 (72 frames) |
| markets | 4, forming 1 real basket (`KXPRESPERSON-28`) | 3, forming 1 real basket (`KXMLBAL-26`) |
| fee context | 4 resolved — `quadratic`, ×1, series base | 3 resolved — incl. `quadratic_with_maker_fees` |
| settlement registry | consulted, **explicitly empty** | consulted, **explicitly empty** |
| relation registry | consulted, **explicitly empty** | consulted, **explicitly empty** |
| live decisions | 20 (16 binary, 4 basket) | 147 (75 binary, 72 basket) |
| replay decisions | 20 | 147 |
| **decision mismatches** | **0** | **0** |
| **book-state mismatches** | **0** (15 compared) | **0** (75 compared) |
| network calls during replay | 0 | 0 |

Each `MARKET_METADATA` observation carries the raw 53-field `/markets` response
alongside the normalised view and names the normaliser, so a recorded fact can
be re-derived if normalisation later changes. The fee snapshot carries the raw
series, event and fee-change payloads the timeline was resolved from — session B
included a series with a real scheduled change.

Earlier transport-only runs, for reference:

| Run | Observations | Book states compared | Mismatches |
| --- | --- | --- | --- |
| busy markets, 120 s | 13,392 | 13,385 | **0** |
| quiet markets, 120 s | 32 (21 idle polls) | 26 | **0** |

Every real decision is `BLOCKED_SETTLEMENT_SEMANTICS` with
`detector_did_run = False`. That is the correct and expected state: **no Kalshi
market has a human-reviewed settlement certificate**, the registries were
consulted and found empty, and the system therefore *knows* it cannot prove
anything about these markets. It is a determinate conclusion, not a gap — and
§4 shows exactly how far the claim weakens when the evidence for it is removed.

The positive path is consequently exercised only on synthetic history, and that
is a limitation of the *evidence*, not of the engine: the same coordinator, the
same detectors and the same resolver run in both. A real proven decision
requires a human to review a contract and issue a certificate, which is Step 8's
workflow and is deliberately not something this pass can do for itself.

The two earlier runs differ instructively. The first recorded a
`CONNECTION_FAILED` because the capture tool treated `WebSocketIdle` as a socket
failure, where the live collector pings and continues. The replay faithfully
reproduced that — but what was recorded did not match what the live path would
have done, so the capture was fixed rather than the comparison.

---

## 11. The prospective history boundary

We do **not** possess historical full-depth Kalshi WebSocket books for any
period before our prospective capture began. Rigorous arbitrage research starts
at the point raw order-book capture became available, and not earlier.

Candles, trades, last prices and midpoints are not substitutes for executable L2
depth: none of them says what quantity could actually have been bought at what
price. Synthesising depth from them for contractual-arbitrage research would
produce numbers with the shape of a backtest and none of its meaning.

---

## 12. Offline by construction

The replay package imports no HTTP or WebSocket client, no venue client, and no
registry that reads current state — asserted by a test that reads the module
source, and by tests that make every socket path fail loudly, including a full
CLI suite run with `connect`, `connect_ex`, `create_connection` and
`getaddrinfo` all blocked.

Missing historical context is reported as missing. It is never fetched, because
backfilling from today's API is precisely the leak the whole design exists to
prevent. There is no `--fill-from-api` flag.

```
predarb replay inspect   <bundle>   # describe without replaying
predarb replay verify    <bundle>   # integrity only, non-zero on failure
predarb replay knowledge <bundle>   # what it knew, by category, at the final horizon
predarb replay run       <bundle>   # replay both detector families, optional report
```

Capture, live processing and offline verification in one operator command:

```
uv run python tools/capture_replay_bundle.py --seconds 90 --markets 4
uv run python tools/capture_replay_bundle.py --basket-event KXPRESPERSON-28
```

A run report and an oracle file are **derived output, never source evidence**.
Bundles and oracle files live under a gitignored local research path; nothing
credential-bearing is recorded into either.
