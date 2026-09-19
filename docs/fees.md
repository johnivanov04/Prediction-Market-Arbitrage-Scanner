# The Kalshi taker fee engine

Phase 1, taker only, point-in-time, read-only. No profitability logic lives
here: this layer answers "what does this trade cost in fees?" and stops. It
contains no notion of edge, profit or arbitrage, and deliberately exposes no
function that could be mistaken for one.

The single most important output of this layer is not a number. It is the
**classification** attached to the number: whether a fee exists at all, how
exact it is, the proven interval it lies in, and whether its multiplier
semantics are established. A detector is allowed to rely on the number only as
far as that classification permits.

---

## 1. Five quantities, never collapsed

Conflating any two of these gives a wrong answer, so they are separate values
with separate types.

| Quantity | Definition | Type |
| --- | --- | --- |
| `raw_model_fee` | `M · rate · C · P · (1 − P)` | `Decimal`, unrounded |
| `trade_fee` | `ceil_6dp(raw_model_fee)`, charged per **fill** | `Money` |
| `rounding_fee` | realigns the balance to the member's precision grid | `Money` |
| `rebate` | returned from the per-**order** accumulator | `Money` |
| `net_fee` | `trade_fee + rounding_fee − rebate` | `Money` |

`raw_model_fee` stays a `Decimal` because it routinely carries more than six
decimal places; the documented worked example is `$0.00363825`. It becomes
money exactly once, at the ceiling step, through `Money.ceil_from_decimal()` —
the only sanctioned conversion. Rounding it earlier would pre-empt `ceil_6dp`
and change the charged fee.

No float appears anywhere in this chain. A property test asserts every monetary
output is an exact `Money` and that `raw_model_fee` is a `Decimal`.

### Worked example (from the official documentation)

1 contract at `$0.055`, non-direct member (`$0.01` balance grid):

```
raw_model_fee  = 0.07 × 1 × 0.055 × 0.945  = $0.00363825
trade_fee      = ceil_6dp($0.00363825)     = $0.003639
signed_revenue = −$0.055000                          (a buy is cash out)
unaligned      = −$0.055000 − $0.003639    = −$0.058639
aligned_change = floor_cent(−$0.058639)    = −$0.060000
rounding_fee   = −$0.058639 − (−$0.060000) = $0.001361
rebate         = $0                                  (nothing accrued yet)
net_fee        = $0.003639 + $0.001361     = $0.005000
```

The floor is toward **negative infinity**, not toward zero. Truncation would
give `−$0.050000` and every number after it would be wrong.

---

## 2. Per fill, per order, per account

Three different scopes, and the distinction is what makes pre-trade estimation
inexact.

- `trade_fee` and `rounding_fee` are computed **per fill**.
- The rounding accumulator is scoped to **one order** and persists across that
  order's fills. Not per market, per price, per fill, or global.
- Balance precision is a property of the **account**: `$0.0001` for direct
  members, `$0.01` for non-direct members.

`BalancePrecision` has no default. Which class applies cannot be discovered
without reading account data that Phase 1 has no business touching, and
guessing would make every `rounding_fee`, `rebate` and `net_fee` silently wrong
for half of all users while still being reported as exact. So it is required
configuration.

Multi-leg strategies get **one accumulator per leg**, because each leg is a
separate order. Sharing one would invent rebate timing the exchange would not
produce — and it is not cosmetic: two legs of 3 contracts at `$0.1234` cost
`$0.0596` as two orders and `$0.0496` as one, a full rebate step apart.

---

## 3. An L2 level is not a fill

This is the central limitation, and the reason the estimator layer exists.

Step 5 produces one fill slice per L2 price level. A price level is **aggregate
depth**: it may hold one resting order or a hundred, and a taker order crossing
it produces one exchange fill per resting order matched. The book does not
reveal that fragmentation, and the fee mechanics are per-fill. So the final
account-level fee depends on information that is not observable before
execution.

The question this raises is whether the fee is invariant to fragmentation
anyway, or failing that whether it can be bounded. Worked out below. In short:
the raw model fee is invariant, the net fee is **not** invariant for either
member class — and the net fee is nonetheless **bounded on both sides**, because
the venue's documented 0.01-contract granularity makes the number of fills
finite.

### 3.1 Fill count is finite — DOCUMENTED, so the rest follows

The venue documents `"Minimum granularity is 0.01 contracts"`. Every positive
fill is therefore a whole number of 0.01-contract increments, so `k` fills need
at least `k x 0.01` contracts:

