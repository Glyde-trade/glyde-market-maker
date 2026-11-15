# Glyde Market Maker

A paper-first market maker for Glyde's One API. Python 3.11+, one YAML file and one command.
Strategy designs are adapted from [Hummingbot](https://github.com/hummingbot/hummingbot)
(Apache-2.0); see [NOTICE](NOTICE).

## Current status

Paper and explicit live commands share the strategy/risk runner. Live connectivity still requires
Glyde to expose the requested One API capabilities and your exact registered account permissions;
unit tests do not establish that a deployed account is enabled or that live trading is qualified.
The bot stops on refused or ambiguous actions rather than falling back to another venue.

## Start in five minutes

**1. Install.** Once the `v0.1.0` GitHub Release is published, install its wheel with
[pipx](https://pipx.pypa.io/stable/installation/) (Python 3.11+):

```sh
pipx install 'https://github.com/Glyde-trade/glyde-market-maker/releases/download/v0.1.0/glyde_market_maker-0.1.0-py3-none-any.whl'
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

**2. Create a paper config.** The wizard asks for a market, order size and limits. It never asks for
or stores a private key, never enables live mode, and refuses to overwrite an existing file.

```sh
glyde-mm init
```

Or copy [examples/paper.yaml](examples/paper.yaml) to `glyde-mm.yaml`.
Amounts are base units; spread fractions use `0.002` for **0.2%**. The default is one bid and one ask,
refreshed every 30 seconds. Hanging orders are off.

**3. Run paper first.** Quotes and fills are simulated against the public live book. No trading key
or funded account is needed; paper fills are not a prediction of live execution.

```sh
glyde-mm paper glyde-mm.yaml
```

Stop with Ctrl-C. Review simulated fills and inventory before considering live mode. Paper and live
orders go through the same risk boundary; `paper` forces simulation even if the YAML says `live`.

## Check your setup

```sh
glyde-mm doctor glyde-mm.yaml
```

Doctor validates the YAML and strategy settings, reports whether the configured key environment
variables are present, and makes one public metadata request to `base_url` (by default
`https://api.glyde.trade/v1/info`). It never prints key values, sends private credentials, opens a
WebSocket or submits an order. Missing keys are optional in paper mode and fail the check in live
mode. Invalid config, an unavailable API or an unavailable market also returns a nonzero exit code.
A passing public check does not verify key validity, account permissions or live execution.

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
embed the checked-out commit, which the isolated install check verifies against the build SHA. It does not
publish to PyPI. A mismatched tag fails before publishing; PR packaging checks do not create tags or releases.

To run the same packaging check locally:

```sh
python -m pip install 'pip>=23' 'build>=1.2,<2'
python -m build
python scripts/check-wheel-install.py dist/*.whl
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
