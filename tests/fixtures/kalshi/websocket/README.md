# SYNTHETIC WebSocket fixtures

**Every file in this directory is SYNTHETIC. None is a captured live message.**

## Why

The Kalshi production WebSocket requires authentication. This was verified
empirically, not assumed:

```
wss://external-api-ws.kalshi.com/trade-api/ws/v2
-> websockets.exceptions.InvalidStatus: server rejected WebSocket connection: HTTP 401
```

No API key is configured in this environment (`predarb config` reports
`has_credentials: False`), so no real message could be captured. Rather than
present a fabricated payload as real, these are labelled synthetic here, in
`manifest.json` (`"source": "SYNTHETIC"`), and in every filename
(`synthetic_*.json`).

## Provenance of the shapes

| File | Basis |
| --- | --- |
| `synthetic_orderbook_snapshot.json` | Reproduced **verbatim** from the official WebSocket reference example |
| `synthetic_orderbook_delta_yes_negative.json` | Reproduced **verbatim** from the official WebSocket reference example |
| `synthetic_orderbook_delta_no_positive.json` | Documented shape, values varied (NO side, positive delta) |
| `synthetic_orderbook_delta_fractional.json` | Documented shape, values varied (sub-cent price, fractional delta) |
| `synthetic_orderbook_delta_level_depleted.json` | Documented shape; depletes the snapshot's `0.5400`/`20.00` NO level to exactly zero |
| `synthetic_orderbook_delta_no_timestamp.json` | Documented shape with `ts_ms` omitted (documented optional) |
| `synthetic_error.json` | Reproduced **verbatim** from the official quick-start error example |
| `synthetic_error_subscription_limit.json` | Documented error code 26; message text is our paraphrase |
| `synthetic_subscribed_ack.json` | **Lowest confidence.** The docs show only `{"type": "subscribed", ...}` and never expand `msg`. The `channel` / `sid` fields are inferred from the unsubscribe command taking `sids`. |

## Field-name divergence between REST and WebSocket

This is real and is easy to get wrong:

| Transport | Book field names |
| --- | --- |
| REST `GET /markets/{ticker}/orderbook` | `orderbook_fp` wrapper, inner `yes_dollars` / `no_dollars` |
| WebSocket `orderbook_snapshot` | no wrapper, `yes_dollars_fp` / `no_dollars_fp` directly in `msg` |

The two are modelled by separate wire classes for exactly this reason.

## What must be re-verified once credentials exist

1. The `subscribed` acknowledgement's actual `msg` shape.
2. **`seq` scoping** — connection-global, per-`sid`, per-market, or per-channel.
   This cannot be determined from these fixtures and must not be inferred from
   them. See `docs/api_assumptions.md` A-09; the reconstructor fails closed
   until it is settled.
3. Whether `seq` resets on resubscribe, and the expected gap-recovery procedure.
4. Whether real snapshots order levels ascending, as the REST book does.
