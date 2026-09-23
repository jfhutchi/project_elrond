# Intraday learner implementation plan

The user wants fresh observations, multiple forecasts and measured improvements throughout
the trading day, with PAPER execution. This extends the authorized learner implementation;
the daily five-session learner and its saved results remain intact.

## Contract

One fixed CPU ridge model and mean baseline; completed one-minute IEX raw bars,
31-minute feature history, fixed 30-minute SPY-relative target, one forecast cohort
per ten-minute slot. Provider receipt time and durable recording time are distinct
from bar time. Historical examples are exploratory and never count as forecasts.
Missing bars are not filled. Targets must fit inside the authoritative XNYS session.

Reuse the existing learner estimators, native task, market-data clients, immutable
hashes, canonical observation burden, promotion and execution controls. The existing
intraday worktree supplies accounting and timing lessons, not a profitable strategy.
Raw capture is archival only; protected periods must be excluded before analysis.

Do not retrain every tick. First fit needs at least 20 training sessions and five
validation sessions. Subsequent challenger fits require new matured cohorts and a
fixed cooldown. Purge labels overlapping validation. Every attempted fit is charged
before computation; failures cannot trigger repeated automatic searches. Model changes
never inherit PAPER qualification. Forecasts and outcomes remain immutable.

PAPER_OBSERVATION is the entry observation stage; PAPER_QUALIFIED requires authentic
forward evidence later. Gate checks read persisted exact deployment/model/configuration
identities. Coordinator recovery, account, risk, portfolio vetoes, kill switch, single
writer and independent protection must all remain in the order path. Promotion revocation
must not disable time-based exits. No automatic fabricated qualification or direct orders.

## Steps

- [ ] Provider collector with immutable actual receipts and daily/minute timing tests.
- [ ] Pure intraday features, labels, immutable models and predictions with leakage tests.
- [ ] Durable cycle connecting capture, guarded observation/forecasting, resolution and
  bounded retraining; recovery/idempotence and permanent trial-charge tests.
- [ ] Paper eligibility gateway and concrete risk-checked execution adapter; rejection,
  revocation, duplicate intent and horizon-exit tests. No new live capability.
- [ ] Dashboard freshness, separate daily/intraday evidence and actionable blockers.
- [ ] Native deployment after focused verification/review, preserving current trading
  runtime and stores; report which components actually executed and which gates remain.

The April–September 2026 protected daily window is also excluded from intraday analysis
under the canonical observation identity. Switching frequency does not create fresh
statistical budget. Data collection and software capability are not investment evidence.
