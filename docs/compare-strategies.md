# Compare strategies on a public tape

Capture books and trades once, then reuse that exact file entirely offline:

```sh
glyde-mm record-tape --config examples/paper.yaml --minutes 5 --include-hl --tape-file tape.jsonl
glyde-mm compare --config examples/paper.yaml --tape-file tape.jsonl --preset balanced
```

Only `record-tape` uses the network. It subscribes to public Glyde `l2Book` and
`trades`; `--include-hl` additionally records public Hyperliquid `l2Book`. It
never requests accounts, wallets or private channels. Files are created as 0600
and never overwritten. Incomplete captures are retained but report an error.
A tape must contain a trade-stream observation; an empty observed trades array
is allowed, but book-only recordings cannot establish trade coverage.

`compare` opens no sockets, loads no signer or key, writes no risk state and
never creates live orders, even if the input YAML says `mode: live`. Use a
single-symbol config. The YAML supplies the symbol, starting paper balance and
all risk caps. Each independent simulator starts flat with the same balance and
caps. The selected `conservative`, `balanced` or `active` preset generates each
strategy's valid configuration; the YAML's existing `strategy` and
`strategy_config` are not used. Preset quantities differ by strategy to stay
within the shared caps. This is not a claim of identical notional exposure.

Rows are PMM, inventory-skewed Avellaneda-Stoikov, fixed grid, and PMM around the
recorded HL midpoint. The latter uses the preset's raw-deviation and freshness
guards, not Glyde data relabelled as HL. Missing HL books produce **unavailable**
with no invented performance numbers. Stale or divergent recorded HL stops only
that row. If HL books exist, the common window starts when both book baselines
are present; earlier prints seed dedupe only. Original source/receipt times
remain intact. Old baselines may immediately fail freshness checks. Grid bounds
are fixed from the common initial Glyde midpoint; volatility warmup consumes real
forward observations, with no lookahead or pre-warmed prices.

All rows use the real strategy lifecycle, risk boundary and public-tape paper
fill model. Source events retain file order, with 250 ms virtual strategy ticks
between events and after events at tick boundaries. Duplicate/old prints cannot
be replayed as new volume. Stops cancel quotes and never resume; remaining
inventory is still marked to subsequent Glyde books through the common end.
Cancelling quotes does not close positions. The placement-rate cap still applies:
frequent fills can force refreshes that exhaust it, and that stop is shown.

The table reports:

- **Fills:** simulated fill events, including partial fragments, not completed orders.
- **Inventory min..max:** signed base-unit extrema, including the flat start.
- **PnL USDC:** final marked equity minus starting balance, including open inventory.
- **Max drawdown USDC:** largest drop from prior peak marked equity, not just loss
  from the starting balance. Marks are sampled after source events and ticks.
- **Two-sided uptime:** percentage of the full common window with at least one
  resting bid **and** ask. Warmup, risk stops and one-sided inventory suppression
  reduce it; stopped rows retain the same denominator.
- **Status:** completed, the first risk stop, or unavailable source data.

All results are hypothetical. Displayed-depth queue approximation cannot recover
true priority, latency, hidden liquidity or market impact; fees, funding, margin
and liquidation are excluded. A profitable simulation does not predict live PnL.

## File format

`glyde.public-tape` version 1 is JSONL. The header names `symbol` and
`size_decimals`; each subsequent event contains exactly `received_at_ms`,
`source` (`glyde` or `hyperliquid`), `channel` and `data`. Books use standard
`{coin,time,levels}` and trades use standard arrays of
`{coin,side,px,sz,time,tid}` with taker side `B` or `A`. HL trades are not accepted
as Glyde fills. Receipt times and each book source cut must not regress. One
recording is bounded to 100,000 events, 256 MiB, 1 MiB per event and 60 minutes
plus capture deadline slack (10 seconds). Trades/marks never get a local restamp.

Existing `record`/`replay --book-file` commands keep their book-only format and
fill model. They are not silently upgraded into a tape. Synthetic test data in
`tests/test_compare.py` is an accounting/control fixture, not historical returns.
