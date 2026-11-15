# Offline One API harness

`glyde_mm.testing.FakeOneApiServer` runs inside the test process. It does not
start an HTTP server or open a WebSocket connection. Its HTTP adapter supports
both `/info` and `/v1/info`, and the corresponding `/exchange` paths.

```python
import httpx

from glyde_mm.client import OneApiClient
from glyde_mm.config import Config
from glyde_mm.testing import FakeOneApiServer

fake = FakeOneApiServer()
fake.set_book("BTC", bids=[("99", "2")], asks=[("101", "2")])

async def read_book():
    config = Config(base_url="https://offline.invalid/v1")
    async with httpx.AsyncClient(transport=fake.http_transport()) as http:
        async with OneApiClient(
            config, http_client=http, connect_ws=fake.connect_ws,
        ) as client:
            book = await client.book("BTC")
            assert str(book.mid) == "100"
```

The foundation client owns signing and parsing; inject both transports so tests
cannot fall back to the network. A transport's optional `user=` binds its exchange
actions to that test account. The server's `user` is the default account, with
10,000 simulated USDC. Separate server instances share no state.

## Fixture controls

- `set_book(coin, bids=[(price, size)], asks=[...])` replaces external liquidity.
  Use decimal strings. User orders remain; crossing new liquidity fills them.
  Locked/crossed or invalid seed books are refused before the old book changes.
- `market_order(coin, buy=True, size="0.1", user=...)` sends an IOC against actual
  resting fixture liquidity. Its default counterparty differs from the main user.
- `advance_time(milliseconds)` advances only the fixture timestamps. Nothing
  polls or sleeps; tests control when market updates occur.
- `await disconnect()` closes every fake socket and wakes pending receives.
  Reconnecting and subscribing produces a fresh baseline.
- `info(body)` and `exchange(envelope, user=...)` expose the same in-process JSON
  operations for focused strategy fixtures. `requests` records HTTP bodies;
  use only ephemeral test credentials and never log real signed requests.

HTTP reads include `meta`, `l2Book`, `allMids`, `openOrders`, `userFills` and
`clearinghouseState`. Exchange actions include limit orders, cancellation by ID
or cloid, and modify/batch modify. Matching uses price/time priority, exact
Decimal quantities, GTC remainders, IOC cancellation, post-only refusal and
reduce-only bounds. Self-crossing cancels the resting own order.

Fake sockets implement async context management, `send`, `recv`, `close` and
async iteration, matching the foundation client's injected connector. Subscriptions
use normal HL JSON acknowledgements and `l2Book`, `trades`, `orderUpdates`,
`userFills`, `allMids`, `openOrders` and `clearinghouseState` channels. Fill
baselines have `isSnapshot: true`; subsequent fills have `isSnapshot: false`.

## Scope

This fixture does **not** qualify authentication, signature/nonce validation,
real margin/liquidation, funding, fees, venue precision filters or live trading.
It models one-way positions and realized/unrealized PnL with zero fees and 1x
notional margin only. Test balances are simulated; there are no money endpoints,
wallets or external services. Unsupported routes and order types fail explicitly.

Wire references: [HL info](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint),
[exchange](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint),
and [subscriptions](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions).
No upstream strategy or SDK code is copied into this implementation.
