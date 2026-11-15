# Local-only taker flow

`taker_flow.py` sends small signed IOC orders to an **owned local One API test
stack** to exercise its real public trade tape and paper fills. It never accepts
a non-loopback API origin, including the production Glyde API. This is development
traffic, not a retail strategy or a performance test.

Install the package in your checkout. Prepare a dedicated, disposable local test
account with a real registered trading key and namespace, funded by that local
fixture. In a private YAML file, set `mode: live`, its loopback `base_url`, `symbol`,
registered `owner` and `vault_address`, and `private_key_env: LOCAL_TAKER_KEY`.
Supply that disposable key through the environment, never through a command-line
argument or a committed YAML file. Do not use production credentials.

```sh
python examples/dev/taker_flow.py --config local-taker.yaml --confirm-local-test \
  --orders 200 --interval 3 --quantity 0.0001 \
  --max-order-notional 25 --max-total-notional 2500 --seed 7
```

Run `glyde-mm paper <paper-config>` against the same local public API in another
terminal. Its account is simulated; the helper's orders execute on the local
fixture. Use a separate fixture account for resting maker liquidity to avoid
self-trade prevention. The fixture must supply enough actual resting liquidity
on both sides for the full run. This helper does not create liquidity, credit
balances, register keys, enable engine flags, bypass guards or invent trades.

Both backend One API exchange routing and the engine's incoming signed-exchange
capability must be enabled by the fixture owner. A public-read-only legacy stack
is insufficient. There is no fallback to another API or a simulated receipt.

Defaults are 20 orders, three seconds apart and quantity 0.0001. A seeded shuffle
chooses one buy and one sell in each pair, with 1–3 bps of marketability around the
current best quote, quantized to the market's price/size precision. IOC orders
never intentionally rest. Partial terminal fills are possible; consult actual
public trades/account receipts for executed quantities, not the requested size.
Prices/quantities use Decimal. Hard caps are 1000 orders, at least 0.25 seconds
between orders, 25 USDC per order and 5000 USDC total attempted notional. Requests
consume the budget before dispatch; no failure refunds it.

The helper prints the original client order ID before sending. A refusal,
transport failure, ambiguous receipt or budget exhaustion stops immediately.
**It never retries, re-signs an original, cancels other actors' orders or flattens
positions.** Inspect that exact original before any manual continuation. The
summary counts terminal fill receipts, not proof that every requested unit filled.

`tests/test_taker_flow.py` uses the real SDK signing/client and socket-free fake
matching server. It verifies signed IOC execution, distinct original IDs, local
origin fences, budgets and no retry on unknown/refused outcomes. This proves the
helper protocol in isolation; a particular local or production deployment still
needs separate qualification. No production requests are needed or authorized by
this example.
