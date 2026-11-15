# Contributing

Start with a small, reproducible paper experiment. No funded account or real signing/read
credential should be needed to develop or test a contribution.

## Questions and ideas

When [GitHub Discussions](https://github.com/Glyde-trade/glyde-market-maker/discussions) is enabled,
use it for setup questions, config comparisons and early strategy ideas. Until then, open a blank
issue for a question. Use the **Bug report** or **Strategy request** issue forms for actionable work.
Before proposing a new strategy, check the [chooser](docs/strategies.md#strategy-chooser),
[config gallery](configs/gallery/README.md) and [risk FAQ](docs/risk-limits.md).

A useful discussion includes the version, paper scenario and expected behavior. A backtest or paper
result is not proof of profitability or live execution. Keep private account details out of posts.

## Set up locally

Use Python 3.11+ in a virtual environment:

```sh
git clone https://github.com/Glyde-trade/glyde-market-maker.git
cd glyde-market-maker
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
```

On Debian/Ubuntu, install the matching `python3-venv` package if `ensurepip` is unavailable.
See the [first-run tutorial](docs/first-market-maker.md) before changing a live configuration.

## Make one focused change

Keep one behavior or documentation gap per PR. Explain the trigger, resulting behavior and scoped
validation. Show a failing regression before a fix when practical; avoid weakening assertions.
For strategy changes, use the shared `RiskManagedClient` and lifecycle rather than creating another
signer, order coordinator or risk bypass. Cover outstanding exposure, stale inputs, cancellation
and ambiguous original outcomes where your change touches them.

Use exact Decimal amounts, round to the market step, and test without network access using
[`FakeOneApiServer`](docs/offline-testing.md). Keep logs and reports on their existing allowlists.
Never commit keys, tokens, account data, run logs, generated packages or test output.

Run the relevant tests while iterating, then check formatting. For example:

```sh
pytest -q tests/test_fake_one_api.py tests/test_runner.py
ruff check .
ruff format --check .
```

CI runs the complete test suite and an isolated built-wheel install. Changes to packaging should
also use the [package checks](README.md#package-releases).

## Strategy and config submissions

Gallery configs must remain paper-only, contain comments explaining their assumptions and keep
explicit finite position, per-order notional, daily-loss and freshness limits. Do not include keys
or account identifiers. Example prices and sizes are illustrative: venue minimums and current
prices can still refuse them. Document any warmup or fixed price band.

For adapted algorithms, include a public source revision, compatible license and the required
NOTICE attribution. Explain differences from the original; do not imply upstream endorsement.

## Report a problem safely

A bug report should include `glyde-mm --version`, minimal steps, expected/actual behavior and the
smallest relevant non-secret settings. `glyde-mm report <logdir>` provides a bounded support summary;
review it before sharing. Never paste raw signed requests, authorization headers, credentials or
private account information. Contact repository maintainers privately about a suspected security
vulnerability rather than posting exploit details or secrets in a public issue.

Opening a PR does not authorize a live trading test, deployment, release or credential use.
