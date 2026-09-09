#!/usr/bin/env bash
# Historical Pi deployment support remains documented in this directory. The operator retired the
# host on 2026-09-08, so this entry point must never inspect, install, update, or start anything.

set -euo pipefail

printf '%s\n' \
  'ERROR: Pi deployment is retired by operator policy; this installer makes no changes.' \
  'Use deploy/omen/setup-windows.ps1 for the current Omen PAPER runtime.' >&2
exit 1
