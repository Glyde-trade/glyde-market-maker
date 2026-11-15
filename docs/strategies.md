# Choose a paper strategy

Copy an example, then run `glyde-mm paper --config YOUR_COPY.yaml`.
Amounts are base-asset units; spreads are fractions (`0.002` = 0.2%). Check the
current price, venue minimum and risk caps before changing to live mode.

## Strategy chooser

| Strategy key | What changes | Paper experiment | Important behavior |
| --- | --- | --- | --- |
| `pure_market_making` | Fixed spreads around the moving midpoint; optional linear inventory sizing | Learn quote placement, refresh and cancellation | Extra levels and size skew share the same position cap |
| `avellaneda_market_making` | Reservation price/spread respond to sampled volatility and inventory | Compare adaptive prices with fixed spreads | Needs consecutive moving-price samples; gaps restart warmup |
| `inventory_skewed` | Avellaneda prices plus smaller inventory-increasing quotes | Explore reducing new exposure away from a signed target | Shares Avellaneda warmup; never upsizes to chase the target |
| `grid` | Fixed levels within your chosen price band | Explore a configured range | Quotes retire outside the band; the band does not track the market |

Conservative, Balanced and Active are editable paper **presets**, not additional strategies or
profit/risk guarantees. Start with the [first-run tutorial](first-market-maker.md) and keep the
[risk limits](risk-limits.md) explicit. See the [inventory-skewed example](../examples/inventory-skewed-paper.yaml).

### Pure market making — [paper.yaml](../examples/paper.yaml)

Quotes a bid and ask at fixed distances from the moving midpoint.  
Set `bid_spread`, `ask_spread`, `order_amount` and `order_refresh_time`.  
Use it to learn the quote/refresh lifecycle with the simplest pricing rule.

### Avellaneda — [avellaneda-paper.yaml](../examples/avellaneda-paper.yaml)

Moves quotes with measured volatility and your position relative to its target.  
Set `risk_factor`, `liquidity_kappa`, `inventory_target_position` and sample window.  
Use it to paper-test adaptive pricing; warmup needs 31 consecutive one-second
samples with nonzero movement, and missing intervals restart that window.

### Grid — [grid-paper.yaml](../examples/grid-paper.yaml)

Places fixed price levels inside a band you choose; outside it, quotes retire.  
Set `lower_price`, `upper_price`, `levels` and `order_amount`.  
Use it for a range-bound experiment after setting the band around today's book.

All examples use simulated funds, no trading key, bounded position/notional/loss
limits and no hanging orders. A paper result is not a live-profit guarantee.

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

# Avellaneda market making

`avellaneda_market_making` uses Hummingbot's normalized Avellaneda reservation
price and spread logic and the **same** execution lifecycle and risk wrapper.
It does not create a second order coordinator.

```yaml
strategy: avellaneda_market_making
strategy_config:
  order_amount: "0.0001"
  order_refresh_time: 30
  risk_factor: "1"
  liquidity_kappa: "1"
  time_left_fraction: "1"
  volatility_samples: 30
  sample_interval_ms: 1000
  minimum_spread: "0.001"
  inventory_target_position: "0"
```

With midpoint `s`, signed position `p`, target `p*`, account equity `E`,
absolute midpoint-change volatility `sigma`, risk factor `gamma`, configured
liquidity sensitivity `kappa` and normalized time fraction `tau`:

```
q = (p - p*) * s / E
reservation = s - q * gamma * sigma * tau
spread = gamma * sigma * tau + 2 / gamma * ln(1 + gamma / kappa)
```

The bid/ask start half a model spread from the reservation price and are clamped
to at least `minimum_spread` **per side** from midpoint, matching PMM's fraction
units. Hummingbot's normalized formula uses absolute volatility rather than the
textbook squared-volatility variant. Long inventory shifts both prices down;
short inventory shifts them up. Position and equity scale together, so the
inventory adjustment does not depend on account size alone.

Volatility is the RMS of absolute midpoint changes, sampled once per source-time
interval over a bounded window. It is not percentage volatility or a tick-rate
estimate. At least `volatility_samples + 1` consecutive interval samples are
required. Missing intervals reset the window; repeated callbacks cannot fill it.
Missing/zero volatility produces **no quotes** and retires tracked orders at the
next tick. Warmup completion enables the next tick without waiting for an empty
refresh period. No fixed-spread or guessed-volatility fallback is used.

`liquidity_kappa` is an explicit positive inverse-price calibration; this first
implementation does not claim to estimate trade-arrival intensity or optimal
parameters from an incomplete tape. Defaults and the example are illustrative,
not a profitability claim. Paper-test parameters and venue minimums first.
Positive equity is required. Levels, amount steps, price bands, rounding and
post-only behavior follow PMM. Hanging orders remain disabled.

The normalized formula is rewritten from Hummingbot's
`avellaneda_market_making` at revision
`9af100d6822da7d2d0291a906c730ef172284ee2`, Apache-2.0. See `NOTICE`.

## Compare against a recorded market

[Record and replay public books](replay.md) to compare these strategies over the
same captured window, using simulated funds and the same risk limits.

## PMM ping-pong and scaled levels

Set `ping_pong_enabled: true` in PMM's `strategy_config` for streaming paper.
Both sides are initially eligible. Each identified fill on a currently tracked
order, including a partial fill, switches eligibility to the opposite side.
An opposite fill switches back; it does not re-enable both sides. Duplicate
trade/order identities and fills for foreign/canceled orders do not switch it.
All old quotes must receive definitive cancellation before replacement; unknown
outcomes freeze quoting. An unidentified/mismatched own fill refuses the session.
A defensive budget of 4096 distinct fills per active quote cohort also stops on
overflow instead of forgetting dedupe evidence. The cache clears only after
confirmed cohort cancellation.

This is a deliberate adaptation of Hummingbot's filled-order balancing: Glyde
uses identified partial fills to alternate whole sides across all ladder levels.
It does not size an opposite quote to match the previous fill, force reduce-only,
flatten inventory or promise round-trip profit. Position/notional/loss/rate caps
still apply. If a band or inventory skew prohibits the needed side, the strategy
waits; it never substitutes the wrong side. Streaming paper supplies identified
local fills. Live currently has no qualified private-fill callback, and book-only
replay has no trade identities, so both refuse this mode before trading.

`price_floor`/`price_ceiling` preserve Hummingbot's directional convention, using
the quote-reference midpoint: suppress all asks at/below the floor and all bids
at/above the ceiling. The other side remains eligible. These are not strict limits
on every order's absolute price and do not close an existing position. They are
null/disabled by default. Crossing a boundary cancels wrong-side quotes before
the refresh timer; returning inside restores both eligible sides promptly.

For zero-based level `i`, PMM's bid/ask distance is
`max(base_spread + i * order_level_spread, minimum_spread)` and base quantity is
`order_amount + i * order_level_amount`, before optional inventory skew and
precision rounding. Amount increments must be nonnegative. Levels are additive,
not compounded percentages. Risk validates the entire ladder; increasing levels
or amounts never increases the caps.

PMM's Conservative/Balanced/Active presets now expose `ping_pong_enabled: false`
and null bands explicitly, retaining their prior sizing and live eligibility.
Python callers may request `strategy_preset(..., ping_pong_enabled=True)`;
`examples/ping-pong-paper.yaml` shows the editable paper-only combination. Update
its fixed bands and size reference for your market instead of copying them into
live settings.
