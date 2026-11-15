# Glyde Market Maker

[![CI](https://github.com/Glyde-trade/glyde-market-maker/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/Glyde-trade/glyde-market-maker/actions/workflows/ci.yml)

A paper-first market maker for Glyde's One API. Python 3.11+, one YAML file and one command.
Strategy designs are adapted from [Hummingbot](https://github.com/hummingbot/hummingbot)
(Apache-2.0); see [NOTICE](NOTICE).

## Current status

`glyde-mm paper <config>` runs the selected strategy through the risk layer into the paper
exchange: it places simulated quotes, fills them against subsequent public books and cancels
remaining quotes on exit. It never sends a trading action or reads signing credentials.

`glyde-mm run <config>` with `mode: live` sends orders and cancellations through the same risk
boundary to One API, using Hyperliquid-compatible SDK signing and the configured vault namespace.
Offline CLI tests exercise paper fills, signed live placement/cancellation and risk refusals before
submission. They do not qualify a deployed account: live use still requires the API capabilities,
registered account permissions and credentials. Refused or ambiguous actions stop quoting.

Follow [Your first market maker in 10 minutes](docs/first-market-maker.md) for the paper → report →
small-live workflow. See the [strategy chooser](docs/strategies.md#strategy-chooser) and
[risk-limit FAQ](docs/risk-limits.md) before changing a config.

Browse the five commented [paper configs in the gallery](configs/gallery/README.md), or
[contribute a bug report, strategy idea or PR](CONTRIBUTING.md).

> **Quickstart GIF — placeholder:** a captioned paper-mode `init → doctor → paper → Ctrl-C → report`
> recording will go here. No demo recording is attached yet; use the
> [text tutorial](docs/first-market-maker.md).

## Start in five minutes

**1. Install.** Once the `v0.2.0` GitHub Release is published, install its wheel with
[pipx](https://pipx.pypa.io/stable/installation/) (Python 3.11+):

```sh
pipx install 'https://github.com/Glyde-trade/glyde-market-maker/releases/download/v0.2.0/glyde_market_maker-0.2.0-py3-none-any.whl'
glyde-mm --version
```

Until that release exists, or to install from source, use the Git fallback in a virtual environment:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install 'git+https://github.com/Glyde-trade/glyde-market-maker.git'
```

If environment creation reports that `ensurepip` is unavailable on Debian/Ubuntu, install the
matching `python3-venv` package before continuing.

**2. Create a paper config.** The wizard asks for a market, strategy, beginner preset, reference price and limits. It never asks for
or stores a private key, never enables live mode, and refuses to overwrite an existing file.

```sh
glyde-mm init
```

Or copy [examples/paper.yaml](examples/paper.yaml) to `glyde-mm.yaml`.
Amounts are base units; spread fractions use `0.002` for **0.2%**. The default is one bid and one ask,
refreshed every 60 seconds with the Conservative preset. Hanging orders are off.

**3. Run paper first.** Quotes use the public live book; resting fills use the public
trade tape with approximate queue position and partial fills. No trading key or
funded account is needed; paper fills are not a prediction of live execution.

```sh
glyde-mm paper glyde-mm.yaml
```

Stop with Ctrl-C. Review simulated fills and inventory before considering live mode. Paper and live
orders go through the same risk boundary; `paper` forces simulation even if the YAML says `live`.

## Check your setup

```sh
glyde-mm doctor glyde-mm.yaml
```

Doctor validates YAML and strategy settings and checks public metadata plus a fresh
book. It reports the quantity step and the runner's price tick at the current
midpoint. The public metadata does not expose minimum order size/notional, so
those limits are reported as unavailable rather than inferred from precision.
Missing keys are optional in paper mode and fail the check in live mode.

Ordinary doctor runs never send credentials. To check your configured live
account's read permissions explicitly:

```sh
glyde-mm doctor live.yaml --private
```

This performs only authenticated `clearinghouseState` and `openOrders` reads;
it prints neither balances nor addresses nor key values. Paper configurations
refuse `--private` before connecting. Doctor never opens a WebSocket, constructs
a signing wallet or submits an order. Even a successful private check does not
prove trading-key authorization or that a live order would be accepted.
Configuration, connectivity, stale/missing book and requested read-permission
failures return a nonzero exit code.

## Go live with small, explicit limits

Create/authorize an API wallet for your Glyde trading account using Glyde's account controls. Use its
**trading key**, not a main wallet seed phrase. Use the exact account and any assigned vault/namespace
from your account's discovery data. A trading key is not a read credential; provide the separate read
credential when the API requires one. This bot does not create credentials or grant permissions.

Copy [examples/live-small.yaml](examples/live-small.yaml), replace the public account placeholder,
and set any assigned vault address. Keep private keys out of YAML and shell history. For example,
Bash can read the trading key without echoing it:

```sh
read -r -s -p 'Trading API key: ' GLYDE_API_PRIVATE_KEY
export GLYDE_API_PRIVATE_KEY
printf '\n'
# Set GLYDE_READ_API_KEY separately when your account requires private read authorization.
glyde-mm run live-small.yaml
```

`run` uses the YAML mode; live must be selected explicitly. Confirm the current market price,
exchange minimum order size and precision. `order_amount × price` must fit `max_order_notional`;
an example can be too small for the exchange or too large for its configured cap. Refused quotes
must be corrected in the config, not retried with larger limits automatically.

The defaults cap one order at **25 USDC**, absolute position at **0.001 BTC** and daily account equity
loss at **5 USDC**. These are local guardrails, not a loss guarantee. Daily equity changes include
transfers as well as trading PnL. Use a dedicated trading account: cancel-all on disconnect or exit
can cancel other orders in the same account. Review remaining orders in your account if cancellation
fails; loss of connectivity cannot guarantee a successful cancel.

Unknown order/cancel outcomes stop new orders and retain the original identity. The bot does not
resend an ambiguous order or replace it with a new nonce/ID. Stale books, unavailable account reads
and daily-loss breaches stop quoting. Preserve the runtime risk-state directory across restarts so
the daily-loss baseline is retained.

## Fixed-range grid

Use [examples/grid-paper.yaml](examples/grid-paper.yaml) to try evenly spaced levels inside a fixed
price band. Set the band around the current book first. Levels below the midpoint buy; levels above
it sell; no quote sits at the midpoint. Out-of-band markets cancel and wait, and rounding never
moves quotes outside the band. Grid uses the same risk limits and cancel-before-replace lifecycle.

```sh
glyde-mm paper examples/grid-paper.yaml
```

For a beginner preset, run `glyde-mm init --output grid.yaml`, choose **grid**,
then Conservative, Balanced or Active. Enter the current reference price: the
wizard fixes a band at 90–110% of that value and budgets sizes within your caps.
The band stays fixed until you edit the YAML; it does not follow a moving market.

| Preset | Fixed levels | Refresh | Trade-off |
| --- | --- | --- | --- |
| Conservative | 3 | 60 seconds | Fewer, wider-spaced quotes and lower turnover |
| Balanced | 5 | 30 seconds | More coverage inside the same band |
| Active | 7 | 10 seconds | Denser quotes and more cancellation/refresh activity |

`levels` counts the whole band, not levels per side. Midpoint, too-close and
coarse-tick duplicate levels are skipped. All levels share the position cap;
adding levels does not grant extra exposure. Try `glyde-mm doctor grid.yaml`,
then `glyde-mm paper grid.yaml`. No preset enables live trading.

The linear-level design references
[Hummingbot's grid executor](https://github.com/hummingbot/hummingbot/blob/9af100d6822da7d2d0291a906c730ef172284ee2/hummingbot/strategy_v2/executors/grid_executor/grid_executor.py)
(Apache-2.0; see NOTICE). This is a simple two-sided quoting grid using Glyde's
shared lifecycle, without a separate paired take-profit executor or profit guarantee.

## Inventory-skewed quoting

Try [examples/inventory-skewed-paper.yaml](examples/inventory-skewed-paper.yaml)
for Avellaneda-style prices plus smaller quotes on the side that increases
inventory away from your target. Long above target: smaller bids; short below
target: smaller asks. The side moving toward target keeps its configured size;
this strategy never enlarges an order to chase the target.

```sh
glyde-mm paper examples/inventory-skewed-paper.yaml
```

`inventory_target_position` is a signed base position (zero means flat),
`risk_factor` shifts reservation prices, and `order_amount_shape_factor` controls
how strongly the away-from-target side shrinks (a Decimal from 0 to 100). Higher factors suppress it
more; zero keeps Avellaneda's original sizes. Sizes round down to the market step;
a quote below the minimum is omitted. A target is a preference, not an order or
a risk-limit override. Existing/open orders still count toward the same caps.

The wizard's Conservative, Balanced and Active presets include this strategy and
always write paper YAML. Warmup needs 31 consecutive one-second samples with
nonzero movement; gaps reset it. Exponential sizing is adapted from
[Hummingbot's Avellaneda strategy](https://github.com/hummingbot/hummingbot/blob/9af100d6822da7d2d0291a906c730ef172284ee2/hummingbot/strategy/avellaneda_market_making/avellaneda_market_making.pyx)
(Apache-2.0; see NOTICE), using signed perpetual exposure rather than spot wallet
balances. Shared risk checks, cancel-before-replace, unknown-outcome stops and
paper limitations apply. It is not a promise of profitable or target-reaching fills.

## Quote around a public Hyperliquid reference

For **pure market making**, choose `hyperliquid` at the wizard's quote-center
prompt, or try [examples/external-reference-paper.yaml](examples/external-reference-paper.yaml):

```sh
glyde-mm paper examples/external-reference-paper.yaml
```

This subscribes to the same coin's public Hyperliquid mainnet `l2Book` at
`wss://api.hyperliquid.xyz/ws`, following its
[public subscription protocol](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions).
No Glyde credential, owner address or signed action is sent to Hyperliquid.
All orders, paper fills, balance reads and position/notional/loss caps stay on
Glyde. This is a price-reference option, not trading or transferring on another venue.

The quote center is `HL mid × (1 + reference_skew_bps / 10000)`. Zero skew centers
on HL; positive skew shifts both quotes up, negative down. Bid/ask spreads and
levels retain their normal fractional units. Orders stay post-only: a candidate
that would cross the actual Glyde best bid/ask is omitted, not converted to a taker.
Grid and Avellaneda variants retain their own pricing and refuse this option.

The guard compares the **raw** mids using `abs(Glyde mid - HL mid) / HL mid × 10000`.
Above `max_reference_deviation_bps`, missing/invalid/regressed HL data, source or
arrival age over `reference_stale_seconds`, or a reference disconnect pulls quotes
and stops the run. It never falls back to the Glyde mid or reconnects silently;
inspect the cause before restarting. Freshness/deviation are checked again after
private account/open-order awaits before reserving a new batch. Cancellation can
fail or arrive after a fill, so this is not a loss or instantaneous-cancel guarantee.

The paper wizard's Conservative/Balanced/Active reference presets use deviation
caps of 25/50/100 bps, zero skew and a three-second reference-age bound. YAML stays
editable and paper-only. Skew is bounded to ±1000 bps, deviation to 0–1000 bps
(excluding zero), and reference age to 0–60 seconds (excluding zero). Use the exact
coin name on both venues; there is no symbol inference or FX conversion. Doctor
checks the Glyde feed, not reference availability. Single-source book replay has
no HL history and stops with `reference_unavailable` rather than inventing it.

## Known limitations

- Streaming paper fills approximate queue position from displayed depth and public
  trades; they omit actual queue priority, hidden liquidity, fees, funding, margin
  and liquidation. Book-only replay uses its existing depth-cross model, without a
  trade tape. See [paper fill assumptions](docs/paper-fills.md). Recorded-book replay
  needs a prior local recording and cannot reconstruct missing historical depth.
- Live mode requires the requested Glyde capabilities and registered owner, namespace,
  trading key and read credential. Doctor's optional private check proves read access
  only; it cannot establish signing authority or order acceptance.
- Public metadata provides size precision, but no minimum order size/notional. The
  displayed runner tick is derived at the current midpoint; venue validation remains final.
- Risk limits are local guardrails, not guaranteed loss bounds. Daily loss tracks equity
  against a UTC-day baseline, including unrealized changes and transfers; it does not
  flatten existing positions. Run one bot process per dedicated account: the local state
  file is not a cross-process coordinator.
- Disconnects, stale data and ambiguous writes stop quoting. Cleanup attempts cancellation,
  but cannot guarantee success while disconnected and can affect other orders in the same
  account. Keep persistent risk state and reconcile original orders after an unknown outcome;
  restarting or deleting state is not reconciliation. Ambiguous order submissions are not
  automatically resubmitted.
- Strategies provide quoting logic, not a profit guarantee. The fixed grid does not manage
  separate paired take-profit exits. The init wizard always writes paper configurations.

## Configuration

| Key | Meaning |
| --- | --- |
| `mode` | `paper` by default; `live` explicitly enables real trading |
| `symbol` | Exact market symbol, such as `BTC` |
| `owner` / `vault_address` | Public registered account and assigned namespace, never inferred |
| `private_key_env` | Name of the signing-key environment variable |
| `read_api_key_env` | Name of a separate private-read credential variable |
| `strategy_config.order_amount` | Quantity per level in base units |
| `strategy_config.bid_spread` / `ask_spread` | Fractions around the reference price |
| `risk.max_position` | Maximum absolute projected position, including outstanding orders |
| `risk.max_order_notional` | Maximum USDC notional per order |
| `risk.max_daily_loss` | Daily equity-loss kill switch in USDC |
| `risk.stale_book_seconds` | Maximum book source/arrival age |
| `risk.account_age_seconds` | Maximum account-observation age |

Keep decimal values quoted in YAML. The only default API origin is `https://api.glyde.trade`.
No signing key is needed to browse a public book in paper mode. Private endpoints may refuse
requests until account permissions and read authorization are configured.

## Docker

Build from this repository; credentials are supplied at runtime, never during the build:

```sh
docker build -t glyde-mm .
docker run --rm -it --init \
  --mount type=bind,source="$PWD/glyde-mm.yaml",target=/work/glyde-mm.yaml,readonly \
  --mount type=volume,source=glyde-mm-state,target=/work/.glyde-mm \
  glyde-mm paper /work/glyde-mm.yaml
```

The container runs as a non-root user. For live mode, mount the reviewed live YAML and pass existing
environment variables by **name** with `--env GLYDE_API_PRIVATE_KEY --env GLYDE_READ_API_KEY`;
use `run /work/live-small.yaml`. Do not copy credentials into the image.

## Development

```sh
python -m pip install -e '.[dev]'
pytest
ruff check .
```

Tests use fake One API clients/servers and require no network or credentials.

## Package releases

The maintainer updates `project.version` in `pyproject.toml`, then creates the matching `v<version>`
tag. The tag workflow builds the source archive and wheel, tests an isolated wheel installation,
and attaches both to a GitHub Release using only the repository's `GITHUB_TOKEN`. Release packages
embed the checked-out commit, which the isolated install check verifies against the build SHA.
Both CI and tag builds run `twine check --strict` on the distributions. LICENSE and NOTICE
are included in the source and wheel archives. A mismatched tag fails before publishing;
PR packaging checks do not create tags, releases or PyPI uploads.

PyPI publishing is **disabled by default**. To enable trusted publishing, a maintainer must:

1. Check the normalized project name `glyde-market-maker` on PyPI. An absent project is
   not a reservation or a guarantee that registration will be accepted.
2. Configure a pending or existing PyPI Trusted Publisher with owner `Glyde-trade`,
   repository `glyde-market-maker`, workflow `release.yml`, environment `pypi`.
3. Configure the repository's GitHub environment `pypi` (prefer tag restrictions and
   required reviewers), then set the repository variable `PYPI_PUBLISH_ENABLED=true`.
4. Push a new matching version tag. The publish job downloads only the distributions
   checked in that same workflow run and authenticates via GitHub OIDC; no PyPI token,
   password or long-lived publishing secret belongs in the repo or workflow.

The existing GitHub Release still uses only `GITHUB_TOKEN`; OIDC permission is limited
to the separate PyPI job. No project has been registered or uploaded by adding this workflow.
See [PyPI trusted publishing](https://docs.pypi.org/trusted-publishers/using-a-publisher/)
and [pending publisher setup](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/).

To run the same packaging check locally:

```sh
python -m pip install 'pip>=23' 'build>=1.2,<2' 'twine>=6,<7'
python scripts/write-build-info.py "$(git rev-parse HEAD)"
python -m build
python -m twine check --strict dist/*
python scripts/check-wheel-install.py dist/*.whl --expected-commit "$(git rev-parse HEAD)"
```

### Run diagnostics

Every paper or live run writes private, rotated JSONL diagnostics under the config
directory's `.glyde-mm/logs/run-<id>/`. Each segment includes the package version
and source commit (or `unknown` when the installation has no commit metadata).
The runner prints that identity at startup. Each run keeps at most four 1 MiB
segments; older run directories remain until you remove them. Diagnostics contain
mode, strategy, event counts and a fixed exit category, never keys, addresses,
balances, order bodies or raw errors. Directory permissions are 0700 and files 0600.

To prepare a support ticket without reading credentials or contacting the exchange:

```bash
glyde-mm report .glyde-mm/logs
# Or choose one run directory to share only that session.
```

The command prints validated JSON with build identity, mode, strategy, last retained
counters and exit category. A missing exit is labeled `incomplete`; it does not
mean cleanup succeeded. Rotated-away history is explicit. Malformed logs are refused
without printing their contents. Reports accept up to 50 runs / 64 MiB; select one
run if the directory exceeds that limit. Review the summary before sharing it.
