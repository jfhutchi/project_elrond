# Learner progress alerts — first delivery batch

This batch addresses portions of #251, #252 and #253. It does not close those issues.

The worker now publishes independent forecast, training and PAPER-entry status in `worker.json`.
A successful blocked tick is not reported as productive learning. Protected dates, session
closure and the first 31 regular-session minutes are expected waits. Missing adapters belong
to engineering. PAPER approval applies to an exact eligible deployment package, never to
permission to ignore scientific evidence. Insufficient training observations are a distinct state.

`state/alerts.db` is a dedicated operational side store. It never migrates the learner or
trading database. Active alerts survive restart and include owner, action, first-seen time,
duration, acknowledgement and escalation count. Acknowledgement does not resolve a gate.
Failed/incomplete measurements retain prior unresolved gates. Recovery records elapsed duration.
Notifications are persisted in its event outbox and included in scheduler stdout receipts;
the console presents active conditions. External push delivery is **NOT CONFIGURED**. A
completed local event is not a claim that a person saw it.

Optional `alert-policy.json` under the learner results directory configures integer
`warning_seconds` (default 1800), `critical_seconds` (3600), `reminder_seconds` (3600), and
`notify_severities` (default `["WARNING", "CRITICAL"]`). Invalid policy makes the worker emit
`ALERT_STATE_FAILED` and fail rather than silently disable alerts. Critical reminders continue
after acknowledgement until measured recovery. Thresholds cannot enable orders or alter risk.

The watchdog accumulates only eligible intervals with two measured checks in the same exchange
session. Bar capture and task exit alone do not reset it; observations, predictions, scored
outcomes, evaluations and immutable training updates do. Intentional waits pause a preexisting
stall without falsely resolving it. A dead worker cannot run its own watchdog: independent
daemon/protection/ingestion monitoring, dead-worker alert dispatch, external delivery, console
acknowledgement controls, and periodic noncritical digests remain follow-on acceptance work.

From the matching reviewed checkout, acknowledgement of an existing alert is available as:

```powershell
.venv/Scripts/python.exe -m quantbot.operations.alerts --database <learner-directory>/state/alerts.db --acknowledge PAPER_PROMOTION_REQUIRED
```

It opens only an existing alert store. The next worker receipt refreshes the console's
acknowledgement display. It does not approve/promote a model, enable PAPER entries, change the
kill switch or resolve the underlying gate.

## Observe the first eligible forward session

Without changing the clock, reservations, policy or store, read the existing worker receipt and
immutable `intraday/observations`, `predictions`, `outcomes` and `scores` files. Verify the pinned
code, receipt timestamp, exchange-session date, actual source timestamps and model identity.
The deterministic boundary test uses temporary stores and synthetic bars: September 30 is
refused; October 1 with complete 31-minute lookback issues one observation/model forecast;
duplicate ticks issue none; the exact later target is required for resolution. PAPER orders
and retraining remain separately gated. This test demonstrates code behavior, not future data
availability, profitability or actual October 1 operation.

For a 09:30 Eastern open, first eligible features are around 10:01 Eastern (14:01 UTC), and a
10-minute scheduler may issue them on its first later tick. The exact 30-minute target must
subsequently arrive before an authentic score can be recorded. Verify the Omen remains awake
and signed in under the current task mode. Keep observation evidence separate from real PAPER
fills and from historical replay. Do not invent gap forecasts after an outage.

Merge and deployment remain operator decisions. This branch is stacked on the unmerged learner
branch; deployment requires the exact reviewed code pin and corresponding console update.
It does not authorize a model deployment, broker adapter commissioning, task-principal change,
database migration, maintenance outage, or reboot/sign-out recovery experiment.
