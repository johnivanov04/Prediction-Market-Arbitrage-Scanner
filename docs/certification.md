# Settlement evidence and human certification

Step 7 established that a contractual-arbitrage claim needs a
`SettlementCertificate`. Step 8 is the workflow that produces one: capture the
evidence, fingerprint it, put it in front of a person, record what they decided,
and detect when the evidence moves out from under that decision.

---

## 1. Evidence is not a certificate

Two different objects, deliberately.

**Evidence** is what the venue published: rules text, notional, market type,
early-close conditions, settlement sources, contract documents. It is a fact
about the world, captured at an instant and never edited.

**A certificate** is a claim about that evidence, made by a person:

> a human reviewed this exact version of the relevant settlement evidence and
> approved this specific payoff claim.

Evidence without a decision proves nothing. A decision without the evidence it
was made against cannot be re-examined. Both are stored, and a certificate
names the snapshot it came from.

---

## 2. The claim is narrow

`STANDARD_BINARY_COMPLEMENT` asserts exactly one proposition:

> For every permitted terminal settlement state of this market, YES payout +
> NO payout equals the explicit notional.

That is what the Step 7 detector needs and nothing more. It does **not** assert
that the event is fair, that the market is correctly priced, that the outcome is
likely, that the wording is good, that another market is equivalent, or that an
event group is exhaustive.

Keeping the claim narrow is what makes review tractable. A reviewer asked "is
this market fine?" cannot answer; a reviewer asked "can YES and NO ever fail to
sum to $1.00?" can.

---

## 3. What gets fingerprinted, and why a rules hash is not enough

A certificate binds to a fingerprint over all evidence the claim requires. A
market's settlement semantics span more than its rules text: the notional, the
market type, early-close conditions, expiration metadata, strike definitions,
the parent event's structure, the series' settlement sources and any external
contract document. Any of these can change while `rules_primary` stays
byte-identical.

Binding to the rules hash alone would permit exactly the failure this design
exists to prevent:

> rules unchanged, materially relevant settlement evidence changed, certificate
> silently still valid.

`rules_hash` survives as a separately visible component because it is the first
thing a reviewer reads and the most useful line in a diff. It is no longer the
binding.

### Encoding rules

| Rule | Why |
| --- | --- |
| absent / null / empty hash differently | a field that vanished from the API is not a field that arrived as `null` |
| floats are refused, not normalised | no canonical text form round-trips, so the digest would depend on how a value was decoded |
| external documents hashed by **content** | a URL that did not change is not evidence that its contents did not change |
| volatile fields excluded | price, volume, open interest and book state are not settlement semantics |
| schema version is part of the digest | a change to the encoding must not silently reconcile two different bundles |
| component hashes retained | so drift says *what* moved, not merely *that* something did |

The volatile exclusion is the one that protects the workflow's credibility.
Fingerprinting price would invalidate every certificate on every tick, and a
reviewer who sees constant drift stops reading it.

---

## 4. Evidence policy, per claim

There is no universal evidence list. Different claims need different proof, and
one global list would either over-collect — blocking approvals on evidence the
claim does not need — or under-collect, approving on evidence that does not
establish it.

Each claim declares `REQUIRED` / `OPTIONAL` / `IGNORED` components, and every
required component carries a written rationale (asserted by test: an inclusion
nobody can justify is an inclusion nobody can argue with).

If a required component is missing, null, or a required document cannot be
fetched, the request is `EVIDENCE_INCOMPLETE` and **approval is blocked** —
computed, not left to the reviewer to notice. A reviewer shown a bundle with a
missing contract document has no way to answer "can this market void?", and an
approval made anyway would look identical to a well-founded one.

`IGNORED` is listed explicitly rather than implied by omission, so a field's
exclusion is a recorded decision someone can argue with.

### Documents are conditional, not optional

A governing contract document is a third category again:

| `DocumentRequirement` | When | Effect |
| --- | --- | --- |
| `ABSENT` | no URL published | nothing governs, so nothing is missing |
| `PRESENT_BUT_OPTIONAL` | URL published, claim does not depend on it | captured and fingerprinted |
| `PRESENT_AND_REQUIRED` | URL published and governs the claim | content **must** be retrieved |

For `STANDARD_BINARY_COMPLEMENT`, `contract_terms` and `contract` are
conditionally required. Most Kalshi series publish neither, and requiring one
would make those markets permanently un-approvable — a statement about the venue
rather than about the claim. But once the venue references a governing document,
that document governs, and a review conducted without it is not a review of the
contract.

