# Changelog

GitHub version tags/releases are the publication record; Unreleased changes require current source.

## [Unreleased]

- Cleanup only touches the bot's own orders. Every order carries a client order id tagged
  `0x676d6d31` ("gmm1"); startup, reconnect, stale-pause, risk-halt, shutdown and `stop`
  sweeps cancel only tagged or self-recorded orders, so manual orders in the same account stay
  open (they still count toward risk limits).

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
