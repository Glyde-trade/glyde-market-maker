# Operating a market maker

Use a dedicated account and one runner per account. The bot cancels only its own
tagged orders, but other orders in the account still count toward its risk limits. Paper results, doctor success
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
| `preflight-<hash>.json` | Five-minute public-check PASS bound to the complete effective config; never private/trading authority. |
| `paper-<hash>.json` / `live-<hash>.json` | Daily equity baseline/loss latch, rolling placement budget and unresolved original execution intent. Hash binds mode, endpoint, owner and vault. |
| `control-<hash>.operation` / `.run` / `.stop` | Local lock/stop coordination; control identity also includes configured symbols. |
| `logs/run-<id>/events*.jsonl` | Bounded, private diagnostic segments with version/commit, event sequence and outcome. |
| `logs/run-<id>/dashboard.json` | Atomic cached display snapshot, not venue authority. |
| `stops/stop-<id>.json` | Explicit stop result; inspect `outcome`, `failure` and remaining-order status. |
| `live-wizard/session-<id>/` | Frozen wizard config, isolated preview/live log paths and final session report; account risk state remains shared. |

The runner derives the hashed path above; `risk_state_path` is not a supported YAML
field. Keep config location stable.
Moving the config without state or renaming a Docker volume creates a different
local history; it is not a safe reset. Preserve the entire directory privately.
Do not publish state, account IDs, keys, env files or webhook URLs. The runner uses
private files/directories; root and Docker administrators can still inspect them.

## Risk-state durability and slow disks

State snapshots are immutable and written by one background worker, with at most
one active write and one coalesced successor. File `fsync`, atomic replacement and
directory `fsync` run off the asyncio loop, so disk latency cannot stall incoming
books or manufacture stale-feed quote churn. Checkpoints must include execution
intent and order-rate history; incomplete old files refuse startup without being
rewritten. No migration or automatic reset is provided.

An external order or cancellation still awaits its write-ahead intent barrier;
nonblocking does not mean dispatching before durability. Orders recheck the latest
account, orders, books and stop request after that wait. The placement budget stays
reserved through disk/network waits and is retained for 60 seconds after a reply.

Confirmed-reply clears are write-behind. A crash before their barrier completes
can retain a conservative original-intent quarantine even for a completed action;
reconcile the original IDs rather than clearing it blindly. A write error refuses
further placements. Orderly shutdown drains the writer; direct `RiskGuard` users
must await `flush_state()` before depending on a checkpoint and `close_state()`
on exit. Slow or failed storage can delay a durability barrier or shutdown, but
never authorizes an unjournaled order or resets the loss/rate limits.

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

## Optional local Prometheus metrics

Metrics are **off by default**. Add these keys to the exact bot YAML:

```yaml
metrics_enabled: true
metrics_port: 9464
```

The running bot serves `http://127.0.0.1:9464/metrics` in Prometheus text format.
Only loopback binding is supported; it has no authentication and must not be
forwarded to the Internet. Choose different ports for simultaneous runners.
The endpoint starts/stops with the bot, has no write API and makes no exchange
request on a scrape. An occupied port fails startup before trading.

Configure a local Prometheus scrape target:

```yaml
scrape_configs:
  - job_name: glyde-mm
    static_configs:
      - targets: ['127.0.0.1:9464']
```

Import [the sample Grafana dashboard](grafana-market-maker.json), selecting your
Prometheus datasource. Its scrape cadence is monitoring only, not HTTP polling
of account/order/market state. Counters reset with each runner session (including wizard preview/live); use Prometheus
`rate()`/`increase()` for time-window counts. Market labels are zero-based indexes
in the config's market order, never symbols, owner addresses, keys or URLs.
Refusal labels come from a fixed allowlist; arbitrary venue error text is omitted.

