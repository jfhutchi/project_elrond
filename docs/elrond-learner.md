# Elrond Learner (#242)

The learner fits supervised models of **five exchange sessions of security price return minus
SPY price return**. It produces scores and rankings. It has no order, risk override, strategy
replacement, or promotion path.

## What the software establishes

- Twelve fixed features use 60 sessions of history: returns, volatility, volume change, relative
  strength, moving-average distance, acceleration and broad-market return/volatility.
- The supplied session calendar determines the exact fifth target session. Missing history,
  missing target prices, late inputs and duplicate observations fail closed. Dates are not
  compressed to the next available price. This deliberately limits the first version to
  complete panels; missing/delisted outcomes need a future explicit lifecycle.
- Features, labels and their hashes are separate immutable records. The full symbol roster,
  calendar identity, source snapshot, feature version and original prices participate in lineage.
- TRAIN uses existing `DataRole.DISCOVERY`; validation uses `VALIDATION`; holdout uses
  `PROTECTED_EVALUATION`. Targets crossing either chronological boundary are purged. Validation
  cannot enter the fitting API. Holdout prices are never materialized by the dataset builder.
- Mean, ridge and fixed quadratic ridge are implemented without new dependencies. Ridge learns
  coefficients and stores its training-only means/scales. Quadratic ridge adds squared terms
  under one fixed versioned transformation; it does not search for features.
- Every fit creates a new model ID. Retraining with a parent creates a separate successor.
  Dataset, fitted artifact, observation IDs, parameters, seed, code/lock/environment identities,
  training MSE and validation MSE survive persistence and reload.
- Alembic revision `0029` adds five learner tables to the existing research store. Ordinary opens
  create or migrate nothing. Predictions have a storage-owned receipt timestamp; resolution
  requires a separate session after the prediction has committed. Return arithmetic is verified
  against the frozen initial prices and exact target prices.
- IC, quantile spreads, hit rate, coverage and equal-weight selection turnover are descriptive.
  Constant scores and small cross-sections produce no IC. Incomplete outcome coverage suppresses
  IC/spreads. A t-statistic requires at least 20 non-overlapping periods and nonzero variance;
  it still assumes more independence than has been established, and earns no significance claim.
- Every learner fit attempt is recorded before fitting under the reserved `learner:` search
  namespace and counts in the registry's cumulative burden. Failures and actor renaming cannot
  erase that burden. Later hypothesis registration can conservatively double-count a selected
  attempt; no correction that lowers burden is implemented.

## Evidence classes and limits

**All current input bundles are `LOCAL_DECLARED`.** Timestamp checks and hashes verify internal
consistency; they do not authenticate a provider, a historical point-in-time universe or a
calendar. A fixed roster does not establish survivorship-free equity evidence. Adjusted price
returns are not automatically total returns.

`HISTORICAL_REPLAY` predicts validation rows using an artifact fitted on TRAIN. Predictions are
written now, then resolved in a separate transaction. The data may have been observed before the
model was created. These are exploratory validation diagnostics, never genuine forward evidence.

`PROSPECTIVE_LOCAL` records local predictions within **five minutes** after their feature cutoff
and before the target, using the service and storage clocks. This is a conservative, versioned
project default that prevents late issuance after observing most of the target path. It proves
the timing of receipt under the local database/host trust boundary, not authenticity of market
inputs. Clock and database administration are not cryptographically attested.

`SHADOW` is reserved. Current local-declared sources cannot create it. No forward PAPER evidence
is created. The system's genuine shadow/forward status stays **NOT ESTABLISHED**.

Protected evaluation still requires the existing registry and `ExperimentRunner`. Ordinary
learner operations refuse registered protected windows, including known vendor aliases, and
refuse recycling a learner's declared holdout. A split declaration alone does not reserve fresh
statistical capacity or establish that a window was untouched. The CLI accepts local bundles;
there is **no protected-safe production market-store loader yet**. Do not export protected
prices into a bundle: reading a file has already exposed its contents before validation can act.

Registered windows use the existing registry's lifecycle: live reservations and consumed windows
block; expired reservations lapse. Learner holdout manifests remain protected. Resolution rechecks
current protection before using target prices, even when protection was added after prediction.

## Commands

Every learner command requires an explicit existing database and bypasses trading-runtime
construction. Use a dedicated research store for the first runs. No broker credentials are needed.

```powershell
uv run quantbot create-database --database learner.db --confirm
uv run quantbot learner --database learner.db status
uv run quantbot learner --database learner.db dataset --input bundle.json `
  --train-start 2020-04-01 --train-end 2020-06-30 `
  --validation-start 2020-07-01 --validation-end 2020-09-30 `
  --holdout-start 2020-10-01 --holdout-end 2020-12-31
