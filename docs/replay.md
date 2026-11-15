# Replay a recorded public book

Try the same paper strategy against the last N minutes **in a local recording**:

```sh
glyde-mm record --config examples/paper.yaml --minutes 5 --book-file books.jsonl
glyde-mm replay --config examples/paper.yaml --minutes 5 --book-file books.jsonl
```

`record` listens to the public `l2Book` feed until observations cover the requested
window. It reads public market precision, never private accounts or credentials,
and never sends an order. A new file is created with private permissions; an
existing file is never overwritten. Capture errors retain the partial file, but
replay refuses it if it lacks the requested coverage. A stalled feed ends capture
with an error after the window plus ten seconds.

The current client has no historical depth endpoint. A current book or candle
cannot reconstruct yesterday's bids and asks: capture must happen first. Replay
ends at the newest recorded observation, not today's wall clock. It refuses
wrong symbols, regressed times, unsupported metadata, files over 256 MiB or
100,000 observations. Windows are whole minutes from 1 to 60.

Switch `--config` to an Avellaneda or grid config to compare another strategy
against the same captured market. Each replay starts with that config's simulated
balance and zero inventory. It runs the real strategy factory, paper simulator,
position/notional/loss caps and 250 ms ticks on a virtual clock. Source timestamps
and receipt gaps are preserved; replay stops at the first risk refusal rather
than hiding a missing/stale feed. Risk state is isolated in memory and cannot
clear or modify your regular run's persistent loss latch. Even a config marked
`live` is simulated; replay constructs no network client or signer.

The summary shows inventory, simulated equity PnL, fills, the first risk stop and
seconds actually simulated. A stop can end before the requested window does.
Open quotes are cancelled before replay returns. Avellaneda still needs its
consecutive-sample warmup. All paper limitations apply: queue priority, fees,
funding and liquidation are not simulated, and historical hypothetical fills
are not proof that a real order would have filled or made money.