| Metric prefix `glyde_mm_` | Meaning |
| --- | --- |
| `quotes_placed_total` | Accepted resting/filled order receipts at the risk boundary; excludes unknown outcomes. |
| `quotes_cancelled_total` | Confirmed per-ID cancellation receipts observed by that boundary; excludes already-closed results. Extra venue orders discovered only inside raw cancel-all are not counted. |
| `quotes_refused_total{reason}` | Definite venue rejections or pre-dispatch risk refusals, by fixed category; unknown writes are not counted as refusals. |
| `fills_total{market}` | Delivered, identified fill events; partial fills count as events, not whole completed orders. Live history snapshots are not new fills. |
| `inventory{market}` | Signed base quantity from the latest account observation. |
| `realized_pnl`, `unrealized_pnl` | USDC. Paper uses simulator cash/PnL, with its zero-fee model. Live unrealized PnL requires complete supplied position facts; live realized PnL is unavailable because equity/cash changes cannot distinguish trading from transfers/funding. |
| `daily_loss_headroom` | Maximum daily loss minus loss from the retained UTC-day equity baseline; can be negative or exceed the limit after gains. It is not margin/withdrawable balance. |
| `book_arrival_age_seconds{market}`, `book_source_age_seconds{market}` | Age of the last committed book by local receipt and source clock; scrapes never restamp it. |
| `account_arrival_age_seconds`, `account_source_age_seconds` | Same for the complete sequenced owner cut. Paper has no owner stream. |
| `gaps_total` | First detected sequence/session/epoch/cursor continuity failure per failed cache. |
| `stale_pauses_total` | Markets paused for a stale book (or a stale exposed peer) while connected; `book_stale`/`book_fresh` run-log events carry the market index and pause length. |
| `reconnects_total` | Subsequent stream baselines within the same process/collector. Each automatic reconnect after a socket drop adds one; a new runner session resets it. |

`NaN` means unavailable or not yet observed, **not zero**. PnL is observational,
not accounting/settlement proof. Inventory/headroom are cached and refreshed with
the runner's progress publication (normally every two seconds); consult stream
age before interpreting them. A failed or stopped process is an absent endpoint,
not a healthy zero balance. Metrics never alter raw risk or stream admission.

## A bounded first paper run

```sh
glyde-mm paper examples/paper-btc.yaml --duration 10m --report btc-paper-result.json
glyde-mm paper examples/paper-eth.yaml --duration 10m --report eth-paper-result.json
```

Duration accepts seconds (`20s` or `20`), minutes (`10m`) or hours (`1h`), up to
24 hours for paper. The deadline requests orderly stop; it does not skip cleanup.
Ctrl-C also stops cleanly and labels the result as interrupted. Both examples
use public book/trade data only: no read credential or signing wallet is needed,
and all orders/collateral/fills are simulated. Keep one session active per shared
config/state identity; run these examples sequentially or use separate owned
config directories. There is no live trading or automatic funding.

The report path must be new, with an existing parent directory. It is created
privately (0600) before startup and never overwrites an existing file or symlink.
After cleanup the JSON records actual uptime, accepted/cancelled quote receipts,
refusals by fixed reason, partial-fill event count, inventory changes, realized
and unrealized simulated USDC PnL, and maximum observed peak-to-trough equity
loss. Sampling follows actual book/fill/quote events, not a scrape timer. The
inventory path retains at most 10,000 points and flags truncation; drawdown still
uses every observation. The maximum drawdown is sampled, not an intra-event or
live loss guarantee; the paper model excludes fees/funding and approximates queues.

Read `outcome`, `termination`, `cleanup_confirmed` and `open_quotes_at_end`
first. `stopped` with `duration_elapsed` and confirmed zero quotes means the
requested local session finished normally; it is not a live execution claim.
A source gap stops this runner rather than silently resuming. A socket drop retires
quotes and reconnects. A subscription the server defers (an error frame carrying
`retryAfter`, or reason `PROJECTION_UNAVAILABLE`) is handled the same way, waiting at
least `retryAfter` seconds, and is retried even before the first quote; any other
subscription refusal still ends the run. A stale book retires that market's quotes and resumes on the next
fresh book, stopping only after `risk.max_stale_pause_seconds`. `pause_counts` records
each such episode, not durations.
Transport/setup failures return nonzero and the result remains failed or
unobserved. A signal is distinct from completing the requested duration. Disk
write failure also returns nonzero; do not treat a missing/partial JSON as success.
