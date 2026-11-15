# Changelog

GitHub version tags/releases are the publication record; Unreleased changes require current source.

## [Unreleased]

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

[Unreleased]: https://github.com/Glyde-trade/glyde-market-maker/compare/v0.3.0...master
[0.3.0]: https://github.com/Glyde-trade/glyde-market-maker/releases/tag/v0.3.0
[0.2.0]: https://github.com/Glyde-trade/glyde-market-maker/releases/tag/v0.2.0
[0.1.0]: https://github.com/Glyde-trade/glyde-market-maker/releases/tag/v0.1.0
