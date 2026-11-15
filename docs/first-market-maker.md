# Your first market maker in 10 minutes

Use the first session to learn the paper workflow, stop cleanly and inspect its report.
Live mode is a separate, explicit step once your account, permissions and limits are ready.
This guide uses current master / v0.2.0 features; the older v0.1.0 wheel has no report command.

## Minutes 0–2: install and create a workspace

With Python 3.11+ and [pipx installed](https://pipx.pypa.io/stable/installation/), install current Git:

```sh
pipx install 'git+https://github.com/Glyde-trade/glyde-market-maker.git'
glyde-mm --version
mkdir my-glyde-mm
cd my-glyde-mm
```

For a pinned wheel instead, use the [README release install command](../README.md#start-in-five-minutes).
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
glyde-mm paper glyde-mm.yaml
```

The runner prints its build identity, then a `PAPER` status with inventory, simulated PnL,
fills, open quotes and any risk stop. Public books drive a local ledger; no real orders are sent.
With a wide spread, zero fills can be normal. Avellaneda-based strategies additionally wait for
consecutive moving-price samples before quoting.

Stop with **Ctrl-C**. Inspect `PAPER SUMMARY` and its open-quote count. Cancellation errors or risk
stops need attention; do not assume a quiet terminal means cleanup succeeded. Paper results omit
fees, funding, liquidation and real queue priority, and cannot establish live profitability.

## Minutes 7–8: inspect the support report

```sh
glyde-mm report .glyde-mm/logs
```

This reads local logs only. Check the version/commit, last counters and outcome. `incomplete`
means no retained exit event; `history_truncated: true` means older segments rotated away.
The report intentionally excludes balances, PnL, keys and order bodies; financial figures are in
the paper terminal output. Select one `.glyde-mm/logs/run-…` directory if you only want that session.
Review the JSON before sharing it. Keep `.glyde-mm` between restarts: it also stores risk state.

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
glyde-mm paper live-small.yaml
```

That last command still forces paper mode. Stop it and inspect the summary. When you deliberately
intend to submit real orders with the reviewed config, run:

```sh
glyde-mm run live-small.yaml
```

Watch the account and stop with Ctrl-C. Verify outstanding orders at the venue after any connection
or cancellation failure. The bot refuses unavailable, stale or ambiguous execution; it cannot
promise a successful cancel while disconnected. Never restart or delete risk state to escape an
unknown original order/cancel outcome.

Next: [choose a strategy](strategies.md), [understand risk refusals](risk-limits.md), or
[record and replay public books offline](replay.md).
