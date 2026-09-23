# Exhaustiveness: what it would take to prove AT_LEAST_ONE

`AT_MOST_ONE` says *no two of these can both win*. `AT_LEAST_ONE` says *one of
these must win*. They sound symmetrical and are not: one is falsified by finding
a pair, the other by finding an outcome nobody listed.

This document is the research behind the second claim. **No AT_LEAST_ONE
certificate has been issued**, and the economics for one are deliberately not
built.

---

## 1. Three claims that must never be conflated

| | Claim | Falsified by | Established from |
| --- | --- | --- | --- |
| **A** | **Venue membership completeness** — we enumerated every Kalshi market for event E | a market in neither tier | API enumeration |
| **B** | **Mutual exclusion** — at most one selected member settles YES | two simultaneous winners | contract rules (Step 9) |
| **C** | **Outcome exhaustiveness** — at least one selected member MUST settle YES | one uncovered outcome | contract rules (this step) |

**A does not give C.** Suppose Kalshi lists "Candidate A wins", "Candidate B
wins", "Candidate C wins", and those are provably every market it ever created
for the event. The election can still be won by candidate D, be cancelled, be
void, produce no qualifying winner, or settle on a fair-price mechanism. Nothing
in the catalogue reveals an outcome the catalogue never had a market for.

They are separate types — `CombinedVenueMembershipEvidence` and
`RelationCertificate(claim=AT_LEAST_ONE)` — with separate fingerprints and
separate storage, so neither can be produced by satisfying the other.

---

## 2. The live/historical partition (A-48)

Current documentation states the boundary outright.

`GET /markets`:
> Markets that settled before the historical cutoff are only available via
> `GET /historical/markets`.

`GET /events/{ticker}`, on `with_nested_markets`:
> **Historical markets settled before the historical cutoff will not be
> included.**

`GET /historical/cutoff` returns the boundary (`market_settled_ts` and three
others), and the guide adds that the cutoffs advance over time.

### Enumeration order is a correctness property

The cutoff moves while we work. If a market crosses it between two queries:

| Order | Result |
| --- | --- |
| live, then historical | returned by **both** — a duplicate, which we detect |
| historical, then live | returned by **neither** — a silent omission |

Duplicates are recoverable; omissions are invisible. Enumeration therefore
always queries the live tier first, and reads the cutoff before *and* after so a
mid-walk move is recorded rather than discovered later as an unexplained gap.

### Measured behaviour (94 stratified events, 2026-09-22)

| Measure | Count |
| --- | --- |
| events sampled, stratified by category and status | 94 |
| both paths exhausted | **94** |
| union members | 1,108 |
| nested view omitted | **307 (27.7%)** |
| events with at least one omission | **43 (46%)** |
| members the nested view had but neither tier did | **0** |
| events where nested set == live-tier set, exactly | **94 / 94** |

The nested event view returns *exactly* the live tier. Its omissions are
precisely the archived markets, so reading it alone under-reports membership on
nearly half of events.

The partition is **not strict**. `KXKNESSET-27` returned two markets from the
live tier that reported settlement *before* the cutoff — markets the
documentation says live endpoints do not carry. The tiers overlap in the safe
direction; deduplication by ticker is mandatory.

### Why the object is called evidence, not a certificate

`Market.is_provisional` is documented as:
> If true, the market may be removed after determination if there is no activity
> on it.

Membership can **shrink**. A removed market is in neither tier, so no successful
enumeration proves that `live + historical` is every market the event ever had.
`CombinedVenueMembershipEvidence` records which paths were exhausted under which
cutoff, lists its caveats, and declines to call itself complete. Its `statement()`
never contains the word.

---

## 3. `mutually_exclusive` cannot prove AT_LEAST_ONE (A-52)

Documented:
> If true, only one market in this event can resolve to 'yes'. If false,
> multiple markets can resolve to 'yes'.

The contrast fixes the meaning: the flag bounds the **maximum** number of YES
resolutions. It says nothing about the minimum, and the word is "can", not
"must".

