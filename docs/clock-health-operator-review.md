# Clock health and operator review

## PAPER core timing and recovery

The [PAPER core](paper-core.md) uses the explicit `PAPER_TIME_V2` provider-relative
contract for the single-provider PAPER lane. Broker clock/calendar evidence and measured
request timing bound entry uncertainty; process monotonic time projects elapsed time.
The two-second entry bound remains. Missing, expired or unsuitable evidence causes an
in-scope entry wait and bounded retry, with automatic recovery after fresh acceptable
evidence. Routine clock/provider failures do not latch a persistent global kill switch.

The guardian schedules owned deadlines independently. Uncertainty overlapping a deadline
cannot extend a hold; ownership and order gates still govern risk-reducing actions, and
an unsupported closed-session exit remains visible for retry. Durable outcome arithmetic
and past-only learning do not require a new entry-clock sample. Suspicious evidence is
quarantined at the affected scope; manual stops and integrity faults are not silently cleared.

Windows synchronization status is diagnostic, not proof of absolute UTC accuracy. The
provider-relative contract does not establish independent cross-provider/cross-host time
authority, and it does not enable external-news research in the core. Functional timing
commissioning remains part of the [exact deployment package](evidence/paper-core/omen-deployment/README.md).

## Legacy observer alert and one-time diagnosis reference

The existing observer probe below retains its own alert lifecycle. Its persistent alert
is not the replacement core's entry authority or a reason to clear a manual stop. The
diagnostic commands are historical procedures, not instructions to perform a clock repair
as part of the PAPER core documentation or rollout.

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
