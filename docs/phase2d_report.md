# Phase 2D — Rothera Exchange and Clearing qualification

Research only. No adapter, no transport, no credentials, no orders. Branch
`phase-2`.

**Verdict: FAIL — but for a different reason than any previous venue, and by a
much narrower margin.**

Rothera is the first venue in this research whose product terms state the
residual construction outright. Single-contract complement conservation is
genuinely close to proven. What fails is the *cross-market* floor: every
contract conserves perfectly within itself while nothing ties a partition's
members together.

---

## 1. Current governing documents

Retrieved 2026-09-29 from the exchange's own regulatory index,
`rothera.io/reg-notices`, cross-checked against the CFTC filing chain. Rothera
is the former LedgerX, by way of MIAXdx, now a Robinhood and Susquehanna joint
venture, registered as both DCM and DCO.

| Document | Version | Pages | Extraction | sha256 |
|---|---|---|---|---|
| **DCM Rulebook** | 2026-05-20 | 105 | CLEAN | `26084b165a930e1c47c3ab78ef523859eae79e5b76e8a20de8deed928814bced` |
| **DCO Rulebook** | 2026-05-20 | 101 | CLEAN | `7f644f206b98a79e940d1a038665688d253d3858f7a2e7ae3b8f51ed4c4e8c9e` |
| **Fee Schedule** | 2026-05-20 | 1 | CLEAN | `23457e6e3bfa95f7653a59bfefb443eeebbb6267f2f88782aae05b8237326629` |
| Common-terms amendment | 2026-06-17 | 3 | CLEAN | `92580557c0ead57b86985a2b4fbd60271528bdcc540046b25ff2cd3989b26d4f` |

The DCM hash is **identical** to the copy taken independently during the
phase-2C shallow screen. That is how 2026-05-20 is established as current rather
than merely latest-found.

**Amendments since May 2026.** One material filing: a 40.6(a) self-certification
dated 2026-06-17, effective trade date 2026-07-02, replacing "position limit"
with "position accountability level" for seven products. It touches no
settlement term — but it proves certifications are **amended in place without
being reissued**, so the filings read below state a superseded position-limit
term, and a content hash alone does not establish currency. No further DCM or
DCO rulebook amendment was posted through 2026-09-29.

Prior versions exist under the LedgerX and MIAXdx names and were not collected;
neither `SourceHistory` claims exhaustiveness, so this record may not support a
negative dating verdict.

---

## 2. Governing graph

```
DCM Rulebook 2026-05-20 ──┬─> Contract Specifications (Rule 1.16(A), published on Website)
                          ├─> Rule 7.2 "Procedures" ──> procedures published on Website  [NOT LOCATED]
                          └─> Rule 1.11 Emergency Rules
DCO Rulebook 2026-05-20 ──┬─> Rule 5.1 Novation ──> "all terms ... must conform to the Contract Specifications"
                          ├─> Rule 5.2 Settlement of Contracts  (no payout formula)
                          └─> Rule 6.1 Full Collateralization
Contract Specifications (14 Part 40 certifications) ──> the ONLY source of payout arithmetic
```

**Neither rulebook defines the vocabulary the products depend on.** The DCM
rulebook contains **zero** occurrences of "Event Contract", "Expiration Value",
"Settlement Value", "Payment Criterion", "final settlement", "in-the-money" or
"long position". This is the Polymarket US architecture again — and, as there,
a rulebook screen alone is not a screen of the venue.

---

## 3. The finding: the residual construction, stated outright

Every one of the fourteen families read carries this clause verbatim:

> "the Contract will resolve based on the last fair market price as determined
> by the Exchange pursuant to Rothera DCM Rule 7.2. **Long position holders will
> receive the number of Contracts held multiplied by the fair market price, and
> short position holders will receive the number of Contracts held multiplied by
> $1 minus the fair market price.**"

This is what phases 1, 2A and 2C could not obtain. The short is **defined as the
residual of the long**, so `long + short = $1.00` for *every* value of `p`. Who
picks the price, and how badly, is irrelevant to conservation.

This is the distinction the phase-2D brief drew, and it lands on the favourable
side:

| | conserves? |
|---|---|
| discretion over a **price** `p`, with short ≡ `$1 − p` | **yes, always** |
| discretion over **two independent payouts** | no |

Ordinary settlement is equally explicit — "the long position holders are paid …
and the short position holders receive no payment", and the converse — so the
losing side's zero is written, not inferred. Settlement Value of an
in-the-money contract is $1.00; minimum tick $0.01.

