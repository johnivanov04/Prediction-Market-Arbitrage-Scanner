# Order-Book Reconstruction

How a WebSocket frame stream becomes book state we can prove, and what happens
when we cannot.

## 1. The pipeline

Strictly ordered. Single reader. No concurrency inside.

```
raw frame text
      │  journal            append-only, before anything else
      ▼
generic envelope extraction
      │
      ▼
sid/seq integrity check     BEFORE routing, always
      │
      ▼
typed routing
      │
      ▼
book mutation (if applicable)
```

Two orderings in that diagram are load-bearing.

**Sequence checking precedes routing.** The documented `ok` response to
`update_subscription` carries `sid` and `seq`, so control frames consume
sequence numbers. A reconstructor that routes first and sequences only
order-book messages sees a phantom gap every time a control frame passes, and
would resynchronise constantly against a perfectly healthy stream.

**Journalling precedes parsing.** A frame we cannot parse is exactly the frame
worth having on disk, and a book whose inputs were not all recorded cannot be
audited later.

## 2. The sequence invariant

From the A-09 production evidence (`docs/api_assumptions.md`): `seq` is keyed on
`(connection epoch, sid)` and is dense within it — 2,752 adjacent pairs, all
advancing by exactly one, zero exceptions.

> Track `seq` per `(connection epoch, sid)`. The first frame of a sid
> establishes the baseline; every subsequent frame carrying a `seq` must equal
> `expected_seq`. A skip, repeat or decrease invalidates **every book belonging
> to that sid**, and the violating frame mutates nothing. Frames without a `seq`
> pass through without advancing the counter. Sequence state is discarded on
> disconnect.

**A gap invalidates the whole sid, not one market.** One subscription covers
many markets under one counter, so a hole could have carried an update for any
of them. Invalidating only the market named in the *next* frame would leave the
genuinely affected book silently stale — which is the failure mode that matters,
because that book still looks tradeable.

**Density is observed, not guaranteed.** Nothing in Kalshi's documentation
promises it. Phase 1 treats it as dense anyway, because the costs are
asymmetric: a false violation costs a resynchronisation, while a missed gap
costs a book that looks tradeable and is not.

**Once violated, a stream stays violated** until an explicit recovery boundary.
That is what stops a reconstructor from "catching up" to a corrupted stream and
quietly declaring a book valid again.

## 3. Book state machine

| State | Meaning |
| --- | --- |
| `WAITING_SNAPSHOT` | Subscribed, but no authoritative snapshot yet in this epoch |
| `VALID` | A snapshot established it, and every frame since passed |
| `INTEGRITY_UNKNOWN` | Something happened we cannot reason past |
| `UNSUBSCRIBED` | No longer subscribed; nothing keeps it current |

Only `VALID` is ever scannable.

```
subscribe ──▶ WAITING_SNAPSHOT ──snapshot──▶ VALID ──valid deltas──▶ VALID
                     ▲                         │
                     │                         │ sid sequence violation
                     │                         │ negative quantity
                     │                         │ malformed payload
                     │                         │ unknown sequenced frame
                     │                         │ connection lost
                     │                         │ journal failure
                     │                         ▼
                     └────fresh snapshot── INTEGRITY_UNKNOWN
```

Invalidated books keep their contents — useful when investigating what went
wrong. They are unreachable to a scanner because integrity gates that, not
emptiness.

## 4. Integrity vs freshness

Three different clocks answering three different questions. Conflating them
produces both failure modes at once: quiet markets get wrongly excluded, and
genuinely broken books stay eligible because they happen to be busy.

| Signal | Question | Health signal? |
| --- | --- | --- |
| book change time | When did *this market's* book last change? | **No** — a resting quote can sit unchanged for hours and be perfectly correct |
| sid activity time | When did this subscription last carry a sequenced frame? | No — a whole subscription can be quiet |
| connection liveness | Is the socket alive? | **Yes** — this gates eligibility |

**Liveness comes from the transport, not from application traffic.** When no
frame has arrived for a while, the collector sends a protocol Ping and waits for
the Pong. A Pong proves the socket works; silence proves nothing.

Kalshi documents this mechanism directly: it sends Ping frames every 10 seconds
with the body `heartbeat`, clients respond with Pong, and clients may initiate
their own Pings — which is what the collector does (A-39). A client-initiated
probe is preferred over waiting for the server's next heartbeat because it gives
a *positive* answer on demand rather than inferring health from the absence of
something.

The cadence is documented; our tolerance window is not. No consequence for a
missed Pong is specified, so how long to accept silence remains **our local
safety policy**, labelled as such wherever it surfaces.

This was not theoretical. An earlier version inferred health from application
frames and dropped the connection every 30 seconds on quiet markets. With
transport pings, the same markets ran a full session on one connection with zero
reconnects.

## 5. Snapshots and deltas

**A snapshot is replacement, never a merge.** Merging would let a level the
venue has dropped survive indefinitely, and the whole value of a snapshot is
that it settles what the book *is*. Validation completes before anything is
mutated, so a malformed snapshot leaves previous state untouched rather than
half-applied.