### Measured, on 17 mutually-exclusive events

| Outcome | Events (of 11 fully settled) |
| --- | --- |
| exactly one winner | 9 |
| **zero winners — demonstrated ALL-NO** | **2** |
| multiple winners | 0 |

`KXGOVCANOMR-26`: `mutually_exclusive = true`, 10 members, all settled, **zero**
YES winners — nine NO and one settling to `scalar`. `KXNEWROLEX-26JAN`: 11
members, all settled, zero winners.

Both are mutually exclusive *and* reached the exact state AT_LEAST_ONE forbids.
The zero-multiple-winner column is the other half of the story: the flag does
hold as an upper bound. It simply is not a lower bound.

A regression test asserts that no code path derives AT_LEAST_ONE from the flag.

---

## 4. The forbidden state

For n binary members, AT_LEAST_ONE permits every joint assignment except one.

```
AT_MOST_ONE    forbids two or more YES;   ALL-NO is permitted
AT_LEAST_ONE   forbids ALL-NO;            two, three or all YES are permitted
```

`JointStateRule` represents this symbolically — a predicate plus a description
of what it forbids. AT_LEAST_ONE over n members permits `2ⁿ - 1` assignments,
and one sampled event had **300** members; materialising that is not a state
space, it is an outage. Explicit enumeration exists only for tests and refuses
above 12 members.

**Multiple YES is valid.** Reading AT_LEAST_ONE as "exactly one" would import a
mutual-exclusion guarantee nobody reviewed. Tests cover one, two and all
members winning; all three are permitted.

`EXACTLY_ONE` has no primitive. It is `AT_MOST_ONE` AND `AT_LEAST_ONE`, and the
right way to reach it is to compose two independently reviewed certificates over
the same canonical member set — not to invite a third review that could approve
the conjunction without either half. The two claims' permitted sets intersect
exactly at the single-winner assignments, which a test pins.

---

## 5. Structured metadata (A-53)

| Field | Membership? | Mutual exclusion? | Exhaustiveness? |
| --- | --- | --- | --- |
| `mutually_exclusive` | no | **yes** | **no** |
| `strike_type` + `floor_strike`/`cap_strike` | no | no | **supports a structural argument** |
| `functional_strike`, `custom_strike` | no | no | no — no documented grammar |
| `market_type`, `collateral_return_type` | no | no | no |
| `product_metadata` | no | no | **no** — typed `object`, no schema |
| `primary_participant_key` | no | no | no — never populated in 935 members sampled |
| `is_provisional` | **negatively** | no | no |

Observed `strike_type` values across 935 members:

| Value | Count |
| --- | --- |
| `custom` | 415 |
| `greater` | 314 |
| `(none)` | 92 |
| `structured` | 48 |
| `greater_or_equal` | 31 |
| `less` | 19 |
| `between` | 16 |

`greater` dominates because Kalshi threshold ladders are **nested cumulative
thresholds** (X > 10, X > 20, X > 30), not disjoint buckets. Those overlap, and
they leave everything below the lowest threshold uncovered — a direct ALL-NO
path. 30 of 99 sampled events showed exactly this shape.

---

## 6. The interval helper

`predarb.semantics.partition` reads only documented structured strike fields —
never titles. "Above 70 degrees" in a title is a sentence written for humans;
deriving a settlement threshold from it would be inferring contract semantics
from marketing copy.

Coverage statuses: `GAP_FREE`, **`GAP_FREE_IF_DOMAIN_DISCRETE`**, `GAP`,
`UNBOUNDED_LOWER_TAIL_UNCOVERED`, `UNBOUNDED_UPPER_TAIL_UNCOVERED`,
`NOT_APPLICABLE`, `MALFORMED`.

### The temperature-bucket case

`KXHIGHTTTN-26SEP23` has six members:

