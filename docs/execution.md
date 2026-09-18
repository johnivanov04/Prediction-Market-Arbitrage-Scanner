# Executable Depth and Cost

How authoritative book state becomes *what can actually be bought right now, and
what it costs before fees*.

## 1. What this layer deliberately does not do

There is no profit here. No fees, no payoff, no expected value — and in
particular **no `max_profitable_size`**.

Profitability cannot be determined without fees, fee rounding, guaranteed
payoff, settlement semantics and every required leg. Computing a "profitable
size" in this layer would mean inventing some of those, and a number that looks
like profit but was computed without fees is worse than no number at all.

What this layer produces is a **curve**: cost as a function of quantity. Later
layers combine curves with fees and payoff constraints to find profitable size.
Keeping the curve free of economics is exactly what makes it reusable for every
strategy.

## 2. Authoritative book vs execution view

| | Authoritative book (Step 4) | Execution view (Step 5) |
| --- | --- | --- |
| Contents | Exactly what Kalshi sends: YES bids, NO bids | Derived asks for immediate acquisition |
| Mutability | Live, mutated by the reconstructor | Immutable snapshot |
| Purpose | Venue truth | Our interpretation of it |

Derived asks never enter `MutableOrderBook` or `BookView`. Mixing venue truth
with our arithmetic would make it impossible to tell later which is which.

## 3. Why asks are derived

Kalshi publishes **bids only**. To buy YES you cross resting **NO** bids:

```
yes_ask = notional - no_bid        depth = that NO bid's quantity
no_ask  = notional - yes_bid       depth = that YES bid's quantity
```

Verified live: a market with best YES bid `0.4900` and best NO bid `0.5000`
quotes a best YES ask of `0.5000` and a best NO ask of `0.5100`.

### Explicit notional

The complement uses `VenueInstrument.notional_value`, never a hardcoded `$1.00`.
Every Phase 1 binary market sampled has had a $1 notional, and reading the field
costs nothing while assuming it silently rescales every cost. `Price.complement`
*requires* the notional as an argument, so there is no call site where it could
be defaulted.

The derivation is only valid for a **binary** contract whose two outcomes
partition the payout. A scalar or unverified instrument raises
`UnsupportedExecutionSemanticsError` — it is not a universal truth about
prediction markets.

## 4. Book liquidity identity

A direct NO bid and the YES ask derived from it are **two views of the same
aggregate resting liquidity**. Consuming both would double-count depth that
exists once.

`BookLiquidityId` is keyed on the *source* bid — never the derived ask — so two
legs reaching for the same level produce equal ids regardless of which outcome
each is buying:

```
(connection_epoch, sid, market_ticker, source_outcome, source_price, book_seq)
```

### What it is not

The name is deliberately `Book`-prefixed, because the scope is narrow:

- **Not an exchange order.** Kalshi's depth is aggregated — one L2 level may
  contain many resting orders from many participants.
- **Not a queue position.** There is no intra-level ordering, so nothing can be
  said about fill priority.
- **Not a reservation, and not stable across book versions.** `book_seq` is the
  *view's* sequence number, which advances on any update to the sid, including
  unrelated markets. Two ids differing only in `book_seq` may well describe the
  same contracts; the type makes no claim either way.

Its Phase 1 purpose is exactly two things: **provenance** (tracing a derived
level back to its quoted source) and **collision detection within a single
view**. A future execution system that places orders will need real reservation
and version-consistency semantics; this is not that.

### Collision behaviour

`detect_liquidity_collisions` compares identities rather than re-deriving
arithmetic, and `multi_leg_costs` **refuses to aggregate** when one exists —
returning a summed cost would produce a number that looks executable and is not.
Step 5 does not resolve collisions; it makes ignoring them impossible.

Note what is *not* a collision: buying YES and buying NO on the same market draw
on two genuinely different bid sides, so those legs do not conflict.

## 5. Displayed price is not executable price

The best ask is a price for *some* quantity, often small. The price for the
quantity you actually want is a different number, and the only way to know it is
to walk the book. That is what the curve is for.

Live example: a book with 49 levels and 4.38M contracts of depth quotes 100
contracts entirely at the top level, but a larger order walks into progressively
worse prices. A detector that used the headline ask would overstate its edge at
every size above the top level.

