# Phase 2C — Next-venue qualification: Polymarket US / QCEX

Research only. No adapter, no transport, no credentials, no orders. Branch
`phase-2`, built on frozen `phase-1` HEAD `f77569f`.

**Verdict: FAIL.** Polymarket US does not pass the phase-2C gate. The remaining
unknown is not live price.

---

## 0. The correction this phase had to make

An earlier pass in this same phase screened the Polymarket US DCM Rulebook for
the phase-1 failure modes and found none of them: no "fair allocation", no "last
traded price", no rounding rule, no discretionary allocation path, and a
Contract Outcome definition that names both sides in both branches. On that
evidence the venue looked materially cleaner than Kalshi and ForecastEx.

It is not. **The rulebook is clean because it specifies no product's payout.**
Rule 10.2 sends every specification to "the rules governing such Contract", and
the rulebook supplies no payout arithmetic for anything. The numbers live in the
Part 40 product certifications — and the athletic certification contains both of
the things the rulebook does not.

Screening a venue's rulebook is not a screen of the venue. Recorded as A-63.

---

## 1. Gate 0 — single-contract terminal payoff

| Source | Says |
|---|---|
| Rulebook Rule 1.1, "Contract Outcome" | Payout Condition satisfied → $1.00, Settlement Amount to longs; not satisfied → $0.00, Settlement Amount to shorts |
| Rulebook Rule 1.1, "Settlement Amount" | "the fixed amount required to be paid by the Seller to the Purchaser on the Settlement Date if the Payout Condition is satisfied" |
| **Rule 9.101(D)**, first two bullets | long $1.00 / short $0.00, and long $0.00 / short $1.00 — **both sides named in both branches** |
| **Rule 9.101(D)**, third bullet | **"If the Outcome is a tie …, then each long and short AEC position shall receive fifty cents ($0.50)."** |

The ordinary branches are the most explicit terminal-payout language found in
either phase. Nothing is proven by silence; the phase-1 `both_branches_explicit`
gate is satisfied on the first reading, which neither Kalshi nor ForecastEx
managed without hunting.

The tie is a **third terminal state**. It conserves the notional exactly
($0.50 + $0.50 = $1.00) while disproving strict two-state binariness — the A-62
distinction with the sign reversed from Kalshi `ENTITYOUTCOME`, where a
fractional payout came with an *unknown* complement.

### The blocker: Rule 9.101(K), "Cancellation"

> If an event is canceled prior to any Outcome determination, the Exchange, in
> its sole and absolute discretion, resolve any remaining open positions in a
> manner that it deems fair and appropriate, which may include a final settlement
> based on **last-traded prices**, **$0.50 per contract**, or **other fair and
> equitable valuation**. All such determinations by the Exchange shall be final
> and binding.

Nothing constrains the two sides to sum to the notional. "Last-traded prices" is
a single number, not a pair, and the rule does not say the short receives the
residual. "Other fair and equitable valuation" states no arithmetic at all.

This is Kalshi Rule 6.3(c)(b) in substance, relocated from the rulebook to the
product certification — and it was invisible to a rulebook screen.

**`ComplementConservationProof.status` = `COMPLEMENT_CONSERVATION_DISPROVEN`.**

### Mechanism census for Rule 9.101

| Mechanism | Rule | Status |
|---|---|---|
| `ORDINARY_BINARY` | 9.101(D) bullets 1–2 | PROVEN_COMPLEMENTARY |
| `TIE_SPLIT` | 9.101(D) bullet 3 | PROVEN_COMPLEMENTARY (but not binary) |
| `CANCELLATION_LAST_RESULTS` | 9.101(K) Cancellation | **UNRESOLVED, reachable — blocks** |
| `INDETERMINATE_FALLBACK` | 9.101(K) Postponement | UNRESOLVED |
| `OUTCOME_REVIEW` | Rulebook 10.4 + Rule 1.1 | PROVEN_COMPLEMENTARY |
| `VOID_REFUND` | Rulebook 2.8(d)(iii) | **excluded — `RESIDUAL_VENUE_INTERVENTION_RISK`** |

Closure is **not** established: Section K governs irregularities "not covered in
Section D" as an enumerated list, but Rulebook Rules 10.3, 10.4 and 2.8(d) reach
the same contracts and the interaction is unstated.

**Scope note (applied in phase 2D).** Rule 2.8 emergency authority is now
excluded from the semantic proof and disclosed as
`RESIDUAL_VENUE_INTERVENTION_RISK`. **The verdict is unchanged.** Polymarket US
never depended on the emergency power to fail: Rule 9.101(K) cancellation is a
product clause reachable on any rained-off game and stays inside the proof, as
does 9.101(K) postponement. That is exactly the narrowness the exclusion is
designed to preserve — a venue does not get to reclassify a product settlement
clause by pointing at its emergency chapter.

