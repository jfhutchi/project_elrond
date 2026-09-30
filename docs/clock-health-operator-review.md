# Clock health and operator review

The existing bounded PAPER calendar/clock GET probe runs at observer startup and
each scheduled observation. A broker timestamp more than two seconds ahead of the
local response timestamp raises one persistent `CLOCK_SYNC_REQUIRED` alert. The
alert uses the ordinary durable acknowledgement, reminder and recovery lifecycle.
Missing, expired or old provider measurements retain the alert; only a fresh clock
measurement within the existing market-status tolerances establishes recovery.
An old provider timestamp cannot distinguish transport delay from local clock skew
and remains unverified. This probe does not establish Windows Time service health.

A skew diagnosis grants no verified market-open or entry authority. For bounded
offsets under 60 seconds, separately fetched calendar evidence may establish an
expected closed-session wait only when both timestamps share the same Eastern
date and the same closed phase (before open, after close, or no session), and the
broker reports closed. Actual failures, missing measurements and never-completed
protection/reconciliation receipts remain visible. Existing stale-loop alerts are
retained during that wait without reminders or false recovery. Entry clock gates,
deadline freshness and trading risk controls are unchanged.

The new alert remains local until the parent reviews and deploys the exact static
catalog mapping. The currently deployed dispatcher does not accept this unknown
code; this source change does not enable external transmission.

Read-only operator diagnosis may use `w32tm.exe /query /status` and
`w32tm.exe /query /peers`. If the operator explicitly approves a new one-time
repair, run exactly `w32tm.exe /resync` once from an approved elevated prompt, then
verify status and a fresh PAPER clock probe. Access denied or canceled elevation
ends that attempt. Do not retry elevation or change peers, services, scheduled
tasks, registration, registry keys, security, or persistent settings. This document
prepares the repair instruction only; no clock command was executed for this work.