```
T64    less,    cap=64      "64° or below"
B64.5  between, 64..65
B66.5  between, 66..67
B68.5  between, 68..69
B70.5  between, 70..71
T71    greater, floor=71    "72° or above"
```

Over the **reals** there are gaps at 65–66, 67–68, 69–70. Over **whole
degrees** the buckets tile perfectly. Which is true depends on what the
settlement value can be, and that is in the contract.

The helper reports `GAP` by default. A supplied step does **not** make it
`GAP_FREE` — it produces `GAP_FREE_IF_DOMAIN_DISCRETE`, which carries
`required_domain_step` and preserves `unconditional_gaps`, the holes that exist
over a continuous domain. The condition is named, not discharged.

Discharging it takes `DomainConstraintEvidence`: the variable, the units, the
step, **which evidence component carries the language**, that component's
content hash, the sentence relied on verbatim, and a named reviewer who read it.
Any of those missing and the result stays conditional.

| Input | Status | Supports a proof? |
| --- | --- | --- |
| nothing | `GAP` | no |
| `--domain-step 1` alone | `GAP_FREE_IF_DOMAIN_DISCRETE` | **no** |
| step + rationale but no source | `GAP_FREE_IF_DOMAIN_DISCRETE` | **no** |
| attested evidence (source + hash + quote + reviewer) | `GAP_FREE` | yes |
| attested evidence, step 0.5 | `GAP` | no |

Explicitly **not** evidence: a market title saying "whole degrees", a strike
rendered without decimals, a history of integer settlements, or a CLI parameter
on its own. The first is marketing copy, the second formatting, the third a
sample, the fourth an assertion with nobody's name on it.

The coverage fingerprint covers the intervals **and** the domain evidence, so a
certificate relying on a partition goes stale when a member's strike moves *or*
when the source that established the domain changes.

**Gap-free coverage never completes a proof.** It shows that *if* the underlying
resolves to a real number in the asserted domain, some member's condition holds.
It leaves untouched: an undefined or disputed underlying, cancellation, void,
the domain bound itself, and whether the value can land exactly on a boundary.

The helper issues nothing. A reviewer reads the result; the result never reads
itself.

---

## 7. The evidence policy

`AT_LEAST_ONE` declares an `ExhaustivenessBasis`, which decides what is
*material*:

`ProofBasis` has exactly two members:

| Basis | Rests on | Also requires |
| --- | --- | --- |
| `CONTRACT_LANGUAGE` | the rules say one of these must occur | documents actually retrieved |
| `STRUCTURED_PARTITION` | documented strikes tile an **evidence-established** domain | gap-free coverage; contract still governs cancellation and void |

**`VENUE_MEMBERSHIP_COVERAGE` is not a basis.** It is `SupportingEvidence`, and
the type system enforces that — `ProofBasis("VENUE_MEMBERSHIP_COVERAGE")` raises.

The reason is Step 11's own research. Membership evidence refuses to claim
completeness; `is_provisional` lets a market vanish from both tiers (A-51); the
documented live/historical partition was observed overlapping (A-48); and no
query reveals an outcome the venue never listed a market for. A reviewer must
never be able to approve

> "we queried every endpoint we know about"

as though it meant

> "one of these propositions must necessarily be true".

The first is a fact about a catalogue. The second is a fact about the world.

Membership may **corroborate** a proof whose basis is one of the two above.
When cited it must still be sound — exhausted enumeration, stable cutoff, no
uncovered members, no open event, no provisional members — and it is
fingerprinted, so a change invalidates the certificate. When not cited it stays
audit-only and its quality is never demanded.

Beyond AT_MOST_ONE, the policy additionally requires a retrieved governing
document and a recorded settlement source **outright** — not merely if
referenced. The ALL-NO question is normally answered by cancellation and void
provisions, and those live in the contract.

### Membership reliance is conditional, and that matters

Step 9 established that observed membership is *audit-only* for AT_MOST_ONE: a
market appearing outside the subset cannot make "at most one of {A,B,C} settles
YES" false.

