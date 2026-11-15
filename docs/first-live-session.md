# Guided first live session

This current-source command is for a deliberate, **single-symbol** first session, not
unattended trading. It requires a reviewed config with `mode: live`, exact owner/vault
addresses and credentials already present through your private environment file.
Never put key values in YAML. Doctor/paper success is not proof of trading authorization.

```sh
glyde-mm live-wizard live-small.yaml
```

The wizard reads fresh public metadata/book data and derives the **Conservative** preset
for the configured strategy. Custom strategy settings are replaced, not merged. The proposed
settings are saved privately and printed before proceeding. It never raises the supplied risk
caps: per-order and gross exposure are capped at 25 USDC, daily-equity-loss threshold at 5 USDC,
and order attempts at 60/minute; tighter supplied caps remain. The position cap is at most
25 USDC at the sizing snapshot. Prices can move, these are not loss guarantees, and venue
minimums may still refuse the resulting small size. No cap is raised to make an order fit.

1. Run doctor on the **derived** config, including explicitly requested private read checks.
   A failure ends the wizard without placing anything.
2. Run a **120-second paper preview**, displaying the usual paper status/summary. It uses
   exactly the proposed live settings, strategy and account identifiers, with only `mode`
   changed to paper. Orders/balances remain simulated; no trading action or private paper
   account read occurs. Failure, interruption or an early exit prevents live startup.
3. Read the limits again and type the **exact symbol**. EOF, Ctrl-C or a mismatch cancels;
   there is no automatic confirmation or `--i-understand` exposure override here.
4. Start live using that same frozen config. Editing the original YAML during preview
   cannot change what starts. Existing symbol orders/positions, stale data, risk latches
   and unresolved original outcomes still refuse startup or stop quoting.
5. At **30 minutes from live-runner invocation**, set the raw stop event and begin normal
   cancellation/cleanup. Ctrl-C can stop earlier. Cleanup may take additional time and can
   fail while disconnected; the timer does not guarantee venue cancellation or zero loss.

The session writes private `config.yaml`, diagnostic logs and `report.json` under
`.glyde-mm/live-wizard/session-…/` beside your original config. The report distinguishes
refusal, preview cancellation, auto-stop and cleanup failure, and contains no credential
values. Existing account risk/unknown-outcome state stays in the original `.glyde-mm`
directory rather than being reset for the wizard.

Use a dedicated account. Cleanup can cancel manual orders on the selected symbol; it
never flattens positions. If cancellation fails, verify the venue and reconcile the
original IDs; do not delete risk state or automatically rerun. From another terminal,
the [stop command](../README.md#if-something-goes-wrong) with the **original config path**
can coordinate the session. Multi-symbol first sessions are intentionally refused; use
the ordinary reviewed `run` workflow after practicing with multi-symbol paper mode.
