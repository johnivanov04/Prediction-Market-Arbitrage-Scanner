# Logical relations and the AT_MOST_ONE NO basket

The first multi-market detector. Buy `q` NO in every market of a certified
mutually-exclusive subset: if at most one of them can settle YES, at most one NO
leg can lose, so the worst case is "every leg pays except one".

---

## 1. Two certificates, two proof obligations

| | Proves | Does not prove |
| --- | --- | --- |
| `SettlementCertificate` | what YES and NO pay in each of *this market's* states | anything about other markets |
| `RelationCertificate` | which **joint** YES combinations are possible | what any of them pays |

A basket needs **both**, per member and for the group. The relation alone is
economically empty: "at most one of these settles YES" says nothing about what a
NO pays, or what a void does. The settlement certificates alone say nothing
about whether two markets can both win.

**They are issued independently.** A relation review does not require its
members to already hold payout certificates: a human judges "can two of these
both settle YES?" from the rules, which is a different question from "what does
a NO pay here?". Requiring one first would impose an ordering the two proofs do
not have. What a relation review *does* require is complete semantic evidence
for every selected member -- rules text, rules hash, settlement metadata --
because without those the reviewer has nothing to read.

The **detector** then demands both, current and unstale, before any economics.
So certifying a relation early costs nothing in safety: members can be certified
afterwards, and the basket becomes evaluable without reissuing the relation
certificate, provided no semantic evidence drifted in between.

They are separate objects rather than one overloaded record, because a market
with proven payoff semantics and an unreviewed relation would otherwise look
identical to one with both.

A `RelationCertificate` proves **only**:

> Among these selected certified member markets, at most one may settle YES
> under the reviewed evidence.

Not that one must settle YES. Not that the set is exhaustive. Not that the
members share a notional, that fee semantics are resolved, or anything about
liquidity or price.

---

## 2. Why AT_MOST_ONE, and only AT_MOST_ONE

`GET /events` omits markets that settled before the historical cutoff (A-46), so
its market list is **current observed membership, never proven exhaustive
membership**.

**AT_MOST_ONE survives that.** Over a selected subset its states are: none of
the selected markets settles YES, or exactly one does. A winner *outside* the
subset — including a historical market the API never returned — is economically
identical, from the basket's point of view, to "all selected markets settle NO",
which is already an enumerated state. The NO legs all pay in that case, which is
the *best* outcome for the basket, so an unseen outside winner can only help.

**AT_LEAST_ONE, EXACTLY_ONE and PARTITION do not.** Each asserts that some
outcome must occur among a known set — precisely a completeness claim about a
set the venue does not guarantee is complete. A missing settled market is a
missing outcome, and a basket priced on "one of these must win" would be
unhedged against it.

None is implemented, and there is no convenience alias. The enums exist in
`domain/enums.py` so the storage schema needs no migration later; a test asserts
no detector module implements them.

Exhaustiveness may **not** be inferred from `mutually_exclusive` (a statement
about conflict, not coverage), the current market list, its length, or titles.

---

## 3. Selected subsets, not events

A certificate names an explicit, canonically sorted member set. "This event is
mutually exclusive" does not silently authorise markets added to it later.

- A new member requires new evidence and a new certificate.
- The old certificate stays valid **for its own subset** — a market appearing
  elsewhere does not make the reviewed subset's relation false.
- `covers()` is exact, not superset: a certificate reviewed over {A, B, C} is
  not a certificate about {A, B}, because the reviewer answered questions about
  the set as presented.

### Material evidence vs audit-only observation

Observed membership is captured and stored under a field literally named
`observed_event_membership_NOT_PROVEN_EXHAUSTIVE`, so nobody reading the record
mistakes it for an outcome universe. It is **deliberately excluded from the
material fingerprint**.

| Material (fingerprinted, drift invalidates) | Audit-only (retained, diffable) |
| --- | --- |
| event fields supporting mutual exclusivity | membership outside the selected subset |
| governing documents (by content hash) | |
| the canonical selected member set | |
| each selected member's semantic fingerprint | |

The reason is logical, not convenience: "at most one of {A, B, C} settles YES"
cannot be falsified by a market appearing or vanishing *outside* {A, B, C}.
Fingerprinting outside membership would expire certificates on unrelated venue
activity — churn that teaches reviewers to ignore drift, which is exactly what
drift detection must not become.

Changing the **selected** set is different: that requires a new certificate,
because `covers()` is exact. Subset certificates are not derived from supersets
even though AT_MOST_ONE is hereditary; that would be a proof-derivation
mechanism, and it should be explicit rather than hidden in a lookup.

---

## 4. The n+1 state space

For n selected members:

```
NONE_OF_SELECTED : every selected market settles NO
<ticker_1>       : member 1 settles YES, all others NO
...
<ticker_n>       : member n settles YES, all others NO
```

Exactly **n + 1** states — not `2**n` combinations filtered down. The forbidden
combinations are forbidden *by the certificate*, so enumerating and discarding
them would build a state space the relation says cannot exist and then rely on a
filter to remove it. Building only the reachable states means a two-YES state
never exists to be mishandled.