| Situation | Result |
| --- | --- |
| no URL published | may be `COMPLETE` |
| URL published, fetched, readable | may be `COMPLETE` |
| URL published, fetched, unreadable | `COMPLETE`, but manual viewing required |
| URL published, **fetch failed** | `EVIDENCE_INCOMPLETE` |
| unreadable and not acknowledged | issuance blocked |

The failure this closes: "the field is globally OPTIONAL" quietly becoming "the
document may fail to load and the certificate can still be issued". A failed
fetch is still fingerprinted, so it is visible and retryable — but a governing
document we could not read is missing evidence, not an absent document.

## 5. External contract documents

Series metadata may publish `contract_url` and `contract_terms_url`. These are
fetched as raw bytes over plain public HTTP with no credentials, ever.

**Raw bytes and their SHA-256 are authoritative** for change detection. Text
extraction exists only so a human can read something, and its quality is
recorded honestly:

| `TextExtraction` | Meaning |
| --- | --- |
| `CLEAN` | plain text decoded fully |
| `PARTIAL` | markup stripped crudely; readable but not authoritative |
| `FAILED` | we hold bytes we cannot render (every PDF lands here) |

A PDF is reported as `FAILED` rather than guessed at. Showing a reviewer mangled
binary as though it were contract text would be worse than showing them nothing,
because they might read it.

When extraction is not `CLEAN`, the document **requires manual viewing**: the
review CLI prints the source URL and the content hash, asks the reviewer to
confirm they read it there, and refuses to issue a certificate if they did not.
Approving on the strength of garbled text would be approving nothing.

The acknowledgement **binds to the exact content hash**, not to the document
name. An acknowledgement of version A does not satisfy version B: a contract
amended between review and issuance has not been read, however recently the
reviewer looked at the old one. And because the document hash is part of the
evidence fingerprint, a post-issuance amendment also makes the certificate stale
for live use.

A failed fetch is itself evidence and is fingerprinted as such — "we could not
read the terms" is something a reviewer must see, not a silent absence.

### Repository policy

Downloaded contract documents are **not** committed. The local research store
(`data/semantics/`, gitignored) holds evidence snapshots, decisions,
certificates and raw document bytes. The repository carries schemas, synthetic
fixtures and small reviewed API fixtures — not a growing mirror of exchange
contract material that is not ours to redistribute.

---

## 6. The review workflow

```
predarb evidence capture <ticker>          # snapshot + fingerprint
predarb evidence show <snapshot-id>
predarb evidence diff <snap-a> <snap-b>

predarb certificates request <ticker>      # capture + open a review request
predarb certificates review <request-id> --reviewer <name>
predarb certificates list [--requests]
predarb certificates show <certificate-id>
predarb certificates validate <ticker>     # re-fetch, check applicability
predarb certificates refresh <ticker>      # re-capture, diff vs last snapshot
```

No JSON is edited by hand at any point.

### The checklist

Ten questions, each with the answer the claim requires. Answers are recorded
with the decision, so a later reader sees what the reviewer was asked and what
they said — including where they were uncertain.

`UNCERTAIN` exists so honest doubt has somewhere to go. Without it, a reviewer
facing an ambiguous clause must either overclaim or abandon the review, and the
first is far more likely. An `UNCERTAIN` answer blocks approval.

The claim is a conjunction, so a single answer outside the safe set — "yes, it
can void" — blocks issuance rather than merely warning.

### Approving is deliberately awkward

There is no "press Enter to approve". The reviewer types `APPROVE <TICKER>` in
full; anything else that starts with `APPROVE` is refused with the exact phrase
required. `REJECT` and `MORE` (needs more evidence) are the alternatives.

### No automatic certification

Nothing in the codebase can produce a `VERIFIED` certificate without a recorded
human decision. Text tooling may later summarise or flag clauses, and any such
helper is **advisory**: text similarity is not evidence of settlement behaviour,
and a model's confidence is not a person's judgement.

---

## 7. Issuance

A certificate is issued only when every one of these holds:

1. evidence is `COMPLETE` for the claim
2. the decision is `APPROVED`
3. the decision's fingerprint matches the request's
4. the bundle presented is the one that was reviewed
5. the claim matches
6. every checklist question is answered, and answered safely
7. every unreadable document was acknowledged as viewed at source
8. the payoff tables pass the complement structural check

