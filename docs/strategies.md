# Pure market making

`pure_market_making` quotes both sides of the current midpoint. Spreads are
fractions: `0.001` means 0.1%, not 0.001%. All amounts and prices use Decimal.

```yaml
strategy: pure_market_making
strategy_config:
  bid_spread: "0.001"
  ask_spread: "0.001"
  order_amount: "0.0001"
  order_refresh_time: 30
  order_levels: 1
  order_level_spread: "0.001"
  order_level_amount: "0"
  minimum_spread: "0.0005"
  inventory_skew_enabled: false
  inventory_target_position: "0"
  inventory_range_multiplier: "1"
  hanging_orders_enabled: false
```

Choose the amount against the current price, venue minimum order size and your
risk limits; this example is not valid for every market or price. The runner must
supply the market's price tick, quantity step and minimum notional. Bids round
down, asks up, and sizes down. Below-minimum orders are omitted. The transport
still validates the venue's additional price precision rules.

Each additional level adds `order_level_spread` to the spread and
`order_level_amount` to the base quantity. `minimum_spread` is a floor on newly
proposed spreads; resting orders that breach it are refreshed immediately.
At or above `price_ceiling`, no new bids are placed. At or below `price_floor`,
no new asks are placed. These optional prices must be positive and ordered.

Inventory skew increases the side that moves toward `inventory_target_position`
and reduces the other side, with each multiplier bounded between zero and two.
The target is a **signed base position**, default zero for a flat perpetual
account. Unlike spot Hummingbot, it does not assume that leveraged positions are
spendable base inventory or derive a spot allocation from collateral.

All orders use post-only (`Alo`). Refresh cancels the strategy's tracked orders
and waits for confirmed cancellation before reading the latest account and
placing new quotes. `already_closed` is accepted only as a typed, authoritative
terminal outcome from the client; arbitrary rejection text is not proof.
Hanging orders are disabled and enabling them is refused. Ambiguous submission,
cancellation or mismatched receipts freeze the strategy without retrying.
Fills request a fresh account read; fill callbacks never release reservations.
`stop()` freezes new placement and requests symbol-wide cancellation.

## Runner interface

The shared factory is `create_strategy(name, context, strategy_config)`.
`StrategyContext` takes a **RiskManagedClient**, symbol, Decimal `price_tick`,
Decimal `quantity_step`, optional Decimal `min_notional`, and a monotonic clock.
Feed the risk wrapper before forwarding `on_book(Book)` or `on_fill(Fill)`;
call async `on_tick()` on the runner clock, and async `stop()` on shutdown.
Strategy code never reads keys or submits through a raw exchange connection.
Client receipt identity and unknown-outcome handling remain mandatory.

## Attribution

The level, refresh, minimum-spread, price-band and linear inventory-skew designs
are rewritten from Hummingbot's `pure_market_making` and
`inventory_skew_calculator` at revision
`9af100d6822da7d2d0291a906c730ef172284ee2`, Apache-2.0. See `NOTICE`.
