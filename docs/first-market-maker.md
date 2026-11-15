# Your first market maker in 10 minutes

Use the first session to learn the paper workflow, stop cleanly and inspect its report.
Live mode is a separate, explicit step once your account, permissions and limits are ready.
This guide uses **current master**; released wheels may lack the new flags. The Git install
below includes them. Check `glyde-mm paper --help` after upgrading.

## Minutes 0–2: install and create a workspace

With Python 3.11+ and [pipx installed](https://pipx.pypa.io/stable/installation/), install current Git:

```sh
pipx install 'git+https://github.com/Glyde-trade/glyde-market-maker.git'
glyde-mm --version
mkdir my-glyde-mm
cd my-glyde-mm
```

For a pinned wheel instead, use the [README release install command](../README.md#start-in-five-minutes)
and consult that release's supported flags.
Already installed an older version? Use `pipx upgrade glyde-market-maker` for its configured source,
or `pipx uninstall glyde-market-maker` before choosing a different install source.

## Minutes 2–4: start with a paper preset

```sh
glyde-mm init
glyde-mm doctor glyde-mm.yaml
```

Choose `pure_market_making` and `conservative` for the simplest first session. Enter the market's
current price as the sizing reference; it is not fetched or updated by the wizard. Review the
editable YAML before running it. The wizard always writes `mode: paper`, never asks for a key
value and refuses to overwrite an existing config.

Conservative uses wider quotes and a slower refresh than Balanced or Active. Preset names describe
quote behavior, not a promise of lower financial risk. All presets retain your position, per-order
notional and daily-loss caps. Amounts are base units, so price changes can make an earlier size
exceed its cap or fall below the venue minimum.

This ordinary doctor command checks public market data. Missing keys are fine in paper mode; a failed public check
must be resolved before continuing. A passing check does not verify private permissions or fills.

## Minutes 4–7: run paper, then stop

```sh
glyde-mm paper glyde-mm.yaml --duration 3m --report paper-result.json
```

The runner prints its build identity, then a `PAPER` status with inventory, simulated PnL,
fills, open quotes and any risk stop. Public books drive a local ledger; no real orders are sent.
With a wide spread, zero fills can be normal. Avellaneda-based strategies additionally wait for
consecutive moving-price samples before quoting.

The timer stops quoting after three minutes and begins cancellation; **Ctrl-C** stops earlier.
Duration accepts seconds, `s`, `m` or `h`, up to 24 hours. Inspect `PAPER SUMMARY`,
`open_quotes_at_end` and `cleanup_confirmed` in the new private `paper-result.json`.
It includes simulated PnL, drawdown, fills, inventory and termination reason. Choose a fresh
filename each run; existing reports are never overwritten. A risk stop or failed cleanup needs
attention. Paper results omit fees, funding, liquidation and actual queue priority.

## Minutes 7–8: inspect the support report

```sh
glyde-mm report .glyde-mm/logs
```

This separate **support** report reads local logs only, not `paper-result.json`. Check the
version/commit, last counters and outcome. `incomplete` means no retained exit event; `history_truncated: true` means older segments rotated away.
The support report excludes balances, PnL, keys and order bodies; financial figures stay in
the terminal and private paper result. Select one `.glyde-mm/logs/run-…` directory if you only want that session.
Review the JSON before sharing it. Keep `.glyde-mm` between restarts: it also stores risk state.

## Optional: BTC and ETH in one process

Copy [the multi-market paper example](../examples/multi-symbol-paper.yaml) as
`multi-paper.yaml`, reviewing amounts and prices before running:

```sh
glyde-mm paper multi-paper.yaml --duration 3m --report multi-result.json
```

**Live mode** uses one Glyde WS session for all selected books and private account/orders/fills.
Paper subscribes to public books/trades, with the same risk boundary and a simulated account.
Each market can choose its own `strategy`, settings and base-unit `max_position`; shared caps
bound per-order notional, gross `max_total_notional`, daily equity loss and order rate.

A stale **flat** market cancels its quotes while fresh peers can keep quoting. A fresh book
restarts that market's strategy, including volatility warm-up. A stale nonflat position can
block other quotes that need its valuation. A private stream gap/disconnect or ambiguous
cancel stops the shared session. Confirmed original IDs are not cancelled twice; restart
needs fresh complete snapshots and preserved risk state. Offline tests prove responsive
simulated cancellation within two seconds, not a production SLA or hard-kill cleanup.

## Minutes 8–10: prepare a small live configuration

Only continue when Glyde serves your requested capabilities and you have a registered trading
account, its exact owner and vault/namespace addresses, a trading API key and a read credential.
This runner requires both addresses; if either is unavailable, stop rather than guessing one.
Use a dedicated account: cleanup can cancel other orders in the same account.

```sh
cp glyde-mm.yaml live-small.yaml
```

In `live-small.yaml`, explicitly change `mode` to `live`, add your exact `owner` and `vault_address`,
and review every size and cap. Start with one quote level. Check current notional, tick/size rules
and the venue minimum; a size that cannot fit your chosen caps must be corrected, not bypassed.
The keys stay out of YAML. In **Bash**, read their values without echoing them or putting them in
shell history (use the names from your config if you changed them):

```bash
read -r -s -p 'Trading API key: ' GLYDE_API_PRIVATE_KEY
export GLYDE_API_PRIVATE_KEY
printf '\n'
read -r -s -p 'Read credential: ' GLYDE_READ_API_KEY
export GLYDE_READ_API_KEY
printf '\n'
glyde-mm doctor live-small.yaml
glyde-mm doctor live-small.yaml --private
glyde-mm paper live-small.yaml
```

The explicit `--private` check reads account and open orders; it does not sign or test order
permissions. That last command still forces paper mode. Stop it and inspect the summary. When you deliberately
intend to submit real orders with the reviewed config, run:

```sh
glyde-mm preflight live-small.yaml
glyde-mm run live-small.yaml
```

Preflight PASS expires after five minutes and is tied to the exact config. Current master
requires an advertised minimum size; missing venue limits mean FIX, not a guessed value.
Native `glydeOrderRules` support is **pending** [One API PR1206](https://github.com/Glyde-trade/glyde/pull/1206)
and held [MM PR81](https://github.com/Glyde-trade/glyde-market-maker/pull/81). It will use
native lot/price constraints and USD minimums on both rounded quote sides, invalidate old
receipts and refuse absent/invalid rules. Neither paper nor doctor bypasses that prerequisite.
Do not assume source support means your endpoint has enabled it.

Read the printed pre-flight limits and type the exact symbol to confirm live startup. Existing
orders or a position on that symbol refuse startup; `--i-understand` explicitly accepts that
exposure after review, while confirmation, risk caps and unknown-outcome stops still apply.
A refused pre-flight does not cancel those orders. The checks are a snapshot, not account locking.

Watch the account and stop with Ctrl-C. Verify outstanding orders at the venue after any connection
or cancellation failure. The bot refuses unavailable, stale or ambiguous execution; it cannot
promise a successful cancel while disconnected. Never restart or delete risk state to escape an
unknown original order/cancel outcome.

Next: [choose a strategy](strategies.md), [understand risk refusals](risk-limits.md), or
[record and replay public books offline](replay.md).
