# Operating a market maker

Use a dedicated account and one runner per account. Paper results, doctor success
and a local dashboard do not authorize live trading. Start with a copied paper
config and review [all strategy/risk keys](strategies.md).

## One-shot public doctor

```sh
glyde-mm doctor path/to/paper.yaml
```

The ordinary command validates YAML/strategy settings, reports configured key
**environment-variable names and presence only**, and makes one `meta` request and
one `l2Book` public request for each market. No private header, signing wallet, exchange
action, WebSocket or retry/polling loop is used by this default check. Paper keys
are optional; missing required live keys return failure without sending them.

Clock diagnostics use the existing public response's HTTP `Date`, not an extra
request. They bound server-minus-local time using request start/end and Date's
one-second rounding. Detectable offset beyond ±1 second, or a backward local clock
jump during a request, returns failure. This is not NTP or an assertion that a
proxy's clock equals the engine clock. Missing/malformed Date prints **unavailable**;
it does not fail an otherwise valid public check. Book source freshness is still
checked, but an old book cannot distinguish feed lag from clock skew. Fix system
time or connectivity deliberately; doctor never changes the clock or risk limits.

Existing explicit `doctor live.yaml --private` additionally checks account/orders
read permission. It is opt-in and never signs or places orders. It remains separate
from the public-only default and cannot prove trading authorization.

## State location and ownership

For `path/to/bot.yaml`, ordinary CLI commands use `path/to/.glyde-mm/`:

| Path | Purpose |
| --- | --- |
| `paper-<hash>.json` / `live-<hash>.json` | Daily equity baseline/loss latch, rolling placement budget and unresolved original execution intent. Hash binds mode, endpoint, owner and vault. |
| `control-<hash>.operation` / `.run` / `.stop` | Local lock/stop coordination; control identity also includes configured symbols. |
| `logs/run-<id>/events*.jsonl` | Bounded, private diagnostic segments with version/commit, event sequence and outcome. |
| `logs/run-<id>/dashboard.json` | Atomic cached display snapshot, not venue authority. |
| `stops/stop-<id>.json` | Explicit stop result; inspect `outcome`, `failure` and remaining-order status. |
| `live-wizard/session-<id>/` | Frozen wizard config, isolated preview/live log paths and final session report; account risk state remains shared. |

The accepted legacy `risk_state_path` YAML field does not relocate the streaming
CLI state: the runner derives the hashed path above. Keep config location stable.
Moving the config without state or renaming a Docker volume creates a different
local history; it is not a safe reset. Preserve the entire directory privately.
Do not publish state, account IDs, keys, env files or webhook URLs. The runner uses
private files/directories; root and Docker administrators can still inspect them.

## Stop, kill switch and cancel-all

Ctrl-C/SIGTERM requests orderly runner cleanup. From another local terminal:

```sh
glyde-mm stop path/to/bot.yaml
```

Use the same config location, endpoint, mode, owner/vault and symbol set. Keep
`mode: paper` in paper configs: `paper` forces simulation even for live YAML, while
`stop` addresses the mode actually written in YAML. Never use a live config to stop
a paper session. The local mechanism does not control another host or old runners.

Stop blocks concurrent startup, asks the local runner to exit and waits up to 15
seconds for its session lock. In live mode it attempts persisted original IDs
**before** private order discovery, then symbol-scoped cancel-all and an
authoritative empty open-order check. That sweep may cancel manual/other-bot orders
on those symbols; it excludes other configured-out symbols. A read/cancel/identity
failure returns a failed stop report, not proof that orders are gone.

Paper stop coordinates the local simulator; it makes no live exchange call and
cannot establish anything about venue orders. Neither stop nor daily-loss refusal
closes positions. SIGKILL, crashes and power loss cannot perform cleanup. Inspect
the venue and remaining exposure separately before any restart. Never interpret a
timeout, missing read or HTTP error as an empty order set.

## Restart deliberately

1. Keep the original config and state; stop the old process and inspect its result.
2. Resolve unknown orders/cancels by their original order/client IDs at the venue.
   Do not resend with new IDs or delete a persisted intent to bypass refusal.
3. Preserve UTC-day loss state and rolling order budget. Restart does not clear
   them; a new UTC-day baseline does not restart a stopped process.
4. Run public doctor, then paper if the strategy/config changed. Start live only
   with reviewed registration, private permissions, limits and typed confirmation.

`--i-understand` accepts existing selected-symbol exposure at live startup; it does
not bypass confirmation, freshness, unknown-outcome or risk checks. Docker/systemd
recipes deliberately avoid automatic restarts; see [VPS operation](vps.md).

## Read logs without inventing execution proof

```sh
glyde-mm report path/to/.glyde-mm/logs
glyde-mm dashboard --state path/to/.glyde-mm/logs/run-<id>/dashboard.json
```

Reports show retained build identity, outcome, counters and truncation status.
`incomplete` means no retained end event; `history_truncated` means rotation removed
earlier history. Neither proves that the venue is empty. Preserve original state
and useful diagnostic segments before rotating whole run directories. A clean
local exit still does not close inventory or qualify the next session.

Dashboard is loopback/read-only and uses cached local observations. Choose the new
run's exact snapshot after restart; do not expose it through a public tunnel.
Chat alerts are best effort, not a kill switch or cancellation receipt. Execution
comparison needs a complete [reconciliation capture](reconciliation.md), not these
diagnostic logs. Check [troubleshooting](troubleshooting.md) for refusal categories.