Rounding: `UNSPECIFIED`. The three enumerated outcomes land on the cent grid, so
ordinary settlement has nothing to round; the cancellation path does, because
last-traded prices quote to $0.001 and no rule says how a payout derived from
them becomes payable, or whether the short's share is the residual.

### What is genuinely better than Kalshi

Rule 10.4's Contract Outcome Review output is **typed**: "the Company may
determine the final outcome of a Contract", and Contract Outcome is defined as
one of two values with the Settlement Amount going to a named side. Kalshi's
Rule 6.3(c)(b) authorised a "fair allocation" and never said what was being
allocated. Rule 10.3(b)'s indeterminacy remedy is likewise a timing remedy
("may at its sole discretion adjust the Expiration Date"), not a payout remedy.
Rule 2.8(d)(iii) emergency cancellation is a bounded refund, not a valuation.

Those are real improvements. They are not enough, because a different and
untyped path is also reachable.

---

## 2. Clearinghouse

QC Clearing LLC, Clearing Rulebook 2025-12-31, sha256 `a5a91041b08f8e1e…`, 60
pages, extracted clean. Novation is the standard model; full collateralisation;
Rule 5.3(d) pays "in accordance with the relevant Contract Rules"; offset is
restricted to "Contracts with the same terms and conditions", i.e. same-contract
only, so it is no escape for a cross-market basket. **The clearing rulebook
defines no payout of its own.**

It is also **four months older than the DCM rulebook that cites it**. The
clearing version actually in force on 2026-08-05 has not been read. Recorded as
`DependencyClosure.UNKNOWN` — the phase-1 fail-closed rule applied to a currency
gap rather than an unreadable PDF. Neither source history claims exhaustiveness.

---

## 3. Rule 1.5 precedence — UNRESOLVED

> Notwithstanding any provision of these Rules to the contrary, the Product
> Specifications … shall govern the applicability of these Rules to **trading in**
> such Contract and, in the event of any conflict between these Rules and the
> Product Specifications, the Product Specifications shall govern **with respect
> to trading in** the relevant Contract. In the event of any conflict between the
> Product Specifications and the Contract Terms, the Contract Terms shall govern
> **with respect to trading in** the relevant Contract.

The "notwithstanding" opener is as strong as precedence language gets. The
operative scope is stated three times as *trading in* the Contract, and
settlement is not trading.

* **Reading A** — product terms govern settlement. Sections D and K are titled
  "Settlement" and "Additional Settlement Conditions"; on reading B the venue has
  no stated payout for a tie at all. A tied event pays **both sides $0.50**.
* **Reading B** — the Rules' Contract Outcome governs settlement. It is
  exhaustive on its face over the Payout Condition's two truth values, and it is a
  Rule. A tie is not a third truth value of a predicate, so a tied event is a
  Payout Condition not satisfied and the short receives **$1.00**.

No published interpretation, FAQ or advisory reconciling the two was located.
`ControlFinding.UNRESOLVED` → `PrecedenceStatus.PRECEDENCE_UNRESOLVED`, which
fails closed.

The two readings are not cosmetic — a basket built on either is wrong half the
time. Structurally this is the Kalshi Rule 7.1 vs. 6.3(c) problem with the
documents swapped: there, two rules in one book; here, a book and a certification
separated by a precedence clause whose scope does not quite reach the question.

---

## 4. Product families

Located and read in full: **Athletic Event Contracts** (Rule 9.101, 2025-09-30)
and **Combinatoric Athletic Outcome Contract** (`ptc0520263802`, 2026-05-20).
A February 2026 filing (`ptc02092638974`) certifies Reality Television
Contracts; its Attachment A was not in the retrieved PDF. Other athletic
families named in Polymarket US material — Athletic Outcome, Athletic Qualifier,
Athletic Statistic, Athletic Rank, Athletic Achievement, Athletic Tie — were not
located as separate certifications; the athletic terms retrieved cover match
outcomes under one rule. **The 2025-09-30 certification is the most recent
athletic filing located; a later amendment may exist and was not found.**

### CAOC — the strongest relation language at any venue reviewed

> **Joint Probability:** Every outcome must be satisfied for the Contract to
> resolve to $1.00. The Contract resolves to $1.00 **if and only if** every leg
> is satisfied. If any single leg is not satisfied, the Contract resolves to
> $0.00, regardless of the outcomes of any remaining unsettled legs.