Verified exhaustively over all 101 admissible prices on the tick grid, and at
sub-tick precision, in `tests/unit/test_rothera_qualification.py`.

---

## 4. Rule 7.2 — the citation does not hold

The critical gate, and the answer is blunt.

**DCM Rule 7.2 is titled "Procedures."** In full, it authorises the Company DCM
to adopt procedures relating to *trading on the Platform*, including procedures
to: determine the **daily settlement price**; disseminate bid/offer and trade
prices; record and account for contracts; perform market surveillance; set order
size limits; set position limits; and set daily price fluctuation limits. Rule
7.2(B) lets it amend those procedures and publish them on the Website.

It says nothing about final settlement of an event contract. It does not mention
a fair market price. It reserves nothing. The rulebook's **only** definition of a
fair market price is in the Error Trade Policy — "In applying the No
Cancellation Range, the Company shall determine the fair market price for the
Company Contract" — for a different purpose entirely.

So when every certification says the price is determined "pursuant to Rothera
DCM Rule 7.2", and adds "Consistent with DCM Rule 7.2, Rothera reserves the
right to make settlement determinations", **the cited rule does not confer the
authority claimed.** This is the phase-1 Kalshi BOND/CRIMECHARGE pattern again.

### What that does and does not break

It does **not** break conservation, and saying otherwise would repeat the error
phase 2C was corrected for. The residual formula lives in the product terms and
holds whatever Rule 7.2 authorises, because it holds for every `p`.

What it breaks is the **procedure for choosing `p`**. Rule 7.2(B) makes any
adopted procedure a separate Website-published document, which was not located.
That is an open `GoverningDocumentDependency` with `UNKNOWN` closure.

### Answers to the specific questions

| Question | Answer |
|---|---|
| Can Rothera choose a settlement **price** only? | In the product clauses, yes — that is all they give it. |
| Can it choose long and short payouts independently? | **No clause permits this.** No Rothera text sets the two sides separately. |
| Does every chosen price feed `long = p`, `short = 1−p`? | Yes in 12 of 14 families on every fair-market path; **two paths omit the split** (§6). |
| Can it void/refund/cancel in another economic form? | Not in the product terms — **zero** occurrences of "void" or "refund" in all 14. DCO Rule 10.13 is titled "Contracts Not Voidable". Only Rule 1.11 emergency reaches it. |
| Can it modify Contract Specifications after trading starts? | Yes — Rule 1.11(B)(9), and the 2026-06-17 amendment shows in-place amendment in practice. |
| Are open positions grandfathered? | **Not stated.** The June amendment is silent on open interest. |
| Can Source Agency / Underlying changes alter the payoff relation? | The Source Agency hierarchy is fixed in each certification; a change would be a Part 40 amendment. Not a settlement-time discretion. |
| Is there a Contract Settlement Review Panel? | **No.** Rothera has no analogue of Kalshi's Market Outcome Review or Polymarket's Contract Outcome Review Process. Settlement determinations are made by the Exchange directly. |

### Rule 1.11 Emergency Rules — excluded from the proof, disclosed beside it

> "(3) provide alternative settlement mechanisms for any Contract (including by
> **altering the settlement terms or conditions** or fixing the settlement
> price) … (9) **modify or suspend any provisions of the Rules**"

**Scope decision (applied after the first draft of this report).** Broad
exchange-wide emergency authority is *not* part of the ordinary contractual
settlement proof. It is authority over the venue and over the Rules themselves,
fired by a declared Emergency rather than by any contingency in a contract's own
terms, and gated by CEO/President/CCO determination plus prior Regulatory
Oversight Committee approval (Rules 1.11(B)–(D)).

The rationale is structural, not convenient: a proof that no sovereign,
regulator or exchange will ever intervene is unobtainable on any regulated
venue. Requiring it would reject every venue for a property none of them has,
rather than discriminate between them — which is a different thing from proving
what a contract pays when it resolves normally.

Recorded as **`RESIDUAL_VENUE_INTERVENTION_RISK`**, carried on the proof object,
printed by `describe()` and `payload()`, and reproduced in every report.
Disclosed, never eliminated. No conclusion resting on it is described as
risk-free; the strongest available claim is
**`CONTRACTUALLY_GUARANTEED_UNDER_NORMAL_GOVERNING_SETTLEMENT`**.