There is one issuance path and no bypass. Synthetic test constructors are
separately named so they cannot be confused with reviewed ones, and a test
asserts no `certify` / `auto_issue` / `force_issue` / `trust_market` function
exists.

---

## 8. Persistence

`data/semantics/` — `evidence/`, `requests/`, `decisions/`, `certificates/`,
`documents/`. Files rather than Postgres: these are low-volume research records
that must stay readable and diffable by a person, and a database dependency
would buy nothing here.

Records are content-addressed and **never silently overwritten**. Writing a
record that already exists with different content raises, because that means two
different things are claiming the same identity. Mutable indexes are replaced
atomically.

A stored snapshot must re-fingerprint to the digest it had when captured — the
review CLI reloads from disk, so a lossy encoding would make issuance fail
against the very evidence that was reviewed. That regression is tested directly,
including nested mappings, nested sequences, and absent/null/empty.

---

## 9. Drift and historical validity

`predarb certificates validate <ticker>` re-fetches evidence and reports:

| Applicability | Meaning |
| --- | --- |
| `ACTIVE` | evidence matches; usable for live classification |
| `STALE_EVIDENCE_DRIFT` | evidence moved since review |
| `EVIDENCE_UNAVAILABLE` | current evidence could not be established |
| `NOT_YET_VALID` / `EXPIRED` | outside the certificate's validity window |

Drift makes a certificate **inapplicable to current evidence**. It does not
rewrite the record as though the review never happened: the stored certificate
stays exactly as issued, still `VERIFIED` for the evidence it was issued
against. New evidence requires a new review and a new certificate.

That distinction is not bookkeeping. A replay of a past instant needs the
certificate that was actually in force then.

### Point-in-time, both directions

`active_at(market, claim, current_fingerprint, at)` never returns a certificate
issued after `at`. Certification tomorrow must not leak backward into a backtest
of yesterday, or the replay silently inherits knowledge nobody had. Tested.

---

## 10. How live scanning gets certificates

The scanner consults the registry and nothing else:

| Registry state | Scanner behaviour |
| --- | --- |
| no certificate | `BLOCKED_SETTLEMENT_SEMANTICS` |
| active certificate | detector proceeds |
| certificate exists, evidence drifted | `BLOCKED_SETTLEMENT_SEMANTICS` + `CERTIFICATE_STALE` |

Evidence is captured fresh on every run — a cached fingerprint would defeat
drift detection entirely. There is deliberately **no `--skip-certification`
flag** for live contractual-arbitrage mode.

---

## 11. Live validation, 2026-09-19

Four active NCAA football markets captured, four review requests generated,
**zero certificates issued**.

| Check | Result |
| --- | --- |
| evidence captured | 4 markets, 45 components each |
| completeness | `COMPLETE` for all four |
| contract documents | both PDFs retrieved per market (301,600 B and 40,044 B), hashed, extraction `FAILED` |
| manual viewing required | `contract`, `contract_terms` on every market |
| fingerprint stability | immediate recapture: **no drift, 45 components unchanged** |
| drift detection | on a locally-mutated copy: `CHANGED: document.contract_terms.sha256, market.rules_secondary, series.settlement_sources` |
| certificates issued | **0** — awaiting human review |

The review queue is left for a person:

| Request | Market | State |
| --- | --- | --- |
| `2a65fa63aec6` | `KXNCAAFGAME-26SEP19PURUCLA-PUR` | `AWAITING_REVIEW` |
| `38cf2e706e7a` | `KXNCAAFGAME-26SEP19SDAKBSU-SDAK` | `AWAITING_REVIEW` |
| `4733538fc0fc` | `KXNCAAFGAME-26SEP19JMUSDSU-JMU` | `AWAITING_REVIEW` |
| `ed765d49e2e2` | `KXNCAAFGAME-26SEP19NIUARIZ-NIU` | `AWAITING_REVIEW` |

Structurally notable, without judging the semantics: every one of these carries
two contract PDFs that our extraction cannot render, so each will require the
reviewer to open both at source before approval is permitted. Settlement sources
are populated (NCAA and ESPN, with URLs) and the notional is `$1.0000`
throughout.

Two bugs were found by running this, both now fixed and regression-tested: the
policy required `market.settlement_kind` which the capturer never emitted, so
every market was permanently `EVIDENCE_INCOMPLETE`; and nested mappings were
stringified on the way to disk, so a reloaded snapshot re-fingerprinted
differently and issuance would have failed against the evidence that had just
been reviewed.
