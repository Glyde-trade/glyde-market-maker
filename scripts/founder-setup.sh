#!/usr/bin/env bash
# One-command setup for a founder-preset live bot on Glyde's BTC book.
#
#   bash scripts/founder-setup.sh [preset]
#
# Installs glyde-mm, asks for your public owner/vault addresses and your API (agent) wallet key,
# and writes ~/glyde-mm/<preset>/ with live.yaml, start.sh and stop.sh. The key is stored in
# ~/glyde-mm/<preset>/.agent-key (chmod 600) and is never printed or written into YAML.
# Nothing trades until you run start.sh and type the symbol to confirm.
set -euo pipefail

REPO_URL="https://github.com/Glyde-trade/glyde-market-maker.git"
RAW_URL="https://raw.githubusercontent.com/Glyde-trade/glyde-market-maker/master/configs/founders"
API="https://api.glyde.trade/v1"
PRESETS=(tight-ladder hl-anchored adaptive-vol inventory-shy grid-wall deep-book)
DESCRIPTIONS=(
  "Inner book: 20 small levels per side, \$3 apart, from ~\$18 off mid (fixed spreads)"
  "Near book: 10 levels per side centred on Hyperliquid's live price, not Glyde's"
  "Mid book: Avellaneda spreads that widen and tighten with volatility"
  "Mid book: volatility pricing that shrinks orders on the side that grows its position"
  "Wide book: 24 static grid levels across +/-2.5% around today's price"
  "Outer book: 12 larger levels per side from 0.8% to 1.7% off mid"
)

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

# --- 1. Pick a preset ---------------------------------------------------------------------------
preset="${1:-}"
if [[ -z "$preset" ]]; then
  say "Pick a preset. Each founder should run a different one so the book gets varied logic."
  for i in "${!PRESETS[@]}"; do printf '  %d) %-14s %s\n' $((i + 1)) "${PRESETS[$i]}" "${DESCRIPTIONS[$i]}"; done
  read -r -p "Number: " choice
  [[ "$choice" =~ ^[1-6]$ ]] || die "choose a number from 1 to ${#PRESETS[@]}"
  preset="${PRESETS[$((choice - 1))]}"
fi
[[ " ${PRESETS[*]} " == *" $preset "* ]] || die "unknown preset '$preset' (choose: ${PRESETS[*]})"
dir="$HOME/glyde-mm/$preset"
say "Setting up '$preset' in $dir"

# --- 2. Install glyde-mm ------------------------------------------------------------------------
if ! command -v pipx >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    say "Installing pipx with Homebrew"
    brew install pipx
  else
    die "pipx is required. Install it (https://pipx.pypa.io/stable/installation/) and re-run."
  fi
fi
pipx ensurepath >/dev/null 2>&1 || true
export PATH="$HOME/.local/bin:$PATH"
say "Installing the latest glyde-mm"
pipx install --force "git+$REPO_URL" >/dev/null
glyde-mm --version

# --- 3. Account details -------------------------------------------------------------------------
say "Your Glyde account (public addresses, safe to paste)"
read -r -p "Owner address (the wallet you log in to Glyde with): " owner
[[ "$owner" =~ ^0x[0-9a-fA-F]{40}$ ]] || die "owner must be 0x followed by 40 hex characters"
read -r -p "Vault address (assigned by Glyde for your API wallet): " vault
[[ "$vault" =~ ^0x[0-9a-fA-F]{40}$ ]] || die "vault must be 0x followed by 40 hex characters"

mkdir -p "$dir"
chmod 700 "$dir"
if [[ -s "$dir/.agent-key" ]]; then
  say "Keeping the API wallet key already saved in $dir/.agent-key"
else
  say "API (agent) wallet private key. Typing is hidden; it is saved only to $dir/.agent-key"
  read -r -s -p "Agent private key: " key
  echo
  [[ "$key" =~ ^(0x)?[0-9a-fA-F]{64}$ ]] || die "the key must be 64 hex characters (optionally 0x-prefixed)"
  (umask 177 && printf '%s' "$key" >"$dir/.agent-key")
  unset key
fi

# --- 4. Config ----------------------------------------------------------------------------------
template="$(cd "$(dirname "$0")/.." 2>/dev/null && pwd)/configs/founders/$preset.yaml"
if [[ -f "$template" ]]; then
  cp "$template" "$dir/live.yaml.template"
else
  curl -fsSL "$RAW_URL/$preset.yaml" -o "$dir/live.yaml.template"
fi
sed -e "s/REPLACE_WITH_YOUR_PUBLIC_GLYDE_ACCOUNT_ADDRESS/$owner/" \
    -e "s/REPLACE_WITH_YOUR_ASSIGNED_PUBLIC_VAULT_ADDRESS/$vault/" \
    "$dir/live.yaml.template" >"$dir/live.yaml"
rm -f "$dir/live.yaml.template"

if grep -q REPLACE_LOWER_PRICE "$dir/live.yaml"; then
  mid=$(curl -fsS -m 10 -X POST "$API/info" -H 'content-type: application/json' \
    -d '{"type":"allMids"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["BTC"])')
  lower=$(python3 -c "print(int(float('$mid') * 0.975))")
  upper=$(python3 -c "print(int(float('$mid') * 1.025))")
  sed -i.bak -e "s/REPLACE_LOWER_PRICE/$lower/" -e "s/REPLACE_UPPER_PRICE/$upper/" "$dir/live.yaml"
  rm -f "$dir/live.yaml.bak"
  say "Grid band set to $lower - $upper around BTC $mid. Re-run this script to re-centre it."
fi

# --- 5. Start/stop scripts ----------------------------------------------------------------------
cat >"$dir/start.sh" <<EOF
#!/usr/bin/env bash
# Preflight then start immediately (a preflight PASS expires after 5 minutes).
# When asked, type BTC in CAPITALS to confirm live trading. Ctrl-C stops and cancels the bot's orders.
# The terminal session is also recorded to run.log so scripts/report-bug.sh can attach the error.
set -euo pipefail
export PATH="\$HOME/.local/bin:\$PATH"
cd "$dir"
export GLYDE_API_PRIVATE_KEY="\$(cat .agent-key)"
printf '\n===== start %s =====\n' "\$(date -u +%Y-%m-%dT%H:%M:%SZ)" >>run.log
cmd='glyde-mm preflight live.yaml && glyde-mm run live.yaml --i-understand'
if [[ "\$(uname)" == Darwin ]]; then
  script -q -a run.log bash -c "\$cmd"
else
  script -q -a -c "\$cmd" run.log
fi
EOF
cat >"$dir/stop.sh" <<EOF
#!/usr/bin/env bash
# Stop the bot from another terminal; it cancels its own tagged orders.
set -euo pipefail
export PATH="\$HOME/.local/bin:\$PATH"
cd "$dir"
export GLYDE_API_PRIVATE_KEY="\$(cat .agent-key)"
glyde-mm stop live.yaml
EOF
chmod 700 "$dir/start.sh" "$dir/stop.sh"

# --- 6. Check -----------------------------------------------------------------------------------
say "Checking the config against Glyde (read-only)"
if (cd "$dir" && GLYDE_API_PRIVATE_KEY="$(cat .agent-key)" glyde-mm preflight live.yaml); then
  say "Ready. Start the bot with:"
  printf '  %s/start.sh\n' "$dir"
  printf '\nType BTC (capitals) when asked. Stop with Ctrl-C, or %s/stop.sh from another terminal.\n' "$dir"
else
  say "Preflight did not pass. Read the message above; docs/founders.md lists the usual causes."
  exit 1
fi