A snapshot is **not** a sequence reset: it takes the next number in the sid's
stream like anything else. One was observed at seq 250 mid-stream after
`add_markets`. Treating a snapshot as a reset would discard a real gap.

A snapshot may legitimately be **empty** — observed live on markets that had
finished trading.

**Deltas are signed relative changes.** Missing level means zero; a result of
zero removes the level; **a negative result is an invariant failure and is never
clamped.** Clamping would paper over a missed message and leave a book that
looks correct. A delta arriving before a snapshot cannot be applied at all —
applying it to an empty book would fabricate state that was never sent.

## 6. Direct quotes only

The authoritative book stores **exactly what Kalshi sends**: YES bids and NO
bids. No derived asks.

Kalshi publishes bids only, so an ask is an arithmetic consequence of the
opposing bid against the contract's notional — a derivation, not venue truth.
Mixing the two here would make it impossible to tell later which is which, and
consuming a derived ask double-counts the resting interest it came from. Derived
asks belong to the execution view, built later.

## 7. Connection loss and recovery

On any disconnect, every book sourced from that connection becomes non-valid
immediately, and the epoch's sequence state is discarded. After reconnect: new
epoch, resubscribe, wait for fresh snapshots. **Sequence authority never crosses
connections** — and note Kalshi restarts `seq` at 1 on a new connection, so
carrying state across would read as a decrease.

**Recovery policy: full connection reconnect.** Heavier than strictly necessary,
and chosen anyway:

- the hole belongs to a shared sid and could have affected any of its markets;
- a reconnect is a clean boundary that is trivial to audit — everything after it
  derives from data the venue sent after it;
- clever repair is where correctness goes to die.

**REST cannot repair a WebSocket gap.** A REST book is a snapshot at some other
instant with no sequence coordinate shared with the stream. Splicing it in
produces a book that is neither state, and no later delta can be known to apply
to it. REST is useful for advisory cross-checking only.

A journal failure **stops** the collector rather than reconnecting. Reconnecting
would produce fresh, correct-looking books with a hole in their history.

## 8. Raw journal

Append-only JSONL, one object per line, with the original frame carried verbatim
as a string. The exact payload must remain recoverable — a re-serialisation of a
parsed model would preserve any parser bug rather than the evidence.

JSONL rather than Parquet, for now:

- **simple to audit** — a line is a record, readable and greppable without
  tooling, which matters for a file whose purpose is evidence;
- **record boundaries make tail recovery straightforward** — a reader can
  consume up to the last complete line and say exactly where trust ends;
- **losslessness is easy to verify** — payload plus SHA-256 over the original
  bytes;
- streaming columnar writers buffer, and getting crash-safety and back-pressure
  right there is real work for volumes we do not yet have.

### What this does not give us

Append-only JSONL is **not** crash-safe in any strong sense. Each record is
written and flushed out of the process — which survives a *process* crash — but
a flush is not an `fsync`: a machine or kernel failure can still lose an
OS-buffered tail. `fsync` per frame is available (`fsync=True`) and is **off by
default**; paying a syscall per frame to defend against a failure mode we have
not measured would be premature.

Why the weaker guarantee is acceptable:

- **A lost tail cannot corrupt a live session's authority.** Losing the process
  destroys the connection, and a new connection is a new epoch requiring fresh
  snapshots. No book survives to be quietly trusted against a truncated record.
- **The risk is historical, not live.** The danger is a replay mistaking a
  partial record for data.

So the burden falls on the reader. `read_journal` rejects a malformed line
outright. `read_journal_tolerant` exists for forensic use: it returns the
complete prefix plus an explicit `JournalTail` describing the incomplete final
record, and still **raises** on corruption in the middle — a bad line before the
end is damage, not an interrupted write. A final line that parses but lacks its
newline delimiter is also reported as a tail rather than returned as data:
without the delimiter we cannot show it is complete.

### Failure policy

**Nothing may be silently dropped.** A write failure raises, and the
reconstructor treats it as fatal: books stop being scan-eligible and journal
health does not recover on its own. Journal records never contain handshake
headers, signatures, key ids or PEM data.

## 9. Provenance

Each book carries compact coordinates, not an unbounded list — a busy market
would grow that without limit inside a live object. The journal holds the full
chain.

| Field | Answers |
| --- | --- |
| `connection_epoch`, `sid` | Which connection and subscription confer authority |
| `snapshot_raw_id`, `snapshot_seq` | Which snapshot established this book |
| `latest_raw_id`, `latest_seq` | Which frame last touched it |
| `applied_frames` | How many mutations since the snapshot |

## 10. Scan eligibility

Deliberately structural only. Fees, settlement semantics and relationship
verification are later gates; this answers *"is this book provably what the
venue sent, right now?"*

Requires all of: journal intact, connection healthy, active subscription in the
**current** epoch, an authoritative snapshot observed, and no unresolved
violation.

## 11. Single-reader ordering

Exactly one coroutine receives from the socket and drives reconstruction, and
each frame is handled to completion before the next is taken off the socket.

Arrival order is the order the venue applied the updates in. Concurrency inside
this loop would reorder mutations while leaving the sequence numbers looking
fine — the one failure the whole design exists to prevent. Parallelism belongs
downstream, where consumers hold immutable views.
