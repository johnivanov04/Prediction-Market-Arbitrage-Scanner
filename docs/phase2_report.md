# Phase 2 — Venue study: final report and freeze

Branch `phase-2`, built on frozen `phase-1` HEAD `f77569f`. Research only
throughout: no transport was implemented, no credentials were configured, no
order was ever placed, and no live API call was made to any venue.

---

## 0. Status

```
PROJECT_IMPLEMENTATION                              = COMPLETE

ORIGINAL_SINGLE_VENUE_CONTRACTUAL_ARBITRAGE_THESIS  = NOT_SUPPORTED_ON_INVESTIGATED_VENUES

LIVE_PROFITABLE_CONTRACTUAL_ARBITRAGE               = NOT_DEMONSTRATED

TRADING                                             = NOT_ATTEMPTED
```

---

## 1. The thesis, stated and answered

> **Can we build a practical scanner for executable, single-venue
> prediction-market opportunities whose positive worst-case profit is
> contractually guaranteed after fees, depth, settlement semantics and execution
> constraints?**

### **NOT SUPPORTED BY THE VENUES INVESTIGATED.**

Not "more research required." Five venues were examined against five conditions
that must hold *simultaneously*:

| | Kalshi | ForecastEx | Polymarket US | Rothera | ProphetX |
|---|---|---|---|---|---|
| Useful contractual relation | ✗ | ✓ | ✓ | ✓ | — |
| Exhaustive settlement proof | ✗ | partial | ✗ | ✗ (close) | ✗ |
| Guaranteed basket payoff floor | — | ✗ | ✗ | ✗ | — |
| No venue rule eliminating the edge | ✓ | ✗ | ✓ | ✓ | — |
| Only live price remaining unknown | ✗ | ✗ | ✗ | ✗ | ✗ |

**No venue produced a row of all five.** Each failed at a different place, which
is the substance of the result rather than a coincidence: the failures were not
one recurring defect but four distinct ones, and a fifth venue closed on a
ceiling clause.

This conclusion is about the venues investigated and about single-venue
contractual arbitrage. It is not a statement about cross-venue arbitrage,
statistical mispricing, or any other strategy — none of which were investigated,
and none of which belong in this conclusion. They would be separate projects if
ever explicitly chosen.

---

## 2. Venue outcomes

### KALSHI (phase 1, frozen at `f77569f`)

- **Implementation, replay and evidence architecture: successful.** Live book
  reconstruction, executable-depth modelling, exact arithmetic, bitemporal
  capture, offline decision reproduction, durable state recovery after faults.
- **Live contractual detector: blocked by governing semantic ambiguity.**
  Rule 6.3(c)(b) authorises a "fair allocation" and never says what is being
  allocated. No rounding model exists anywhere in the corpus. Rule 7.1 versus
  6.3(c) precedence is unresolved.
- Settlement census: 36 families, **0** strict two-state.
- Final: `IMPLEMENTATION_COMPLETE / SEMANTIC_LIVE_EXECUTION_BLOCKED_BY_VENUE_GOVERNANCE`.

### FORECASTEX (phase 2A–2B)

- **Same-market YES+NO edge algebraically eliminated.** Rule 401(d) inverse
  pricing fixes a pair at **$1.01**; Rule 604 nets it at **$1.00**. The
  same-market arbitrage is exactly **−$0.01 by construction** — no quote can
  rescue it.
- Settlement semantics materially cleaner than Kalshi: Rule 603(a) states both
  sides in both branches, and both Participant Split and Fair Price use the
  **residual** construction.
- **A useful nested cross-market implication exists** — threshold ladders give
  provable `NESTED_IMPLICATION`.
- **Alternate settlement destroys the guaranteed cross-market floor.** Rule 604
  offsetting is same-Forecast-Market only, so a cross-market basket has no
  netting escape and is exposed to Rule 414(b)(3), which caps combined payout at
  $1.00 with no floor.
- Verdict: `RELATION_SEMANTICS_UNRESOLVED`. Fails.

### POLYMARKET US / QCEX (phase 2C)

- **Useful combinatorial and relational structures exist.** The CAOC
  certification states an explicit biconditional AND — *"resolves to $1.00 if
  and only if every leg is satisfied"* — the strongest relation language found
  anywhere in the study.
- **Ordinary and tie paths may conserve.** Rule 9.101(D) names both sides in
  both ordinary branches, and the tie pays $0.50 a side, which conserves exactly
  while disproving binariness.
