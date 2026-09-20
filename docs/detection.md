# Payoff, settlement certification, and the binary complement detector

The first end-to-end detector. It combines authoritative books, executable
depth, exact gross costs, proven fee intervals and verified settlement
semantics to decide whether buying YES and NO in one market is profitable in
**every** allowed settlement state.

This is the first place the system may emit something called a
contractual-arbitrage candidate, so most of this document is about the
conditions under which it refuses to.

---

## 1. Settlement states and state-independent profit

A **settlement state** is one terminal way a market can resolve. A **payoff
table** says, per contract, what each state pays. A portfolio's payoff in a
state is the sum over its positions, computed exactly:

```
position payoff = payout_per_contract x quantity
```

A 4dp price times a 2dp quantity is exactly 6dp, so nothing rounds.

A portfolio has **state-independent profit** when its *worst-case* payoff
exceeds its worst-case cost. Worst case means the minimum over every allowed
state — not an average, not a likely case. There is no probability anywhere in
this layer and there will not be: a claim about every state does not depend on
how likely any state is. Introducing a weighting would turn a proof into an
estimate.

For the complement, `q` YES + `q` NO pays `q x notional` in both states, so the
worst case equals the best case. That result **emerges from the payoff engine**
evaluating the certificate's tables; no detector hardcodes "payout = q".

### Unknown is not impossible

The engine refuses to evaluate a portfolio whose positions do not specify a
payoff for every allowed state. A missing state is not a zero-payoff state and
not a zero-probability state. A worst case computed over the states someone
remembered to list is not a worst case.

---

## 2. Why `market_type == "binary"` is not enough

This is the single most important gate in Step 7.

Kalshi's rules acknowledge markets that can resolve to a **fair-market value**
or carry a **did-not-play** adjustment, and those outcomes are governed by the
per-series contract terms rather than by any API field. A contract can carry
`market_type == "binary"` and still have a terminal value that no two-state
table describes.

If that happens and we assumed `{0, notional}`, the complement's "guaranteed"
payout would simply not arrive, and a portfolio sold as riskless would lose
money. So `market_type` is treated as a description, never as evidence.

Equally inadmissible as evidence: the title, the ticker, the event category and
the yes/no subtitles. None of them specify settlement.

---

## 3. The settlement certificate

A machine-readable, dated, evidence-bearing assertion about one market's payoff
table:

| Field | Purpose |
| --- | --- |
| `market_ticker`, `notional` | what it is about |
| `rules_hash` | the exact rules text it was verified against |
| `settlement_model` | `STANDARD_BINARY_COMPLEMENT`, `SCALAR`, `NON_STANDARD`, `UNKNOWN` |
| `allowed_states`, `yes_payoff`, `no_payoff` | the complete payoff tables |
| `status` | `VERIFIED` / `REVIEW_REQUIRED` / `REJECTED` / `INVALIDATED` |
| `evidence`, `verified_by`, `verification_method` | who asserted this, and on what basis |
| `verified_at`, `valid_from`, `valid_to` | when it applies |

Only `VERIFIED` permits a proof. `REVIEW_REQUIRED` is the default for anything
unexamined, so a market nobody has looked at fails closed by construction.

Two structural guarantees:

- A `VERIFIED` certificate **cannot be constructed without** evidence, a named
  verifier, a verification method and a rules hash. A bare assertion is not a
  proof, and a certificate with no hash could never be invalidated.
- A certificate claiming `STANDARD_BINARY_COMPLEMENT` is **checked at
  construction**: exactly two states, and YES + NO must pay exactly the notional
  in both. A mislabelled certificate fails immediately rather than silently
  producing a wrong worst case.

There is deliberately no `from_instrument`, `from_market_type` or `infer`
constructor. Asserted by test.

### Evidence-fingerprint invalidation

The certificate binds to a **settlement evidence fingerprint**, not to the rules
text alone. At the point of use:

```
current_fingerprint != certificate.evidence_fingerprint  ->  INVALIDATED
current_fingerprint is None                              ->  INVALIDATED
```

Unavailable evidence invalidates for the same reason changed evidence does: if
we cannot *show* the evidence is unchanged, we must not assume it.

A rules hash alone is too narrow. A market's economically relevant settlement
evidence spans the notional, the market type, early-close conditions, expiration
metadata, strike definitions, the parent event's structure, the series'
settlement sources and any external contract document. Any of these can change
while `rules_primary` stays byte-identical, which would otherwise permit:

> rules unchanged, materially relevant settlement evidence changed,
> certificate silently still valid.

`rules_hash` survives as a separately visible field because it is the component
a reviewer reads first and the most useful one in a diff — but it is no longer
the binding.

The fingerprint keeps **per-component hashes**, so drift is explained rather
than merely detected: `CHANGED: series.settlement_sources` tells a reviewer
whether to re-read the contract or just re-approve a cosmetic field. "The
aggregate hash changed" would not.

Encoding rules that matter:

- **absent, null and empty are three different facts** and hash differently. A
  field that vanished from the API is not a field that arrived as `null`.