## 6. The curve

One breakpoint per source level, each carrying cumulative quantity, cumulative
cost, VWAP, marginal price and the liquidity identity consumed.

`gross_cost(q)` evaluates at **arbitrary** quantities, not just breakpoints: a
request that stops halfway through a level pays that level's price for the part
it takes. Prefix sums plus a bisect, so repeated evaluation does not re-walk the
book.

Properties the later layers rely on, all property-tested:

- executable asks are ordered cheapest-first;
- `filled ≤ requested` and `filled ≤ depth`;
- gross cost is the exact sum of fill slices;
- cost is non-decreasing in quantity;
- marginal price never improves as deeper levels are consumed;
- the same immutable view always produces the same curve.

### Ordering is explicit

The best bid is the *highest* price, which derives to the *cheapest* ask. The
two orderings are inverses, so relying on the book's order would silently invert
the curve. Levels are sorted explicitly.

## 7. VWAP is not a price

`total_cost / total_quantity` is very often not representable on the venue's
4-decimal price grid. Three contracts at `0.0001` and one at `0.0002` average
`0.000125` — finer than a price can express.

Rounding that into a `Price` would be wrong twice: it loses value, and it
implies the number is something you could put on an order. A VWAP is an
**analytical ratio**; an order price is a grid point.

`AveragePrice` stores the exact numerator and denominator and renders to a
decimal only when a human needs to read it, with the precision stated at that
moment rather than baked into storage. `is_exact_on_price_grid` is available for
diagnostics and is **not** a licence to convert.

## 8. Partial fills

The default is a partial quote: "the book is thinner than you hoped" is ordinary
information, not an error. `filled_quantity`, `unfilled_quantity` and
`fully_fillable` say exactly what the displayed book would give.

`require_full_quantity` raises for research that needs all-or-nothing. Nothing
here submits an order — a quote is a statement about the current book, not an
instruction.

No slippage is fabricated beyond the visible book. If the book ends, the quote
ends.

## 9. Empty books

A VALID empty book is legitimate — observed live on markets that had finished
trading. Zero executable depth is the correct answer, returned as a valid
zero-depth result rather than an error. The book is not corrupt; there is simply
nothing to buy.

## 10. Only authoritative books may be quoted

`ExecutionContext` is passed explicitly and carries what made the book
trustworthy: the live epoch, journal health, connection health. Quoting is
refused for `WAITING_SNAPSHOT`, `INTEGRITY_UNKNOWN`, `UNSUBSCRIBED`, a stale
epoch, an unhealthy journal or an unhealthy connection.

There is deliberately **no `skip_validation` flag**. A replay caller must
*construct* the authority that applied at the moment being replayed, rather than
disabling a check — which means the reason a book was quotable is always
recorded rather than assumed.

### One exclusion is deliberately not blocking

`EXECUTION_BLOCKING_EXCLUSIONS` covers combo, provisional, non-binary and
missing-notional instruments. It does **not** include a missing or inconsistent
price grid.

The grid validates *quoted* prices; its absence says nothing about whether the
resting bids are real. Those bids are liquidity the venue published, and
refusing to price against them because tick metadata is missing would reject
genuine depth on a technicality. The derived ask is still bounds-checked against
`[0, notional]`, which is the check that matters here.

## 11. Immutability

A curve is a snapshot pinned to `(epoch, sid, seq)` and a construction time.
Levels are copied into tuples, so a later mutation of the live book leaves an
existing curve untouched. A caller holding a curve holds a fact about a specific
book state, not a live handle.

## 12. Multi-leg gross cost

Every Phase 1 structure buys the same quantity in several contracts. The
aggregator computes the common fillable quantity (the minimum across legs — one
empty leg makes it zero, which is the correct answer) and the total gross cost
at the union of the legs' breakpoints.

It decides nothing. The output is the input the fee and payoff layers consume.

## 13. Crossed and locked books are diagnostics, not faults

`best_yes_bid + best_no_bid ≥ notional` is economically interesting and exactly
the state later detectors will want to inspect. `complementary_spread` is
exposed as a diagnostic; such a book is **never** silently removed or repaired.

Sequence integrity and economic opportunity are different concerns. A book can
be protocol-perfect and economically strange, and conflating the two would
either hide real opportunities or excuse real corruption.
