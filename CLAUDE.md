# glyde-market-maker: agent instructions

This repo is the Glyde team's market-maker bots and the team's bug channel for Glyde testing. Most
people using it are not engineers. Start with [README.md](README.md) and [docs/founders.md](docs/founders.md);
the [`glyde-bot` skill](.claude/skills/glyde-bot/SKILL.md) covers running, checking and reporting bots.

## How to reply

People won't read long monologues. Keep replies short, and end every reply with these two parts, in
this order:

1. **Your actions**: a numbered list of exactly what the person must do (type BTC in a tab, close
   tabs, approve something). Write "None" if there's nothing.
2. **TL;DR**: one to three plain sentences on what happened and where things stand.

## Report everything as issues on this repo

- Bugs, Glyde behavior the bots run into, and requests or questions for Glyde's engineers go in
  **GitHub issues on Glyde-trade/glyde-market-maker**. Don't route them through Slack or DMs, and
  don't use the private Glyde repo. Glyde's engineering agents poll these issues and follow up.
- Open them directly; you don't need to ask first. One issue per distinct problem: search open
  issues and comment on a match instead of opening a duplicate. Use `bash scripts/report-bug.sh
  <bot dir>` for bot stops; titles follow `[bot-stop] <preset>: <error>`.
- **This repo is public.** Never post keys, read credentials, signed requests, account or vault
  addresses, balances, or private Glyde source. Redact before posting.

## Hard rules for bots

- Never print or paste an agent private key. Never automate the live confirmation (the person types
  the symbol). One bot per Glyde account.
