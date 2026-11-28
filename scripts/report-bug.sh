#!/usr/bin/env bash
# File a bot bug as a GitHub issue on the public glyde-market-maker repo.
#
#   bash scripts/report-bug.sh [bot-dir]      # default: the current directory
#
# Collects, from the bot folder made by scripts/founder-setup.sh: glyde-mm version, the preset and
# its non-secret settings, the last run's outcome and recent events, the tail of run.log, and
# Glyde's health right now. Addresses and anything key-shaped are redacted. Shows you the issue,
# searches for an existing one with the same failure, and posts only after you say yes.
# Uses the GitHub CLI (gh) when it is logged in; otherwise saves the text for you to paste.
set -euo pipefail

REPO="Glyde-trade/glyde-market-maker"
API="https://api.glyde.trade/v1"
dir="$(cd "${1:-.}" && pwd)"
[[ -f "$dir/live.yaml" ]] || { echo "No live.yaml in $dir. Pass your bot folder, e.g. ~/glyde-mm/tight-ladder" >&2; exit 1; }
export PATH="$HOME/.local/bin:$PATH"

redact() {
  sed -E -e 's/0x[0-9a-fA-F]{64}/0x<redacted-64>/g' \
         -e 's/(^|[^0-9a-fA-Fx])[0-9a-fA-F]{64}([^0-9a-fA-F]|$)/\1<redacted-64>\2/g' \
         -e 's/0x[0-9a-fA-F]{40}/0x<redacted-address>/g' \
         -e 's/\x1b\[[0-9;]*[A-Za-z]//g' -e 's/\r//g'
}

preset="$(sed -n 's/^# Founder preset: //p' "$dir/live.yaml" | head -1 || true)"
preset="${preset:-$(basename "$dir")}"
version="$(glyde-mm --version 2>/dev/null || echo unknown)"
run_dir="$(ls -td "$dir"/.glyde-mm/logs/run-* 2>/dev/null | head -1 || true)"
outcome="unknown"
events=""
if [[ -n "$run_dir" && -f "$run_dir/events.jsonl" ]]; then
  outcome="$(grep '"run_ended"' "$run_dir/events.jsonl" | tail -1 | sed -n 's/.*"outcome":"\([^"]*\)".*/\1/p' || true)"
  outcome="${outcome:-still running or incomplete}"
  events="$(grep -v '"progress"' "$run_dir/events.jsonl" | tail -25 | cut -c1-400 | redact || true)"
  last_progress="$(grep '"progress"' "$run_dir/events.jsonl" | tail -1 | cut -c1-300 | redact || true)"
fi
error_line="$(grep -a -E 'Unable to run|RiskViolation|OneApiError|Error' "$dir/run.log" 2>/dev/null | tail -1 | redact || true)"
log_tail="$(tail -n 40 "$dir/run.log" 2>/dev/null | redact || echo '(no run.log; start the bot with start.sh to record one)')"
health="$(curl -fsS -m 10 "$API/health" 2>/dev/null | cut -c1-400 || echo unreachable)"
settings="$(sed -n '/^strategy:/,$p' "$dir/live.yaml" | redact || true)"
summary=""
if [[ -n "$run_dir" ]]; then summary="$(glyde-mm report "$run_dir" 2>&1 | head -60 | redact || true)"; fi

signature="$(printf '%s' "${error_line:-$outcome}" | sed -E 's/.*(Unable to run: |Error: )//' | cut -c1-90 || true)"
title="[bot-stop] ${preset}: ${signature:-$outcome}"

echo "In one or two sentences, what did you see? (e.g. 'bot stopped 10 min in', 'book empty'). End with Enter:"
read -r what_happened

body_file="$(mktemp "${TMPDIR:-/tmp}/glyde-bug.XXXXXX")"
{
  echo "**What happened (reporter):** ${what_happened:-not given}"
  echo
  echo "| | |"; echo "|---|---|"
  echo "| Preset | \`$preset\` |"
  echo "| Version | \`$version\` |"
  echo "| Run outcome | \`$outcome\` |"
  echo "| Reported at | $(date -u +%Y-%m-%dT%H:%M:%SZ) |"
  echo "| OS | $(uname -sr) |"
  echo
  echo "**Last error line**"; echo '```'; echo "${error_line:-none recorded}"; echo '```'
  echo "**Glyde health at report time**"; echo '```json'; echo "$health"; echo '```'
  echo "<details><summary>Recent run events (no keys or addresses)</summary>"; echo; echo '```'
  echo "${events:-none}"; echo "${last_progress:-}"; echo '```'; echo "</details>"
  echo "<details><summary>Terminal tail (run.log, redacted)</summary>"; echo; echo '```'
  echo "$log_tail"; echo '```'; echo "</details>"
  echo "<details><summary>Strategy and risk settings</summary>"; echo; echo '```yaml'
  echo "$settings"; echo '```'; echo "</details>"
  if [[ -n "$summary" ]]; then
    echo "<details><summary>glyde-mm report</summary>"; echo; echo '```'; echo "$summary"; echo '```'; echo "</details>"
  fi
  echo
  echo "_Filed with scripts/report-bug.sh_"
} >"$body_file"

echo
echo "================ ISSUE PREVIEW (this repo is PUBLIC) ================"
echo "Title: $title"
echo
cat "$body_file"
echo "======================================================================"

if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  key="$(printf '%s' "$signature" | cut -c1-60)"
  existing="$(gh issue list --repo "$REPO" --state open --search "\"$key\" in:title" --limit 5 \
    --json number,title,url --jq '.[] | "#\(.number) \(.title) \(.url)"' 2>/dev/null || true)"
  if [[ -n "$existing" ]]; then
    echo; echo "Open issues that look like the same failure:"; echo "$existing"
    read -r -p "Add this as a comment on one of them instead? Enter its number, or press Enter for a new issue: " dup
    if [[ "$dup" =~ ^[0-9]+$ ]]; then
      read -r -p "Post this report as a public comment on #$dup? [y/N] " ok
      [[ "$ok" =~ ^[yY]$ ]] && gh issue comment "$dup" --repo "$REPO" --body-file "$body_file" && exit 0
      echo "Not posted. Saved at $body_file"; exit 0
    fi
  fi
  read -r -p "Post this as a new public issue on $REPO? [y/N] " ok
  if [[ "$ok" =~ ^[yY]$ ]]; then
    gh issue create --repo "$REPO" --title "$title" --body-file "$body_file"
  else
    echo "Not posted. Saved at $body_file"
  fi
else
  echo
  echo "GitHub CLI isn't logged in, so nothing was posted."
  echo "Saved at: $body_file"
  echo "Open https://github.com/$REPO/issues/new , use the title above and paste the file's contents."
fi
