#!/usr/bin/env bash
# Install the quantbot daemon on a Raspberry Pi as a systemd service.
#
# Handles two very different boards:
#
#   aarch64  (Zero 2 W, Pi 3/4/5)  -- uv, prebuilt wheels from PyPI.
#   armv6l   (Zero, Zero W, Pi 1)  -- system Python 3.11 + venv + pip, wheels from piwheels.
#
# ARMv6 needs the second path because PyPI publishes no armv6l wheel for pydantic-core, which is
# compiled Rust. Building it from source on a single 1GHz core with 512MB of RAM takes hours and
# usually ends in an OOM kill. piwheels.org exists precisely for this and does publish armv6l
# wheels for it, so the fix is to use that index rather than to compile. uv is also Rust and its
# armv6 support is not something to bet an unattended process on, so ARMv6 uses pip.
#
# ARMv6 needs Python >=3.11, so Raspberry Pi OS Bookworm (Debian 12, Python 3.11) or newer.
# Verified working on Raspbian 13 (trixie) with Python 3.13.5 on a Pi Zero W Rev 1.1: piwheels
# serves pydantic_core-2.46.4-cp313-cp313-linux_armv6l.whl, so nothing is compiled. Bullseye
# (3.9) and Buster (3.7) are too old and need a reflash.
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
INSTALL_MODE=""
MODEL="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo unknown)"
echo "     arch:  $ARCH"
echo "     model: $MODEL"

case "$ARCH" in
  aarch64|arm64)
    INSTALL_MODE=uv
    echo "     OK - 64-bit ARM, prebuilt wheels available from PyPI."
    ;;
  armv6l)
    INSTALL_MODE=pip
    echo "     ARMv6 - using system Python + piwheels rather than uv + PyPI."
    ;;
  armv7l)
    INSTALL_MODE=pip
    echo "     ARMv7 - using system Python + piwheels. If this board is 64-bit capable"
    echo "     (Pi 3/4/5), reflashing Raspberry Pi OS 64-bit gives better wheel coverage."
    ;;
  *)
    fail "Unrecognised architecture '$ARCH'. Stopping rather than guessing."
    ;;
esac

say "2/7  Checking memory and clock"
MEM_MB="$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo)"
echo "     RAM: ${MEM_MB}MB"
SWAP_MB="$(awk '/SwapTotal/ {printf "%d", $2/1024}' /proc/meminfo)"
echo "     swap: ${SWAP_MB}MB"
if [ "$MEM_MB" -lt 600 ] && [ "$SWAP_MB" -lt 512 ]; then
  echo "     NOTE: 512MB boards are tight. Installing from piwheels needs no compiler, so this"
  echo "     usually completes as-is, but more swap makes it reliable:"
  echo "         sudo dphys-swapfile swapoff"
  echo "         sudo sed -i 's/^CONF_SWAPSIZE=.*/CONF_SWAPSIZE=1024/' /etc/dphys-swapfile"
  echo "         sudo dphys-swapfile setup && sudo dphys-swapfile swapon"
fi
if command -v timedatectl >/dev/null 2>&1; then
  SYNC="$(timedatectl show -p NTPSynchronized --value 2>/dev/null || echo unknown)"
  echo "     NTP synchronised: $SYNC"
  [ "$SYNC" != "yes" ] && echo "     WARNING: the Pi has no RTC. Session boundaries and point-in-time
     correctness depend on the clock. Fix NTP before trusting a run."
fi

say "3/7  Checking system packages"
# apt is only touched when something is actually missing. Installing from piwheels needs no
# compiler, and a recent Raspberry Pi OS already ships git, python3, venv and pip -- so on a
# current image this step does nothing, which also means it does not fight an apt/dpkg lock
# held by an unattended-upgrade or a full-upgrade running in another shell.
NEEDED=""
command -v git  >/dev/null 2>&1 || NEEDED="$NEEDED git"
command -v curl >/dev/null 2>&1 || NEEDED="$NEEDED curl"
if [ "$INSTALL_MODE" = pip ]; then
  python3 -c 'import venv, ensurepip' >/dev/null 2>&1 || NEEDED="$NEEDED python3-venv"
fi
if [ -n "$NEEDED" ]; then
  echo "     missing:$NEEDED"
  if sudo fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1; then
    fail "apt is locked by another process (an upgrade running elsewhere?).
     Wait for it to finish, then re-run. Refusing to interrupt a package operation."
  fi
  sudo apt-get update -qq
  # shellcheck disable=SC2086
  sudo apt-get install -y -qq $NEEDED
else
  echo "     git, curl and venv already present - skipping apt entirely"
fi

say "4/7  Preparing the Python toolchain"
if [ "$INSTALL_MODE" = uv ]; then
  if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
  fi
  uv --version
else
  PYVER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
  echo "     system python3: $PYVER"
  python3 - <<'PY' || fail "Python 3.11+ is required and this OS has an older one.
     Bookworm (Debian 12) ships 3.11 and trixie (Debian 13) ships 3.13; both support ARMv6.
     Bullseye ships 3.9 and Buster 3.7. Reflash a newer 32-bit image and re-run."
import sys
sys.exit(0 if sys.version_info >= (3, 11) else 1)
PY
  echo "     OK"
  # Raspberry Pi OS ships /etc/pip.conf pointing at piwheels already; make it explicit so the
  # install does not silently fall back to building pydantic-core from source for six hours.
  if ! grep -rqs "piwheels.org" /etc/pip.conf; then
    echo "     adding piwheels index to /etc/pip.conf"
    sudo mkdir -p /etc
    sudo tee -a /etc/pip.conf >/dev/null <<'PY'
[global]
extra-index-url=https://www.piwheels.org/simple
PY
  else
    echo "     piwheels index already configured"
  fi
fi

say "5/7  Fetching the code (branch: $BRANCH)"
if [ -d "$QUANTBOT_HOME/.git" ]; then
  git -C "$QUANTBOT_HOME" fetch origin --prune
  git -C "$QUANTBOT_HOME" checkout "$BRANCH"
  git -C "$QUANTBOT_HOME" pull --ff-only origin "$BRANCH"
else
  git clone --branch "$BRANCH" "$REPO_URL" "$QUANTBOT_HOME"
fi
cd "$QUANTBOT_HOME"
if [ "$INSTALL_MODE" = uv ]; then
  uv sync
else
  # A venv with system-site-packages off, so what installs here is what the service runs.
  [ -d .venv ] || python3 -m venv .venv
  # pydantic-core from piwheels is the whole reason this board works. If pip decides to build
  # it from source instead, the install appears to hang for hours -- so say what happened.
  ./.venv/bin/pip install --upgrade pip setuptools wheel
  echo "     installing dependencies (piwheels; first run on a Zero takes a few minutes)"
  ./.venv/bin/pip install --prefer-binary -e .
  ./.venv/bin/python -c "import pydantic_core, sqlalchemy, httpx; print('     imports OK')"
fi
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
if [ "$INSTALL_MODE" = uv ]; then
  EXEC_START="ExecStart=$HOME/.local/bin/uv run quantbot daemon"
else
  EXEC_START="ExecStart=$QUANTBOT_HOME/.venv/bin/quantbot daemon"
fi
sudo cp "$QUANTBOT_HOME/deploy/pi/quantbot.service" "$SERVICE_FILE"
sudo sed -i "s|__QUANTBOT_HOME__|$QUANTBOT_HOME|g; s|__QUANTBOT_DATA__|$QUANTBOT_DATA|g; s|__USER__|$USER|g; s|__EXECSTART__|$EXEC_START|g" "$SERVICE_FILE"
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