- **Cancellation / discretionary valuation prevents a complete settlement
  proof.** Rule 9.101(K) permits settlement "based on last-traded prices, $0.50
  per contract, or other fair and equitable valuation" in the Exchange's sole
  and absolute discretion.
- **The CAOC relation floor fails under reachable tied-leg behaviour.** CAOC
  models its legs as settling "$1.00/$0.00"; a tied leg against a satisfied leg
  drops the basket floor to **half the notional**, proven by exhaustive
  enumeration.
- **Conservation is `NOT_PROVEN`, not `DISPROVEN`** — corrected in `643f4ed`.
  No PMUS text authorises a shortfall: no combined-payout cap, no residual
  retained by the Exchange, no aggregate-payout language (its one "shall not
  exceed" is a Chapter 11 liability cap).
- Structural finding: the rulebook screened clean only because it specifies no
  product's payout. Rule 1.5's precedence is scoped three times to "trading in"
  the Contract, leaving `PRECEDENCE_UNRESOLVED`.

### ROTHERA (phase 2D)

- **The strongest single-market residual construction found anywhere:**

  ```
  long  = fair market price
  short = $1 − fair market price
  ```

  in all fourteen certifications read. Conserves at every price and every
  precision; `RoundingModel.RESIDUAL`.
- **Ordinary single-contract complementarity substantially succeeds.** Ordinary
  YES/NO names both sides in both branches. Clearing preserves the DCM payoff
  *by rule* — DCO Rule 5.1(D)(3), "all terms of a cleared Contract must conform
  to the Contract Specifications". No settlement fee, no retained residual.
- **Useful relations exist and are proven from text:** soccer is a genuine
  three-way `EXACTLY_ONE` partition over regulation-time goals; baseball a
  two-way partition; Core PCE a `NESTED_IMPLICATION` ladder with the measurement
  pinned to the BEA first release.
- **Cross-market fair-price settlements are independently determined**, so the
  relation basket floor is **not guaranteed**. Abandon a soccer match and the
  basket returns `p_home + p_away + p_tie`, which no rule requires to be a
  dollar — **floor $0.00, not $1.00** — while every contract conserves
  perfectly within itself.
- Remaining ordinary blocker after the emergency-scope correction: the
  Contingencies clause in all 14 families reserves "the right to make settlement
  determinations" with no stated output.
- `ROTHERA_PHASE_2D = FAIL`; `LIVE_PRICE_IS_NOT_THE_ONLY_UNKNOWN = true`;
  `READ_ONLY_TRANSPORT_NOT_JUSTIFIED = true`.

### PROPHETX (phase 2D, closed)

- Rule 5.2 provides discretionary settlement with a **combined-payout ceiling
  but no conservation floor**: "In no case shall the combined payout across
  positions **exceed** the stated maximum Settlement Value." A ceiling is
  satisfied by `$0.40 + $0.40` and by `$0 + $0`.
- Rule 5.2(f) makes determinations final and unappealable; 5.2(a) permits the
  Exchange to "reverse, amend, or resettle a settlement" after the fact.
- **`COMPLEMENT_CONSERVATION_NOT_PROVEN`.** Screen closed.

---

## 3. Emergency-power scope decision

Broad exchange-wide emergency authority is **not** part of the ordinary
contractual settlement proof when it represents extraordinary venue or
regulatory intervention rather than a normal product settlement mechanism.

**Rationale.** A proof that no sovereign, regulator or exchange will ever
intervene is unobtainable on any regulated venue. Requiring it makes contractual
arbitrage unprovable everywhere, which means the requirement is not
discriminating between venues — it is refusing all of them for a property none
of them can have. That is categorically different from proving what a contract
pays when it resolves normally, which is a question about text and does have an
answer.

Recorded as **`RESIDUAL_VENUE_INTERVENTION_RISK`**
(`predarb.semantics.venue_intervention`), carried on the proof object and
printed by `describe()` and `payload()`. **Disclosed, never eliminated.**

The strongest claim the machinery can now produce is
**`CONTRACTUALLY_GUARANTEED_UNDER_NORMAL_GOVERNING_SETTLEMENT`** with separately
disclosed extraordinary venue risk. **Nothing is ever described as risk-free.**

**The exclusion is narrow and machine-enforced.** `IN_SCOPE_MECHANISMS` holds
the entire settlement census and `classify_intervention` refuses to exclude any
of it. These stay fully inside the proof: product cancellation rules; normal
settlement discretion; fair-market-price settlement; tie/push rules; source
failure; outcome review; scheduled contingencies; product-specific discretionary
valuation; any mechanism expressly reachable in ordinary contract resolution.

**Applied to two venues, changing neither verdict.** Rothera Rule 1.11 and
Polymarket US Rule 2.8 left their censuses. Polymarket US remains blocked by
Rule 9.101(K), a product clause reachable on any rained-off game. Rothera
remains blocked by the Contingencies reservation, an ordinary source-delay
contingency — which the scope correction *surfaced*, having previously been
masked by the emergency blocker.

---

## 4. Research lessons

**A. Price-sum arbitrage tests are insufficient.** Every venue in this study
would have produced apparent opportunities under a `Σp < 1` screen. None of them
survived contact with the settlement text.

**B. Alternate settlement clauses routinely dominate apparent payoff
identities.** Kalshi 6.3(c), ForecastEx 414(b)(3), Polymarket 9.101(K), Rothera
Contingencies, ProphetX 5.2 — in every case the exceptional clause, not the
ordinary one, decided the venue.

**C. Single-contract complement conservation does not imply cross-contract
basket conservation.** Rothera is the proof: `long + short = $1.00` exactly, for
every price, in every family — and a basket floor of zero. The two are
independent properties and both can hold at once.

**D. Independent discretionary settlement prices destroy logical partition
floors.** A partition proven from contract text pays exactly one notional only
while the members settle on a shared outcome. Once each settles at its own
determined price, the partition constrains nothing.

**E. Venue matching rules can algebraically remove an apparent arbitrage before
market prices matter.** ForecastEx fixes an inverse pair at $1.01 and nets it at
$1.00. No quote, no depth and no fee schedule can rescue that; the edge is
closed before any data is fetched.

**F. Product-specific filings matter more than high-level venue rulebooks.**
Both Polymarket US and Rothera screened clean at the rulebook layer *because
their rulebooks specify no product's payout*. Screening a venue's rulebook is
not a screen of the venue.

**G. Semantic qualification should precede API/transport implementation.**
Phase 1 built a complete adapter, transport and replay stack for a venue whose
settlement semantics then blocked it. Phases 2A–2D inverted the order and
rejected four venues without writing a line of transport.

**H. Extraordinary venue/regulatory intervention should be tracked separately
from normal contractual settlement semantics** — §3 above.

---

## 5. Engineering result — **SUCCESS**

The research thesis was not supported. The system built to test it worked.

It successfully:

- reconstructed live order books from a real venue's feed;
- modelled executable depth rather than displayed price;
- bounded fees exactly, with venue-specific rounding modes;
- used exact scaled-integer arithmetic throughout — no binary float ever touched
  money, prices, quantities or payouts;
- enforced settlement certification as a gate on execution;
- built dependency closure and version lineage over governing documents,
  including incorporation-by-reference as a graph rather than a leaf;
- reproduced decisions offline from point-in-time captures;
- recovered durable state after induced faults;
- **identified false arbitrage opportunities that simpler scanners would
  accept** — repeatedly, at five venues;
- transferred its semantic proof machinery across five venues and three
  regulatory architectures without redesign.

**The fact that it rejected every venue is evidence that the gate worked.** A
scanner that found opportunities at these venues would have been wrong. The
system's output — *no executable contractual arbitrage is provable here* — is a
correct finding, arrived at by exactly the machinery built to arrive at it.

Where the machinery caught its own operators, it is recorded: a fail-open
dependency bug, a losing-side zero proven by silence, a conflated citation
chronology, a `$1/N` overclaim, a whitespace bug that falsely reported five
strict two-state families, and two `DISPROVEN` verdicts that should have been
`NOT_PROVEN`. Each was found, corrected, and kept in the history rather than
rewritten.

---

## 6. Verification

```
pytest                 2946 passed, 2 skipped, 8 deselected
ruff check .           All checks passed
ruff format --check .  clean
mypy src tests         Success: no issues found in 221 source files
tools/security_scan.py 0 hits across all five scopes
```

- `phase-1` unchanged at `f77569f`.
- No credentials configured or used, anywhere in phases 1 or 2.
- No regulatory PDFs tracked; all live evidence remains under a gitignored local
  research path and is referenced by sha256 only.
- No background process running.
- No live trading attempted.
- No certificate falsely marked approved; the phase-1 acceptance criterion that
  was not met remains recorded as unmet.

---

## 7. Scope of this conclusion

This report closes the original single-venue contractual-arbitrage study. It
does not propose a successor. Cross-venue arbitrage, statistical mispricing and
other strategies were not investigated here and are deliberately excluded from
every conclusion above; they would be separate projects if explicitly chosen
later.

**Phase 2 is frozen.**
