#!/usr/bin/env bash
# Install the quantbot daemon on a Raspberry Pi as a systemd service.
#
# Refuses rather than proceeds when the board cannot run this stack. The original Pi Zero and
# Zero W are ARMv6, and pydantic-core is compiled Rust with no armv6l wheels published -- the
# install would silently fall back to building Rust on a single core with 512MB of RAM, which
# takes hours and usually dies with an out-of-memory error partway through. Better to say so in
# one second than to discover it in three hours.
#
# Idempotent: safe to re-run. It does not touch the ledger and it never writes credentials.
#
# Usage:  bash deploy/pi/setup.sh

set -euo pipefail

QUANTBOT_HOME="${QUANTBOT_HOME:-$HOME/quantbot}"
QUANTBOT_DATA="${QUANTBOT_DATA:-$HOME/quantbot-data}"
ENV_FILE="/etc/quantbot/quantbot.env"
SERVICE_FILE="/etc/systemd/system/quantbot.service"
REPO_URL="${REPO_URL:-https://github.com/jfhutchi/project_elrond.git}"
BRANCH="${BRANCH:-elrond-v0.2}"

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
fail() { printf '\n\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

say "1/7  Checking the board"
ARCH="$(uname -m)"
MODEL="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo unknown)"
echo "     arch:  $ARCH"
echo "     model: $MODEL"

case "$ARCH" in
  aarch64|arm64)
    echo "     OK - 64-bit ARM, prebuilt wheels available for every dependency."
    ;;
  armv6l)
    fail "This is an ARMv6 board (original Pi Zero / Zero W).
     pydantic-core publishes no armv6l wheel, so pip would build Rust from source on a
     single 1GHz core with 512MB RAM. That is not a supported path.

     Options:
       - use a Pi Zero 2 W, Pi 3, Pi 4, or Pi 5 (all aarch64)
       - or run a 64-bit OS on a board that supports it: Raspberry Pi OS (64-bit)"
    ;;
  armv7l)
    fail "This is a 32-bit ARMv7 userland. The board may well be 64-bit capable --
     check with: getconf LONG_BIT ; and reflash Raspberry Pi OS (64-bit) if so.
     32-bit ARM wheel coverage for this dependency set is not reliable enough to trust
     an unattended trading process to."
    ;;
  *)
    fail "Unrecognised architecture '$ARCH'. Stopping rather than guessing."
    ;;
esac

say "2/7  Checking memory and clock"
MEM_MB="$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo)"
echo "     RAM: ${MEM_MB}MB"
[ "$MEM_MB" -lt 400 ] && echo "     WARNING: under 400MB. Add swap before running unattended."
if command -v timedatectl >/dev/null 2>&1; then
  SYNC="$(timedatectl show -p NTPSynchronized --value 2>/dev/null || echo unknown)"
  echo "     NTP synchronised: $SYNC"
  [ "$SYNC" != "yes" ] && echo "     WARNING: the Pi has no RTC. Session boundaries and point-in-time
     correctness depend on the clock. Fix NTP before trusting a run."
fi

say "3/7  Installing system packages"
sudo apt-get update -qq
sudo apt-get install -y -qq git curl ca-certificates

say "4/7  Installing uv"
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
uv --version

say "5/7  Fetching the code (branch: $BRANCH)"
if [ -d "$QUANTBOT_HOME/.git" ]; then
  git -C "$QUANTBOT_HOME" fetch origin --prune
  git -C "$QUANTBOT_HOME" checkout "$BRANCH"
  git -C "$QUANTBOT_HOME" pull --ff-only origin "$BRANCH"
else
  git clone --branch "$BRANCH" "$REPO_URL" "$QUANTBOT_HOME"
fi
cd "$QUANTBOT_HOME"
uv sync
echo "     commit: $(git -C "$QUANTBOT_HOME" rev-parse --short HEAD)"

say "6/7  Preparing the credential file"
sudo mkdir -p /etc/quantbot "$(dirname "$ENV_FILE")"
mkdir -p "$QUANTBOT_DATA"
if [ ! -f "$ENV_FILE" ]; then
  sudo tee "$ENV_FILE" >/dev/null <<EOF
# Populate these by hand, then: sudo chmod 600 $ENV_FILE
# Never commit this file. Paper credentials only -- live trading stays disabled.
ALPACA_PAPER_API_KEY=
ALPACA_PAPER_API_SECRET=

QUANTBOT_CONFIG=$QUANTBOT_HOME/config/strategy-v1-2.yaml
QUANTBOT_DB_PATH=$QUANTBOT_DATA/quantbot.db
QUANTBOT_LOCK_PATH=$QUANTBOT_DATA/quantbot.lock
QUANTBOT_REPORTS_DIR=$QUANTBOT_DATA/reports
QUANTBOT_MARKET_DATA_FEED=iex
QUANTBOT_MARKET_DATA_ADJUSTMENT=all
QUANTBOT_MAX_DATA_AGE_SECONDS=86400
EOF
  sudo chmod 600 "$ENV_FILE"
  echo "     Created $ENV_FILE -- fill in the two Alpaca values before starting."
else
  echo "     $ENV_FILE already exists, left untouched."
fi

say "7/7  Installing the systemd service"
sudo cp "$QUANTBOT_HOME/deploy/pi/quantbot.service" "$SERVICE_FILE"
sudo sed -i "s|__QUANTBOT_HOME__|$QUANTBOT_HOME|g; s|__QUANTBOT_DATA__|$QUANTBOT_DATA|g; s|__USER__|$USER|g" "$SERVICE_FILE"
sudo systemctl daemon-reload
sudo systemctl enable quantbot.service

cat <<EOF

Installed, not started.

Remaining steps, in order:

  1. Put your Alpaca paper credentials in $ENV_FILE
  2. Copy the ledger across from the Windows machine so the qualification window
     carries over rather than restarting. From the Windows box:

       scp "E:/.../quantbot.db" pi@<host>:$QUANTBOT_DATA/quantbot.db

     Copy it while the daemon on Windows is STOPPED, or you may capture a
     partial WAL. Stop it first: Stop-ScheduledTask -TaskName QuantBotDaemon

  3. Verify before starting anything:

       cd $QUANTBOT_HOME && uv run quantbot status
       cd $QUANTBOT_HOME && uv run quantbot reconcile

  4. Start it:

       sudo systemctl start quantbot
       journalctl -u quantbot -f

Do not run both hosts against the same paper account at once. Disable the
Windows task before starting here:

  Disable-ScheduledTask -TaskName QuantBotDaemon

EOF
