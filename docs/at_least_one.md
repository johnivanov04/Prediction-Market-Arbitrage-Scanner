# AT_LEAST_ONE: the BUY-YES basket, and derived EXACTLY_ONE

`AT_MOST_ONE` says *no two of these can both win* and licenses a **NO** basket
(Step 9). `AT_LEAST_ONE` says *one of these must win* and licenses a **YES**
basket. They are mirror images, and this document is about the second.

**No AT_LEAST_ONE certificate has been issued.** Every live group is blocked on
relation semantics before any book is read.

---

## 1. Why a long-YES basket works

If at least one selected proposition must settle YES, then holding YES in all of
them guarantees at least one leg pays. Buy the set for less than that guaranteed
payout and the difference is contractual, not directional.

Book arithmetic: buying YES crosses **NO bids**, so `yes_ask = notional − no_bid`.
A basket of n YES legs costs `sum(N_i − no_bid_i)` and pays at least
`q · min_i(N_i)`. It is profitable exactly when the NO bids are collectively
rich — the mirror of Step 9, where rich YES bids make a NO basket cheap.

---

## 2. Only ALL-NO is forbidden

```
AT_MOST_ONE    forbids two or more YES;   ALL-NO is permitted
AT_LEAST_ONE   forbids ALL-NO;            two, three or all YES are permitted
```

Multiple winners are **valid** under AT_LEAST_ONE, and under this payoff model
they only raise the basket's payout. So the detector requires **only** the
AT_LEAST_ONE proof. It does not ask for AT_MOST_ONE, and asking would be a real
mistake: it would exclude events that are not mutually exclusive, which is most
of them — 65 of 94 sampled events carry `mutually_exclusive = false`.

A test asserts the detector's signature takes no exclusivity parameter, so the
requirement cannot creep back in.

---

## 3. The worst-case payoff theorem

For equal quantity `q` of YES in every member, where member *i* pays `N_i` on
YES and `0` on NO:

> **worst_case_payoff = q · min_i(N_i)**

*Proof.* Let `W` be the set of members settling YES in a permitted state.
AT_LEAST_ONE forbids only `W = {}`, so `|W| ≥ 1`. The portfolio pays
`q · Σ_{i∈W} N_i`. Since every `N_i ≥ 0`, removing a member from `W` cannot
increase the sum, so the minimum is attained at some `|W| = 1`. Among singletons
the payoff is `q · N_i`, minimised at `min_i(N_i)`. **QED**

### Preconditions, checked rather than assumed

| Precondition | Why the theorem needs it | How it is discharged |
| --- | --- | --- |
| `N_i ≥ 0` | a negative payout would make an extra winner *reduce* the total, moving the minimum to `\|W\| = n` | structural — `Price` refuses a negative value at construction |
| losing payoff `= 0` | the arithmetic assumes a losing leg contributes nothing | checked per member; a nonzero losing payout raises |
| no cross-member dependence | one member's payout must not depend on another's outcome | checked per member |

`UnprovablePayoffError` is raised rather than a best-effort bound returned. A
floor that might not hold is not a floor, and putting one into a profit interval
would claim a guarantee the contract does not give.

### Why not just enumerate the singletons

Feeding the n singleton states into the enumeration solver returns the same
number and would misrepresent the state space: it would record that the
portfolio was evaluated over n states when the relation permits `2ⁿ − 1`. A
later reader — or a later detector reusing that payoff object — would see a
state set that says "exactly one member wins", which is AT_MOST_ONE, a guarantee
nobody proved.

So the result is symbolic and says what it actually did: which theorem, which
preconditions held, which member witnesses the minimum, over how many permitted
states. One live event has **300** members; `2³⁰⁰ − 1` is a 91-digit number that
is computed and never built.

Tests cross-check the symbolic minimum against real enumeration of all `2ⁿ − 1`
states for n ≤ 6, and property-test it across random n, notionals and quantities.

---

## 4. Two proof obligations, neither sufficient alone

