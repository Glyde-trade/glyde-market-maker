# Paper versus live execution reconciliation

After a session, compare its **captured** private WebSocket fills with the existing
paper tape simulator using the same acknowledged maker quotes:

```sh
glyde-mm reconcile --session-file /private/path/session.jsonl
```

This command is entirely offline. It reads one bounded capture, never opens an
exchange connection, loads keys, issues orders or changes risk state. Its JSON
report shows per-order live/paper quantities and VWAP, matched quantity, slippage,
paper-only quantity (`missed_live_quantity`), live-only quantity and fee impact.
All decimals remain strings. Positive adverse slippage means worse execution;
negative means price improvement. Buy slippage is live minus paper price; sell
slippage is paper minus live price, multiplied by matched quantity for USDC cost.
Without matched quantity, slippage is unavailable, not zero.

Fee impact is the sum of captured USDC fees, including negative rebates, relative
to paper's explicit zero-fee assumption. Missing fees or another fee token make
that figure unavailable; no conversion or fee schedule is invented. Slippage and
fees are separate execution observations, **not a strategy-profit calculation**.
For the same passive limit, valid fills cannot be worse than the limit price;
the parser rejects such inconsistent captures.

## Required capture

The report requires a `glyde.execution-session` JSONL export, illustrated by the
clearly synthetic fixture in `tests/fixtures/reconciliation/session.jsonl`.
Keep real captures private (0600); never commit user data. Do not include keys,
signatures, HTTP headers or authentication messages.

- Header: `format: glyde.execution-session`, `version: 1`, `symbol`,
  `size_decimals` and the exact private stream `user`.
- Every event has a nondecreasing `received_at_ms` and `type`.
- `public`: `frame` is the captured Glyde `l2Book` or `trades` WS frame. Both
  channels must be present; an observed empty trade baseline is valid.
- `order`: `order` has original `symbol`, `side: buy|sell`, `quantity`, `price`
  and `client_order_id`; `receipt` has `status: resting` and original `order_id`.
  Only definite post-only maker quotes are supported. If included,
  `time_in_force` must be `Alo` and `reduce_only` must be false.
- `cancel`: original `order_id`, plus a `receipt` with that exact ID and
  `status: cancelled|already_closed`. Unknown outcomes are not cancellation proof.
- `private`: `frame` is the original `userFills` frame with matching `data.user`,
  `data.fills` and optional boolean `data.isSnapshot`. Fills retain `coin`,
  `side: B|A`, `sz`, `px`, `time`, `oid`, `tid`, and fee/feeToken when supplied.
  Preserve `cloid` when supplied; mismatches refuse. Snapshot/update duplicates
  are counted once; conflicting duplicates refuse.
- Final `end`: `received_at_ms`, `public_complete: true` and
  `private_complete: true`, written only after a continuous, fully retained capture.
  A gap, restart, reconnect, truncation or unknown action must not be marked complete.

The ordinary bot diagnostic logs and dashboard snapshots deliberately omit these
execution facts. **They are insufficient inputs.** This command does not add an
automatic private-session recorder: export the original acknowledged requests,
receipts and WS frames from your execution capture. Never reconstruct quote
lifetimes from fills, infer zero fills from missing frames, or invent an end marker.
Coverage is declared by the capture producer, not cryptographically proven here.

## What the comparison can establish

Each recorded quote becomes eligible in paper at its acknowledgement receipt time;
cancellation takes effect at its acknowledgement. Public source/placement fences,
queue consumption, partial fills and original IDs come from the existing simulator.
Other actors' fills are excluded. No new strategy is planned and the observed quote
schedule is held fixed: this is a conditional execution comparison, not a forecast
of how a strategy would have adapted to different fills or risk state.

True queue priority, hidden liquidity and order/cancel latency are absent. A
paper-only quantity is a **model discrepancy**, not proof a real fill was missed.
Fills during dispatch/ACK latency can appear live-only. Captures require a usable
book before placement and at most one hour / 16 MiB / 100,000 events. Unsupported,
ambiguous or incomplete evidence refuses instead of producing a successful report.
