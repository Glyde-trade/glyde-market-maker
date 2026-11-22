# Troubleshooting

Start with the terminal's refusal reason and the exact config you ran. Keep the
config and `.glyde-mm` state directory together. Do not delete risk state or raise
limits just to make a refusal disappear.

## Before trying live

```sh
glyde-mm --version
glyde-mm doctor glyde-mm.yaml
```

Doctor checks configuration, key environment-variable presence, public metadata
and a fresh book. It never prints key values. For an explicit live config,
`glyde-mm doctor live.yaml --private` additionally tests account/open-order reads.
Neither check proves signing permission or guarantees that a live order will work.

| Symptom | What to check |
| --- | --- |
| Missing key or private-read refusal | Set the configured environment variable in the shell or private env file used by this process. Check account and namespace permissions. Never paste keys into YAML or a support issue. |
| Symbol, precision or size refusal | Check the exact metadata symbol and quantity step. `order_amount` is base units, not USDC. Rounding can produce zero; a cap can also be below a venue minimum. Do not automatically increase caps. |
| Stale book, reference or account | A stale book pauses that market and resumes on fresh data; it stops the run only after `risk.max_stale_pause_seconds`. A stale reference or account stops quoting: restore the relevant connection. Receiving an old snapshot again does not make it fresh. Increasing freshness limits does not repair the source. |
| `stream disconnected (PROJECTION_UNAVAILABLE, retry after Ns)` or `connect N attempt failed (...)` | The server deferred a subscription. The bot retires quotes, waits at least the server's `retryAfter`, resubscribes and reconciles before quoting again; no action is needed unless it repeats for minutes. A refusal without `retryAfter` still stops the run: check the read key and subscription permissions. |
| Existing orders or position at live startup | Inspect the selected symbols at the venue. Prefer a dedicated account. `--i-understand` accepts existing exposure; it does not bypass risk checks or typed confirmation. |
| No paper fills | Passive orders need qualifying later public trade prints. Paper queue priority is approximate; no fills can be a valid result. See [paper fills](paper-fills.md). |
| Grid or adaptive strategy stays quiet | A grid retires quotes outside its band; adaptive strategies need source-time warmup. Inspect configured bands and the terminal's risk reason before changing anything. |
| Paper ping-pong refuses live or replay | Ping-pong currently needs identified streaming paper fills. Live and book-only replay deliberately refuse it. See [strategy capabilities](strategies.md). |
| Chat alerts are absent | Alerts are optional and best effort, with bounded retries. Check the configured env-variable name privately. Local reports and authoritative venue state remain necessary; a missing alert is not proof of safety. |

## Unknown order or cancellation outcome

Stop issuing new orders. A timeout can mean the venue accepted the original action
but its response was lost. Preserve the original order/client IDs and persisted
execution intent, then check those exact identities at the venue. Do not resend
with a new ID, clear the state file or treat an empty/incomplete read as proof that
nothing happened.

To request local shutdown and cancellation for the configured symbols:

```sh
glyde-mm stop path/to/live.yaml
```

Use the same config location and state directory as the runner. Stop coordinates
an updated local runner and verifies cancellation, but it can fail if reads or
receipts are unavailable. It may cancel other orders on those symbols. It never
closes positions; inspect remaining exposure separately at the venue.

## Read the right report

```sh
glyde-mm report path/to/.glyde-mm/logs
```

The diagnostic report identifies the build, run outcome and retained counters.
`incomplete` means a retained end record is missing; it does not establish whether
orders remain. `history_truncated` means rotation removed earlier history. Neither
is a clean-exit claim. A dashboard is a local snapshot, not current venue proof.
Wizard sessions keep their logs and final report in their printed session path.

For an offline execution comparison, use:

```sh
glyde-mm reconcile --session-file /private/path/session.jsonl
```

This requires a complete execution capture with original acknowledged orders,
public tape and private fills. Ordinary diagnostic logs are insufficient. Missing
fees are unavailable, not zero; paper-only fills are model discrepancies, not proof
of missed venue fills. See the [capture contract](reconciliation.md).

## When to stop and ask for help

Stop after an unknown outcome, daily-loss breach, repeated stale/private-read
refusal, failed cleanup or unexplained live exposure. Verify the venue before
restarting. Daily-loss controls do not flatten inventory or guarantee a maximum
loss. Keep the original state directory, version/commit, config with sensitive
fields removed, refusal text and relevant report status.

Never publish private keys, read keys, webhook URLs, env files, signatures or
authentication headers. Review logs and redact account/order identifiers before
sharing. For more context, see the [risk FAQ](risk-limits.md) and
[first live session guide](first-live-session.md).