**The exclusion is narrow and machine-enforced.** `IN_SCOPE_MECHANISMS` contains
the entire settlement census, and `classify_intervention` refuses to exclude any
of it however it is labelled. Product cancellation, normal settlement
discretion, fair-market-price settlement, tie/push, source failure, outcome
review, scheduled contingencies and product-specific discretionary valuation all
stay inside the proof. A venue does not get to reclassify a product settlement
clause by filing it under an emergency heading — asserted by a parametrised
test over six mechanisms, each claimed venue-wide and unreachable, each still
held in scope.

### What the exclusion changed, and what it did not

Removing Rule 1.11 leaves soccer with **exactly one** blocking mechanism, and it
is not the residual construction. It is this, present verbatim in **all
fourteen** certifications:

> **Contingencies** — "… the Settlement Date, Expiration Date and Expiration
> Time will be delayed until the Underlying outcome or results are released **or
> as otherwise set forth on the Exchange pursuant to DCM Rule 7.2. Consistent
> with DCM Rule 7.2, Rothera reserves the right to make settlement
> determinations.**"

The stated remedy is a delay, which is a timing remedy and conserves. Two things
escape it: "or as otherwise set forth" points somewhere unbounded, and "reserves
the right to make settlement determinations" reserves a settlement power with no
stated output — not a price, not a side, not a split.

**Source delay is an ordinary contingency**, reachable in the normal life of
every contract. It stays inside the proof and is not excluded with the emergency
authority. `UNRESOLVED` — nothing authorises a shortfall, so not
`NOT_COMPLEMENTARY`.

This clause had been overlooked in the first pass of §6, which counted Rule 1.11
as the emergency blocker and did not separately enumerate the Contingencies
reservation. Correcting the scope surfaced it.

---

## 5. DCO / clearing

| | |
|---|---|
| Novation | Rule 5.1: original contract extinguished, replaced by equal and opposite contracts with the DCO. |
| **Terms preserved** | **"All terms of a cleared Contract must conform to the Contract Specifications."** This is the rule that carries the DCM residual formula into clearing. |
| Collateralization | Rule 6.1: full collateralization required, "including payment of premiums and payment of settlement obligations". |
| Final settlement cash flow | Rule 5.2(B): "On the Settlement Date, the Company will notify all Participants of any final amount payable (if applicable)." **No payout formula.** |
| Long/short accounting | Gross positions per account; net only for customer omnibus reporting. |
| Settlement-price ingestion | Not described. The DCO consumes a result; it does not compute one. |
| Can DCO rules alter a DCM settlement value? | **Not in the ordinary course.** Only the DCO's own Rule 1.11 emergency equivalent. |
| Error/default/emergency | Rule 10.13 "Contracts Not Voidable"; transfers at the latest settlement price on default. |
| Fees at settlement | **None.** Zero occurrences of "settlement fee" or "clearing fee" in the Fee Schedule. |
| Residual retained by clearinghouse | **None found.** No aggregate-payout, cap or retention language. |

Clearing preserves the DCM-defined payoff **by rule**, not by inference from
collateralization. That is a genuine positive and the strongest clearing result
in this research.

---

## 6. Product families examined (14, all CFTC-filed specifications)

Baseball Outcome · U.S. Core PCE Price Index · U.S. Weekly Jobless Claims ·
Soccer Outcome · Soccer Spread · Soccer Total Goals · Football Outcome · Pro
Football Match Outcome · Pro Football Spread · Pro Football Totals · Pro
Football Advance to Playoffs · NBA Championship · NBA Conference Championship ·
NBA Division Championship.

All fourteen: Settlement Value $1.00, minimum tick $0.01, residual construction
present, **zero** void/refund paths.

FOMC Rate Decision was **not** located as a Rothera product. Political families
(Congressional Control, Election Outcome, Party Nominee, Political Party
Outcome) exist but were out of scope per the brief.

### Mechanism enumeration

| Mechanism | Rule | Status |
|---|---|---|
| Ordinary YES / ordinary NO | Contract Terms, "Trading and Settlement" | **PROVEN_COMPLEMENTARY** |
| Fair-market price (residual stated) | cancellation / abandonment bullets | **PROVEN_COMPLEMENTARY** |
| Fair-market price (split **not** stated) | Baseball DQ-before-first-pitch; Core PCE release cancelled entirely | UNRESOLVED |
| **Reservation of settlement determinations** | Contract Terms, "Contingencies" — **all 14 families** | **UNRESOLVED** |
| Emergency alteration | DCM Rule 1.11(B)(3),(9) | **excluded — `RESIDUAL_VENUE_INTERVENTION_RISK`** |
| Tie / push | no tie state in baseball or pro football; soccer uses a **separate contract** | n/a |
| Void / refund | **absent** | n/a |
| Rounding | no rounding rule in either rulebook | not needed — see §8 |

