# Frozen five-minute observation-only producer

`learner.five_minute_forward` can evaluate already-fitted, content-addressed
five-minute models and append local prospective observations/outcomes. It has
no fitting, order submission, deployment, promotion or risk-override API. This
does not replace or activate any model in the existing PAPER entry worker.

The caller must verify the frozen experiment specification, both canonical
PRE_ENGINE charges and original model hashes. It supplies the original immutable
learner declarations and canonical protected-range guard, plus a fresh healthy
receipt from the existing clock monitor. Both gates run before capture and again
after capture. New credentials, SIP routes, paid providers and protected-date
exceptions are not supported.

Provider captures retain actual receipt time and separate `alpaca:iex:raw:5min`
identity. Decisions require seven genuine completed same-session DIA/SPY bars,
the fixed ten price-only features, unchanged two-minute freshness and thirty-
minute target, and enough time before the actual session close. Intended polling
cadence is ten minutes, aligned in the first minute after a completed five-minute
boundary. This preserves forecast cadence while reducing price-bar granularity.
An off-boundary timing wait is distinct from a missing panel, warmup or a closed
session/horizon. No missing data is filled and no tolerance is relaxed.

A prospective model must exist before the decision. Receipt must precede the
decision, and recording must precede the target. Historical rows cannot be
reclassified as forward evidence. Each fixed model records its numeric score
and the unchanged 0.001 cost-floor comparison. That comparison grants no entry
authority, including for a positive score.

Each model/cutoff record is append-only. A repeated cutoff never regenerates an
old forecast from newer prices. Outcomes require elapsed exact target bars for
both asset and benchmark, retain actual availability/source identity, and link
to the immutable prediction. Missing target data remains pending. Corrupted or
oversized archives require bounded investigation rather than fabricated labels.

All stores and receipts belong in a separately named observation root. They are
`PROSPECTIVE_LOCAL` evidence, not automatically qualified promotion evidence,
broker trades, daily account P&L or profitability. Legacy position protection,
exit ownership, active trading model, holdings and trial history remain untouched.

Deployment is a separate decision: a new Limited observational scheduled task,
isolated store and pinned native code/config/models, using existing credentials.
It must not edit existing trading tasks, operational schema or protections. Back
up nonsecret config/task definitions; rollback disables only this new task and
preserves all observations. Real acceptance includes an on-time forward forecast,
subsequent provider-backed outcome and repeat/deduplication check. Synthetic
integration tests are not a substitute for those receipts.
