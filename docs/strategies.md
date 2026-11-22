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
  order_amount: "0.0002"
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
  order_amount: "0.0002"
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

### Optional streaming-paper trade input

Set `volatility_source: book_and_trades` to include actual public `trades` alongside
`l2Book` through the existing One API stream. The default `book` keeps the original
midpoint-only model. No separate exchange client or order coordinator is created.

The combined estimator samples completed source-time intervals. For each interval
it averages the latest accepted midpoint with that interval's trade VWAP, then
computes RMS changes across `volatility_samples + 1` consecutive complete samples.
The next interval's first event closes the previous interval, so warmup also needs
that closing event. Both book and trade input are mandatory per interval. A missing
channel, skipped interval or zero volatility withholds quotes; losing readiness
retires existing quotes on the next tick, even before the refresh deadline.
Trades that arrive after their interval has closed are ignored. Duplicate trade
IDs within an interval count once; conflicting IDs or more than 4096 distinct
prints in one interval refuse rather than silently truncating the sample.

This is an explicit Glyde estimator adaptation. It does not fit Hummingbot's
trade-arrival intensity or infer optimal `liquidity_kappa` from an incomplete tape.
Combined mode currently runs only in streaming paper; live and book-only replay
refuse it before credentials or orders. Inventory, rounding, post-only placement,
confirmed cancellation and shared risk admission remain the existing lifecycle.
See [the paper config](../examples/avellaneda-trades-paper.yaml).

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

## Complete YAML key reference

Unknown keys refuse. Quote decimal values in YAML; quantities and position targets
are base-asset units, spread fractions use `0.001 = 0.1%`, and refresh tolerance
uses percentage points (`0.1 = 0.1%`). Defaults are not reviewed trading sizes.

### Session keys

| Key | Default | Meaning |
| --- | --- | --- |
| `mode` | `paper` | `paper` or explicit `live`; the `paper` command always simulates. |
| `symbol` | `BTC` | Exact metadata symbol for one market; do not combine with `markets`. |
| `markets` | empty | 1–16 unique market mappings; requires gross total-notional cap. |
| `base_url` | `https://api.glyde.trade/v1` | One API root; HTTPS, or HTTP on loopback for fixtures only. |
| `owner` | empty | Registered private-read owner; required for live. |
| `vault_address` | null | Discovered signed trading namespace; required for live, distinct from owner. |
| `mainnet` | true | Signing source/network selection; not permission or account discovery. |
| `private_key_env` | `GLYDE_API_PRIVATE_KEY` | Trading-key environment variable name, never its value. |
| `read_api_key_env` | `GLYDE_READ_API_KEY` | Optional private-read override variable name, never its value; when unset, reads are minted from the trading key. |
| `paper_balance` | `10000` | Positive simulated starting equity, in USDC. |
| `strategy` | `pure_market_making` | A chooser key above, or a trusted [plugin](plugins.md). |
| `strategy_config` | empty | The selected strategy's keys below. |
| `risk` | defaults below | Shared admission limits; never raised by strategy settings. |
| `metrics_enabled` | false | Enable read-only Prometheus observations on loopback; no exchange polling. |
| `metrics_port` | 9464 | Loopback TCP port (0 chooses a free port); choose distinct ports for separate runners. |
| `alerts_enabled` | false | Best-effort after-cleanup chat notifications; not a safety controller. |
| `alert_webhook_env` | `GLYDE_MM_ALERT_WEBHOOK` | Optional webhook variable name; never put its URL in YAML. |

Each entry in `markets` has these keys:

| Key | Default | Meaning |
| --- | --- | --- |
| `symbol` | required | Unique exact metadata symbol. |
| `max_position` | required | Positive base-unit cap for this market. |
| `strategy` | session strategy | Optional strategy type for this market; shared settings must be compatible. |
| `strategy_config` | empty | Overrides merged into the session's strategy settings for this market. |

### Shared risk keys