Notional $1.00; tick $0.001–$0.01; position accountability $25,000 notional; no
position limits; margin 100% of at-risk. Underlying: "the joint Settlement
Amount of two or more underlying Contracts". Accelerated Settlement fires when a
leg settles against its side before the latest leg's expiration.

This is an explicit logical AND stated as a biconditional — not an implication
to be argued, and nothing about correlation.

---

## 5. Relation classes

From the biconditional alone, with no price assumption:

* **AT_LEAST_ONE** — long the combination, short every leg. All-satisfied pays
  the combination; any single failure pays that leg's short; no state pays
  nothing. Worst case one notional.
* **NESTED_IMPLICATION** — the combination implies each leg, one proof per leg.
  The converse fails, since the other legs may fail.

### Why both break

CAOC models its constituents as settling **"$1.00/$0.00"**. Per §1, an athletic
leg has a $0.50 tie state. With one leg tied and the other satisfied:

| | pays |
|---|---|
| combination (leg A not satisfied) | $0.00 |
| short leg A (tie) | $0.50 |
| short leg B (satisfied) | $0.00 |
| **total** | **$0.50** |

The worst case is **half the notional**. Verified by exhaustive enumeration over
every assignment in `tests/unit/test_polymarket_us_qualification.py`, not
argued: two-state legs give a floor of $1.0000, tri-state legs give $0.5000.

The alternative reading — that a tie is a state the Payout Condition does not
address — is not better. It leaves the combination with no stated outcome.

**`RelationFeasibility.SEMANTICS_UNRESOLVED`.** Not `NO_USEFUL_RELATION`: the
relation is real and better drafted than anything at Kalshi or ForecastEx. Not
`PROVEN_BUT_VENUE_MECHANICS_ELIMINATE_EDGE` either — see §7. What blocks is
which document governs the legs.

---

## 6. Fees

Published at `docs.polymarket.us/fees`.

* Taker: `Fee = Θ × C × p × (1 − p)`, Θ = 0.0695; max $1.74 per 100-lot.
* Maker rebate: Θ = −0.0125; max −$0.31 per 100-lot.
* **Combinatorial taker: `Fee = C × p × [0.0695 × (1 − p) + 0.04 × (1 − p)⁴]`** —
  the combination carries its own, larger schedule, which matters because the
  combination is a leg of every basket above.
* All fees and rebates rounded to the nearest $0.01 using **banker's rounding**.
* Volume rebate tiers at 10 / 25 / 50%.

**Fees are deducted from balance at trade execution, not from the settlement
amount.** So they are an addition to the cost side of every basket inequality
and never reduce the payoff — which is the favourable direction, and the
opposite of a venue that nets fees out of settlement.

---

## 7. Economic feasibility — no venue mechanic closes the edge

Rule 10.1(c) sets a $0.001 minimum quote increment; Rule 9.101(F) bans orders
below $0.001 or above $0.999. Rule 9.101(I) sets margin at 100% of at-risk: the
purchaser posts the trade price, the seller one dollar minus it.

**Nothing ties the two sides' prices to each other.** There is no analogue of
ForecastEx Rule 401(d), which fixes an inverse pair at $1.01 and makes the
same-market complement arb exactly −$0.01 by construction. A long/short pair may
be quoted as low as $0.002 under the rules, and `is_algebraically_impossible`
returns `False` at that floor. (That is a statement about what the rules permit,
not about what a book will show.)

So Polymarket US is the first venue in this research where the edge is *not*
foreclosed by venue algebra. The blocker is entirely semantic.

---

## 8. API feasibility — and one finding that constrains what is permissible

REST plus gRPC streaming. `GET /v1/orderbook/{symbol}` returns L2 depth;
`/bbo` returns top of book; symbols look like
`tec-nfl-sbw-2026-02-08-kc`; messages carry `transactTime`; REST is
snapshot-only and rate-limited; gRPC exposes `MarketDataSubscriptionAPI`.

**Correction, carried deliberately:** the documentation states that read-only
market data requires **Auth0 JWT authentication with `read:marketdata` scope**
("No Participant ID Required", no KYC). An earlier search summary in this phase
reported "no authentication"; that was wrong. Market data is therefore **not
documented as public**, so the standing authorisation — *"a plain unauthenticated
read-only validation is allowed only if documented as public and needed to
validate API semantics"* — **does not apply**, and no live call was made.