```
k * 0.01 <= Q    =>    k <= Q / 0.01    =>    k_max = Q / 0.01
```

`Q = 0.01` permits one fill, `Q = 1.00` permits a hundred. This is the fact that
makes everything below possible: without a ceiling on `k`, the per-fill rounding
term has no ceiling either and no pre-trade bound exists.

An earlier revision of this document claimed the net fee was *unbounded* for a
non-direct member. **That was wrong**, and it was wrong in the direction that
matters: it would have forced a detector to discard opportunities it could in
fact reason about. Fragmentation is unknown, but it is finite.

### 3.2 The raw model fee **is** fragmentation-independent — PROVEN

For fixed `P`, `M x r x C x P x (1-P)` is linear in `C`, so for any split of `C`
into `c1 + c2 + ... + ck` the sum is identical. Exact, with no rounding before
summation. This is the one quantity stateable precisely before execution.

### 3.3 Maximum rounding fee per fill is `B - u` — PROVEN

Per fill, `rounding_fee = unaligned - floor_B(unaligned)`. Every term is an
exact whole number of micro-dollars: a 4dp price times a 2dp quantity is exactly
6dp (the scale invariant this codebase is built on), and `trade_fee` is 6dp by
construction. In micro-dollar units the rounding fee is therefore an integer
remainder `unaligned mod B`, which lies in `[0, B - u]` where `u = $0.000001`.

The upper end is attained, so the bound is tight rather than merely safe: a fill
with `unaligned ≡ -u (mod B)` rounds by exactly `B - u`. Measured maximum on a
direct member's `$0.0001` grid: `$0.000099`, exactly `B - u`.

### 3.4 The proven interval

Two identities do all the work. Writing `U_i = gross_i + trade_i` for each fill,
and using `aligned_i = -ceil_B(U_i)`:

```
net_total = SUM ceil_B(U_i) - gross_total - rebates        (identity 1)
net_total = SUM trade_i + final_accumulator                (identity 2)
```

Both are verified directly by test over hundreds of thousands of random orders
(`test_net_total_is_trade_total_plus_leftover_accumulator`).

**Lower bound — `ceil_6dp(F)`.** From identity 2: the accumulator only ever
holds overpayment awaiting rebate, so `final_accumulator >= 0`; and ceiling is
superadditive, so `SUM trade_i >= ceil_6dp(SUM f_i) = ceil_6dp(F)`. Hence

```
net_total >= ceil_6dp(F)
```

for any number of levels and any fragmentation whatsoever. This is the bound
published to callers as the floor, and it does **not** depend on the
one-fill-per-level assumption.

**Upper bound — `ceil_6dp(F) + k_max x B - u`.** Three facts combine:

```
SUM trade_i     <= ceil_6dp(F) + (k-1) * u     ceiling overshoot
SUM rounding_i  <= k * (B - u)                 section 3.3, once per fill
SUM rebate_i    >= 0                           rebates never add cost
```

so `net_total <= ceil_6dp(F) + (k-1)u + k(B-u) = ceil_6dp(F) + kB - u`, and
substituting `k <= k_max` from section 3.1 gives the bound. The ceiling-overshoot
step: writing `ceil(x) = x + d(x)` with `d` in `[0, u)`, the difference
`SUM ceil(f_i) - ceil(F) = SUM d_i - d(F) < k`; both sides are whole
micro-dollars, so it is at most `k - 1`.

**Zero quantity is a separate case.** With `k_max = 0` the upper-bound formula
would return `ceil_6dp(F) - u`, a *negative* bound. An order that acquires
nothing produces no fills, so all five fee quantities are exactly zero and both
bounds collapse onto zero. `net_fee_bounds()` short-circuits it rather than
trusting the arithmetic, and the algebra above assumes `k_max >= 1`. A non-zero
model fee paired with zero quantity is refused outright: the model fee is linear
in quantity, so that combination can only mean the caller paired a fee with the
wrong quantity.

**The bound is loose.** For a non-direct member buying 3 contracts it permits
$3.00 of fees on a $2 position, because it assumes all 300 permitted fills round
against you by a full cent and no rebate ever lands. That is acceptable: a loose
proven bound lets a detector say "not provably riskless", where no bound forces
it to say nothing at all.

### 3.5 One fill per level is the cheapest — PROVEN at a single price

Not assumed, and not merely observed. At a single price, comparing any split
against the single fill, with `D = SUM ceil_B(U_i) - ceil_B(U_single)`:

