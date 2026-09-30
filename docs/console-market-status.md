# Console market status

The Operations dashboard shows **Market open**, **Market closed**, or **Market status
unavailable** for the configured PAPER XNYS market. It compares the broker clock with the
separate session-calendar endpoint, including holidays, early closes and Eastern daylight
saving time. A normal closed market is informational, not a Needs attention item.

Observation and protection age warnings are suppressed only while a fresh measurement verifies
a scheduled closure. During an open session the existing 45-minute threshold counts from the
later of session open and the last check, so overnight silence does not trigger a warning at
the opening bell. Actual protection failures, unevaluated positions, reconciliation differences,
incidents and the kill switch remain visible. A current-session observation logging closed, or
a broker clock disagreeing with the calendar, needs attention. Yesterday's log is not treated
as today's clock.

The console refreshes this read-only measurement on GET requests, at most once per minute,
and expires it at session boundaries. Reload the page to see an updated check. Concurrent
requests share the probe; its five-second deadline bounds network delays. Control POSTs do not
wait for it. Unavailable, stale or failed measurements are explicitly unknown, never a guessed
closure, and do not suppress known stale-loop warnings. These cache timings are console-only
project defaults and do not change execution, scheduler cadence or risk controls.

The broker timestamp is retained separately from local receipt time. Up to two seconds of
forward clock skew is allowed (a console project default), but disagreement across a session
boundary remains unknown until the next check. A live Omen probe initially revealed roughly
15 milliseconds of forward skew; the regression test covers that case and rejects large skew.

Both endpoints currently come from Alpaca, so the comparison detects disagreement between the
clock and calendar, not a correlated error affecting both endpoints. Unsupported calendars
report unavailable. The probe owns no database and issues only GET requests: it never records
a heartbeat, changes trading state, clears the switch, or grants order authority. Credentials
and raw transport errors are excluded from the public status.