**The two unsplit paths.** Within the same certifications, two clauses state the
price but not the split:

> Baseball: "If `<baseball team>` is disqualified, deemed ineligible, or has the
> result of `<baseball game>` vacated **before the game has started**, the market
> will resolve based on the last fair market price …"
>
> Core PCE: "If the U.S. Bureau of Economic Analysis' release is **cancelled
> entirely**, then the Contract will settle to the fair market price …"

The contextual inference that these inherit `long = p`, `short = $1 − p` is
strong — same document, same defined phrase, every neighbouring clause states
it. It is an inference all the same, and this project does not settle payout
questions by strong inference. `UNRESOLVED`. One sentence in the next amendment
would close it.

---

## 7. Single-market complement result

**Per-mechanism:** every ordinary mechanism is `PROVEN_COMPLEMENTARY` except the
Contingencies reservation (all 14 families) and the two unsplit fair-market
paths (baseball, Core PCE). Rule 1.11 is excluded from the proof and disclosed
as residual venue intervention risk.

**Does `p / (1−p)` become proven once emergency authority is out of scope?**
**No — for no family.** The residual construction *itself* is
`PROVEN_COMPLEMENTARY` and conserves at every price and every precision. What
blocks is a separate open-ended reservation sitting beside it in every
certification. Soccer, the cleanest family, has exactly one blocking mechanism
after the correction; baseball has two.

**Family verdict:** `APPLICABLE_GOVERNING_EVIDENCE_INCOMPLETE` — mechanism
closure is not established, because Rule 7.2's Website-published procedures were
not located. Closing that gap alone yields
`COMPLEMENT_CONSERVATION_NOT_PROVEN` (asserted by test).

**No mechanism at this venue is `NOT_COMPLEMENTARY`.** No Rothera text
authorises a terminal state where the two sides miss the notional — no
combined-payout cap, no residual retention, no independent-payout clause.

For calibration, a control test removes the unresolved paths and the verdict
becomes `COMPLEMENT_CONSERVATION_PROVEN_FOR_SUBSET` — **and even then** its
`conservation_scope` is `CONTRACTUALLY_GUARANTEED_UNDER_NORMAL_GOVERNING_SETTLEMENT`,
its `exact_bound()` names the undisclosed-against intervention power, and its
`describe()` still prints the residual-risk paragraph. The machinery cannot
produce an unqualified guarantee.

**Rothera is two sentences away from a proof**, and neither sentence is about
the residual formula itself.

---

## 8. Rounding

- **Permitted precision:** minimum tick $0.01; no precision stated for the fair
  market price itself.
- **Rounding method:** none specified — **zero** occurrences of "round" in
  either rulebook.
- **Is `p` rounded before short is computed?** Not stated.
- **Is short literally `1 − p` after rounding?** **Yes** — the text computes the
  short *from* the fair market price, not independently.
- **Independent rounding possible?** **No.** The construction is definitionally
  residual.

**Conservation survives exactly at any precision.** `RoundingModel.RESIDUAL`.
This is the first venue in the project where the absence of a rounding rule is
harmless rather than fatal, because there is no second quantity to round.

Rothera rounds in exactly one place — fees — and those are account debits.

---

## 9. Relation structures

Two relation families are provable from filed text.

**Soccer is a genuine three-way partition.** The Payment Criterion makes a
winner one who "scored more goals (a strictly greater number) than its opponent
at the conclusion of regulation time (90 minutes plus stoppage time only)", and
the Exchange "may, for any `<soccer match>`, list a separate 'tie' iteration …
each `<soccer team>` must have scored an equal number of goals (including 0-0
draws)". Over one number those three are mutually exclusive and exhaustive.
Extra time and penalties are excluded from the winner determination (except for
"to advance" iterations), which *sharpens* the partition. `EXACTLY_ONE`, proven.

**Baseball is a two-way partition.** No tie is possible — "strictly greater
number … including any extra innings". Forfeit is handled explicitly and
preserves it: the forfeiting team resolves No, the opponent Yes.

