# Glyde Market Maker

[![CI](https://github.com/Glyde-trade/glyde-market-maker/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/Glyde-trade/glyde-market-maker/actions/workflows/ci.yml)

Market-maker bots for Glyde's live BTC book, and the team's place to test Glyde and report what
breaks. Python 3.11+, one YAML file per bot. Strategy designs are adapted from
[Hummingbot](https://github.com/hummingbot/hummingbot) (Apache-2.0); see [NOTICE](NOTICE).

## Glyde team: run a bot in three commands

Each person runs **one bot on their own Glyde account**, each with a **different preset**, so the
book gets several kinds of logic at different depths. Full walkthrough: [docs/founders.md](docs/founders.md).

```sh
git clone https://github.com/Glyde-trade/glyde-market-maker.git && cd glyde-market-maker
bash scripts/founder-setup.sh            # pick a preset, paste owner + vault, paste API wallet key (hidden)
~/glyde-mm/<preset>/start.sh             # type BTC (capitals) when asked
```

| Preset | Logic | Where it sits in the book |
|---|---|---|
| [`tight-ladder`](configs/founders/tight-ladder.yaml) | Fixed spreads, 20 small levels, gentle inventory skew | Inner: ~0.03% to ~0.13% from mid, $3 apart |
| [`hl-anchored`](configs/founders/hl-anchored.yaml) | Fixed spreads centred on Hyperliquid's mid, not Glyde's | Near: 0.08% to 0.26% |
| [`adaptive-vol`](configs/founders/adaptive-vol.yaml) | Avellaneda: spread follows measured volatility and inventory | Mid: from 0.15%, widening when the market moves |
| [`inventory-shy`](configs/founders/inventory-shy.yaml) | Avellaneda pricing, shrinks the side that would grow its position | Mid: from 0.3% |
| [`grid-wall`](configs/founders/grid-wall.yaml) | Static grid that doesn't follow the price | Wide: 24 levels across ±2.5% of today's price |
| [`deep-book`](configs/founders/deep-book.yaml) | Fixed spreads, larger size, slow refresh | Outer: 0.8% to 1.7% |

Stop with Ctrl-C in the bot's terminal, or `~/glyde-mm/<preset>/stop.sh` from another one.

## Report a bug

Open an issue on **this public repo**; it is the team's standard bug channel for Glyde testing.

```sh
bash scripts/report-bug.sh ~/glyde-mm/<preset>
```

It collects the version, preset, run outcome, recent events, terminal tail and Glyde's health,
redacts addresses and keys, shows you the issue, checks for an open issue with the same error, and
posts only when you say yes (via the GitHub CLI, or it saves the text for you to paste).

Using Claude Code? This repo ships a [`glyde-bot` skill](.claude/skills/glyde-bot/SKILL.md) that
sets up, checks, diagnoses and reports bots the same way. Open Claude Code in this folder and ask it
to start your bot or find out why it stopped.

## Rules that matter

- **One bot per account.** Bots cancel every `gmm1`-tagged order on their account at startup and
  reconnect, so two bots on one account cancel each other.
- **Never paste the API wallet key into chat, YAML, issues or logs.** Setup keeps it in
  `~/glyde-mm/<preset>/.agent-key` (readable only by you).
- **Orders are post-only and at least $10 each.** Levels too close to the price, or skewed below
  $10, are skipped rather than sent.
- **A preflight PASS lasts 5 minutes.** `start.sh` runs it right before starting; if you miss the
  BTC prompt, just run `start.sh` again.

## More

- [Founder guide](docs/founders.md): accounts, presets, start/stop, reading what the bot says
- [Troubleshooting](docs/troubleshooting.md) and [risk limits](docs/risk-limits.md)
- [Strategy and config reference](docs/strategies.md), [operations](docs/operations.md),
  [full reference](docs/reference.md) (paper mode, multi-market, alerts, releases, internals)
- [One API developer quickstart](docs/one-api-quickstart.md) for writing your own client
- [Contributing](CONTRIBUTING.md) and [changelog](CHANGELOG.md)

## If something goes wrong

From another terminal on the same machine, with the same config and credentials:

```sh
glyde-mm stop ~/glyde-mm/<preset>/live.yaml   # or ~/glyde-mm/<preset>/stop.sh
```

This requests a local runner stop, waits for its cleanup to finish, then cancels and
verifies glyde-mm's own tagged open orders on the configured account **and symbol**. Orders
you placed yourself stay open. A dedicated account is still recommended. Positions are
**not** closed; other symbols are not canceled. A paper config stops only its local paper runner and
does not load keys or contact the exchange.

The config must use the same `.glyde-mm` directory beside the config file as the running
bot. The command also works after a crash: it tries persisted unresolved original IDs
before open-order discovery. It writes a private JSON report under `.glyde-mm/stops/`
and exits nonzero unless cancellation is confirmed. A disconnected venue, unavailable
read credentials, another stop in progress, or a runner that cannot exit within 15 seconds
produces **failed**, never a successful stop claim. This coordinates only updated runners
on this machine using that state directory; it cannot stop another machine or an older bot.

Keep the report and risk state. Do not delete an unknown-outcome record to force a restart;
reconcile the original order IDs with the venue. A new explicit `run` after a successful stop
can start quoting again and still requires its live confirmation and usual risk checks.
For diagnostic logs, use `glyde-mm report path/to/.glyde-mm/logs`.


## Docker

Build locally from a reviewed checkout for now: v0.6.0 images were published to
GHCR, but anonymous pulls are unavailable until the organization owner makes the
package public. Credentials are supplied at runtime, never during the build:

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
The GHCR workflow builds amd64/arm64 images on default-branch `master` pushes and is
called by the matching-version tag release. Tags are `master` or `v<version>`, plus
`sha-<full SHA>`. Publication requires the real 20-second paper CLI/container smoke with a
loopback fake One API, non-root UID and public-only requests. PR checks run on GitHub-hosted
Ubuntu runners: offline tests (Python 3.11–3.13), strict mypy, Ruff check/format, packaging and
container smoke. Publishing uses only the job-scoped `GITHUB_TOKEN` for registry login;
credentials are never build arguments. Package visibility remains owner-managed. These checks
do not qualify live exchange credentials.

## Development

Use the [complete strategy/config reference](docs/strategies.md) and
[operations guide](docs/operations.md) for paper examples, public doctor,
state preservation, restart, stop and log interpretation.

```sh
python -m pip install -e '.[dev]'
pytest
ruff check .
```

Tests use fake One API clients/servers and require no network or credentials.

### New action shapes need the Glyde order-shape contract

A bot change that sends an action shape Glyde has not seen before must first pass the
Glyde Prime order-shape contract. A new shape is a new action `type`, a new grouping, a
new time-in-force, a trigger or reduce-only combination, or a new way of batching modifies
or cancels. Add the shape to `SHAPES` in
[`services/prime/crates/prime-engine/tests/order_shape_contract.rs`](https://github.com/Glyde-trade/glyde/blob/master/services/prime/crates/prime-engine/tests/order_shape_contract.rs)
in the Glyde repository, then run this from the Glyde checkout:

```sh
cd services/prime && cargo test --locked -p prime-engine --test order_shape_contract
```

The contract signs the shape through Glyde's engine, Hyperliquid adapter and signer.
It fails if the shape would stop the adapter or halt trading for everyone. On 28 Nov 2026
a batchModify that only the signer refused did exactly that.