| Needed | Says |
| --- | --- |
| AT_LEAST_ONE relation certificate over the **exact** canonical member set | which joint outcomes are possible |
| a current settlement certificate **per member** | what YES pays in them |
| current valid books | what the legs actually cost |
| supported fee semantics on every leg | what the fees can be |

The relation alone gives no payouts; the individual certificates alone give no
joint constraint. The detector demands both and refuses a certificate whose
claim is not AT_LEAST_ONE — its state family is not interchangeable with
AT_MOST_ONE's.

### Supported payoff model

Step 12 supports only the standard binary model: YES pays the notional in the
YES state and nothing in the NO state. Scalar, fair-price, refund/void and
partially-winning tables are **refused**, not approximated. Those break the
theorem's second precondition directly.

---

## 5. Execution, fees and the profit interval

One immutable BUY-YES execution curve per member, from Step 5, unchanged. Depth
comes only from resting NO bids — never a midpoint, last trade or displayed
probability.

**Every leg must fully fill `q`.** A partially filled basket has no floor: the
guarantee is over the whole position. Any short leg gives `INSUFFICIENT_DEPTH`.

**Collisions block.** Detection runs across all n legs, not pairwise. Distinct
tickers normally imply distinct liquidity, but "normally" is not a guarantee and
consuming one source level twice would manufacture depth.

**Fees are independent per leg** (Step 6), each a separate hypothetical order
with its own bounds, multiplier status and balance-precision assumption. An
unknown fee is never zero: a single leg that cannot support a contractual claim
gives `BLOCKED_FEE_SEMANTICS` for the whole basket.

```
cost_lower   = gross + Σ fee_lower
cost_upper   = gross + Σ fee_upper
profit_lower = worst_case_payoff − cost_upper     ← the only figure a proof rests on
profit_upper = worst_case_payoff − cost_lower
```

| Condition | Classification |
| --- | --- |
| `profit_lower > 0` | `PROVEN_CONTRACTUAL_ARBITRAGE` |
| `profit_upper ≤ 0` | `PROVEN_NOT_PROFITABLE` |
| otherwise | `INDETERMINATE_COST_BOUNDS` |

Strictly positive means strictly positive. **Zero is not arbitrage**, and a test
pins the exact-zero case.

Acquisition cash is gross by leg. No `collateral_return_type` / MECNET netting
benefit is assumed — those semantics are unresolved (A-16), and counting relief
we cannot prove would inflate every profit figure.

---

## 6. Execution risk is not part of the proof

All n legs are separate, non-atomic orders. The contractual proof holds once
they are all filled; getting there is a race.

`execution_status = RACE_EXPOSED`, `leg_count = n`, `non_atomic = True`. There
is no `LOCKED` status and no order submission anywhere in Phase 1. The result
carries four independent status axes and deliberately no single `profit`, `fee`
or `confidence` field — a number that collapses a proof, a bound and a race
invites being read as a promise.

---

## 7. Bounded quantity search

`evaluate_quantity(q)` for one quantity, `search_yes_basket` for a sweep of
every 0.01 increment in `[min, max]`. Curves, certificates, relation semantics
and the fee context are built once and reused; rebuilding them per quantity
would be slower and would let a mid-sweep context change produce results
evaluated against different facts.

No pruning. A heuristic that skipped quantities would make "no candidate" mean
"no candidate among the ones we looked at". The searched interval, the step, the
evaluation count and whether depth truncated the sweep all travel with the
result, so a barren interval cannot be quietly promoted to a barren market.

---

## 8. Derived EXACTLY_ONE

```
EXACTLY_ONE(S)  ==  AT_MOST_ONE(S)  AND  AT_LEAST_ONE(S)
```

Composed at evaluation time from two independently reviewed certificates, and
**not** a third human-reviewed primitive. A single review could approve the
conjunction without either half being established — and the halves have
genuinely different evidence policies. AT_MOST_ONE asks whether two members can
both win, from the rules in front of the reviewer. AT_LEAST_ONE asks whether
every member can lose, which needs cancellation and void provisions and, on a
partition basis, evidence about the settlement domain.

