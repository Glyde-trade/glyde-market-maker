# Market making 101

A market maker offers to buy and sell. Other traders choose whether to trade with those
quotes. Running a bot does not mean both sides will fill, or that the spread becomes profit.

## The spread is an opportunity, not a paycheck

Suppose you bid 99.90 and ask 100.10. The difference, 0.20, is your quoted spread.
If you buy one unit at 99.90 and later sell it at 100.10, the price difference is 0.20
before costs. One side may fill without the other. Wider quotes can sit untouched;
tighter quotes can fill more often while leaving less room for costs and mistakes.

## Inventory is the position left between fills

After that buy, you own one unit. If its price falls to 90, the position has lost 9.90
in value even though your original sell quote looked profitable. Repeated buys can build
inventory faster than sells remove it. Several symbols can fall together, so they are
not automatically diversification.

Position limits count potential fills from outstanding orders, not only what you already
hold. Smaller orders and fewer levels reduce exposure; they cannot prevent a price gap.
Stopping this bot attempts to cancel orders but **does not close positions**.

## Adverse selection: the fill you did not want

A trader may buy your ask just before the market rises, leaving you selling too cheaply.
Or they may sell into your bid just before it falls. Your screen can look current while
the price has already changed elsewhere. This is adverse selection. More fills can mean
more losses; fill count alone is not a strategy score.

## Fees can consume the spread

Count costs on both the buy and sell. In the example, hypothetical total fees of 0.12
would leave 0.08 of the 0.20 difference, before funding and other effects. Those numbers
are arithmetic, **not Glyde's fee schedule**. Check your account's current maker/taker
fees and funding terms; do not assume a rebate. A price change between fills can outweigh
many successful small spreads. A daily-loss threshold is not a guaranteed maximum loss.

## Why paper and live fills differ

Paper streaming estimates queue position from displayed depth and consumes public trade
prints. It cannot see hidden liquidity or your actual venue queue position, and its orders
do not change the real book. It omits fees, funding, margin, liquidation and real latency.
Book-only replay uses a different crossing model and cannot invent a missing trade tape.

Use paper to learn controls and identify obvious risk refusals. [Compare strategies](compare-strategies.md)
on the same recording to inspect inventory, drawdown and stops as well as fills and PnL;
different presets can still use different quantities. A good simulated result does not
establish live profitability or trading permission.

Next: [choose a strategy](strategies.md), [understand risk limits](risk-limits.md), and
[run a paper-first session](first-market-maker.md). Only consider the
[guided live workflow](first-live-session.md) after reviewing your account and limits.