| Key | Default | Meaning |
| --- | --- | --- |
| `max_position` | `0.01` | Worst possible signed position including pending/open quotes; multi-market entries supply their own cap. |
| `max_order_notional` | `10` | Positive per-order USDC bound, using a conservative price reference. |
| `max_daily_loss` | `5` | Equity fall from the persisted UTC-day baseline; includes unrealized changes and transfers. |
| `stale_book_seconds` | `5` | Positive source/arrival freshness limit, at most 3600 seconds at runtime. |
| `max_stale_pause_seconds` | `300` | Positive bound on one stale-book pause. A stale market retires its quotes and resumes on a fresh book; staying stale longer ends the run as a `stale_book`/`stale_book_source` risk stop. |
| `account_age_seconds` | `10` | Positive account-observation age limit, at most 3600 seconds at runtime. |
| `max_orders_per_minute` | 60 | Rolling individual placement-attempt budget, integer 1–6000, shared by all markets. |
| `max_total_notional` | null | Optional positive gross position-plus-orders bound across markets; mandatory with `markets`. No cross-asset netting. |

### Execution keys accepted by all four built-in strategies

| Key | Default | Meaning |
| --- | --- | --- |
| `order_amount` | `0.001`; grid requires it | Positive base quantity; all levels still consume risk capacity. |
| `order_refresh_time` | 30 | Positive finite seconds between comparisons, not permission to retain unsafe quotes. |
| `refresh_tolerance_pct` | null | Optional 0–100 percentage-point price tolerance; null disables retention. Sizes/fills/safety/max-age override it. |
| `max_order_age` | 300 | Positive finite maximum quote age in seconds, at most 86400. |
| `minimum_spread` | PMM/grid `0`; adaptive `0.001` | Nonnegative fraction below 1; minimum distance per side from midpoint. |

### PMM-only keys

Together with the five execution keys, this is every `PureMarketMakingConfig` key:

| Key | Default | Meaning |
| --- | --- | --- |
| `bid_spread` | `0.001` | Nonnegative bid fraction below 1. |
| `ask_spread` | `0.001` | Nonnegative ask fraction below 1. |
| `order_refresh_tolerance_pct` | null | Alias in the same units; configuring both tolerance names refuses. |
| `filled_order_delay` | 0 | Finite 0–86400-second pause after identified own paper fills, including partials; nonzero live setting refuses. |
| `order_levels` | 1 | Integer 1–50 per side; one means no extra ladder. |
| `order_level_spread` | `0.001` | Nonnegative additive price fraction per level, below 1. |
| `order_level_amount` | `0` | Nonnegative additive base quantity per level. |
| `inventory_skew_enabled` | false | Enable linear sizing toward target; multipliers stay between 0 and 2. |
| `inventory_target_position` | `0` | Finite signed base-position target. |
| `inventory_target_base_ratio` | null | Optional 0–1 signed-position-value/equity target; only effective with skew; cannot combine with a nonzero position target. |
| `inventory_range_multiplier` | `1` | Positive multiplier on two-sided ladder quantity; ratio mode also caps range at half equity/reference price. |
| `price_ceiling` | null | Positive quote-reference ceiling; suppress bids at/above it. |
| `price_floor` | null | Positive quote-reference floor; suppress asks at/below it; must be below ceiling. |
| `ping_pong_enabled` | false | Streaming paper only: alternate the eligible side on identified tracked fills; live/book-only replay refuse. |
| `hanging_orders_enabled` | false | Only false is supported; true refuses. |
| `reference_source` | `glyde` | `glyde` midpoint or explicit public `hyperliquid` reference for Glyde execution. |
| `reference_skew_bps` | `0` | Signed −1000..1000 basis-point adjustment, only with external reference. |
| `max_reference_deviation_bps` | `50` | Positive maximum reference/Glyde divergence, at most 1000 bps. |
| `reference_stale_seconds` | `3` | Positive external-reference age limit, at most 60 seconds. |