### Derived, never stored

Persisting it would create a third record with its own lifecycle, staleness
rules and — inevitably — a way of outliving a parent it no longer matches. A
derivation recomputed on every decision cannot. That also gives the replay
semantics free: at a horizon where either parent was unknown, the derivation
simply does not happen, so a future-issued certificate cannot improve an earlier
decision.

### What must match, and what must not

**Must match**: the canonical member set, and each member's *settlement*
evidence — the per-member semantics both proofs were reviewed against.

**Must not be required to match**: the two aggregate evidence fingerprints.
Their policies differ, so they will always differ, and requiring equality would
make derivation impossible. Both are recorded so a derivation can be audited
back to what was approved.

Refusals are explicit: `MISSING_AT_MOST_ONE`, `MISSING_AT_LEAST_ONE`,
`WRONG_CLAIM`, `MEMBER_SETS_DIFFER`, `PARENT_STALE`, `PARENT_NOT_YET_VALID`,
`MEMBER_EVIDENCE_DIVERGED`, `NO_VALIDITY_OVERLAP`. There is no partial
derivation — half of EXACTLY_ONE is a claim we already have, and calling it the
conjunction would overstate it.

### State semantics

For n members, EXACTLY_ONE permits exactly **n** states — one winner in turn.
Not `n + 1` (AT_MOST_ONE's extra all-NO state is removed by AT_LEAST_ONE) and
not `2ⁿ − 1` (AT_LEAST_ONE's multi-winner states are removed by AT_MOST_ONE). A
test walks every subset and checks the derived permission equals the conjunction
of the two parents', which is where the `n` comes from rather than being
asserted.

### No EXACTLY_ONE detector

There is no `detect_exactly_one()`. The strategy map today is:

| Relation | Basket |
| --- | --- |
| `AT_MOST_ONE` | buy NO in every member |
| `AT_LEAST_ONE` | buy YES in every member |
| `EXACTLY_ONE` | both logical proofs available; no separate economics |

A future planner may exploit both directions at once. For now the derived proof
is a semantic and audit object.

---

## 9. Replay

The production detector, reached through the same `EvaluationCoordinator` a live
session uses. No replay-specific detector exists.

Relation certificates are indexed by **event and claim**. One event can carry
both an AT_MOST_ONE and an AT_LEAST_ONE certificate, and they forbid opposite
states, so a shared key would let a lookup for one return the other. A plan
asking for AT_MOST_ONE over members that only have an AT_LEAST_ONE certificate
finds nothing — which is the correct fail-closed answer, arrived at before the
detector is even called.

The synthetic timeline walks:

| | | |
| --- | --- | --- |
| T0 | registries explicitly empty | `BLOCKED_SETTLEMENT_SEMANTICS`, detector not run |
| T1 | relation certificate known, members uncertified | still blocked, now on member certificates |
| T2 | member certificates known | detector runs |
| T3 | book refresh | `PROVEN_CONTRACTUAL_ARBITRAGE` |
| T4 | one leg's NO bid collapses | `PROVEN_NOT_PROFITABLE` |
| T5 | fee change on an unmapped multiplier | `BLOCKED_FEE_SEMANTICS` |
| T6 | relation evidence drifts | `BLOCKED_SETTLEMENT_SEMANTICS` |

28 observations, 12 triggers, 12 decisions, 6 of which ran the detector. Live
and replay `DecisionRecord`s match field for field, and a bundle round-trip
preserves every decision and the run digest.

---

## 10. Live validation

26 events examined, 18 with two or more members, **0** with an AT_LEAST_ONE
relation certificate. Every group stops at `no_AT_LEAST_ONE_relation_certificate`
before any book is read.

Zero is the expected and correct result. No gate was weakened to produce a
number — a funnel that reached the economics by lowering a bar would be
measuring the bar.

---

## 11. Commands

```
uv run python tools/scan_yes_basket.py --events 40
```

Nothing here submits, cancels or prices an order, and no account endpoint is
reachable from any of it.