That does not carry over unchanged. An AT_LEAST_ONE proof may *cite* membership
as corroboration, and when it does, a membership change should invalidate it. So
membership is fingerprinted **exactly when it was cited**, and not otherwise.
Binding it unconditionally would expire certificates on changes their proofs
never consulted — and teaching reviewers to ignore drift is exactly what drift
detection must not become.

`MembershipReliance.MATERIAL` never means "this is the proof". It means "this
corroboration was cited, so its change matters".

---

## 8. The review checklist

Twelve questions, all aimed at one target: *is there a terminal state in which
every selected member settles NO?* Covering the outcome universe, an explicit
guarantee in the rules, unnamed and write-in outcomes, the event never
occurring, cancellation/postponement/DNP, void and refund, scalar and fair-price
settlement, ties and co-winners, member-set completeness, membership
enumeration, documents reviewed, and — last — that the claim is not being used
to assert mutual exclusion.

The checklists are routed by claim rather than shared, because the two ask
opposite things of the same facts:

| | AT_MOST_ONE asks | AT_LEAST_ONE asks |
| --- | --- | --- |
| cancellation | could it create **multiple YES**? | could it make **every member NO**? |

One list covering both would have to be satisfied by contradictory answers.

`UNCERTAIN` blocks approval on every question: the claim is a conjunction, and an
uncertain conjunct makes the conjunction uncertain. Approval needs the exact
phrase `APPROVE AT_LEAST_ONE <REQUEST_ID>` — it names the claim and the request,
so it cannot be typed by accident or reused for a different claim over the same
members. There is no auto-approval path and no flag that skips the checklist.

---

## 9. Historical settlement study

Across 64 fully-settled multi-market events:

| Outcome | Events | Share |
| --- | --- | --- |
| exactly one winner | 22 | 34% |
| **zero winners — demonstrated ALL-NO** | **21** | **33%** |
| multiple winners | 21 | 33% |

A third of settled events reached the state AT_LEAST_ONE forbids. The claim is
empirically rare, not the default.

This is falsification evidence only. A zero-winner event proves ALL-NO is
reachable for that event. A history of one winner every time proves nothing
about the future: past behaviour does not bind contract semantics, and an event
that has always had a winner may still carry a cancellation clause nobody has
had cause to exercise.

---

## 10. Candidate families

| Verdict | Meaning |
| --- | --- |
| `POTENTIALLY_PROVABLE` | structured evidence points the right way — where to look, not a conclusion |
| `REQUIRES_MANUAL_REVIEW` | nothing structural either way; only the rules text can decide |
| `CLEARLY_NON_EXHAUSTIVE` | demonstrated ALL-NO, or a structural gap |
| `BLOCKED_BY_SCALAR_OR_SPECIAL_SETTLEMENT` | "at least one YES" would not mean "at least one paid the notional" |

Over 99 sampled events: 86 `REQUIRES_MANUAL_REVIEW`, 13 `CLEARLY_NON_EXHAUSTIVE`,
**0 `POTENTIALLY_PROVABLE` from structure alone**. No event had gap-free
coverage without a reviewer-asserted domain.

The one family that comes close is **whole-degree temperature buckets** — a
catch-all low bucket, contiguous bands, a catch-all high bucket — and only once
a reviewer asserts the granularity. Named-candidate events (`custom` strikes,
415 of 935 members) offer nothing structural at all, which is the honest reason
they sit in manual review.

`KXGOVCANOMR-26` is the cautionary case: mutually exclusive, ten named
candidates, every member settled, zero YES winners, and one member settling
`scalar`.

---

## 10b. Membership survives irrelevant financial anomalies

A-49: `/historical/markets` returns negative `yes_bid_size_fp` on finalized
markets. Those values make a market unfit for executable book work. They say
nothing about whether the ticker belongs to the event.

Before the split, one such page failed an entire walk, so two of forty sampled
events reported **zero members** — indistinguishable from an event that
genuinely has none. A data-quality problem had become a semantic conclusion.

