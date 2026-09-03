#!/usr/bin/env bash
# Refuse to deploy against an env file that enables live trading.
#
# Usage:  deploy/refuse-live-trading.sh <env-file>
# Exit 0: the file is paper-only. Exit 1: it is not, and the reason names the line.
#
# WHY THIS EXISTS AS ITS OWN FILE. `deploy/omen/setup.sh` carried this check inline and it
# checked nothing: it grepped for `LIVE_TRADING=true`, and there is no `LIVE_TRADING` setting.
# `Settings` reads `TRADING_MODE` and `LIVE_TRADING_ACKNOWLEDGED` with `extra="ignore"`, so a
# variable literally named `LIVE_TRADING` is discarded by the config layer and read by nothing
# else in the tree. The regex could not match `LIVE_TRADING_ACKNOWLEDGED` either, because it
# required `=` immediately after `LIVE_TRADING`. An env file setting `TRADING_MODE=LIVE` passed,
# while the script reported that it had refused. `deploy/pi/setup.sh` had no check at all: it
# writes a paper env file, but only `if [ ! -f "$ENV_FILE" ]`, so an existing live one survived.
#
# One file, two callers, because two copies of a safety check drift and the drift is invisible
# until the day it matters. It is executable rather than sourced so it can be run directly by a
# test, without fabricating a HOME or risking a real `systemctl --user` call.
#
# WHAT AUTHORITY THIS HAS: none. The boundary that actually holds is in `config.py`, where
# `TradingMode.LIVE` without `LIVE_TRADING_ACKNOWLEDGED` is refused, and in the fact that no
# agent may supply live credentials. This is a deployment-time tripwire in front of it. A
# tripwire that cannot fire is worse than none, which is the whole reason this file exists.
set -euo pipefail

env_file="${1:-}"
[ -n "${env_file}" ] || { printf 'usage: %s <env-file>\n' "$0" >&2; exit 2; }
[ -f "${env_file}" ] || { printf 'refusing: %s does not exist\n' "${env_file}" >&2; exit 2; }

refuse() { printf 'refusing: %s\n' "$1" >&2; exit 1; }

# The last assignment wins, which is how systemd's EnvironmentFile and a shell both read it.
# Leading whitespace and spaces around `=` are tolerated when *matching* on purpose: being
# permissive about what counts as an assignment makes this refuse more, not less.
setting() {
  sed -n -E "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "${env_file}" \
    | tail -n 1 \
    | sed -E 's/[[:space:]]+$//; s/^"(.*)"$/\1/; s/^'\''(.*)'\''$/\1/'
}

# Absence is safe: both default to PAPER in `Settings`. Only a present, non-PAPER value is a
# refusal -- including a lowercase `paper`, because `TradingMode` is a StrEnum whose value is
# exactly "PAPER" and anything else fails to parse. A value that would crash the daemon at
# startup should not get as far as being installed.
for name in TRADING_MODE BROKER_ENVIRONMENT; do
  value="$(setting "${name}")"
  if [ -n "${value}" ] && [ "${value}" != "PAPER" ]; then
    refuse "${name} is '${value}' in ${env_file}; only PAPER may be deployed, and only the human operator may authorize real capital"
  fi
done

# Every spelling pydantic reads as true, lowercased before comparison.
acknowledged="$(setting LIVE_TRADING_ACKNOWLEDGED | tr '[:upper:]' '[:lower:]')"
case "${acknowledged}" in
  1 | true | yes | on | t | y)
    refuse "LIVE_TRADING_ACKNOWLEDGED is '${acknowledged}' in ${env_file}; only the human operator may authorize real capital"
    ;;
esac

# Not a refusal, because the line is inert -- but somebody wrote it believing it did something,
# and that belief is what the old guard was built on. Say so rather than passing in silence.
if grep -Eq '^[[:space:]]*LIVE_TRADING[[:space:]]*=' "${env_file}"; then
  printf 'warning: %s sets LIVE_TRADING, which configures nothing. The settings that govern this are TRADING_MODE and LIVE_TRADING_ACKNOWLEDGED.\n' \
    "${env_file}" >&2
fi