- **floats are refused**, not normalised — there is no canonical text form that
  round-trips, so accepting one would make the digest depend on how a value was
  decoded.
- **external documents are hashed by content, never by URL.** A URL that did not
  change is not evidence that its contents did not change.
- **volatile fields are excluded.** Price, volume, open interest and book state
  are not settlement semantics. Including them would invalidate every
  certificate on every tick, which trains reviewers to ignore drift.
- the **schema version is part of the digest**, so a change to the encoding
  itself cannot silently reconcile two different bundles.

Building the live evidence bundle — fetching the event, series and contract
documents — is Step 8. Step 7 ships the binding and the drift semantics.

### Scalar and non-standard settlement

The payoff engine represents any finite state set, including three-state and
scalar-style tables — domain mathematics is kept deliberately wider than venue
support. But Phase 1 **certifies none of them**. If a market can settle to a
fair-market value, a DNP adjustment, a 50/50 split, a scalar value, a void or
refund, or anything unknown, and no explicit complete payoff specification
exists, contractual-arbitrage classification is blocked.

`tests/unit/test_payoff.py` includes the demonstration: a YES+NO portfolio that
looks state-independent across `{YES, NO}` stops being so the moment a refund
state exists that the table did not account for.

---

## 4. Cost: exact gross, bounded fees

**Gross cost is exact.** It comes from executable depth — walking the opposing
side's resting bids — never from a displayed, midpoint or last price. Step 5
established that; Step 7 only consumes it.

**Fees are an interval.** Step 6 proved the true net fee lies in
`[ceil_6dp(F), ceil_6dp(F) + k_max x B - u]`, where fill fragmentation is
unobservable but finite. So the cost is an interval too:

```
minimum_possible_total_cost = gross_cost + fee_lower_bound
maximum_possible_total_cost = gross_cost + fee_upper_bound
```

There is no `fee` field anywhere when the value is an interval, and no `profit`
field. The names are `profit_lower_bound` and `profit_upper_bound`, which cannot
be misread as a number to bank.

### The profit interval

```
profit_lower_bound = worst_case_payoff - maximum_possible_total_cost
profit_upper_bound = worst_case_payoff - minimum_possible_total_cost
```

| Condition | Classification |
| --- | --- |
| `profit_lower_bound > 0` | `PROVEN_CONTRACTUAL_ARBITRAGE` |
| `profit_upper_bound <= 0` | `PROVEN_NOT_PROFITABLE` |
| `lower <= 0 < upper` | `INDETERMINATE_COST_BOUNDS` |

The third case is **not** arbitrage and is never described as one. Fee
uncertainty prevented a proof; that is the finding.

Arbitrage requires **strictly** positive guaranteed profit. Exactly zero is
`PROVEN_NOT_PROFITABLE`. The boundary is one micro-dollar wide and is tested on
both sides.

### A structural consequence for non-direct members

The fee upper bound admits up to one balance step of unrebated rounding per
fill, and one contract permits 100 fills across two legs. So the admissible
rounding per contract is:

| Member class | Admissible rounding per contract |
| --- | --- |
| direct (`$0.0001` grid) | `2 x 100 x $0.0001 = $0.02` |
| non-direct (`$0.01` grid) | `2 x 100 x $0.01 = $2.00` |

A complement can pay at most the `$1.00` notional per contract. **So a
same-market complement can never be proven for a non-direct member at any
quantity**, and shrinking the size does not help — the margin shrinks with it
while the per-fill cent does not. For a direct member `$0.02` leaves real room.

This is a property of the bound, not of any particular book. It is recorded
rather than worked around: the honest output for such an account is
`INDETERMINATE_COST_BOUNDS`.

When the member class is unknown, the conservative non-direct grid is used, so
the answer is never accidentally the optimistic one.

---

## 5. Contractual proof is not execution safety

`PROVEN_CONTRACTUAL_ARBITRAGE` means exactly:

> if every leg fills at the quoted depth, the payoff exceeds the cost in every
> allowed settlement state.

It does **not** mean the trade can be executed. The two acquisitions are
separate, non-atomic orders; the book can move between them, and one leg may
fill while the other does not — leaving a one-sided position with a
state-*dependent* payoff.

The axes are therefore separate, and never collapsed into a confidence score:

| Axis | Values |
| --- | --- |
| `semantic_status` | `VERIFIED`, `BLOCKED` |
| `payoff_status` | `STATE_INDEPENDENT_PROFIT`, `NON_POSITIVE`, `INDETERMINATE`, `NOT_EVALUATED` |
| `cost_status` | `EXACT`, `BOUNDED`, `UNAVAILABLE` |
| `execution_status` | `DEPTH_VERIFIED`, `RACE_EXPOSED`, `STALE_OR_INVALID` |

Every profitability claim carries `RACE_EXPOSED`. There is **no `LOCKED`
value**, and there will not be while the system places no orders — asserted by
test.

---

## 6. Blocking conditions