**Core PCE / Jobless Claims are threshold ladders.** `<above/below/between/
exactly/at least/at most> <percent>` over "the month-to-month change in the
monthly Core PCE Price Index", with the measurement pinned hard: "Only the
seasonally adjusted figure from the **first official release** is used for
settlement", revisions and other sources expressly excluded. `NESTED_IMPLICATION`,
proven.

Nothing here was inferred from ticker names.

---

## 10. Cross-market alternate-settlement compatibility — **this is what fails**

Buy all three legs of a soccer match. Ordinary settlement pays exactly one
notional. Then the match is abandoned, and each contract settles at **its own**
fair market price:

```
basket return = p_home + p_away + p_tie
```

No rule requires those three to sum to a dollar. Each contract still conserves
**perfectly** — long plus short is one notional, exactly, for any price — while
the sum *across* members is three independent discretionary determinations.

**Guaranteed floor: $0.00, not $1.00.**

This is the ForecastEx phase-2B failure mode arriving by a different route, and
it is why phase 2D added `predarb.semantics.payout_relation`: a YES/NO relation
cannot express a state in which no contract is YES or NO and both sides are
paid. The new model separates the two questions structurally:

| | Rothera |
|---|---|
| within-contract conservation | **COUPLED** (residual) |
| across-contract price coupling | **INDEPENDENT** |

A control test shows the machinery is not simply returning zero: coupling the
prices to the notional restores the floor to $1.00. That names exactly what an
amendment would have to say — *the fair market prices of a partition's members
shall sum to the Settlement Value* — and Rothera says no such thing.

The Core PCE ladder dies the same way: the implication constrains which
contracts are YES, and on the fair-market path none of them is.

---

## 11. Venue pricing / netting constraints — **no elimination**

| | |
|---|---|
| Trading model | Conventional **central limit order book**, "price and time priority algorithm" (Rule 4.3(B)), anonymous. |
| YES/NO sides | No dual-book structure. One book per contract; "long"/"short" are buy/sell of that contract. |
| Complementary order matching | **None.** |
| Independent contract books | **Yes** — each certification is its own contract. |
| Automatic opposite-side netting | Offset only for "Contracts with the same terms and conditions" (DCO Rule 5.1(C)) — same-contract only. |
| Cross-market netting | **None.** |
| Exchange-enforced pricing identity | **None.** No analogue of ForecastEx Rule 401(d). |
| Other | Block Trades permitted; no RFQ; no auctions; no indications of interest; Binary Options have no Reasonability Levels or No Cancellation Ranges. |

**Gate 3 passes cleanly.** Nothing in Rothera's mechanics algebraically
eliminates an edge.

---

## 12. Fee model

From the Fee Schedule effective 2026-05-20, charged to **both** buyer and
seller of every trade:

```
Order fees = MAX(round(k × p × (1 − p) × c, 2), 0.01)      round half up
```

| Participant type | k |
|---|---|
| FCM — Retail Customer | **0.02** |
| Market Maker | 0.03 |
| FCM — Professional Trading Firm | **0.12** |

No maker/taker split. No rebates. **No settlement or clearing fee.** No language
altering the contractual payout — fees are account debits, so they land on the
cost side of a basket inequality and never reduce what a contract pays.

Implemented exactly in `predarb.venues.rothera.fees`, in `Decimal` throughout,
and validated against the schedule's own worked example (k=0.06, 100 contracts
at $0.35 → **$1.37**). A half-cent case pins the rounding mode as half-up rather
than banker's — Polymarket US uses the other one, so the distinction is live.

**Symbolic threshold.** For an *n*-member partition basket at prices `pᵢ`, held
to resolution by a retail participant:

```
lock requires    Σ pᵢ  +  Σ MAX(round(0.02 · pᵢ(1−pᵢ)·c, 2), 0.01)/c   <   $1.00
```

The `p(1−p)` shape helps: a partition's legs mostly sit near the extremes where
the fee vanishes. The professional tier at 0.12 costs up to $0.03 per contract
per side, which against a $1.00 notional is severe on a multi-leg basket.

Derived symbolically only. No live prices were obtained.

---

## 13. Read-only data accessibility — **not researched**

The brief gated this on the semantic and relation gates surviving. They did not
(§10), so no API, protocol, depth, sequencing or historical-data research was
performed, and no account was connected.

Recorded for completeness: Robinhood Derivatives is listed as a clearing member.
That does not imply Robinhood is the only data path, and nothing was assumed
either way.

---

## 14. ProphetX — screen closed

