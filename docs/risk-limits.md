# Risk-limit FAQ

These are software guardrails, not a guaranteed maximum loss or a substitute for venue margin rules.
All strategies use the same risk boundary in paper and live mode.

## Is order amount a USDC amount?

No. `strategy_config.order_amount` is in base units. For example, `0.0001 BTC` at a hypothetical
100,000 USDC/BTC is about 10 USDC of notional. The runtime uses a conservative current price
reference for its per-order check, so the wizard's earlier reference price is not lasting authority.
Prices, precision and minimum order requirements still have to fit your config.

## Why does `max_position` refuse a new quote when my position is small?

The bound includes outstanding and pending orders, not just filled inventory. It checks the
possible long and short positions if each side fills. More levels consume more of that budget;
inventory skew may also change sizes. Cancelling a quote does not release its reservation until
its outcome is established. Reduce configured size or levels while respecting the venue minimum.

## Does `max_order_notional` cap every order or the whole account?

Each order. It is not an account-wide exposure cap. The position limit separately bounds directional
base exposure including outstanding orders. Neither setting configures leverage or liquidation
protection at the venue. A refusal does not authorize automatically increasing either cap.

## Is `max_daily_loss` realized trading PnL?

It measures the fall in account equity from the first accepted account observation of the UTC day.
Unrealized changes and transfers can affect it. Once breached, quoting stops and cancellation is
attempted. Existing positions are not automatically flattened; slippage, fees and connectivity can
produce losses beyond the configured number.

The baseline and loss latch persist across restarts in `.glyde-mm`. An account observation on a new
UTC day establishes a new daily baseline; it does not restart a stopped process. An unresolved
execution intent remains blocked across restart and day rollover. Deleting state is not a repair.

## Why are there no quotes despite a healthy public connection?

Check the terminal's risk reason, public market precision, current price and available account
permissions. Sizes may round to zero, breach a cap or miss a venue minimum. A grid retires quotes
outside its configured band. Avellaneda and inventory-skewed pricing need consecutive source-time
samples with nonzero movement; gaps restart warmup. A current public book does not establish that
private reads or live trading are available.

## Can I increase freshness timeouts to make an outage disappear?

`stale_book_seconds` and `account_age_seconds` bound the observations used to admit orders. Stale
source timestamps, repeated old books and unavailable private reads are not fresh authority.
Restore the feed or account access instead of treating larger timeouts as proof of safety.

## What should I do after an unknown order or cancel outcome?

Stop and reconcile the original order/client IDs against authoritative venue state. The bot persists
the original execution intent and refuses new risk; it does not replace an ambiguous order with a
new ID or nonce. A report helps identify the run and exit category but is not execution proof.
Keep the state directory and resolve the original outcome before attempting further trading.

## Why does live startup refuse my existing orders or position?

The retail CLI requires explicit typed-symbol confirmation, then reads account/open-order state
before managed cleanup exists. An occupied selected symbol is refused without placing or cancelling
orders. `--i-understand` accepts that startup exposure after review; it does not waive confirmation,
position/notional/loss caps, freshness or unresolved-intent checks. Other symbols do not trigger this
startup refusal. This snapshot is not a lock against another process; use a dedicated account.

## Does Ctrl-C guarantee there are no live orders?

It requests cancellation and cleanup. Successful cleanup depends on transport and authoritative
receipts; verify venue state after any failure. Known order IDs are cancelled before discovery,
but a read or cancel failure still prevents a clean-success claim. Use a dedicated account because
symbol/account cleanup may affect orders placed outside this bot.

## Does doctor or paper mode prove live trading will work?

No. Ordinary doctor checks config, key presence, public metadata and a fresh public book. Explicit
`doctor live-small.yaml --private` also checks account/open-order read permissions; it still does
not sign or establish trading authority. Paper simulates
fills and account state locally; it omits fees, funding, liquidation and real queue priority. Live
requires supported One API capabilities and correct owner, namespace and key permissions. The bot
never falls back to another venue when those are unavailable.