1. A single fill never earns a rebate: its accumulator reaches only
   `rounding_1 < B`, so zero whole steps are available. Hence
   `net_single = ceil_B(U_single) - gross`.
2. `D >= 0`, because `ceil_B` is superadditive and `SUM U_i >= U_single`. Both
   terms are multiples of `B`, so **`D` is a multiple of `B`**.
3. `SUM rounding_i = D + rounding_single - T`, where
   `T = SUM trade_i - ceil_6dp(f(C)) >= 0`.
4. Rebates never exceed rounding charged, so
   `R <= D + rounding_single - T < D + B`, since `rounding_single < B`.
5. `R` is a multiple of `B` and `R < D + B`, therefore **`R <= D`**.
6. `net_split - net_single = D - R >= 0`. ∎

Step 5 is where it turns: the rebate is quantised to `B`, so it can never claw
back more than the whole steps that fragmentation itself added.

**Multi-level: NOT proven.** The argument breaks when an order spans several
prices, because the baseline can itself strand accumulator value and the same
inequality no longer closes. Searched instead, with no counterexample found:

| Search | Configurations | Counterexamples |
| --- | --- | --- |
| All 9,999 prices, n = 2..5, both grids | 599,940 | 0 |
| Two-price orders, 3 multipliers, both grids | 105,300 | 0 |
| Randomised 1-4 levels, up to 25 contracts | 720,000 | 0 |

Status: **PROVEN** for a single price level, **OBSERVED** for multi-level. The
figure published to callers as the floor is `ceil_6dp(F)` from section 3.4,
which is proven in all cases, so no user-facing claim rests on the unproven
half.

### 3.6 Net fee is not fragmentation-invariant — for either member class

Measured, using our implementation of the documented mechanics:

| P | C | Member | 1 fill | 100 fills |
| --- | --- | --- | --- | --- |
| `$0.6789` | 3 | direct | `$0.045800` | `$0.045800` |
| `$0.6789` | 3 | **non-direct** | `$0.053300` | **`$0.963300`** |
| `$0.1234` | 7 | direct | `$0.053100` | `$0.053100` |
| `$0.1234` | 7 | **non-direct** | `$0.056200` | **`$0.136200`** |

An earlier revision called the direct-member column *invariant*. **That claim is
withdrawn.** It was empirical non-discovery promoted to an invariant, and it is
false. An exhaustive sweep over all 9,999 prices found 114 direct-member
configurations whose net fee changes with fragmentation. The smallest:

```
0.02 contracts at $0.0001, direct member:
    one fill   -> $0.000098
    two fills  -> $0.000198
```

Equality in the table above is therefore *equal in these examples*, not an
invariance. Both member classes are exposed; the non-direct class is merely
exposed about 100x harder, because its grid is 100x coarser.

#### Why fragmentation costs anything at all

Two documented rules interact badly at small fill sizes:

1. Each fill's balance change is floored to the member's grid, so each fill can
   contribute up to `B - u` of `rounding_fee`.
2. A rebate is **capped so that no individual fill's net fee goes negative**.

Rule 2 is the trap. A fill whose own fee is smaller than one balance step can
never afford a rebate, so the accumulator grows and nothing comes back.

The property suite states this exactly: *whenever the accumulator is left
holding a whole step or more, that fill's own fee was too small to return it*
(`test_a_stranded_step_means_the_fill_could_not_afford_it`). The
obvious-looking invariant — that carried rounding always stays below one step —
is **false**, and was falsified by Hypothesis rather than by inspection.

### 3.7 The official "converges" statement — reconciled

The API documentation says:

> The **fee accumulator** applies across all fills of an order so that the total
> fee converges to what a single equivalent fill would cost.

Our dust-fill results sit in tension with that sentence, so it was investigated
against current official sources rather than waved through.

| Question | Finding |
| --- | --- |
| Is our implementation consistent with the documented steps? | **Yes.** The five steps are implemented verbatim, and both official worked examples reproduce exactly. |
| Is the rebate cap based only on this fill's `trade_fee + rounding_fee`? | **Yes.** "capped so the fill's net fee cannot be negative" — and `net = trade + rounding - rebate >= 0` is exactly `rebate <= trade + rounding`. |
| Is there an end-of-order reconciliation or final refund? | **No.** Not on the fee-rounding page, not in the API changelog. |
| Is there documented fill aggregation that would make fragmentation impossible? | **No.** Fill granularity is not specified; the fills endpoint defines a fill only as "when a trade you have is matched". |
| Can the accumulator legally still hold value once an order completes? | **Yes — the documentation's own example does it.** Its three-fill example ends with `$0.002` carried and never returned. |
| Is stranded accumulator value defined on order termination? | **No.** Undefined in every reachable source. |

