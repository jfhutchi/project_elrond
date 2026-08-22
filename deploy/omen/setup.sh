#!/usr/bin/env bash
# Install the coordinator's user units (#38).
#
# Deliberately does nothing that needs root. The one privileged step -- enabling lingering -- is
# left to the operator and checked for here, because installing an always-on service that stops
# when the last terminal closes is worse than not installing it: it looks deployed.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
units="${HOME}/.config/systemd/user"
repo="${HOME}/project_elrond"
env_file="${HOME}/.config/quantbot/quantbot.env"

fail() { printf 'refusing: %s\n' "$1" >&2; exit 1; }

[ -d "${repo}" ] || fail "${repo} does not exist; clone the repository there first"
[ -x "${repo}/.venv/bin/quantbot" ] || fail "${repo}/.venv is missing or has no quantbot entry point"
[ -f "${env_file}" ] || fail "${env_file} does not exist; the units read credentials from it"

# Mode matters as much as existence. A 644 credential file is readable by every user on the host.
mode="$(stat -c '%a' "${env_file}")"
[ "${mode}" = "600" ] || fail "${env_file} is mode ${mode}; it must be 600"

# The daemon must never be brought up with live trading enabled by a deployment script.
if grep -Eqi '^[[:space:]]*LIVE_TRADING[[:space:]]*=[[:space:]]*(1|true|yes)' "${env_file}"; then
  fail "LIVE_TRADING is enabled in ${env_file}; only the human operator may authorize real capital"
fi

if [ "$(loginctl show-user "${USER}" --property=Linger --value 2>/dev/null || echo no)" != "yes" ]; then
  fail "lingering is off, so these services would stop when your last session ends.
  Run:  sudo loginctl enable-linger \"\${USER}\"
  Then re-run this script. It needs a password, which is why it is not scripted here."
fi

mkdir -p "${units}" "${repo}/reports"
install -m 644 "${here}"/quantbot-daemon.service \
               "${here}"/quantbot-dashboard.service \
               "${here}"/quantbot-dashboard-refresh.service \
               "${here}"/quantbot-dashboard-refresh.timer \
               "${units}/"

systemctl --user daemon-reload
systemctl --user enable --now quantbot-daemon.service
systemctl --user enable --now quantbot-dashboard.service
systemctl --user enable --now quantbot-dashboard-refresh.timer

printf '\n--- status ---\n'
systemctl --user --no-pager --lines=0 status \
  quantbot-daemon.service quantbot-dashboard.service quantbot-dashboard-refresh.timer || true

# A timer that reports NextElapse=infinity will never fire again. That happened on the Pi and
# was invisible until somebody looked, so the check is printed rather than assumed.
printf '\n--- next dashboard refresh ---\n'
systemctl --user list-timers --all quantbot-dashboard-refresh.timer --no-pager

printf '\nDashboard: http://127.0.0.1:8080/  (loopback only by design; see README)\n'