The extra state is named for the *subset*, not the event, because it covers both
"nothing in the event won" and "something outside the subset won".

---

## 5. Payoff is derived, never hardcoded

Each member's NO payoff is lifted from its own certified table into the joint
space: in joint state `T`, market `T` is in its YES state and every other member
is in its NO state. The certificate's YES state is read back from the payoff
table itself, so a certificate cannot disagree with itself about which state
pays.

The familiar closed forms then **emerge** from the generic engine:

| Case | Worst-case payoff |
| --- | --- |
| common notional `N` | `q x (n - 1) x N` |
| differing notionals | `q x (sum(N_i) - max(N_i))` |

Both are asserted in property tests rather than written into the detector. A
detector that hardcoded `q * (n - 1) * N` would be right for equal notionals and
quietly wrong for unequal ones.

Losing the largest NO leg is the worst case, which is why the maximum notional
is the one subtracted.

---

## 6. Execution, fees and cost

**Depth.** Every leg is quoted from its own executable BUY-NO curve — crossing
YES bids, never a midpoint, last price or headline probability. **Every leg must
fully fill `q`**; a partial basket is a different portfolio with a
state-dependent payoff.

**Collisions.** Checked across *all* legs, not pairwise. Different tickers
normally imply distinct liquidity, but "normally" is not a guarantee, and
double-counting one source level would manufacture depth that does not exist.

**Fees.** Each leg is a separate hypothetical order with its own accumulator and
its own bounds. If even one leg has an unresolved multiplier (A-14), an
unsupported fee type, or unavailable configuration, the basket is
`BLOCKED_FEE_SEMANTICS` and the blocking legs are named. An unavailable fee is
never counted as zero.

**No netting.** Acquisition cash is reported gross by leg.
`collateral_return_type` / MECNET semantics are unresolved (A-16), so no
exchange collateral-netting benefit enters the economics. It may be displayed as
metadata; it may not reduce capital in a classification.

```
cost_lower  = gross + sum(fee_lower_i)
cost_upper  = gross + sum(fee_upper_i)
profit_lower = worst_case_payoff - cost_upper    <- the only figure a proof uses
profit_upper = worst_case_payoff - cost_lower
```

`profit_lower > 0` is `PROVEN_CONTRACTUAL_ARBITRAGE`; `profit_upper <= 0` is
`PROVEN_NOT_PROFITABLE`; anything between is `INDETERMINATE_COST_BOUNDS` and is
never called arbitrage. Exactly zero is not arbitrage.

A basket profits exactly when the members' YES bids sum above the notional — the
n-market generalisation of a crossed complement.

---

## 7. Execution risk is worse, and separate

An n-leg basket is more race-exposed than a two-leg complement: n separate,
non-atomic orders, any of which can miss. Results carry `RACE_EXPOSED`,
`leg_count` and `non_atomic = True`. There is no `LOCKED` status and will not be
while the system places no orders.

---

## 8. Drift

A relation certificate stops applying for live use when any of these move:

- the relation evidence fingerprint (event fields, selected set, documents)
- **any selected member's settlement evidence** — the joint claim was reviewed
  against those payoff tables
- the member set itself

Historical records are never rewritten. `active_at()` will not return a
certificate issued after the instant asked about, so a replay cannot inherit a
certification nobody had yet.

---

## 9. The human boundary

`mutually_exclusive = true` is strong structured evidence. It pre-populates a
review request. It **cannot certify one**.

The ten-question checklist forces explicit answers on ties and co-winners,
cancellation and DNP, scalar settlement, whether every member is individually
certified and current, and — directly — whether the claim is only at-most-one
with no exhaustiveness assertion. `UNCERTAIN` blocks. Approval requires the
literal phrase `APPROVE AT_MOST_ONE <REQUEST_ID>`.

A request whose members lack individual settlement certificates is
`EVIDENCE_INCOMPLETE` and cannot be approved at all.

### Live discovery, 2026-09-21

300 open events scanned, **57 mutually exclusive**, 4 captured:

| Metric | Value |
| --- | --- |
| relation review requests generated | 4 |
| awaiting human review | 0 |
| blocked `EVIDENCE_INCOMPLETE` | **4** |
| candidate member markets | 20 |
| members with an individual settlement certificate | **0** |
| events with a verified relation certificate | 0 |

All four are blocked for the same reason: no member carries an individual
settlement certificate. That is the gate working. The queue —
`KXNEXTNATOSECGEN-99`, `KXNEWPOPE-70`, `KXNEXTDNCCHAIR-45`,
`KXMILLENNIUMNEXT-45` — is left for a person.

Running this found a real bug in our own code first. The initial run reported
"57 mutually-exclusive events, 0 with at least two members", which looked like a
plausible fact about the venue. It was not: `with_nested_markets=true` nests
markets inside the event object while the envelope's top-level `markets` key
stays empty (A-47), and we were reading the empty one.

---

## 10. Terminology

Never write "complete event outcome set" for an AT_MOST_ONE certificate. The
correct phrase, used in the certificate evidence text, the detector warnings and
the audit output, is **selected certified mutually-exclusive subset**.