The documentation's own Example 2 therefore already exhibits a non-zero residue
at order end; it simply does not address a residue of a *whole step or more*,
which is what the dust case produces.

Conclusion, labelled honestly: the explicit mechanical steps imply the dust-fill
edge case, and this appears to qualify the informal word "converges" — which
reads as a description of the mechanism's intent under normal fill sizes, where
it does converge, rather than as a guarantee of strict fragmentation invariance.
**We implement the explicit mechanical steps and do not assume the stronger
invariance.** Status: mechanics DOCUMENTED; the edge case INFERRED from them;
the scope of "converges" UNKNOWN.

If Kalshi does perform an undocumented end-of-order refund, our bounds remain
correct — a refund can only reduce the fee, and our published interval already
extends down to `ceil_6dp(F)`.

## 4. The estimator's contract

`estimate_fees()` returns one of two **different types**, not one type in two
states.

### 4.1 `FeeQuote` — a fee exists

Carries every intermediate, the config provenance, the balance precision, the
segmentation assumed, `max_fill_count`, warnings, and always both bounds:

| `Exactness` | Meaning |
| --- | --- |
| `EXACT_ACTUAL_FILLS` | Computed from a known fill stream. Bounds collapse onto the value |
| `BOUNDED_ESTIMATE` | Point estimate plus the proven interval from section 3.4 |

There is deliberately **no `UNBOUNDED` state**. Fill count is provably finite
for any known quantity, so every computable quote is at least bounded; and a
configuration that cannot be computed is a different type rather than a weaker
enum value. `LEVEL_AGGREGATED_ESTIMATE` is likewise gone — the segmentation
assumption is recorded in its own field, where it belongs.

A construction-time check rejects any quote whose point estimate falls outside
its own bounds, so a derivation error cannot ship quietly.

### 4.2 `UnavailableFee` — no fee exists

Carries the ticker, the configuration, the quantities, and the reason. It
carries **no monetary fields at all**.

This is the fix for a real hazard in the previous revision, which returned a
quote full of zeros plus a warning. Zero is a genuine, reachable fee —
`fee_multiplier = 0` is live on 14 series — so a zero standing in for "unknown"
is a number a caller subtracts by accident. Now:

```python
total = sum(leg.estimated_net_fee.units for leg in legs)  # AttributeError
```

and under mypy, `Item "UnavailableFee" of "FeeQuote | UnavailableFee" has no
attribute "estimated_net_fee"`. Misuse fails at type-check time, not in a
spreadsheet three layers later.

### 4.3 Multiplier semantics gate the arbitrage claim

`supports_arbitrage_claim` is true only when `multiplier_status` is `IDENTITY`:

| `MultiplierStatus` | When | Effect |
| --- | --- | --- |
| `IDENTITY` | `fee_multiplier = 1` | Every reading of the schedule agrees; the A-14 ambiguity cannot change the number |
| `UNRESOLVED_MAPPING` | anything else | Numbers still reported, as a hypothetical; never load-bearing for a riskless claim |

See A-14: the schedule has separate maker and taker multiplier columns and the
API exposes one field, so for `M != 1` the figure rests on an unproven mapping.

### 4.4 Multi-leg

Each leg is its own order and gets its **own** accumulator; sharing one would
invent rebate timing the exchange would not produce. It is not cosmetic — two
legs of 3 contracts at `$0.1234` cost `$0.0596` as two orders and `$0.0496` as
one, a full rebate step apart.

Bounds add: if each leg's true fee lies in its interval, the total lies in the
sum of the intervals. **Every total is `None` if any leg is unavailable** — a
partial total would be a number that silently omits a leg.

### 4.5 Unknown account class

Balance precision is a property of the account, and Phase 1 does not read
account data. `BalancePrecision.unknown_member()` returns the **coarser**
non-direct grid, marked `assumed`. A bound derived on a coarser grid also holds
on a finer one, so this over-states fees for a direct member rather than
under-stating them for a non-direct one — the only safe direction. Property-
tested against both classes.

## 5. Failing closed

An unsupported configuration yields `UnavailableFee` rather than falling back to
the general quadratic formula. A fee wrong in the optimistic direction turns a
losing trade into an apparent arbitrage, so there is no default.

Refused: any `fee_type` outside `quadratic` (A-14 has the live census and why
the maker variants are not assumed to share the taker rate), maker liquidity,
and any notional other than `$1.0000` — the published `(1 - P)` term is written
against a `$1` payout and whether it generalises to `(N - P)` or to a normalised
`P/N` is undocumented.

