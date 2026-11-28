# Changelog

GitHub version tags/releases are the publication record; Unreleased changes require current source.

## [Unreleased]

## [0.7.1]

- Order-entry pacing follows the limits Glyde reports. The client reads
  `glydeOrderRateLimits` once and paces to the place and cancel buckets the server admits;
  against a server that does not report them it defaults to Glyde's current limits (place
  120/min burst 60, cancel 240/min burst 80) instead of v0.7.0's place 30/15 and cancel 20/10.
  Actions carry up to 16 entries, so a 40-level ladder re-places in one burst. Retry-After is
  always honoured. Live `max_quote_actions_per_minute` defaults to 360.

## [0.7.0]

- Glyde rate-limit refusals are definitive. Glyde admits order entries per account per
  minute, counted per entry: place 30 (burst 15), cancel 20 (burst 10). The client now paces
  every action to those buckets, sends at most 15 places or 10 cancels per action, and waits
  out a refusal's Retry-After before the next one. A rate-refused cancel in the sweep (mid-run,
  on reconnect and at shutdown) is resent for up to 300s instead of latching
  `cancel_outcome_unknown`. Live `pure_market_making` runs default
  `max_quote_actions_per_minute` to 50 (#120).
- `glyde-mm stop` reads `openOrders` and `historicalOrders` first and cancels only the
  persisted originals still resting, at most 10 per action, waiting out Retry-After. The stop
  report and `STOP` line name the failure class (`rate_limited`, `timeout` or
  `stop_unconfirmed`), still with no exception text or addresses (#121).
- A planned read-credential rotation (about every 4.5 minutes) keeps the resting quotes and
  is not a fault. On the new socket the strategies adopt their quotes when the fresh snapshot
  shows exactly those quotes; anything else takes the full reconcile. Events log
  `read_rotation` / `read_rotated` and metrics count `read_rotations_total`, so real drops
  stay visible (#122).
- A stale account read is a drop that resnapshots, not a risk stop: quotes are pulled, the
  stream reconnects, and the run ends only past the 300s reconnect window.
- A timed-out first connect, and an unresolved startup cancel latch, retry inside the same
  300s window instead of exiting for a supervisor restart.
- An untagged open order the bot cannot parse (for example a pending Market IOC or a trigger
  order listed with `limitPx` "0") is skipped and logged once, instead of failing the owner
  snapshot.

- A fill landing while the strategy is busy requoting no longer ends a live run with
  `live stream consumer overflow` (shown as `live stream invalid, gapped or disconnected`
  before the stream-fault diagnostics below). Books arrive at ~20/s, so a requote that held
  the consumer for more than ~1.5 s filled the 32-event queue, and the next owner cut carrying
  a fill was fatal, typically a few minutes into a run. Owner cuts now coalesce into one
  queued event that carries every undelivered fill, and evict the oldest queued book when the
  queue is full. Fills are never dropped; a backlog beyond 4096 undelivered fills still fails
  closed.
- A live stream failure now says why it stopped instead of the bare
  `live stream invalid, gapped or disconnected`. The message names the fault (`invalid`,
  `gapped` or `disconnected`), the raw validation error, the frame's channel and group, the
  expected versus received sequence, and the WebSocket close code and reason. A refused
  subscription names Glyde's `reasonCode` and refusal text. The run log gains a bounded
  `stream_fault` event, and `glyde-mm report` shows the run's last one.
- A routine account check no longer ends a run with `order_outcome_unknown` or
  `cancel_outcome_unknown` while an order or cancel is still being written. The check now
  waits for the in-flight action to finish instead of mistaking its durable intent record
  for an unresolved outcome. This intermittently stopped paper runs after a few seconds.
- A peer market's book expiring between pretrade validation and dispatch no longer ends a
  multi-market run with `cancel_outcome_unknown`. Nothing was sent to Glyde, so the orders are
  released and refused exactly as a pretrade refusal is, and the stale market is retired alone.
- Shutdown no longer reports a clean stop when the final venue sweep could not read Glyde.
  The reconnect-time deferral of a transient sweep failure does not apply at close, where no
  next connection repeats the sweep; it stops with `cancel_outcome_unknown` instead.
- More than 8 levels per side now runs on Glyde. Glyde refuses an order, cancel or modify
  action with more than 16 entries, so 10+ levels (20+ quotes) ended ~30s after start with
  `RiskViolation: cancel_outcome_unknown` and no quote placed. Batches are now split into
  signed actions of at most 16. A whole-action refusal (`status: err`) is a known outcome:
  nothing was placed, so the bot no longer hunts order history for cloids that never existed;
  it stops with `order_refused` and prints Glyde's refusal text as the cause. The offline
  venue enforces the same 16-entry cap.
- A Glyde stream gap or resnapshot no longer ends a live run. A sequence gap, a new
  session/epoch (an engine restart), a resnapshot request, an unsafe publication, an
  engine-halt refusal code without `retryAfter`, or an order refused with "trading is
  unavailable" now retires quotes and reconnects from a fresh snapshot like a socket drop.
  Starting while Glyde reports its engine halted (HTTP 503) waits, within
  `max_engine_halt_seconds`, instead of exiting.
- A Glyde engine halt no longer ends a live run. While Glyde reports its engine unavailable
  (`GET /v1/health` 503, `glydeSafety`, or an engine/projection refusal code), cancel proof and
  reconnects keep waiting with bounded backoff; that time does not count against
  `max_stale_pause_seconds` and is bounded by the new `risk.max_engine_halt_seconds`
  (default 1800). Once health is READY the bot re-sends the cancels the halt refused, proves
  them from `openOrders`/`historicalOrders`, reconciles its tagged orders and resumes quoting
  with any fill made on resume counted as inventory against `max_position`.
- Cleanup only touches the bot's own orders. Every order carries a client order id tagged
  `0x676d6d31` ("gmm1"); startup, reconnect, stale-pause, risk-halt, shutdown and `stop`
  sweeps cancel only tagged or self-recorded orders, so manual orders in the same account stay
  open (they still count toward risk limits).
- A short Glyde outage no longer ends a live run. A reconnect reconcile whose owner snapshot
  does not arrive (or any transient read failure after every known original is confirmed
  closed) retries with backoff for up to `max_stale_pause_seconds`, instead of becoming
  `cancel_outcome_unknown`. A cancel latch listing no original is never written, and one
  found on disk is ignored. At live startup a `cancel_outcome_unknown` latch clears itself
  when `openOrders` and `historicalOrders` prove every listed original terminal.
- A late cancel receipt no longer ends a live run. A cancel with no or a failed receipt pauses
  trading and retries the venue proof (`openOrders` plus `historicalOrders`) with backoff for up
  to `max_stale_pause_seconds`; proof resumes quoting with no latch, and only missing proof
  latches. A lagging or stalled owner stream (the 2s post-cancel cut deadline, a stale account
  or an incomplete batch) now reconnects instead of becoming `cancel_outcome_unknown`.

## [0.6.1]

- Fix cancellation after uncertain order outcomes and restarts: cleanup retains
  durable originals alongside pending and previously observed IDs, even when
  discovery lags. Successful cancellation never clears an earlier unknown halt
  or replaces its reason. Generated offline histories cover three strategies. (#70)
- Live mode now consumes sequenced account/order/fill snapshots through the WS
  cache instead of periodic private HTTP reads. Gaps, stale inputs and unsafe
  publications latch the session; a restart requires a complete fresh snapshot
  and does not bypass persisted uncertainty. (#64)
- Doctor checks public HTTP Date bounds without another endpoint, reports key
  presence by environment-variable name only, and validates risk freshness limits.
  Strategy/configuration and operations references cover restart/stop semantics. (#67)
- Add an external SDK quickstart and accurately scoped stock Hummingbot probe.
  Offline compatibility is not native/private/deployment qualification. (#63, #66)
- Update live CLI and wizard offline fixtures for sequenced owner baselines.
  Public GHCR pulls still require an org-owner visibility change; build locally
  until that is completed. (#68, #69)

## [0.6.0]

- Tag releases publish version/full-SHA GHCR images for amd64 and arm64, using
  only the repository GITHUB_TOKEN. A real non-root container paper CLI runs
  against a loopback fake One API for 20 seconds and must show quotes, fills and
  confirmed cleanup before image publishing; no credentials are supplied.
- Avellaneda gains opt-in streaming-paper book-plus-trade volatility, using
  complete consecutive source-time intervals. Missing inputs retire quotes;
  default book-only behavior and shared risk admission remain unchanged. (#62)
- PMM adds signed-perpetual equity-ratio inventory targets, an explicit refresh
  tolerance alias, and conservative identified-fill delays in streaming paper.
  These options do not relax risk limits; live refuses nonzero fill delay. (#61)
- Typed Python strategy plugins reuse host-owned risk, placement/cancellation
  identity and lifecycle handling; plugin import explicitly executes local code. (#59)
- CLI paper coverage verifies both quote sides fill through public prints and
  position/notional refusals place no simulated or live orders. (#60)

## [0.5.0]

- Optional chat webhooks send bounded, best-effort risk-stop, unknown-outcome,
  daily-loss and session-end notifications after cleanup. URLs come only from an
  environment variable and are suppressed from transport logs. Alerts are off by
  default and never establish trading or cleanup success. (#53)
- An interrupted wizard preview remains interrupted even if its countdown expires
  during cleanup; live confirmation cannot proceed after that interruption. (#56)
- Offline `reconcile` compares complete captured maker-order receipts and private
  fills against the paper tape model, reporting quantities, slippage and available
  fees. Diagnostic logs alone are insufficient, and discrepancies are not profit
  forecasts or proof of missed venue fills. (#54)
- PMM gains opt-in streaming-paper ping-pong, switching sides only on identified
  fills of tracked orders. Bands and scaled ladder controls retain shared risk
  limits. Live and book-only replay refuse ping-pong before trading. (#55)
- Every source module is checked with strict mypy in CI, typed transport boundaries
  are explicit, and wheels publish the PEP 561 typing marker. (#52)
- The loopback read-only dashboard introduced in v0.4 remains available; chat alerts
  complement local snapshots and reports without adding remote trading controls.

## [0.4.0]

- Guided `live-wizard` runs doctor and a two-minute paper preview of a frozen Conservative
  config before typed live confirmation. A 30-minute raw-stop timer ends new quoting and
  attempts cleanup, with retained account risk state and a private final report. Single-symbol
  only; cleanup can fail and positions are not flattened. (#48)
- `record-tape` captures bounded public books/trades and optional HL reference books;
  `compare` evaluates four paper configurations on the same recorded timeline with fill,
  inventory, marked-PnL, drawdown, quote-uptime and risk-stop metrics. Missing HL data is
  unavailable, never invented. No compare network/signing or persistent risk writes. (#47)
- Public One API compatibility tests replay a frozen synthetic transcript through the actual
  HTTP/WS client. They detect parser/wire drift without claiming production deployment
  compatibility or live trading authorization. (#46)
- An optional loopback read-only dashboard displays private local runner snapshots without
  new exchange reads or trading controls. (#44)

## [0.3.0]

- Paper streaming consumes public trade prints with approximate queue-ahead and partial
  fills. Synthetic tape controls cover allocation and closing fills; fees, funding,
  liquidation and actual venue queue priority remain outside the simulation. (#39)
- Quote refresh tolerance can retain sufficiently close orders until their maximum age;
  a shared rolling order-attempt limit bounds placement bursts. The tolerance is optional
  and does not relax risk or freshness checks. (#38)
- Live CLI startup prints limits, requires the exact symbol to be typed, and refuses
  existing symbol exposure unless explicitly accepted with `--i-understand`. That override
  does not bypass confirmation, risk limits or unresolved-outcome state. (#37)
- `glyde-mm stop <config>` coordinates local runner exit, cancels and verifies the configured
  symbol and saves a private stop report. Positions are not closed; failed confirmation
  returns nonzero. It requires the same state directory and an updated local runner. (#40)
- Retail VPS recipes provide persistent-state Compose and a systemd paper service, private
  `0600` env files, bounded console logs and deliberate interactive live startup. (#41)
- An optional public Hyperliquid reference can guide Glyde quote prices with deviation/freshness
  bounds. Stale or unavailable reference data stops quoting; book-only replay does not invent
  external reference history. A synthetic offline paper-session walkthrough is provided. (#35, #36)
- Repository contribution forms, a five-config paper gallery and tokenless PyPI publishing
  preparation are included. PyPI publishing remains an explicit maintainer opt-in. (#33, #34)

- Multi-symbol sessions run independent instances of one strategy type through one account,
  dispatcher, paper ledger, daily-loss/unknown-outcome state and order-attempt budget. Explicit
  per-market position caps and a required gross `max_total_notional` avoid cross-asset netting.
  Doctor, typed confirmation and stop cover the configured set; record/replay remain single-market. (#43)

## [0.2.0]

- Shared paper/live strategy runner, exact-decimal risk limits and persisted ambiguous
  execution identities; a successful paper session does not qualify a live account.
- Pure market making, fixed grid, Avellaneda and inventory-skewed strategies with presets.
- Public doctor checks and an explicit optional private-read check; no signing permission claim.
- Public-book recording/replay, private rotated run diagnostics and an offline support report.
- Retail tutorial, risk FAQ and versioned GitHub wheel/source archives with embedded commit identity.

## [0.1.0]

- Initial Python One API client, offline fake server, paper simulator and strategy foundation.

[Unreleased]: https://github.com/Glyde-trade/glyde-market-maker/compare/v0.6.0...master
[0.6.0]: https://github.com/Glyde-trade/glyde-market-maker/releases/tag/v0.6.0
[0.5.0]: https://github.com/Glyde-trade/glyde-market-maker/releases/tag/v0.5.0
[0.4.0]: https://github.com/Glyde-trade/glyde-market-maker/releases/tag/v0.4.0
[0.3.0]: https://github.com/Glyde-trade/glyde-market-maker/releases/tag/v0.3.0
[0.2.0]: https://github.com/Glyde-trade/glyde-market-maker/releases/tag/v0.2.0
[0.1.0]: https://github.com/Glyde-trade/glyde-market-maker/releases/tag/v0.1.0
