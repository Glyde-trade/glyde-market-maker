# Paper config gallery

Five self-contained, commented YAMLs for paper experiments. These names describe quote behavior,
not expected returns or guaranteed risk. No config contains a trading key or account identity.

| Config | Behavior | Refresh | What to check |
| --- | --- | --- | --- |
| [conservative.yaml](conservative.yaml) | Smaller PMM quotes, 0.5% per side | 60 seconds | Size must still satisfy the venue minimum |
| [default.yaml](default.yaml) | PMM, 0.2% per side | 30 seconds | Current notional must fit the cap |
| [wide.yaml](wide.yaml) | PMM, 1% per side | 90 seconds | Wide quotes may not fill |
| [inventory-skew.yaml](inventory-skew.yaml) | Adaptive prices and suppressed inventory-increasing size | 30 seconds | Needs consecutive volatility samples with movement |
| [grid.yaml](grid.yaml) | Five levels across a fixed band | 30 seconds | Update the example band to the current market |

All use simulated starting funds of 1,000 USDC, a 0.001 BTC position cap, 25 USDC per-order cap,
5 USDC daily-equity-loss threshold and explicit freshness bounds. Those values are examples, not
venue minimums or a maximum-loss guarantee. Existing and pending orders consume the position cap;
a price change can make a previously valid order too large. Spreads are fractions, sizes are BTC.

From a repository checkout, copy a config into your workspace so edits remain local:

```sh
cp configs/gallery/conservative.yaml my-paper.yaml
glyde-mm doctor my-paper.yaml
glyde-mm paper my-paper.yaml
```

For a wheel/pipx install, download one linked YAML from this gallery and save it as `my-paper.yaml`;
the gallery directory is not an installed CLI workspace. Review its comments and limits first.
`paper` forces simulation; these files deliberately do not contain the addresses or credentials
needed for live mode. Stop with Ctrl-C, inspect the paper summary, then use
`glyde-mm report .glyde-mm/logs` to review the retained run metadata.

See the [tutorial](../../docs/first-market-maker.md), [strategy chooser](../../docs/strategies.md#strategy-chooser)
and [risk-limit FAQ](../../docs/risk-limits.md). Public-book paper fills omit fees, funding,
liquidation and real queue priority; they are not live qualification.