So membership is read by a separate projection
(`venues/kalshi/membership_projection.py`) directly from the **raw** payload,
parsing only what membership depends on:

| | |
| --- | --- |
| **required** | `ticker`, `event_ticker` matching the event asked about |
| **read when readable** | status, lifecycle timestamps, result, settlement value, `is_provisional`, `market_type`, documented strike fields |
| **never required** | anything financial |

An unusable financial field is attached as a `DATA_QUALITY_ANOMALY`, excluded
from semantic reasoning and from the fingerprint, and the market **stays**. An
unusable *identity* field raises: the walk becomes `IDENTITY_UNUSABLE`, the
evidence stops claiming to be a member list, and nothing is guessed.

`KalshiMarket` keeps its invariants unchanged — pricing code must never see a
negative contract count. Strict financial normalisation may reject a record
while membership normalisation accepts its semantic projection. Both readings of
the same bytes are correct, because they answer different questions.

---

## 11. Point-in-time membership

An open event can gain markets, and a provisional market can be removed, so
membership evidence is point-in-time in **both** directions. Each snapshot
records `observed_at`, `knowledge_at`, the cutoff before and after, event status,
the latest member creation time, and per-page cursors and response hashes.

A new market appearing later creates a new membership version; historical
snapshots stay immutable. No eternal membership proof is issued for an open
event — the `caveats()` say so explicitly whenever any member is still unsettled.

---

## 12. Replay

`AT_LEAST_ONE` certificates replay point-in-time like everything else. The
resolver reads the **recorded** claim rather than defaulting, and an unrecognised
claim raises instead of falling back to one we do model.

The NO-basket detector now refuses a relation whose claim is not `AT_MOST_ONE`.
Its payoff table *is* the AT_MOST_ONE state space; pricing an AT_LEAST_ONE
certificate against it would use a guarantee nobody reviewed. The refusal is
tested end to end, through the resolver into a `DecisionRecord`, and live and
replay still agree field-for-field with the new claim present.

No AT_LEAST_ONE economics exist. That is Step 12.

---

## 13. Unresolved

- **Union completeness is unprovable from the API.** `is_provisional` removal
  means no enumeration closes it. Only a venue guarantee would.
- **The partition is documented as strict and observed as overlapping.** One
  event in 94 returned pre-cutoff markets from the live tier. Benign, but it
  means the documented wording cannot be relied on as a disjointness guarantee.
- **`custom` and `structured` strikes are opaque.** 463 of 935 members. Whatever
  structure they hold is not documented, so none of it is readable.
- **`product_metadata` has no schema** and was not usefully populated.
- **Boundary landings.** Whether an expiration value can fall exactly on a
  strike boundary is a tick-size and rounding question the helper does not
  answer.
- **The whole-degree domain for `KXHIGHTTTN` is asserted, not attested.** The
  live request carries `GAP_FREE_IF_DOMAIN_DISCRETE` with its three real-domain
  gaps preserved. Discharging it needs the rules language that states the
  measurement granularity, hashed and acknowledged.
- **No AT_LEAST_ONE certificate exists.** One live request is `AWAITING_REVIEW`.

---

## 14. Commands

```
predarb relations request <EVENT> --claim at-least-one --basis STRUCTURED_PARTITION \
    --member <TICKER> ... \
    --domain-step 1 --domain-variable "daily high temperature" \
    --domain-units "degrees Fahrenheit" \
    --domain-source market.rules_primary=<sha256> \
    --domain-quote "..." --domain-reviewer <NAME> \
    --domain-justification "..."
predarb relations list
predarb relations show <REQUEST_ID>
predarb relations review <REQUEST_ID> --reviewer <NAME>
```

```
uv run python tools/research_membership.py --events 150
uv run python tools/research_exhaustiveness.py --events 140
```

Research output is derived, never source evidence. Bundles, membership snapshots
and registry records live under a gitignored local research path.
