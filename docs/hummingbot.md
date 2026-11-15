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

An adapter for these stock incompatibilities must:

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
Use Glyde MM's own paper runner until the included adapter has a full local
process qualification; stock Hummingbot is not ready for live Glyde by URL override.

## Included `glyde_perpetual` adapter

The source adapter is now in `connectors/hummingbot/glyde_perpetual/`. It subclasses
that pinned upstream connector; it does not patch `hyperliquid_perpetual` globally
or install Hummingbot as a Glyde MM dependency. Use Glyde MM's paper runner first.
This adapter's offline tests qualify its wire boundary, not an entire Hummingbot
process or permission to send live orders.

In a trusted development checkout of the pinned Hummingbot revision, copy the
adapter directory into the derivative connector directory before starting the
framework:

```sh
cp -R connectors/hummingbot/glyde_perpetual \
  /path/to/hummingbot/hummingbot/connector/derivative/glyde_perpetual
```

The source path above is relative to this repository. Follow the pinned
Hummingbot checkout's own build instructions; compiled framework extensions are
not included here. The adapter discovery name is `glyde_perpetual` and its pair
format is `BTC-USD`. Its connect configuration has **four separate secure fields**:

| Field | Meaning |
| --- | --- |
| `glyde_perpetual_owner` | Registered owner address used by private `/info` and WS subscriptions. |
| `glyde_perpetual_vault_address` | Assigned Glyde signing namespace; never substituted for the owner or inferred from it. |
| `glyde_perpetual_private_key` | Trading wallet key already authorized by the Glyde owner. Never use a custody/main key. |
| `glyde_perpetual_read_credential` | Separately issued owner read credential, sent as `X-Glyde-API-Key` on private REST and every private WS handshake. |

Let Hummingbot's secure connect workflow store credentials; do not put them into
example YAML, source control, logs or public issue reports. A trading signature
is not a read credential. Missing read credentials or owner/vault configuration
refuse construction, and the server still verifies read permissions and every
signed action. The adapter does not issue credentials or approve an agent.

Constructor defaults are `glyde_perpetual_base_url="https://api.glyde.trade/v1"`
and `glyde_perpetual_mainnet=True` (HL L1 source `a`). A disposable local test
runtime can explicitly supply an HTTP loopback `/v1` URL and its actual signature
realm; HTTPS is required for other hosts. The WS URL derives from that same URL.
The realm is explicit and does not change because a URL looks like testnet.
Never reuse a synthetic fixture owner or vault for a registered account.

The adapter provides:

- Canonical signed `order` and `cancelByCloid` wire with Decimal strings, the
  explicit assigned vault and monotonic nonces. No Hyperliquid builder fields.
- Owner-bound private reads with the separate header; public metadata/book
  requests omit it. Endpoint overrides cannot redirect the authenticated request.
- Authenticated `orderUpdates` and `userFills` subscriptions. Fill snapshots and
  live updates share bounded `(oid, tid)` deduplication; conflicting evidence
  suspends trading. Only the internal framework queue uses the stock `user`
  fill-processing name; no `user` subscription is sent on the wire.
- `meta` size precision and stable asset-array indexes, including delisted slots.
  Price quantization uses Decimal and the five-significant-figure / `6-szDecimals`
  rule (integer prices remain exact); integer `markPx` strings are accepted.
- Permanent suspension after private read failure, private stream interruption,
  malformed/conflicting private evidence or an unknown exchange outcome. A
  reconnect sends the read credential again but **does not resume quoting**.
  Reconcile and restart explicitly. The original signed envelope remains in
  memory as `unresolved_exchange_envelope`, retained across cleanup cancels;
  do not print it or generate
  a replacement nonce to resolve an unknown result. Explicit cleanup cancels
  remain possible, and unconfirmed cancellations never become success.

The static Hummingbot budget defaults are a 10 USD minimum notional and a 0.025%
maker/taker fee estimate; these are conservative connector budgeting settings,
**not qualified Glyde venue minima or actual fee schedules**. Verify owner fees,
market rules and permission limits before considering a live session. This
connector inherits Hummingbot's framework risk/order tracker and periodic REST
reconciliation; it is separate from Glyde MM's no-polling strategy runner and its
RiskManagedClient limits.

Funding requires actual `oraclePx`, `markPx` and `funding` context. The adapter
refuses missing context rather than inventing zeros. HIP-3, full startup/funding
readiness, order-status/history coverage, leverage and fee reconciliation remain
unqualified against a real service. Unsupported providers must remain unavailable;
these offline successes do not waive those prerequisites for live trading.

### Adapter offline tests

```sh
python -m pytest -q tests/test_hummingbot_connector.py
```

These tests import the **actual adapter modules** with small framework service
and `bidict` doubles because Hummingbot's compiled runtime is not a dependency.
They pass actual signed requests through httpx's in-process transport into
`FakeOneApiServer`, independently recover signatures using the official HL SDK,
and deliver the matching fixture's real order/fill messages through a fake WS
assistant. Explicitly modeled credential/owner checks surround the auth-free
matching fixture; they do not claim a deployed backend authorized the requests.
Socket connections are forbidden. Positive cases and refusals cover missing or
revoked read credentials, foreign owner/endpoint, wrong signing realm/vault,
integer/decimal metadata, fill duplication/conflict, reconnect authentication and
lost-response suspension without retry. Full framework startup, throttling,
compiled order tracking and real venue permissions still require separate local
qualification.
