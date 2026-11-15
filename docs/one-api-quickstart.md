# One API quickstart

Use Glyde's One API for public market data, owner reads and signed trading.
Start with public reads or a paper bot; authorize trading separately.

This guide targets `hyperliquid-python-sdk==0.24.0`. Public SDK calls below were
observed against an owned local Glyde paper stack on 2026-11-15; see the
[verification notes](#verification-and-compatibility-gaps)
for scope and limitations. Private examples are checked against the
installed SDK and request contracts offline. **They are not a completed native
private SDK acceptance run.** Deployment availability is separate from source
support; a 401/404 on public Info means that requested read is unavailable at
that endpoint, not permission to bypass authentication or switch to HL.

## 1. Install and choose the endpoint

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install 'hyperliquid-python-sdk==0.24.0'
```

| Purpose | Address |
| --- | --- |
| SDK base URL | `https://api.glyde.trade/v1` |
| Public/private Info | `POST https://api.glyde.trade/v1/info` |
| Signed actions | `POST https://api.glyde.trade/v1/exchange` |
| Public/private socket | `wss://api.glyde.trade/v1/ws` |

Use the `/v1` SDK base **without a trailing slash**. The SDK appends `/info`,
`/exchange` and `/ws`. For an explicitly owned test stack, obtain its approved SDK base from the
operator and pass it through `GLYDE_ONE_API_BASE`. No hosted sandbox is promised
by this guide. Custom servers must admit their exact API Origin for stock SDK
sockets; see the [verification notes](#verification-and-compatibility-gaps).

## 2. Read public markets without a key

```python
import os
from hyperliquid.info import Info

base = os.environ.get("GLYDE_ONE_API_BASE", "https://api.glyde.trade/v1")
info = Info(base, skip_ws=True, timeout=5)
try:
    print(info.meta())                 # universe and size precision
    print(info.l2_snapshot("BTC"))     # complete current book
    print(info.all_mids())             # decimal-string midpoints
finally:
    info.session.close()
```

Prices/sizes are decimal strings; do exact calculations with `Decimal`. Read
the asset's size precision and engine limits before choosing an order size.
Empty spot metadata is SDK bootstrap compatibility, not spot trading support.
Requested candles/history can return 503 when complete coverage is unavailable.

Public subscriptions also need no wallet or read credential:

```python
import os
import time
from hyperliquid.info import Info

base = os.environ.get("GLYDE_ONE_API_BASE", "https://api.glyde.trade/v1")
info = Info(base, timeout=5)
try:
    info.subscribe({"type": "l2Book", "coin": "BTC"}, print)
    info.subscribe({"type": "trades", "coin": "BTC"}, print)
    info.subscribe({"type": "allMids"}, print)
    time.sleep(20)
finally:
    info.disconnect_websocket()
    info.session.close()
```

`l2Book` messages are full books. The stock SDK suppresses empty `trades` arrays;
zero callbacks can mean no executions, not a disconnected stream. Its callback
dispatcher also does not expose Glyde batch-end/ACK frames as market callbacks.

## 3. Obtain two separate credentials

In your authenticated Glyde account, obtain the registered One API discovery
and authorize a dedicated **API/agent wallet** through Glyde's owner-signature
flow. Use a separate agent key with only the needed trade scope and expiry;
keep the master wallet in the account's signing flow. If that deployment has no
enabled registration/authorization flow, private trading is unavailable there.
The bot must not register a guessed owner or vault as a substitute.

The agent's private key signs orders. A **read credential** authorizes private
Info/WS data and cannot place an order. Neither one replaces the other.
Discovery supplies canonical `user`, assigned `vaultAddress`, decimal-string
`accountId`, `deployment` and `network`. The vault is a Glyde signing namespace,
not a custody/pool address; never derive it from a deposit address.

An application with an existing authenticated user session can mint a read
credential as follows. Supply its current access/refresh token and matching
device ID through your secret manager/environment; this is not a public
address-only token endpoint. Cookie-based applications also retain their CSRF
requirement. Do not log the request or returned credential.

```python
import os
import requests

base = os.environ.get("GLYDE_ONE_API_BASE", "https://api.glyde.trade/v1")
response = requests.post(
    base + "/one-api/session",
    headers={
        "Authorization": "Bearer " + os.environ["GLYDE_APP_ACCESS_TOKEN"],
        "Client-Device-Id": os.environ["GLYDE_APP_DEVICE_ID"],
    },
    json={"refresh_token": os.environ["GLYDE_APP_REFRESH_TOKEN"]},
    timeout=10,
)
response.raise_for_status()
session_data = response.json()["data"]
owner = session_data["user"]
vault = session_data["vaultAddress"]
read_credential = session_data["readCredential"]
```

Keep the following private-read blocks in that same Python process. Alternatively,
pass these values through a private credential provider; never put them in a URL,
public YAML, transcript or repository. Authenticated `GET /v1/one-api/discovery`
returns the mapping without minting a token. Verify the expected deployment and
network before signing; keep `accountId` as an exact string.

The session token has a consume deadline of at most 60 seconds (`expiresAt`, Unix
milliseconds). Initial HTTP reads do not consume it. A socket consumes it once;
its fixed lease is at most five minutes and bounded by the parent session.
Expiry/revocation closes private delivery. A reconnect needs a fresh mint, not
reuse of this credential. Logout/revocation of the parent also revokes its reads.

## 4. Read the authenticated owner

Use the registered owner address as `user`, not the agent or assigned vault:

```python
from hyperliquid.info import Info

info = Info(base, skip_ws=True, timeout=5)
info.session.headers["X-Glyde-API-Key"] = read_credential
try:
    account = info.user_state(owner)             # clearinghouseState
    orders = info.open_orders(owner)
    frontend = info.frontend_open_orders(owner)
    fills = info.user_fills(owner)
finally:
    info.session.close()
```

`userFills` requires a qualified complete history source. A refusal means
unavailable coverage; do not turn it into an empty fill history. HTTP account
and order calls are independently captured reads, not one atomic trading cut.

## 5. Watch private orders, fills and account state

SDK 0.24 knows `orderUpdates` and `userFills`, but has no stock callback mapping
for the Glyde `clearinghouseState`/`openOrders` subscription extensions. For a
combined owner group, use `websocket-client` (installed with the SDK) to receive
raw Glyde frames. This example displays frame kinds only; it does not implement
a trading-state reducer. Run it promptly after minting an unused token.

```python
import json
import threading
from urllib.parse import urlsplit, urlunsplit
import websocket

url = urlsplit(base)
ws_url = urlunsplit(("wss" if url.scheme == "https" else "ws",
                    url.netloc, url.path + "/ws", "", ""))
errors = []

def on_open(ws):
    for channel in ("clearinghouseState", "openOrders", "orderUpdates", "userFills"):
        ws.send(json.dumps({"method": "subscribe", "subscription": {
            "type": channel, "user": owner,
            "glyde": {"version": 1, "delivery": "sequenced", "group": "owner"},
        }}))

def on_message(ws, raw):
    frame = json.loads(raw)
    if frame.get("channel") in {"error", "resnapshot_required", "glydeSafety"}:
        errors.append("private stream unavailable or unsafe; stop and reconcile")
        ws.close()
        return
    print(frame.get("channel"))  # No credential or financial payload logging.

app = websocket.WebSocketApp(ws_url, header={"X-Glyde-API-Key": read_credential},
    on_open=on_open, on_message=on_message,
    on_error=lambda ws, error: errors.append(type(error).__name__))
timer = threading.Timer(20, app.close)
timer.start()
try:
    app.run_forever()
finally:
    timer.cancel()
    app.close()
if errors:
    raise RuntimeError("; ".join(errors))
```

For production trading consumers:

* Authenticate every new physical socket. The header is read authority, not
  signing authority. The example sends it in the header; never send it in
  the URL or reuse a consumed token on a replacement socket.
* Pin ACK-assigned subscription IDs, group, session, owner and epoch. Adding a
  member replaces group membership: discard superseded partial batches and
  wait for the complete selected baseline, not a collection of independent reads.
* Install declared parts together only at matching `glydeBatchEnd`; data indexes
  are integers, and the end has `index:null`. Require a full initial snapshot
  with `previous:null`; subsequent delivery sequences/predecessors must join.
  Reject mixed cuts, missing/reordered parts, regressions and overflow. Global
  engine cursor jumps are valid; cursors are not consecutive group sequences.
* Preserve native publication revisions separately from delivery sequences.
  Unsafe facts immediately stop new orders. Heartbeats, reconnects and a
  standalone ready flag cannot turn old financial data into a safe new cut.
* On disconnect, expiry, stale data or a gap: stop quoting, retain original
  order identities for cancellation/reconciliation, mint again, and wait for a
  fresh complete snapshot before considering a restart. Never replay a signed
  action just because its response was lost. No HTTP polling fallback repairs
  a broken stream's authority.

## 6. Sign orders with the SDK

Use `Exchange` with the Glyde base, the authorized agent wallet, canonical owner
and **exact assigned vault**. Ordinary L1 actions reuse HL MessagePack/EIP-712
signing; Glyde expects realm/source **`b`**, including Glyde production. The SDK
selects that realm for a custom Glyde base URL; do not force HL-mainnet signing.
Stock L1's domain chain ID remains 1337. Glyde master-signed agent grants use a
different `Glyde` domain, deployment/network and expiry binding (chain 42161 or
421614): stock `approve_agent` is not an authorization shortcut. Use the exact Glyde authorization request presented by the account flow,
including its assigned namespace, owner, agent, permissions, expiry and nonce.

The following defines example calls without automatically sending an order.
Provide a dedicated authorized agent key and reviewed quantity/prices. The
market's size/price precision, minimum notional, collateral and trigger bounds
still apply; a signing-compatible request may be refused by risk checks.

```python
import os
import uuid
from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.utils.types import Cloid

exchange = Exchange(Account.from_key(os.environ["GLYDE_API_PRIVATE_KEY"]),
    base_url=base, account_address=owner, vault_address=vault, timeout=10)

def place_limit(tif, quantity, limit_price):
    cloid = Cloid.from_str("0x" + uuid.uuid4().hex)
    reply = exchange.order("BTC", True, quantity, limit_price,
                           {"limit": {"tif": tif}}, cloid=cloid)
    return cloid, reply

def place_bracket(quantity, entry, tp_trigger, tp_limit, sl_trigger, sl_limit):
    return exchange.bulk_orders([
        {"coin": "BTC", "is_buy": True, "sz": quantity, "limit_px": entry,
         "order_type": {"limit": {"tif": "Gtc"}}, "reduce_only": False},
        {"coin": "BTC", "is_buy": False, "sz": quantity, "limit_px": tp_limit,
         "order_type": {"trigger": {"isMarket": False, "triggerPx": tp_trigger,
                                    "tpsl": "tp"}}, "reduce_only": True},
        {"coin": "BTC", "is_buy": False, "sz": quantity, "limit_px": sl_limit,
         "order_type": {"trigger": {"isMarket": False, "triggerPx": sl_trigger,
                                    "tpsl": "sl"}}, "reduce_only": True},
    ], grouping="normalTpsl")

# Choose ONE reviewed action in an owned test environment, then inspect its receipt:
# cloid, reply = place_limit("Alo", quantity, limit_price)  # also "Gtc" or "Ioc"
# exchange.cancel("BTC", oid)                  # actual resting receipt's integer oid
# exchange.cancel_by_cloid("BTC", cloid)       # preserve the original Cloid
# reply = place_bracket(quantity, entry, tp_trigger, tp_limit, sl_trigger, sl_limit)
# Close both sessions when finished:
# exchange.session.close()
# exchange.info.session.close()
```

`Gtc` may rest; `Alo` is post-only and can refuse crossing; `Ioc` cancels unfilled
remainder. Current v11 trigger-limit policy additionally supports reduce-only
TP/SL limits (`isMarket:false`) with `na`, `normalTpsl` and `positionTpsl` under
their native group rules. `normalTpsl` binds one entry and its TP/SL children;
`positionTpsl` contains only reduce-only triggers. Trigger-market orders are not
supported. Older incoming policies refuse these trigger/group requests. A
trigger crossing submits a limit: it does not guarantee a fill. Group acceptance
does not prove child execution, and cancellation/edit lifecycle remains native.

Check outer errors **and every per-order status** (`resting`, `filled`, or
`error`). Preserve signed bytes, original cloids/IDs and nonce for unknown-result
reconciliation; do not invent another order after a timeout. Batches are bounded
to 16 orders and are not a promise of all-or-nothing business success. Stock HL
funding/withdrawal/transfer helpers are not Glyde money authority.

For missing fields, unavailable histories, supported channel/action limits and
the distinction between source support and runtime qualification, consult the
[verification and compatibility gaps](#verification-and-compatibility-gaps). For a paper-first strategy and risk
boundary, use [Glyde Market Maker](https://github.com/Glyde-trade/glyde-market-maker).

## Verification and compatibility gaps

The earlier owned local paper stack accepted the unchanged SDK0.24 `Info`
constructor, meta, spot bootstrap, l2Book and allMids. The public socket received
three subscription ACKs; book/mids callbacks arrived, while empty trade arrays
produced no stock SDK callbacks. Requested candles returned an explicit 503
instead of an invented empty history. This is local observation, not a claim
that the production endpoint is enabled or that private trading was qualified.

All six Python blocks in this guide were additionally exercised offline with
SDK0.24 under a network-denied harness. It checked request shapes, three public
and four owner subscriptions, and recovered six sample L1 signatures in realm b.
Those HTTP/WS fixtures were synthetic; they do not certify native acceptance,
private fill history, reconnect recovery or venue settlement.

| Surface | Limit or required check |
| --- | --- |
| API URL / Origin | SDK base ends in `/v1`, without a trailing slash. A custom server must explicitly admit the SDK's API Origin; do not impersonate an app Origin. |
| Perpetual universe | Native supported assets only; no general HL/HIP-3 account or market proxy. |
| Spot | Empty catalogue is bootstrap compatibility, not spot execution. |
| Private account/orders | Matching registered owner and qualified same-cut native facts required. |
| Fills/candles/funding history | Requires complete qualified coverage; unavailable does not mean empty. |
| Owner WS | Account/open-order channels need raw frames; SDK0.24's stock callback map does not implement those Glyde extensions. |
| Trigger groups | v11 policy required; trigger limits only, native risk/group/adapter rules still apply. No trigger-market or guaranteed fill. |
| Money actions | Stock HL transfer/withdrawal helpers do not acquire Glyde financial authority. |

Begin with `glyde-mm paper <config>` and review simulation results. Paper fills
are approximations; live sessions require independently authorized keys, read
credentials, current projections and small explicit risk limits.
