---
name: glyde-bot
description: Set up, start, check, diagnose and bug-report a glyde-mm market-maker bot on Glyde's live BTC book. Use when someone asks to run a Glyde market-maker bot, why their Glyde bot stopped or the book looks empty, or to report a glyde-mm bug.
---

# Running and reporting glyde-mm bots on Glyde

People using this are usually Glyde team members, often not engineers. Explain in plain words,
do the technical work yourself, and never print secret values.

## Hard rules

- **Never handle the agent private key in chat or logs.** It lives in `~/glyde-mm/<preset>/.agent-key`
  (chmod 600). Read it only into `GLYDE_API_PRIVATE_KEY` inside a command; never `cat` it to output.
- **Never automate the live confirmation.** `glyde-mm run` asks the person to type the symbol
  (`BTC`, capitals). Don't pipe or `expect` it. Open the bot in a terminal they can type in.
- **One bot per Glyde account.** Every bot tags orders `gmm1` and cancels all tagged orders on its
  account at startup and reconnect, so two bots on one account cancel each other. Each person runs
  a different preset on their own account.
- **Ask before posting** an issue or comment: the repo is public.

## Set up and start

```bash
bash scripts/founder-setup.sh            # menu of presets; or pass one, e.g. tight-ladder
~/glyde-mm/<preset>/start.sh             # preflight + run; person types BTC within 5 minutes
~/glyde-mm/<preset>/stop.sh              # from another terminal; cancels the bot's own orders
```

Presets (`configs/founders/`): `tight-ladder`, `hl-anchored`, `adaptive-vol`, `inventory-shy`,
`grid-wall`, `deep-book`. Each one uses different logic on a different part of the book; see
`docs/founders.md`. If two people pick the same one, steer the second to another.

A preflight PASS expires after 5 minutes. If the person missed it, just run `start.sh` again.
Lowercase `btc` is refused; same fix.

## Check on a running bot

Everything is under `~/glyde-mm/<preset>/`:

| What | Where |
|---|---|
| Live status and current quotes | `.glyde-mm/logs/run-<id>/dashboard.json` (`status`, `quotes`, `inventory`, `risk_stop`) |
| Event history | `.glyde-mm/logs/run-<id>/events.jsonl` (`strategy_started`, `disconnected`, `reconnected`, `run_ended` + `outcome`) |
| Terminal output | `run.log` (recorded by `start.sh`) |
| Glyde engine health | `curl -s https://api.glyde.trade/v1/health` → `state` READY or PROJECTION_UNAVAILABLE + reason |
| What is really on the book | `POST https://api.glyde.trade/v1/info` `{"type":"openOrders","user":"<owner address>"}` |

`dashboard.json` is what the bot believes; `openOrders` is what Glyde has. When they disagree,
trust Glyde. Use the **owner** address for `openOrders`, not the vault.

## Reading common situations

| You see | Meaning | Do |
|---|---|---|
| `stream disconnected ...; quotes retired, reconnecting` then `reconnected` | Normal drop, bot recovered | Nothing |
| health `PROJECTION_UNAVAILABLE` / `ENGINE_NOT_READY` / `NATIVE_READ_UNAVAILABLE`, book empty | Glyde-side outage; bot waits (up to `engine_halt` seconds) | Wait; report only if it lasts long or the bot doesn't resume after READY |
| `quoting paused (stale BTC)` | No fresh book; pauses until data returns | Nothing unless it persists while health is READY |
| `run_ended` with `transport_error`, `risk_stop`, or `Unable to run: ...` | The bot stopped | Note the error, restart with `start.sh`, then file a bug if it is new |
| Fewer orders on one side | Inventory skew shrank that side under the $10 minimum, or post-only orders would have crossed | Usually fine; if a side stays thin, report with the preset name |
| `order history proves N late-receipt cancel(s) closed` | Startup cleanup of old orders | Nothing |

Order history statuses from Glyde: `canceled` (by the bot), `filled`, and `internalCancel`, which
means Glyde cancelled it for a venue failure or engine halt. That last one is worth a report.

## Report a bug

```bash
bash scripts/report-bug.sh ~/glyde-mm/<preset>
```

It asks the person for one sentence on what they saw, builds a redacted issue (version, preset,
outcome, recent events, `run.log` tail, health, settings), searches open issues with the same error,
and offers to comment on the existing one instead of opening a duplicate. Show the person the
preview and get their yes before it posts. Titles follow `[bot-stop] <preset>: <error>`.

One issue per distinct failure. If the same error is already open, add a comment there with the new
occurrence; don't open another. Glyde outages that recover on their own don't need an issue unless
they keep happening or the bot fails to resume.

If `gh` isn't logged in, the script saves the issue text and prints the new-issue link to paste into.
