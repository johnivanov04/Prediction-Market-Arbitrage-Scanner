# Durable storage and Phase-1 retention

Phase 1 persists what a later auditor would need to recompute every claim the
system made, and nothing that would put credentials or account data on disk.

---

## 1. Two categories, and why the boundary matters

| | Examples | Regenerable? |
| --- | --- | --- |
| **Source evidence** | raw WebSocket frames, connection lifecycle, REST payloads, metadata, fee observations, evidence snapshots, registry snapshots, certificates | **No.** Lost means gone. |
| **Derived** | reconstructed books, execution curves, fee quotes, `DecisionRecord`s, replay reports, metrics | **Yes**, by replay. |

Derived rows always reference the source rows behind them, and **no derived
table is the only copy of anything needed to reconstruct history**. A decision
without its observations is an assertion nobody can check.

---

## 2. The safety ordering

```
receive observation
  → persist SOURCE evidence         ← fatal if it fails
  → mutate authoritative state
  → evaluate through the coordinator
  → persist DERIVED decision        ← recoverable if it fails
```

If derived persistence fails, the observations remain and replay regenerates
the decision — the cost is a re-run. If **source** persistence fails there is
nothing to regenerate from, so the collector stops: continuing would mean
scanning on evidence it cannot prove it had.

Reversing this would let the system hold decisions whose inputs were never
durably captured. That is an audit trail nobody can verify, which is worse than
having none, because it looks like one.

---

## 3. Backend

SQLAlchemy Core, so the same statements run on SQLite and Postgres.
`architecture.md` names Postgres for the structured catalogue; Phase 1 defaults
to a **local SQLite file with WAL** because a single research process should not
need a daemon alive beside it for a multi-hour soak. Postgres remains reachable
by changing one URL, and nothing in `storage/` uses a SQLite-only construct.

SQLite's defaults are wrong for this and are set explicitly per connection:

| Pragma | Why |
| --- | --- |
| `foreign_keys=ON` | off by default, so declared references would be decorative |
| `journal_mode=WAL` | lets a verification pass read while the collector writes |
| `synchronous=FULL` | a crash must not lose an acknowledged source observation |

Money and quantities are canonical strings. A `REAL` column would undo Step 1 at
the storage layer. Timestamps go through a `UtcDateTime` decorator because
SQLite hands back **naive** datetimes and every other module refuses them.

### Where the bytes live

The append-only raw journal stays the raw-source store (`architecture.md` §5.5).
Observations carry a content hash plus a journal reference, which is enough to
fetch the original bytes and prove they have not changed. Copying multi-kilobyte
frames into SQL as well would double the disk cost to store the same thing
twice. Small context payloads *are* inlined, because that is what a reader
actually needs to see.

---

## 4. Schema and migrations

`SCHEMA_VERSION` is recorded in the database and checked on every open. A
version this build does not recognise **raises** rather than opening: older code
writing into a newer shape corrupts an audit trail in the least visible way
available.

Migrations are a numbered list of `(from, to, callable)`. Alembic is a declared
dependency and is the right tool against an evolving ORM with autogenerate; this
is six hand-written Core tables, and the property worth testing is narrow — a
fresh database reaches head, an older one steps forward once per step, a newer
one is refused. Backward migrations are deliberately absent: Phase 1 never needs
to downgrade, and an unexercised downgrade path is one that does not work.

Tables: `sessions`, `connection_epochs`, `observations`, `decisions`,
`health_events`, `replay_verifications`.

---

## 5. Idempotency

Every write is keyed on a **content-derived identity**, so a retry after an
uncertain result either proves the row exists or inserts it exactly once.

| Record | Identity over |
| --- | --- |
| session | detector plan + start instant |
| observation | session + ordinal + kind + payload hash |
| decision | session + decision ordinal + economic fingerprint |
| health event | session + kind + instant + detail + counters |

The observation identity includes the **payload hash**, not just the ordinal:
two different payloads at one ordinal is a bug worth failing on, not a silent
overwrite. A collector that reconnected mid-write must not produce a second copy
— replay over a duplicated stream reconstructs a different book, and the
duplicate is invisible in every count that reads the table.

---

## 6. Backpressure

The gap between receive and persist is bounded. If persistence falls behind the
feed, the collector becomes **unhealthy and stops**. It does not drop frames: a
silently sampled source stream reconstructs a different book, and the gap is
undetectable afterwards. The high-water mark is recorded either way.

---

## 7. Verification

```
predarb storage verify-decisions <session>
```

Replays the stored source observations, regenerates decisions, and compares
fingerprints. It reports **missing**, **extra**, **mismatched** or **exact
match** — and repairs nothing. Replay is authoritative by construction; silently
rewriting a mismatched row would destroy the only evidence that something went
wrong, and a mismatch is exactly the signal worth stopping for.

An **extra** persisted decision is the more serious finding: it means a stored
claim has no source evidence behind it.

---

## 8. Retention policy

**Keep indefinitely** — source evidence and anything a proof rests on:

- raw journal files (the bytes every claim is ultimately checked against)
- the `observations` table
- settlement and relation certificates, evidence snapshots, review decisions
- session manifests and detector plans
- registry snapshots, including explicitly empty ones — an empty snapshot is
  what separates "we checked and found nothing" from "nobody looked", and
  deleting it silently weakens every absence claim that depended on it

**Regenerable, safe to delete** — rebuilt by replay from the above:

- reconstructed book snapshots
- execution curves and fee quotes
- derived metrics and soak reports

**`DecisionRecord`s: retained, but regenerable.** They make a session queryable
without re-running anything, which is worth the disk. They are not the authority
— `verify-decisions` exists precisely because the stored row is a summary and
replay is the source of truth. Deleting them costs query convenience and no
evidence.

**Never deleted automatically.** Nothing in Phase 1 prunes on a timer. Raw
evidence needed for audit is removed only by a human who has decided to.

Observed growth over the soak is recorded in `phase1_report.md` §20, which is
what a longer collection should be sized against.

---

## 8a. Running a session

```
uv run python tools/collect.py --minutes 45 --markets 6
predarb storage sessions
predarb storage show <SESSION_ID>
predarb storage verify-decisions <SESSION_ID>
predarb storage migrate
uv run python tools/security_scan.py
```

`tools/collect.py` is the consolidated Phase-1 runner: discovery, metadata, fee
context, both registry snapshots, WebSocket liveness, raw journalling, book
reconstruction, all three detectors, durable persistence and an end-of-run
replay verification. It reaches no order, balance, position or fill endpoint,
and there is no flag that bypasses a semantic gate.

---

## 9. Security

The durable dataset holds **public market and research data only**. Nothing
persists a private key, API key ID, signature, auth header, balance, position,
order or fill — there is no code path that reads them, since Phase 1 has no
account endpoints and no write surface at all.

`KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-SIGNATURE` and `KALSHI-ACCESS-TIMESTAMP` are
redacted from every logging path (`redact_headers`), and the signature is
excluded from the `AuthHeaders` repr so a traceback cannot leak it.

The catalogue, raw journals and research JSON live under gitignored local paths
and are never committed.
