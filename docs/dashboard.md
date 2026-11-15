# Local read-only dashboard

Start a bot normally. It prints the path of its per-run display snapshot:

```text
Dashboard state: .glyde-mm/logs/run-<id>/dashboard.json
```

In a second terminal, use that exact path:

```sh
glyde-mm dashboard --state .glyde-mm/logs/run-<id>/dashboard.json --port 8080
```

Open `http://127.0.0.1:8080`. Ctrl-C stops the dashboard; it does not stop the bot.
The service binds only `127.0.0.1`. It deliberately has no authentication, trading
client, mutation routes, reset buttons or access to environment credentials. Do
not expose it through a tunnel or reverse proxy. Any user with access to your
browser or local process can see its displayed account observations.

The bot atomically replaces a private 0600 JSON snapshot in its private run-log
directory every two seconds and after paper cleanup. The dashboard reads that
file; it never calls an exchange, reads a config/credential or changes risk state.
The browser refreshes this local display once per second. A missing, malformed,
symlinked, oversized or publicly readable file is unavailable. It serves only the
explicit display fields, never arbitrary JSON fields or nearby files.

Paper mode shows actual simulator open quotes, the latest 100 simulated fills,
total fill count, signed inventory, equity and PnL from initial paper balance.
Live mode shows cached account inventory/equity and tracked order intents. Those
intents do not prove which orders remain open at the venue. Its equity change is
relative to the daily risk baseline, can include deposits/withdrawals, and is not
a trading-profit calculation. The current live runner supplies no private fill
history to this view: **unavailable is not zero fills**.

A snapshot older than five seconds, with a future timestamp, or belonging to a
finished run is marked **STALE / FINISHED**. Risk reasons are observations, not a
permission to resume. Missing authority cannot be repaired by opening this page.
After stopping the bot, choose a new run's snapshot explicitly for the next run.
Do not delete or edit trading-risk files to clear a warning. The existing `stop`
command and risk-managed bot cleanup remain responsible for cancellation.
