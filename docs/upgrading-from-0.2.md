# Upgrading from 0.2

v0.3.0 is being prepared. Until its release is published, the features below require
a reviewed source checkout. Keep using the published v0.2.0 wheel if you need that exact
version; do not assume an unchanged package version on master implies unchanged behavior.

## Stop before replacing the installation

On v0.2.0, use Ctrl-C in the original terminal, wait for cleanup and verify the account
at the venue. Its wheel has no `stop` command or local runner coordination, so a new
terminal's updated `stop` command cannot prove the older process has exited. Do not run
old and new bots together. Positions are never automatically flattened by cleanup.

Keep a private backup of your YAML, env file and entire `.glyde-mm` directory while the bot
is stopped. Preserve daily-loss state and original unresolved order/cancel identities.
An upgrade is not reconciliation; never delete those records to bypass an unknown outcome.
Use the same config location and state volume after upgrading. Do not use Compose
`down --volumes` or silently switch projects, directories or account namespaces.

## Review the changed behavior

- Live startup now prints a summary and requires exact typed-symbol confirmation. Scripts
  without interactive stdin refuse startup. Do not pipe `yes` or an automatic symbol response.
- Existing orders or positions on the selected symbol refuse startup before managed cleanup.
  `--i-understand` is a deliberate exposure override, not a waiver of the other guards.
- `glyde-mm stop <config>` works with the updated local runner and the same state directory.
  It cancels all orders on the configured symbol, including manual orders, but leaves positions
  and other symbols unchanged. Keep a dedicated account and inspect any failure report.
- `risk.max_orders_per_minute` defaults to `60`; it must be an unquoted integer in `1..6000`.
  It counts attempted order submissions, including multi-order batches. A rate refusal stops
  quoting rather than automatically raising the budget or resending ambiguous submissions.
- `strategy_config.refresh_tolerance_pct` is optional. Its unit is percentage points:
  `"0.1"` means **0.1%**. `max_order_age` defaults to 300 seconds and still retires old quotes.
  Keep tolerance disabled until you have reviewed how it affects your strategy's refreshes.
- Paper streaming now consumes the public trade tape, with approximate queue-ahead and
  partial fills. Earlier paper results and streaming/replayed-book results are not directly
  comparable. Book-only recordings contain no real historical trade tape or external-reference
  history; neither missing input is invented. See [paper fills](paper-fills.md) and
  [replay limits](replay.md).

Existing single-symbol YAML remains the starting point. Multi-symbol configuration is still
pending; do not add a speculative `markets` array until the merged implementation and example
are included in the v0.3.0 release notes.

## Recheck before live

Install the chosen reviewed source revision or the published release wheel, then run
`glyde-mm --version`. Read the [changelog](../CHANGELOG.md), run ordinary doctor and paper,
inspect the report and verify size/notional limits against the current market. The optional
`doctor <config> --private` checks reads only, not signing or order authority.

Use the [VPS recipes](vps.md) if you need a service, persistent state or log rotation.
They default to paper and disable automatic restart. Keep credentials in the private
`0600` env file, never YAML. Only start live deliberately after confirming the account,
small limits, current venue rules and retained risk state.
