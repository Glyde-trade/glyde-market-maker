# Hummingbot Hyperliquid connector compatibility

**A stock `hyperliquid_perpetual` connector is not a drop-in Glyde connector with
only REST/WS URL overrides.** Its isolated public-book and signed order/cancel
methods work with the fake One API wire shapes. Its private authentication,
owner/namespace selection and user-stream subscription need an adapter.

Reference: Hummingbot revision
[`9af100d6822da7d2d0291a906c730ef172284ee2`](https://github.com/hummingbot/hummingbot/tree/9af100d6822da7d2d0291a906c730ef172284ee2/hummingbot/connector/derivative/hyperliquid_perpetual),
Apache-2.0. This is separate from Glyde MM's own SDK-backed client and strategy
adaptations; Hummingbot is not an installed dependency of this package.

## What ran locally

The offline probe loads selected **unchanged upstream method bodies** with Python
AST extraction, the real upstream authenticator and real Ethereum signing. It
replaces framework services with small test objects and sends calls into
`FakeOneApiServer`. It checks signatures independently using the Hyperliquid SDK.
No sockets, external API calls or real credentials are used. Temporary fixture
keys are generated in memory and never printed or retained.

This is **not a full Hummingbot process or startup qualification**. The test host
had Python 3.14, no installed Hummingbot framework/compiled extensions and no Docker.
The base connector, HTTP scheduler, order tracker and funding tasks were not run.
Read-authentication failures below are explicitly modeled contract boundaries,
not measured HTTP responses from a deployed backend. A fake success does not prove
registration, permissions, fees, funding or matching-engine behavior.

| Call | Local result | Exact scope or incompatibility |
| --- | --- | --- |
| Exchange info | PASS, shape only | Stock `metaAndAssetCtxs` request and rule parser accepted synthetic contexts added by the probe; the base fake only implements `meta`. This does not qualify a backend context provider. |
| Order book | PASS | Stock `l2Book` request returned the fake's actual bid/ask depth. |
| Place | PASS, isolated | Stock post-only order construction and upstream authentication produced an SDK-verifiable mainnet signature with the fixture vault. Fake returned a resting order. Startup/bootstrap was bypassed. |
| Cancel | PASS, isolated | Stock cancellation became signed `cancelByCloid`; the fake confirmed cancellation and no owner orders remained. |
| Balances | INCOMPATIBLE | Stock `clearinghouseState` call supplies no Glyde read credential; the modeled `X-Glyde-API-Key` boundary refused it. |
| Positions | INCOMPATIBLE | Same read-credential failure. With a modeled credential added, the vault address still fails the distinct owner binding. |
| User stream | INCOMPATIBLE | Stock WS auth is pass-through and provides no Glyde credential. After modeled authentication, actual fake WS accepted `orderUpdates` but rejected the stock `user` subscription. Glyde uses `userFills`/`userEvents`. |

Working controls: separately model credential permission **and** change the read
owner while retaining the signed vault, and stock balance/position parsers accept
the fake account and a real fake fill's position. An explicitly adapted `userFills`
subscription receives that fill. These are adaptations beyond a URL override;
they are not stock-connector successes.

Another reproduced upstream issue: `_format_trading_rules` derives tick precision
with `markPx.split('.')[1]`. Synthetic `markPx: "100.0"` works; the equally valid
decimal string `"100"` makes it log a parsing error and return no rules. This is a
connector parser limitation, not a reason to alter prices or invent precision.

## Reproduce the offline probe

From a development checkout of this repo, using a virtual environment:

```sh
python -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
git clone --filter=blob:none https://github.com/hummingbot/hummingbot.git /tmp/hummingbot-reference
git -C /tmp/hummingbot-reference checkout 9af100d6822da7d2d0291a906c730ef172284ee2
.venv/bin/python scripts/probe-hummingbot.py \
  /tmp/hummingbot-reference/hummingbot/connector/derivative/hyperliquid_perpetual
```

The script never downloads or installs Hummingbot. It reads the supplied checkout,
asserts the expected method set, blocks socket connections and prints only the
fixed per-call findings. A failed assertion means the probe did not complete;
do not interpret a partial run as PASS. Use trusted upstream source: method bodies
are executable Python. Do not point this probe at a production endpoint.

## Exact URL/configuration boundary

This upstream revision exposes `domain`, not arbitrary `base_url` / `ws_url`
constructor options or YAML fields. A custom domain selects the testnet constants;
it does not interpret the domain string as a URL. In a separately installed
Hummingbot development runtime, the necessary **local-only** override is:

```python
from hummingbot.connector.derivative.hyperliquid_perpetual import (
    hyperliquid_perpetual_constants as constants,
)

# Set before constructing the connector; point only to your disposable stack.
constants.PERPETUAL_BASE_URL = "http://127.0.0.1:8080/v1"
constants.PERPETUAL_WS_URL = "ws://127.0.0.1:8080/v1/ws"
```

Keep the default `hyperliquid_perpetual` domain and use the stack's actual local
ports/path. The authenticator also uses `PERPETUAL_BASE_URL` to choose the signature
network; overriding only a URL helper can accidentally sign the testnet source.
Our probe changes the constants and verifies mainnet-source recovery explicitly.

For base perpetuals only, constructor selection is `enable_hip3_markets=False`.
Vault-shaped signed requests use `use_vault=True`; keys must be disposable fixture
keys registered to that local stack, never real trading credentials. These settings
are **necessary, not a working full-bot recipe**. Do not enable trading merely
because a public book appears.

The remaining adapter must:

1. Supply a separately provisioned read credential for private REST/WS and honor
   its owner, expiry and revocation. Signing a trading action does not authorize
   private reads. Never remove Glyde's authentication checks to make this work.
2. Represent the owner and signed vault namespace separately. Stock `use_vault`
   uses one address both as `vaultAddress` and as the private `user`; a Glyde account
   with different owner/namespace addresses cannot express both that way.
3. Replace the stock `user` subscription with supported fill/event channels and
   adapt their event handling, including snapshots and deduplication. An ACK for
   `orderUpdates` alone is not a working fill stream.
4. Parse exact decimal prices without assuming a decimal point. Obtain price and
   size rules from qualified metadata instead of treating arbitrary price text
   formatting as authoritative tick precision.

HIP-3 discovery, builder-fee approval, order-status/history, leverage and funding
stream behavior remain unqualified here. Stock startup can invoke more than the
seven calls above; the isolated order successes do not waive those dependencies.
Until an adapter and full local process test exist, use Glyde MM's own paper
runner rather than instructions claiming stock Hummingbot is ready for live Glyde.
