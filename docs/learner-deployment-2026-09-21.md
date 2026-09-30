# Learner deployment — September 21, 2026

The operator explicitly authorized deploying and running the new learner after the earlier
development-only milestone. A separate pinned learner runtime was installed and exercised.
Continuous new-market forecasting is **not running or scheduled**. Do not describe this as
an autonomous learning deployment: the existing worker script conducts fixed historical replay.

## Installed and measured

- Learner checkout: `C:/Users/leepa/elrond-learner-runtime`, clean detached
  `f3f345ec63fe23172cbee859b60d3e32472716ea`.
- Native Python: `D:/Elrond/tools/python/cpython-3.11.16-windows-x86_64-none/python.exe`.
  A dedicated virtual environment was installed using `uv sync --frozen --offline --no-dev`;
  all dependencies came from the local cache.
- Research store: `D:/Elrond/learner/state/learner.db`, restored through the supported SQLite
  backup function from the completed experiment. Integrity check passed. Exact row comparisons
  preserved one dataset, three models, 3,696 forecasts, 3,696 outcomes and all 10 search records.
  No refit or trial-accounting reset occurred.
- Original experiment and recovery backup remain in place. Copied immutable reports and the
  command ledger live at `D:/Elrond/learner/evidence`; deployment metadata is in
  `D:/Elrond/learner/deployment.json`.
- Before deployment, another consistent operational backup was created at
  `D:/Elrond/backups/pre-learner-deploy-20260921T150215Z.db`.
- The installed executable evaluated the saved ridge successor successfully (exit 0):

```powershell
& C:/Users/leepa/elrond-learner-runtime/.venv/Scripts/quantbot.exe learner --database D:/Elrond/learner/state/learner.db evaluate 32e17efe-0e93-4832-9040-9cb4b14aceed --evaluation-class HISTORICAL_REPLAY
```

The actual output is `D:/Elrond/learner/logs/deployment-evaluation.json`. This is another
evaluation of existing historical evidence, not a fresh market experiment or a new fit.

## Boundaries and missing continuous operation

The trading runtime remains clean at `a49203d5537d2cae7647ad110d69016a23265371` in
`C:/Users/leepa/elrond-omen-runtime`. Its daemon and position-protection tasks remain Running.
No existing task, PAPER authority, risk setting, credential file, operational schema or runtime
pin was changed. The supplied Windows password was not used or stored by this deployment.

The existing learner has no unattended forward-data producer. Stored adjusted daily bars lack
historical receipt timestamps and membership vintages. Its preserved learner holdout covers
2026-04-01 through 2026-09-30; the guard rejects access to this range, including when a later
forecast's 60-session feature lookback overlaps it. September 30 is therefore not a promise
that forecasts can start immediately afterward. No boundary was moved or bypassed.

A genuine continuing loop needs a separately verified forward-data implementation that records
actual receipt times and a valid decision cutoff, preserves immutable model/source lineage,
checks current protected/forward reservations before reads, resolves only durably issued matured
forecasts, and reports missing or ineligible data without claiming success. It must not create
repeated fits on every scheduler tick or promote the losing ridge model.

An asynchronous scope question was presented to the operator: deploy existing capability only,
or also build that missing forward-data loop. At this checkpoint no answer has arrived and no
recurring backtest/status-only task has been passed off as continuous forecasting. The installed
learner evaluation has completed; no learner process was intentionally left running.
