# Arbitrage Definitions

This document is the reference for what the system is allowed to claim. It is
deliberately conservative: the cost of a missed opportunity is zero, and the
cost of a false one is a real loss on a trade that was never riskless.

---

## 1. The four categories

The system never uses the word "arbitrage" for anything except the first row.

| Category | Claim | What must be proven | Phase 1 |
| --- | --- | --- | --- |
| **True contractual arbitrage** | Profit is guaranteed by the contracts themselves | Worst-case payoff over *every* valid settlement state exceeds all-in executable cost | Detected |
| **Statistical mispricing** | Prices look wrong relative to a model | Requires a model, and is therefore a *view*, not a proof | Out of scope |
| **Stale-market opportunity** | A price is behind the market | Requires proving the quote is stale *and* still executable | Reported as data quality, not edge |
| **Relative value** | One contract is cheap relative to another | Requires a view on the relationship | Out of scope |

A candidate that fails any part of the contractual-arbitrage test is **not**
demoted into one of the other categories as a consolation. It is rejected, with
the reason recorded.

---

## 2. Formal definition

Let `S` be the set of valid terminal settlement states for a portfolio, and
`portfolio_payoff(s)` the total cash the portfolio pays in state `s`.

```
worst_case_payoff = min over s in S of portfolio_payoff(s)
```

A **contractual arbitrage** exists if and only if:

```
worst_case_payoff > all_in_executable_acquisition_cost
```

where the cost uses actual available depth across all required legs, the fees
in effect at that moment, and the exchange's documented rounding.

Three things this definition deliberately excludes:

- **Expected value is irrelevant.** A portfolio that is profitable in 99% of
  states and loses in one is not arbitrage. The minimum is what matters.
- **Probability is irrelevant.** We never weight states.
- **`>=` is not enough.** Equality is not an opportunity, and given fee
  rounding it is usually a rounding artefact.

### 2.1 Enumerating `S` is the hard part

Most false arbitrage comes from an incomplete `S`, not from bad arithmetic. If
a state is missing from the enumeration, the minimum is taken over too small a
set and the worst case is overstated.

The commonest missing state, by far, is **"none of the above"** in a basket
that was assumed exhaustive. See §5.

`S` is therefore derived only from a `VERIFIED` relation plus per-instrument
settlement specs. If any member instrument has
`SettlementKind.UNKNOWN` or `SCALAR`, `S` cannot be enumerated and the payoff
status is `UNKNOWN` — never an arbitrage claim.

---

## 3. Displayed price vs executable price

The single most common source of fake edge.

A displayed best bid/ask is a price for *some* quantity, often one contract. It
says nothing about the price for the quantity you need. A portfolio is only
real at a size where **every** leg is simultaneously available.

Accordingly:

- Profitability is computed from **VWAP across consumed levels**, never from
  top-of-book.
- No opportunity is ever computed at a quantity that is unavailable on one or
  more legs.
- The record names the **limiting leg** — the one that runs out first.
- Profit is evaluated at **book-level quantity breakpoints**, so we can report
  maximum profitable size and maximum fillable size, which are usually
  different numbers.

On Kalshi there is an extra wrinkle: **the book contains bids only**, so every
ask is derived from the opposing side's bid (A-06). A "YES ask" is an
arithmetic consequence of a NO bid, and consuming it consumes that NO bid's
depth. Double-counting the same resting interest on both sides would invent
liquidity that does not exist.

---

## 4. Payoff certainty vs execution certainty

These are independent and are stored as separate axes.

**Payoff certainty** (`PayoffStatus`) asks: *once the portfolio is held, is
profit guaranteed?* It is a statement about contracts.

**Execution certainty** (`ExecutionStatus`) asks: *can the portfolio actually
be acquired at the assumed prices?* It is a statement about the market and our
data.

A portfolio can be `STATE_INDEPENDENT_PROFIT` and `RACE_EXPOSED` at the same
time: guaranteed profit once all legs are filled, with no guarantee that all
legs *will* fill at those prices. Between the first and last fill, prices can
move, and a partially-filled arbitrage is simply a directional position that
nobody chose to take.

Phase 1 places no orders, so `LOCKED` is unreachable by construction.

Related risks tracked on the record rather than folded into the profit number:

- **Capital lockup.** Capital is committed until settlement, which may be
  months. A 2% guaranteed return over nine months is a different proposition
  from 2% over a day, and the record carries the expected settlement timing.
- **Settlement risk.** `disputed` and `amended` are real market statuses
  (A-17). Determination is not always final.
- **Fee-schedule risk.** Fees can change on a schedule (A-12).

---

## 5. Mutual exclusion is not exhaustiveness

These are different properties and the difference is the most dangerous
modelling error in this system.

- **Mutually exclusive** (AT_MOST_ONE): at most one outcome is YES. Zero is
  allowed.
- **Exhaustive** (AT_LEAST_ONE): at least one outcome is YES. More than one may
  be allowed.
- **EXACTLY_ONE**: both of the above, and only then.

Kalshi's `mutually_exclusive` flag maps to **AT_MOST_ONE only** — its official
description says "only one market in this event *can* resolve to yes", which
permits none of them resolving YES (A-15).

