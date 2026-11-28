# Founder guide: run a market-maker bot on Glyde

This is the short path for Glyde team members. You need a Mac or Linux machine, a funded Glyde
account, and about 15 minutes. You don't need to know Python.

## Why everyone runs something different

The goal is a Glyde BTC book that looks full and alive at every depth, and real traffic that
exercises Glyde. Six presets each use different logic on a different part of the book. **Pick one
nobody else on the team is running** (ask in the team channel), so the book gets variety instead
of six copies of one bot.

| Preset | Logic | Book area | Resting size (approx.) |
|---|---|---|---|
| `tight-ladder` | Pure market making: fixed spreads, 12 small levels, gentle inventory skew | Inner, ~0.03%–0.12% from mid, ~$5 apart | ~$500 |
| `hl-anchored` | Pure market making centred on **Hyperliquid's** live mid; stops if Glyde and HL disagree by over 1% | Near, 0.08%–0.22% | ~$350 |
| `adaptive-vol` | Avellaneda–Stoikov: spread and centre follow measured volatility and inventory | Mid, from 0.15%, wider in fast markets | ~$215 |
| `inventory-shy` | Avellaneda pricing that shrinks orders on the side that would grow its position | Mid, from 0.3% | ~$285 |
| `grid-wall` | Static grid that doesn't chase the price | Wide, 16 levels across ±2.5% of the price at setup | ~$285 |
| `deep-book` | Pure market making, larger size, slow refresh, rarely fills | Outer, 0.8%–1.64% | ~$440 |

Sizes are at BTC ≈ $59k and include both sides. Glyde allows each account 30 order placements and
20 cancels per minute, so each preset keeps 12–24 orders that live for minutes, not seconds; the
book fills up because each founder adds their own account's budget. Glyde needs margin for resting orders, so keep a
few hundred dollars more than the resting size in the account. Losses are expected to be small;
each preset has a $15 daily-loss stop and a position cap.

## 1. Get your Glyde account details

You need three things from Glyde:

1. **Owner address**: the wallet you log in to Glyde with (starts with `0x`).
2. **API wallet (agent) key**: create and approve a dedicated API wallet for the bot in Glyde, with
   an expiry, and save its private key somewhere safe. This key can trade but can't withdraw.
3. **Vault address**: the address Glyde assigns to your account for API trading (also `0x…`).

Deposit funds into the account before starting.

## 2. Set up

```sh
git clone https://github.com/Glyde-trade/glyde-market-maker.git
cd glyde-market-maker
bash scripts/founder-setup.sh
```

The script:

- installs `glyde-mm` with `pipx` (installing pipx through Homebrew if needed),
- asks which preset you want,
- asks for your owner and vault addresses,
- asks for the API wallet key with typing hidden, and saves it to `~/glyde-mm/<preset>/.agent-key`
  (only your user can read it),
- writes `~/glyde-mm/<preset>/live.yaml`, `start.sh` and `stop.sh`,
- runs a read-only preflight against Glyde and says whether you're ready.

For `grid-wall` it centres the grid on the current price. Re-run the script any time to re-centre
the grid, change preset, or pick up the latest version.

## 3. Start, check, stop

```sh
~/glyde-mm/<preset>/start.sh
```

When it says `Type BTC to confirm LIVE trading:`, type **BTC in capitals** and press Enter within
5 minutes. If you miss the window or type lowercase, run `start.sh` again.

Leave that terminal open; the bot runs in it. To stop: **Ctrl-C** there, or run
`~/glyde-mm/<preset>/stop.sh` from another terminal. Either way it cancels its own orders. It does
not close your position.

Open Glyde, go to BTC and choose the **Glyde** order book tab. Your levels should appear within a
minute (`adaptive-vol` and `inventory-shy` warm up for about a minute first).

## 4. What the bot's messages mean

| Message | Meaning |
|---|---|
| `strategy started with shared account risk limits` | Running |
| `stream disconnected (...); quotes retired, reconnecting` then `reconnected` | A normal drop; it recovered by itself |
| `quoting paused (stale BTC)` | No fresh prices for a moment; it resumes on its own |
| `ENGINE_NOT_READY`, `PROJECTION_UNAVAILABLE`, `NATIVE_READ_UNAVAILABLE` | Glyde itself is unavailable. The bot pulls its orders and waits (up to 30 minutes) |
| `order history proves N late-receipt cancel(s) closed` | Startup tidy-up of earlier orders |
| `Unable to run: ...` and your shell prompt returns | The bot stopped. Run `start.sh` again, then report it if it's new (below) |

If your book side looks thinner than expected, the usual reasons are: Glyde was briefly
unavailable, the market moved fast and fills made the bot re-place its levels, or inventory skew
shrank some orders below Glyde's $10 minimum.

## 5. Report a bug

```sh
bash scripts/report-bug.sh ~/glyde-mm/<preset>
```

Type one sentence about what you saw. The script shows you exactly what it will post (addresses and
keys are redacted), checks whether the same error already has an open issue, and asks before posting
to this public repo. Add to an existing issue rather than opening a duplicate. Glyde outages that
recover on their own don't need a report unless they keep happening.

If you use Claude Code, open it in the repo folder and ask it to report the bug; the bundled
[`glyde-bot` skill](../.claude/skills/glyde-bot/SKILL.md) does the same steps.

## When preflight doesn't pass

| Preflight says | Fix |
|---|---|
| `FIX ... one_side` or `max_position` | The ladder is bigger than the position cap. Lower `order_amount` or raise `risk.max_position` in `live.yaml` |
| `FIX ... max_order_notional` | The biggest level is over the per-order cap at today's price. Raise `risk.max_order_notional` |
| Key or account errors | Check the owner and vault addresses, and that the API wallet is approved and not expired |
| `PROJECTION_UNAVAILABLE` / `ENGINE_NOT_READY` | Glyde is down for the moment. Try again in a few minutes |

More detail: [troubleshooting](troubleshooting.md), [risk limits](risk-limits.md) and the
[strategy and config reference](strategies.md).
