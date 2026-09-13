# Market-hours monitoring after an overnight start

The September 9 reboot exposed two separate issues. The native daemon process started correctly,
but its closed-market schedule selected the next after-close trading cycle as its first wake.
That skipped daytime observations following an overnight start. Independently, the console
treated 45 wall-clock minutes without a market-hours observation as evidence that the process
was probably stopped, even overnight when observations are not expected.

`next_wake` now selects the broker-reported next open as an `OBSERVE` wake when that open precedes
the next trading cycle. It uses the broker's holiday/session schedule rather than local weekday
arithmetic. A pending after-close cycle before the next open keeps its slot. Closed-market clocks
without a future open are refused rather than producing a nonpositive sleep.

Open-session observations remain on the existing 15-minute cadence. An observation at or after
the session close must not displace the close-plus-five-minute trading cycle: at the close the
broker advances `next_close` to the following session, so waking for an observation at that instant
and then recomputing the schedule could otherwise skip the intended cycle. The last comparison
therefore uses the session close itself, while the trading wake remains five minutes after it.
The runner also retains that known trading slot across observations, because a slow observation
can finish after close. If it finishes after the trading slot too, the pending cycle runs without
an additional sleep, retaining its scheduled identity. This is in-process scheduling, not a
replay of historical failed cycles from storage.
The observation callback cannot submit orders; this change does not introduce intraday trading.

The console retains the real age of the last market-hours observation and calls attention to a
gap. It explains the off-hours pause and directs the operator to runtime service status when
observations are missing during an open session. It cannot infer either process health or death
from an old timestamp alone. This is not an invented heartbeat or a suppressed incident.

Regression tests exercise a complete preopen-to-close run with a broker clock that advances its
next close at the boundary, holiday/weekend next-open selection, the close boundary, invalid
clocks, and honest console wording. Test observations are fixtures, never operational evidence.

The other two dashboard notices retain their actual meaning. The September 8
`UNHANDLED_OPERATIONAL_EXCEPTION` is an unresolved incident record from the broker rejection;
clearing the kill switch does not resolve its incident record. The original response body was
not retained, so the exact rejection reason remains unproven. IEX is a single-venue volume feed;
volume-derived anomaly triggers remain unavailable. Neither notice is removed merely to make
the attention list empty.