### Avellaneda–Stoikov keys

`avellaneda_market_making` accepts the five execution keys plus exactly these keys.
PMM-specific sizing, external-reference and post-fill options are not accepted.

| Key | Default | Meaning |
| --- | --- | --- |
| `order_levels` | 1 | Integer 1–50 per side. |
| `order_level_spread` | `0.001` | Nonnegative additive level spacing fraction below 1. |
| `order_level_amount` | `0` | Nonnegative additive level quantity. |
| `inventory_target_position` | `0` | Finite signed base-position target; no spot-wallet ratio. |
| `risk_factor` | `1` | Positive risk-aversion parameter in the normalized formula above. |
| `liquidity_kappa` | `1` | Positive configured inverse-price depth calibration; not automatically fitted. |
| `time_left_fraction` | `1` | Positive normalized model horizon, at most 1; not an automatic session stop. |
| `volatility_samples` | 30 | Integer 2–4096 changes; warmup needs one more consecutive sample. |
| `sample_interval_ms` | 1000 | Integer source-time interval size, 1–60000 milliseconds. |
| `volatility_source` | `book` | `book` or opt-in streaming-paper `book_and_trades`; see sampling contract above. |
| `price_ceiling` | null | Positive midpoint ceiling; suppress bids at/above it. |
| `price_floor` | null | Positive midpoint floor; suppress asks at/below it; ordered below ceiling. |

### Inventory-skewed-only keys

`inventory_skewed` accepts every Avellaneda key plus:

| Key | Default | Meaning |
| --- | --- | --- |
| `order_amount_shape_factor` | `1` | Decimal 0–100; exponentially reduces only inventory-increasing quantities as normalized distance from the target grows. Zero disables that extra shaping. |

### Grid-only keys

Grid accepts the five execution keys plus exactly these three keys:

| Key | Default | Meaning |
| --- | --- | --- |
| `lower_price` | required | Positive lower fixed-band price. |
| `upper_price` | required | Positive upper price, strictly above lower. |
| `levels` | 5 | Integer 2–50 across the entire band, not per side. |

Grid quotes eligible levels below/above midpoint as bids/asks, skips midpoint and
crossing/below-minimum levels, and retires everything outside its fixed band.

## Bounded paper examples

These complete examples use simulated equity, no keys, and explicit small risk
caps. Copy one before editing, inspect current metadata/price and run doctor. A
size that violates current venue rules or caps must refuse; do not increase caps
automatically to make an example start.

| Strategy | Complete paper config | Experiment |
| --- | --- | --- |
| PMM | [paper.yaml](../examples/paper.yaml) | One bid/ask around the current midpoint. |
| Inventory skew | [inventory-skewed-paper.yaml](../examples/inventory-skewed-paper.yaml) | Avellaneda prices with reduced inventory-increasing size after warmup. |
| Avellaneda–Stoikov | [avellaneda-paper.yaml](../examples/avellaneda-paper.yaml) | Volatility/inventory adaptive pricing; [combined trade input](../examples/avellaneda-trades-paper.yaml) is an optional stricter experiment. |
| Grid/levels | [grid-paper.yaml](../examples/grid-paper.yaml) | Set a reviewed band around the current book; every level shares the caps. |

```sh
cp examples/avellaneda-paper.yaml ./my-paper.yaml
glyde-mm doctor ./my-paper.yaml
glyde-mm paper ./my-paper.yaml
```

For PMM sizing skew instead of adaptive-price `inventory_skewed`, copy
`examples/paper.yaml` and set `inventory_skew_enabled: true` with a reviewed signed
target. It can increase one side up to twice its configured quantity, so budget all
levels. Cancel-before-replace releases capacity only after confirmed outcomes.

All strategies share account loss/rate state, per-order bounds and pending-order
reservations. More levels do not grant extra risk capacity. Loss caps are not
guaranteed maximum losses; stop does not flatten positions. See
[operations](operations.md) before restarting or interpreting a clean report.
