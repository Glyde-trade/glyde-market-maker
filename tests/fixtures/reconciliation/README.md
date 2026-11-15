# Synthetic reconciliation capture

`session.jsonl` is deliberately synthetic test data, not historical market data,
a real account capture or a profitability claim. Its user is a dummy address.

Two acknowledged BTC maker quotes (buy 99, sell 101, one base unit each) are
replayed against the public trade tape. Paper fills 0.5 units on each side.
Synthetic private fills contain 0.25 buy at 98.8 and 0.75 sell at 101.2, within the
original limits, with a duplicate snapshot of the buy. Their USDC fees are 0.01
and a rebate of -0.005. The result is 0.25 paper-only quantity, 0.25 live-only
quantity, -0.15 USDC matched slippage (price improvement) and 0.005 USDC fee impact.
It is not net PnL. Original cancellation receipts close the quote lifetimes.

The completeness marker describes this synthetic fixture only. Never attach one
to a real interrupted, partial or reconstructed capture to make a report pass.