The detector checks semantics before economics, and depth before fees, so an
encouraging number never exists to be quoted out of context.

| Classification | Cause |
| --- | --- |
| `BLOCKED_SETTLEMENT_SEMANTICS` | certificate not `VERIFIED`, rules hash changed or absent, wrong market, notional disagreement, non-binary settlement |
| `INSUFFICIENT_DEPTH` | either leg cannot fully fill `q` |
| `BLOCKED_LIQUIDITY_COLLISION` | the two legs would consume the same resting level |
| `BLOCKED_FEE_SEMANTICS` | fee unavailable, unsupported type, or unresolved multiplier (A-14) |
| `BLOCKED_BOOK_INTEGRITY` | book not `VALID`, stale connection epoch, unhealthy journal |

Both legs must **fully** fill at `q`. A partial fill is a different portfolio,
and half a complement has a state-dependent payoff.

On collisions: buying YES crosses NO bids and buying NO crosses YES bids, so
the two legs normally consume disjoint liquidity. "Normally" is not a guarantee,
and double-counting one aggregate level would manufacture depth that does not
exist, so the check runs anyway.

An unavailable fee is never treated as zero. Zero fees would make a candidate
look *more* profitable, which is the dangerous direction.

---

## 7. Bounded search, and what a null result licenses

`evaluate_quantity(q)` is authoritative for exactly that `q`.
`search_binary_complement(min, max)` sweeps every 0.01-contract increment in
`[min, max]` — exhaustive **within that interval and nowhere else**.

The result carries its own limits: `search_min_quantity`, `search_max_quantity`,
`search_step`, `evaluated_quantity_count`, `search_complete`. A barren sweep
licenses exactly one sentence:

> No proven candidate in `[q_min, q_max]`.

Never "no arbitrage exists". That would require covering the entire feasible
domain with every semantic and cost input proven.

The interval reported is the one **actually covered**: if displayed depth runs
out below `max`, the reported maximum is lowered and a warning says so, rather
than claiming to have searched quantities that could only ever have been
`INSUFFICIENT_DEPTH`.

The caller chooses the cap. There is no built-in economic limit, because any
universal cap would be an invented assumption about what size is worth looking
at.

`classification_counts` tallies **every** evaluation, not just retained ones —
retention is a memory decision, and deriving the funnel from retained results
would under-report by default.

Optimisation so far is limited to what is obviously correct: execution curves
are built once per book state and reused across quantities, and the fee
configuration is resolved once. Any future pruning must be proven to return the
identical candidate set.

---

## 8. Purity and auditability

The detector performs **no I/O**: no network, no database, no clock of its own.
The evaluation instant is passed in. Fees arrive through a caller-supplied
callable, so the module imports nothing from `venues`, `ingest` or `storage` —
a detector cannot tell it is looking at Kalshi. Asserted by a test that reads
the module source.

That is what lets live scanning and replay call the identical function.

Every result retains enough to reproduce it: detection timestamp, ticker,
quantity, connection epoch, sid, book seq, raw journal ids, both quotes with
their fill slices and liquidity ids, certificate identity and rules hash,
allowed states, payoff in every state, fee provenance and bounds, gross cost,
cost interval, profit interval, classification and warnings.

---

## 9. The detector is a canary

Kalshi's matching engine mints complementary pairs, so a persistent, executable,
fee-surviving YES+NO violation inside a single market should be unusual.

**If the live scanner reports many large opportunities, suspect our own stack
first**: complement arithmetic, book inversion, sequence corruption, fee
handling, stale state, duplicated liquidity. Concluding that the venue is
handing out free money is the last hypothesis, not the first.

The detector's real value is that it exercises the whole pipeline against a
payoff invariant that can be checked by hand.

### Live observation, 2026-09-18

8 markets, 90 seconds, `--max-quantity 1.00`, unknown member class:

| Metric | Value |
| --- | --- |
| markets monitored / with VALID books | 8 / 8 |
| quantities evaluated | 700 |
| gross complement candidates (pre-fee) | 0 |
| best observed gross margin | **-$0.010000** (`KXGREENLANDPRICE-29JAN21-NOACQ`) |
| emitted proven candidates | 0 |
| blocked by settlement semantics | 8 (all) |
| hypothetically eliminated by fees | 700 |

Zero proven candidates is the expected and healthy result. The best margin is
negative — the closest market was a full cent away from crossing — so nothing
was even gross-profitable before fees.

All eight markets were blocked on settlement semantics, because **no live
market carries a `VERIFIED` certificate**. Issuing one requires a person to read
that market's contract terms and stand behind the payoff table; the scanner
never mints one. It writes each market's exact rules hash and rules text to
`certificate_review_requests.json` for deliberate human review instead.

The "hypothetical" column exists so that blocking on semantics does not hide
whether the economics were close. It asks what each market *would* classify as
if its semantics were verified, is labelled hypothetical throughout, and is
never emitted as a candidate. Here it confirms the gate is not masking anything:
all 700 evaluations would have failed on fees regardless.