Why the confusion is expensive: a YES basket across a mutually exclusive but
non-exhaustive group has a worst case of **zero**, because every market may
resolve NO. Treating the group as exhaustive assigns it a worst case of the
notional, and turns a portfolio that can lose everything into a "guaranteed
profit".

Concretely, on a five-candidate election market where "someone else" is a real
outcome: buying YES across all five for $0.98 total looks like a two-cent
arbitrage if you assume exhaustiveness, and is a total loss if an unlisted
candidate wins.

`EXACTLY_ONE` is therefore never inferred from venue metadata. It requires a
human reading the rules and recording why the group is exhaustive.

---

## 6. The Phase 1 structures

Throughout: `q` is the quantity per leg, `V` is the per-contract notional
(`notional_value_dollars`, A-18 — **not** assumed to be $1.00), and all costs
are all-in including fees.

### 6.1 Binary complement

For one binary market, buy `q` YES and `q` NO.

If settlement is truly binary, exactly one side pays, so the portfolio pays
`q * V` in every state.

```
profit(q) = q*V - executable_YES_cost(q) - executable_NO_cost(q) - fees
```

**This detector is primarily a correctness canary, not an edge source.**
YES and NO interest on Kalshi are structurally linked by the exchange: the
book is bids-only and the opposing side's ask is *derived* from it (A-06). A
persistent YES+NO arbitrage would be close to the exchange contradicting its
own matching.

So: if this detector fires often, the correct first hypothesis is that **our
book reconstruction or fee logic is wrong**, not that we found free money.
A rising count here should be read as an alert on the pipeline. The most likely
culprits are double-counting derived depth against quoted depth (§3), a stale
book, or a missing fee.

### 6.2 AT_MOST_ONE — the NO basket

For `N` markets where at most one can resolve YES, buy `q` NO in each.

- If one resolves YES: the other `N-1` NO contracts pay → `q * V * (N-1)`
- If none resolve YES: all `N` NO contracts pay → `q * V * N`

Worst case is the first:

```
worst_case = q * V * (N - 1)
arbitrage  iff  all_in_NO_basket_cost(q) < q * V * (N - 1)
```

**Exhaustiveness is not required**, which is what makes this the most useful
Phase 1 structure: Kalshi's `mutually_exclusive` flag supports exactly this
relation and no more.

Note the worst case *decreases* as a fraction of cost when `N` is small: at
`N = 2` the basket pays only `q*V`, so it is just a complement trade in
disguise.

### 6.3 AT_LEAST_ONE — the YES basket

For `N` markets where at least one must resolve YES, buy `q` YES in each.

At least one pays, so the worst case is at least `q * V`:

```
arbitrage  iff  all_in_YES_basket_cost(q) < q * V
```

If more than one can resolve YES the payoff is higher, but the worst case is
what the classification uses.

**This relation cannot come from Kalshi metadata.** There is no exhaustiveness
flag. Every AT_LEAST_ONE relation in the system is human-verified against the
contract rules, with the evidence recorded.

### 6.4 EXACTLY_ONE

EXACTLY_ONE is AT_MOST_ONE **and** AT_LEAST_ONE, so both baskets are evaluated:

- the YES complete-set basket, against a worst case of `q * V`
- the NO basket, against a worst case of `q * V * (N - 1)`

Since exactly one resolves YES, the all-NO state is impossible and the NO
basket's payoff is exactly `q * V * (N - 1)` in every state.

**EXACTLY_ONE is never inferred from `mutually_exclusive`** (§5, A-15).

---

## 7. What rejects a candidate

Any one of these is fatal, and the reason is recorded and counted:

| Rejection | Rule |
| --- | --- |
| Unverified relation | Relation is not `VERIFIED`, or its rules hash changed since proof |
| Unknown settlement | Any member has `SettlementKind.UNKNOWN` or `SCALAR` |
| Stale input | Any input book older than the staleness budget |
| Book integrity unknown | Any input book has an unresolved sequence gap |
| Insufficient depth | Requested quantity unavailable on any leg |
| Eliminated by fees | Gross edge positive, all-in edge not |
| Non-standard instrument | MVE, provisional, or non-`binary` market type |
| Unverified fee schedule | No effective fee schedule with verified constants (A-14) |

The last row is a live gate, not a permanent block: the general fee constants
are verified as of 2026-09-15 (taker `M*0.07*C*P*(1-P)`), so series on the
standard schedule pass it. A series whose multiplier or fee type we cannot
resolve at the relevant timestamp still fails closed.

---

## 8. What a valid negative result looks like

"We observed zero genuine executable arbitrages over N market-hours" is a
result, and the system is built so that it would be a *trustworthy* one. That
requires the same rigour as a positive finding: the deterministic test suite
must have zero false positives, the synthetic true arbitrages must be detected,
and the book reconstruction must be validated against REST state.

The failure mode to guard against is not missing an opportunity. It is
loosening a threshold, widening a staleness budget, or assuming exhaustiveness,
until something finally fires — and then believing it.