DCM Exhibit M Rulebook, Document Version 1.0, filed 2025-12-05, 54 pages,
extracted clean, sha256
`178d04d7408b493c28ed7961eeeec7c2cdaebd2d25b48c015bb206685e6b0ea7`.

Rule 5.2(c) gives the Exchange sole discretion over the Settlement Value and, on
a Settlement Disruption, five remedies including the last traded price, voiding
contracts, and "such other action as it deems appropriate". Rule 5.2(d) is the
only constraint on the pair:

> "In no case shall the combined payout across positions **exceed** the stated
> maximum Settlement Value of the Contract."

**A ceiling is not a complement.** `long + short ≤ $1` is satisfied by
`$0.40 + $0.40` and by `$0 + $0`. Rule 5.2(f) makes every determination final
and unappealable; Rule 5.2(a) additionally permits the Exchange to "reverse,
amend, or resettle a settlement" after the fact.

**`COMPLEMENT_CONSERVATION_NOT_PROVEN`** — not DISPROVEN, on the distinction
this phase was corrected for: a cap permits a shortfall without authorising one.
(The recorded family status is `APPLICABLE_GOVERNING_EVIDENCE_INCOMPLETE`, one
step stricter, since no ProphetX product specifications were obtained; a test
asserts that closing that gap alone yields `NOT_PROVEN`.)

No further ProphetX research is warranted unless a product-specific rule is
proven to override Rule 5.2 for every reachable terminal state. None was located.
**Screen closed.**

---

## 15. Decision gate

| Requirement | Result |
|---|---|
| Governing evidence COMPLETE | **NO** — Rule 7.2's Website procedures not located; closure UNKNOWN |
| Every single-contract mechanism PROVEN_COMPLEMENTARY | **NO** — the Contingencies reservation in all 14 families, plus two unsplit fair-market paths. (Emergency authority excluded from the proof and disclosed as residual risk; excluding it did **not** change this answer.) |
| Useful relation semantics PROVEN | **YES** — soccer `EXACTLY_ONE`, baseball two-way, Core PCE `NESTED_IMPLICATION` |
| Relation survives every reachable mechanism | **NO** — the fair-market regime drops every partition floor to $0.00 |
| Exchange mechanics do not eliminate the edge | **YES** — conventional CLOB, no pricing identity, no netting |
| Only remaining unknown is LIVE EXECUTABLE PRICE | **NO** |

### **Rothera does not pass Phase 2D.**

A bounded live-data experiment is **not** justified. The blocker is not price
discovery — it is that a proven partition has no contractual floor under a
reachable settlement regime. Live prices would tell us nothing about that, and
obtaining them would be research on a basket already known to be unguaranteed.

### Best three candidates, if the gate were relaxed

1. **Soccer three-way partition** — the cleanest relation found anywhere in
   phases 1–2D. Fails only on abandonment price-coupling.
2. **Baseball two-team pair** — same shape, one fewer leg, but carries the extra
   unsplit disqualification path.
3. **Core PCE / Jobless Claims threshold ladders** — a real nested implication
   with an unusually well-pinned measurement; same coupling failure.

---

## 16. What would change the verdict

Three sentences, none of them about the residual formula:

0. **Remove the open-ended reservation.** Delete "or as otherwise set forth on
   the Exchange pursuant to DCM Rule 7.2. Consistent with DCM Rule 7.2, Rothera
   reserves the right to make settlement determinations", or give it a stated
   output. This is now the *only* ordinary blocker for the soccer family.
1. **Couple the prices.** "Where the Exchange determines fair market prices for
   a set of Contracts that partition an event, those prices shall sum to the
   Settlement Value." This alone converts §10 from a $0.00 floor to $1.00 and is
   the single highest-value change.
2. **State the split on the two unsplit paths** — baseball's
   disqualification-before-first-pitch and Core PCE's cancelled release.
3. **Fix or replace the Rule 7.2 citation**, and publish the procedures Rule
   7.2(B) contemplates, closing the dependency.

Absent those, every basket claim at Rothera is UNKNOWN, and UNKNOWN blocks.

---

## Verification

```
pytest                 2943 passed, 2 skipped, 8 deselected
ruff check .           All checks passed
ruff format --check .  259 files already formatted
mypy src tests         Success: no issues found in 220 source files
tools/security_scan.py 0 hits across all five scopes
```

No credentials were configured or used. No orders were placed. No live API call
was made to any venue. All rulebooks and certifications remain in the gitignored
local research path and are referenced here by sha256.