uv run quantbot learner --database learner.db train DATASET_ID --family ridge --alpha 1
uv run quantbot learner --database learner.db predict MODEL_ID --replay
uv run quantbot learner --database learner.db resolve MODEL_ID --input bundle.json
uv run quantbot learner --database learner.db evaluate MODEL_ID --evaluation-class HISTORICAL_REPLAY
uv run quantbot learner --database learner.db train DATASET_ID --parent-model-id MODEL_ID
uv run quantbot learner --database learner.db compare INCUMBENT_ID CHALLENGER_ID
```

The quoted dates are illustrative and must agree with the actual source calendar. A bundle has
`inputs` (each containing `bar` and `available_at`), `sessions` (`XNYSSessionSequence`), `symbols`,
`universe_id`, `source_id`, `observation_dataset`, `snapshot`, and optional `benchmark_symbol`.
`InputBundle` in `quantbot.learner.cli` is the strict schema. Inputs for dataset creation end at
validation end; a calendar may describe future sessions without containing their prices.

For prospective local recording, use `predict MODEL_ID --input bundle.json --as-of UTC_TIMESTAMP`.
The cutoff must identify a supplied session, all inputs must already be available, the complete
declared universe must be present, and the five-minute receipt deadline applies.

## Reproducible software demonstration

```powershell
uv run python scripts/learner_demo.py --output-dir .local-ci/learner-demo
```

The output directory must be new. This generates an explicitly **synthetic** local panel and
calendar, exercises the normal CLI, fits all three models, records/resolves replay predictions,
evaluates them, and creates a separate challenger. Its report is evidence of software behavior.
It is not a market experiment and its returns must not enter research confirmation or promotion.

## Stored market replay and portfolio economics (September 21 extension)

`dataset-from-store --spec spec.json` reads the existing `alpaca:iex:all:1day` bar cache.
Use an explicit **dedicated research copy** of the operational store, preserving its protection
registry and trial history, then migrate only that copy. Never migrate the active runtime for a
learner experiment. Keep the original consistent backup untouched.

`StoreReplaySpec` defines the complete XNYS `sessions`, sorted `symbols`, and six exact-session
split dates (`train_start`, `train_end`, `validation_start`, `validation_end`, `holdout_start`,
`holdout_end`). Obtain the calendar through the existing Alpaca PAPER calendar provider; do not
infer exchange sessions from missing prices. The first training date needs 60 prior sessions.
The SQL reader checks protected and reserved/consumed forward-paper windows, including source
aliases, before reading prices. Its bounded query excludes holdout dates. Missing panels,
conflicting provenance and calendar mismatches fail closed. Source hashes bind the entire
eligible panel, roster, calendar and splits; resolution/economics recheck that original vintage.

These bars lack historical receipt timestamps and membership vintages. Their close-time
availability is a **historical replay assumption**. The fixed ETF roster, IEX single-venue data
and current all-adjusted vintage do not establish survivorship-free or authenticated point-in-time
evidence. All inputs remain LOCAL_DECLARED. Later holdout dates are a manifest, not a declaration
that previously explored dates have become untouched. No genuine SHADOW evidence is issued.

```powershell
.\.venv\Scripts\quantbot.exe learner --database RESEARCH_COPY.db dataset-from-store --spec spec.json
.\.venv\Scripts\quantbot.exe learner --database RESEARCH_COPY.db resolve-from-store MODEL_ID --calendar calendar.json
.\.venv\Scripts\quantbot.exe learner --database RESEARCH_COPY.db economics MODEL_ID --calendar calendar.json --output economics.json
.\.venv\Scripts\python.exe scripts/learner_market_replay.py --database RESEARCH_COPY.db --spec spec.json --output-dir NEW_RUN_DIRECTORY
```

The orchestration script fits exactly mean, ridge (alpha=1, seed=0), and one identical-config
immutable ridge successor. Each fit is durably counted before fitting. A successor that has the
same coefficients establishes reproducibility, not improvement. Failed runs retain their output
directory, per-step log and attempts; retry individual CLI steps instead of discarding evidence.
Retain this research store and its trial records when continuing work; an isolated copy is not
automatic synchronization of research accounting into another store.

Economics uses a fixed long-only equal-weight top quintile with all cutoff ties included. Constant
scores select the entire roster. Starting with the first validation cohort, it uses a deterministic
nonoverlapping subset: entry at the next session open, predetermined exit at the fifth session
close, full liquidation between holdings, fractional units, no leverage and zero cash interest.
The existing research baseline of 5 bps adverse slippage on **each side** and zero commission is
applied equally to the learner, all-universe and SPY portfolios on matching dates. All declared
validation predictions and outcomes must exist; interrupted replay cannot report full coverage.

Reports include predictive rank IC, net and SPY-excess return, daily-close drawdown, traded
notional relative to initial capital, cost, exposure, daily equity and exact execution-price inputs.
Reports are content-hashed and exclusively created; an older file cannot be overwritten. Returns
are descriptive simulations, not calibrated spread/impact/capacity estimates. Daily marks miss
intraday drawdowns. The SPY baseline uses matched holding windows, not continuous buy-and-hold
through cash gaps. No statistical qualification or promotion path is granted by these results.

The [September 21 recovery handoff](learner-recovery-2026-09-21.md) records actual market replay
results, runtime state, preserved attempts, implementation commits and verification scope.

## Not yet proven or implemented

No predictive edge on verified market data, protected holdout result, after-cost portfolio
performance qualifying a model, genuine forward shadow/PAPER result or positive expectancy is established.
Production point-in-time universe/provider ingestion, authoritative outcome sourcing, walk-forward
retraining policies, protected challenger qualification, calibrated portfolio economics and strategy
integration remain outstanding. Validation comparison never promotes a model.

The [implementation and verification report](elrond-learner-issue-242-update.md) records the
committed demonstration, exact test results and the Linux CI prerequisite limitation.