The remaining structural unknown, undocumented either way: whether a
combinatorial contract has its own independent order book or a book derived from
its legs. That determines whether the basket in §5 is even simultaneously
executable, and it cannot be resolved from documentation.

---

## 9. Shallow comparative screens

### Rothera (ROTHDCM)

DCM Rules Version 1, 2026-05-20, sha256 `26084b165a930e18…`, 105 pages,
extracted clean.

| | |
|---|---|
| A. Terminal payout exhaustively specified? | **No — and not even attempted.** Zero occurrences of "payout", "notional", "Expiration Value", "shall receive", "event contract". The rulebook defers wholly to Contract Specifications (Rule 1.16(A): published on the Website). |
| B. Discretionary/fair-price settlement paths? | Unknown at the rulebook layer; 9 "sole discretion", 39 "emergency", 14 "void", 47 "cancel". Zero "fair price", "fair allocation", "last traded price". |
| C. Related markets abundant? | Not determinable without the specifications. |
| D. Combinatorial/parlay structures? | No sign — zero "combo", "parlay", "Payout Condition"; 3 "combination". |
| E. Matching eliminates arbitrage? | Not determinable. Binary Options are fully collateralised, with no Reasonability Levels or No Cancellation Ranges (Rules 10.9–10.10). |
| F. Read-only market data public? | Not established. |

Precedence is *weaker* than Polymarket's: the only "shall govern" clauses cover
NDA conflicts and CEA/CFTC conflicts. There is no clause elevating specifications
over rules at all.

**Verdict: INSUFFICIENT_EVIDENCE.** The rulebook cannot answer question A, and
the real work would be at the contract-specification layer — exactly where
Polymarket US fails. Nothing suggests it would fail differently.

### ProphetX (PROPHX)

The only authoritative document retrieved is `orgdcmprophxexhibgl251205.pdf` —
a **DCM Core Principles Chart** (Exhibit L, v1.0, approval date October 2025),
sha256 `a85d2ea3ee43c121…`, 25 pages. It is a compliance mapping, **not a
rulebook**: 6 "cancel", 1 "proportion", and nothing else relevant.

**Verdict: INSUFFICIENT_EVIDENCE.** No rulebook and no product certification
were obtained, so none of A–F can be answered. Sports-focused and CFTC-approved
as DCM+DCO in June 2026, which makes it structurally interesting — but
"interesting" is not a screen result and is not recorded as one.

---

## 10. Decision gate

| Gate | Result |
|---|---|
| 1. Exhaustive contractual settlement semantics | **FAIL** — Rule 9.101(K) cancellation is a discretionary valuation with no stated arithmetic, so conservation is *not proven* (not disproven); mechanism closure not established; Rule 1.5 precedence unresolved |
| 2. Useful related/partitioned market structures | **PASS on the relation, FAIL on its legs** — CAOC states an explicit biconditional AND, the best relation language found in either phase; its legs reach a $0.50 state it does not model, halving the basket floor |
| 3. No venue mechanic algebraically eliminating opportunity | **PASS** — no inverse-pricing identity; the edge is reachable in principle |

**Polymarket US / QCEX: FAIL.** The only remaining unknown is not live price. It
is which document governs settlement, and that is a question about text, not
about markets.

Neither shallow screen produced a comparative verdict: both return
`INSUFFICIENT_EVIDENCE` on the material actually obtained. Rothera's rulebook
shows the same structure — payouts pushed entirely into contract specifications —
without Polymarket's precedence clause; ProphetX yielded no rules at all.

---

## 11. What would change the verdict

Three specific documents, none of which required inference to identify:

1. **A Polymarket US interpretation of Rule 1.5's scope** — anything stating
   whether product-level settlement clauses control at settlement, not only at
   trading. This single answer decides §3 and most of §5.
2. **An amended athletic certification** removing the "other fair and equitable
   valuation" language from Section K, or defining it as a complement-preserving
   split. Gate 1 turns on this clause alone.
3. **A CAOC amendment stating what happens when a leg settles at $0.50** — either
   admitting the state into the Payout Condition or excluding tie-capable
   contracts from eligibility as legs.

Absent those, the correct classification of every payoff claim at this venue is
UNKNOWN, and UNKNOWN blocks.

---

## Verification

```
pytest                 2943 passed, 2 skipped, 8 deselected
ruff check .           All checks passed
ruff format --check .  249 files already formatted
mypy src tests         Success: no issues found in 211 source files
tools/security_scan.py 0 hits across all five scopes
```

No credentials were configured or used. No orders were placed. No live API call
was made to any venue. Downloaded certifications and rulebooks remain in the
gitignored local research path and are referenced here by sha256 only.
