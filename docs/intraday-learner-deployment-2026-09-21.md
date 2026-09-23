# Intraday learner deployment — September 21, 2026

The user requested fresh market data, predictions throughout the day, retraining and PAPER
trades. The data/learning worker is deployed on the HP Omen and the native ten-minute task
has executed it. PAPER execution remains inactive: no exact learner deployment has passed
the existing promotion gates. No observations, fills or qualification were manufactured.

## Measured deployment

- Development resumed `codex/elrond-learner` and preserved its existing work. Fetch found
  22 commits ahead / zero behind origin/main before these changes. No main merge/rebase/reset.
- Learner runtime: `C:/Users/leepa/elrond-learner-runtime`, clean detached
  `902265b4ca359ebbb2254c01aff8c16f3cbf2d40`.
- Console runtime: `C:/Users/leepa/elrond-console-runtime`, clean detached
  `ea5e0d5d38f3af619774ef693f2f803d7c33afb2`.
- Trading runtime: `C:/Users/leepa/elrond-omen-runtime`, clean detached
  `d10fe786321cb9d7cbe035dcda0b981c96f55887`. Only research accounting changed here;
  all existing runtime task pins were updated, and daemon/protection restarted.
- Operational database remains schema 28; separate learner database remains schema 29.
  Both stores have integrity-checked SQLite backups dated `20260921T171453Z` under
  `D:/Elrond/backups/pre-intraday-*`. Pre-change task XML and deployment metadata are preserved.
- `Elrond Learner` retains indefinite `PT10M` repetition, IgnoreNew and a five-minute limit.
  It uses native Python and the existing environment file, without credential changes.
  The user must be signed in and the laptop awake. Reboot/logout recovery was not tested.
- Browser/API at `http://127.0.0.1:8787/learner` verified the new intraday panel.

The first installed pass completed at 17:19:24 UTC, captured 681 current minute bars and
returned exit zero with explicit forecast/PAPER blockers. The timer fired at 17:19:37 UTC.
After historical seeding, a pass at 17:20:24 UTC reported 684 captured bars and two models.
The next ten-minute timer pass at 17:29:38 UTC captured 711 bars and preserved both models.
The final receipt-reporting patch also ran successfully under its new pin.
These counts overlap across captures; they are not counts of unique new observations.

## Implemented loop

The fixed roster is IWM/QQQ with SPY as benchmark, using raw one-minute IEX bars. Forecast
features require 31 consecutive completed regular-session minutes and a fresh actual receipt.
Each eligible ten-minute slot can issue model-bound forecasts with a fixed thirty-minute
target. Targets cannot cross the session close. Incomplete data remains incomplete.

Immutable files under `D:/Elrond/learner/intraday` preserve observations, model forecasts,
outcomes, model pairs, fit receipts and source-linked simulated scores. Captures are archived
with actual receipt times and query/response hashes. Outcomes resolve only for genuine earlier
observations. One oldest outstanding day per pass supports bounded recovery after long outages.
No missed-session forecasts are backdated.

The first mean/ridge pair was trained from 39,672 bars covering February 2–March 20, 2026:
34 sessions, 930 complete historical cohorts, 1,582 training labels and 278 validation labels.
This is explicitly exploratory historical training, never prospective or shadow evidence.

- Mean model: `387a6f23177fb01de7feb93ffe76a7c1a774024945cd425c60812b86fe718532`.
- Ridge model: `a58f30bd3ac8fa84bb2c33a0988dd6ba72a69dec7ef03eda9021cdaa03bd7026`.
- Validation MSE: mean `1.7344396721910095e-6`; ridge `1.7638037192885579e-6`.
  Ridge did not beat the simple baseline on this measure. No profitability is established.

Later retraining requires at least twenty training and five validation session dates,
24 previously unconsumed matured cohorts and a four-hour cooldown. Following this historical
seed, the worker waits for 25 entirely forward session dates before retraining: combining
the old seed with October data would span the preserved protected period. During that warmup,
the saved models can forecast and accumulate measured results on eligible dates.
Failed fits remain charged and pause further automatic fitting for review. Model pairs publish
only after both fits succeed. Universe/training-policy changes cannot reuse this experiment.

## Remaining gates and honest performance

The April 1–September 30 holdout remains excluded, including minute data under the canonical
`us-equities-daily` observation identity. Today's raw data can be archived but not analyzed.
The current registry permits the October 1 intraday window; every actual pass checks again.
October 1 is the earliest eligible date under the current reservations, not a promise of data
completeness or trading readiness. The old daily learner retains its separate 60-session lookback.

Intraday performance reports use actual future bar observations, next-minute simulated entry
after recording, five basis points slippage per side, no commission and nonoverlapping matched
holding windows. These are simulations, not paper fills or qualification evidence. Currently
there are zero genuine intraday forward predictions, outcomes or learner paper trades.

The PAPER adapter binds the exact model, execution/risk configuration and persisted deployment.
It uses existing sizing, vetoes, broker/coordinator recovery and single-writer controls, and an
independent durable deadline supervisor. It is not commissioned in a native runtime: approved
model promotion, compatible operational composition and independent deadline service remain
required before entry authority. Existing position protection must remain active. A fit or a
successful unit test cannot grant those requirements.

## Accounting and operational checks

The active registry now counts previously omitted `intraday:` and `learner:` rows. Before new
fits its corrected canonical burden was 71; the two new intraday fits raised it to 73. Three
actual daily learner fit rows previously held only in the learner clone were reconciled into
the operational registry with exact-column preservation and conflict rejection, raising it to
76. No model, observation, order or fill was copied into operational evidence.
Receipt: `D:/Elrond/learner/logs/daily-trial-reconciliation.json`.

Current dashboard evidence after the runtime update reported clean reconciliation and a healthy
protection check at 17:17 UTC covering nine positions. Older log errors inspected during the
check were dated September 11–15, not new deployment failures. The existing observer still
reports DEGRADED; learner work does not certify all existing trading prerequisites.

The supported deployment-status report was saved under learner logs. Its startup preflight
remains unverified because its inventory refuses a live WAL store without a consistent snapshot
and cannot establish global coordinator/other-host authority. It did not probe broker
reconciliation. Separate supported backups and live dashboard evidence were checked; these
limits are not presented as a green startup preflight.

Verification before paper-adapter finalization: 345 combined learner/console/integration tests
passed, with five paper fixture errors subsequently corrected after stricter roster binding.
The final data/worker/panel group passed 52 tests plus the added long-outage regression;
research accounting/registry passed 71 tests; deployed schema-28 registry tests passed 63.
Final PAPER and cross-platform checks are recorded below when completed.

To suspend the new learner, disable/stop only `Elrond Learner`. Preserve its store, captures,
model journals and trial rows. Any rollback must keep runtime/task pins paired and retain the
accounting readers for already-spent trials. Do not reset the account or remove consumed trials.
