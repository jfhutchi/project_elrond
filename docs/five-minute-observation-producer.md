# Frozen five-minute observation-only producer

> **Optional legacy observation path.** The [PAPER core](paper-core.md) trader acquires its own declared symbol/benchmark inputs and does not depend on this frozen-model producer. Its owned-executed outcome target, scoped clock recovery and daily all-history learning are separate from the producer's market labels and monitor receipt. Preserve the earlier artifacts and charges; this launcher is not a core role startup recipe.

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

## Prepared native launcher

`python -m quantbot.learner.five_minute_launch --config <absolute-json>
--env-file <existing-profile> --check` validates the clean source commit,
content-addressed frozen specification/data/models, fixed mean/ridge identities,
original canonical PRE_ENGINE charges and PAPER configuration. The check makes
no provider request and creates no output. It does not claim a healthy clock,
fresh provider panel or successful scheduled run.

The ordinary invocation also takes an exclusive observation-root lock, binds
the original canonical registry and immutable learner declarations, and uses the
existing clock monitor (HEALTHY, not future-dated, at most 300 seconds old).
Protected-date and clock gates remain before and after each actual capture.
Both families remain frozen; model fitting and order execution are absent.
Provider failures/timeouts create explicit safe failure receipts and exit 2;
expected calendar/data/clock waits remain explicit waits. The latest receipt and
immutable receipt history live only under the isolated observation directory.

`task_xml()` produces a task definition, not registration. The proposed task is
`Elrond Five Minute Observations`, using the existing InteractiveToken / Limited
principal and existing hidden service host. It starts weekdays at 09:31:05
Eastern, repeats every ten minutes for six hours forty minutes (through 16:11:05),
and has a two-minute task limit / ninety-second capture limit. The native timezone
must already be `Eastern Standard Time`; installation must refuse another zone,
not change it. No wake/sleep, login, credential or execution-policy setting is
changed. Holidays, early closes, incomplete panels, stale bars and horizon waits
still use the actual exchange calendar. A missed run never fabricates forecasts
for elapsed decisions. Pending exact targets can be resolved by subsequent real
captures; missing targets remain pending.

Native install requires a new observation-only scheduling decision. Existing
learner/observer consent is not treated as changed trading-artifact consent.
Use a separate pinned release, frozen-model directory, config and observation
root; seed the original two predictions/outcomes and their capture provenance
without rewriting them. Preserve the task-local original archive as well.
Back up task definitions/nonsecret configuration before registration, inspect
Limited/Interactive settings after registration, and verify one naturally
scheduled forward forecast and its later actual outcome. Rollback disables only
this task and retains all evidence; the original paper workers, protection,
operational database, models, thresholds and holdings are untouched.