A genuinely fee-free series is a different thing and is handled correctly:
`fee_multiplier = 0` gives `raw_model_fee = 0` — and **still incurs balance
rounding**, so `net_fee` is not zero. That is the case a careless detector would
assume has nothing to subtract, and it is why "unknown" and "zero" had to become
different types.

## 6. Point-in-time resolution

Fee configuration is resolved at an instant through `FeeTimeline.resolve_at(t)`,
never from "current" metadata. A change scheduled for tomorrow is invisible to a
book from today, in live scanning and in replay alike. Precedence, most specific
first: event scheduled change, event override, series scheduled change, series
base. Resolution is pure and ties break deterministically on `change_id`, so
replaying the same data twice gives the same answer.

If nothing is known for that instant, resolution returns `None` and the caller
must fail closed. There is no default fee configuration.

### Clearing an override

An event-scoped change with **both** override fields null is a *clearing*
record: the documentation states a null override removes any prior override and
the event falls back to its parent series. Treating it as missing data would
leave a superseded override in force forever — a point-in-time error that would
price every later book with a multiplier the venue had already withdrawn.

It falls back to whatever the **series** says at that moment, which may itself
be a scheduled change rather than the base. A change setting only one of the two
fields is neither an override nor a clearing, and is refused rather than
half-applied.

Status: **implemented from documentation, never observed.** Zero clearing
records appear in 100 sampled live event changes. The wire model accepts them so
ingestion does not crash the first time one appears;
`tools/validate_fees.py` reports the count on every run so the claim stays
honest.

---

## 7. What this layer does not do

No `is_arbitrage`, `profit`, `edge`, `ROI`, `guaranteed_payoff` or
`max_profitable_quantity`. Asserted by test, in both the engine and the
estimator, so it cannot drift in later.

Maker fees are not implemented. The model exists and the `0.0175` rate is
recorded, but maker execution brings queue position and fill uncertainty that
the current immediate-execution model does not cover. The types carry a
`Liquidity` axis so it can be added without reshaping them.

---

## 8. Coverage against production

Counted by `tools/validate_fees.py` on 2026-09-18, over **two separately named
universes**, because `/series` is known to be non-exhaustive (A-42).

**Listed series** (everything `/series` returns): 14,168

| State | Count | Share |
| --- | --- | --- |
| `BOUNDABLE_FOR_ARB` (quadratic, `M = 1`) | 13,973 | 98.62% |
| `CALCULABLE_ONLY` (quadratic, `M != 1`) | 32 | 0.23% |
| `BLOCKED_UNSUPPORTED_TYPE` | 163 | 1.15% |

**Addressable but unlisted** (named in `/series/fee_changes`, absent from the
listing): 23, all `margin_market_maker_program_fees`, all blocked.

**Combined**: 13,973 boundable of 14,191 (98.46%). The universe is the listing
plus fee-change tickers the listing omitted; it is a **lower bound** on what
exists, since a series in neither source would not be counted at all.

An earlier revision reported "14,004 / 14,167 priceable" with unsupported counts
of 160 + 3 + 23. Those do not reconcile: 23 of them belong to a different
universe, and "priceable" silently included 32 series whose multiplier semantics
are unresolved. `CoverageCensus` now refuses to hold counts that do not sum to
its own total, and `CombinedCoverage` refuses to exist without a written account
of how the universe was assembled.

Note the sampling trap this exposes. In `/series/fee_changes`, 55 of 147 rows
are quadratic — but only **7** carry `M = 1`. Scheduled fee changes are largely
promotional (30 rows at `M = 0`, 18 at `M = 0.5`), so that universe is in no way
representative of the series population.

## 9. Verification

- `tests/unit/test_kalshi_fee_model.py` — formula, rounding, refusals
- `tests/unit/test_kalshi_fee_engine.py` — documented worked example, the
  accumulator progression `$0.004 → $0.008 → $0.012 → rebate $0.010 → carry
  $0.002`, the rebate cap, stranded rounding
- `tests/unit/test_kalshi_fees.py` — exactness classification, multi-leg
  independence, measured fragmentation exposure
- `tests/property/test_fee_properties.py` — the fragmentation laws, rounding
  conservation, `net_fee ≥ 0`, splitting an order never costs less
- `tests/integration/test_fees_real_metadata.py` — real captured metadata
- `tools/validate_fees.py` — live read-only census (A-14, A-42)
