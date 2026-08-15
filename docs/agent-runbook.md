# QuantBot Agent Runbook

Continuation notes for any session picking this repository up. `PROJECT_STATUS.md` is
machine-generated and overwritten by `quantbot run-once` / `quantbot doctor`; this file is
hand-maintained and never overwritten.

## What the system is

Adaptive Momentum V1: momentum rotation + long-term trend filter + market regime filter +
Donchian breakout + ATR risk management, trading through Alpaca **paper** only.
`LIVE_TRADING` is disabled and only a human operator may ever change that.

Strategy parameters live in `config/` and are immutable for the running version. Any
material change is a NEW strategy version with a new `configuration_hash` and a new
`strategy_id`; historical results are never overwritten.

| Version | Config | strategy_id | Difference |
|---|---|---|---|
| 1.0.0 | `config/strategy-v1.yaml` | `adaptive-momentum-v1-7d04bc9cc0cb20e6` | Whole shares only |
| 1.1.0 | `config/strategy-v1-1.yaml` | `adaptive-momentum-v1-b73083b817f76b8f` | Fractional shares enabled |

The two configs differ in exactly two fields (`version`, `allow_fractional_shares`); every
signal parameter is identical. 1.1.0 exists because whole-share sizing rejects every entry
below roughly $3,000 of equity — the cheapest universe member needs $619 for one share
under the 10% position cap. Point `QUANTBOT_CONFIG` at the version you intend to run.

Sizing note: for this universe the `POSITION_VALUE` cap binds, not the ATR risk cap, so
positions are effectively equal-weight 10%. The 0.5% ATR risk cap only becomes the binding
constraint when 2×ATR exceeds 5% of price. That is scale-invariant and by design.

## Current state (2026-08-15)

Implementation is complete and verified end to end against fake transports. **No forward
paper observation has begun**, because Alpaca paper credentials are not configured in this
environment. Nothing in the durable ledger has been synthesized.

- 458 tests pass, `ruff check` clean, `mypy --strict` clean.
- The composition root (`src/quantbot/runtime.py`) was the missing piece: every policy
  module existed and was tested, but nothing constructed them into a runnable application
  and every CLI command returned `OPERATION_HANDLER_NOT_CONFIGURED`.

## Operating it

```bash
uv sync --all-extras
```

Required environment (see `.env.example`; the process reads the environment only, never a
dotenv file, so export these in the service unit or shell):

| Variable | Purpose |
|---|---|
| `ALPACA_PAPER_API_KEY` / `ALPACA_PAPER_API_SECRET` | Paper credentials |
| `EXPECTED_ACCOUNT_ID` | Account identity the broker must report, or every gate fails |
| `KILL_SWITCH` | Process-level switch; the durable switch is separate and starts engaged |
| `BROKER_HEALTHY`, `MARKET_DATA_HEALTHY`, `RISK_ENGINE_HEALTHY`, `RECONCILIATION_SUCCESSFUL` | Operator assertions that must all be true before any order is allowed |
| `QUANTBOT_CONFIG` | Strategy config path (default `config/strategy-v1.yaml`) |
| `QUANTBOT_DB_PATH` | Durable SQLite ledger (default `quantbot.db`) |
| `QUANTBOT_LOCK_PATH` | Single-writer lock (default `quantbot.lock`) |
| `QUANTBOT_REPORTS_DIR` | Weekly report output (default `reports`) |
| `QUANTBOT_MARKET_DATA_FEED` | `iex` (free tier) or `sip` (subscription) |
| `QUANTBOT_MAX_DATA_AGE_SECONDS` | Staleness halt threshold (default 86400, sized for daily bars) |
| `QUANTBOT_GIT_COMMIT` | Pin the deployed commit instead of shelling out to git |

First-run order:

```bash
quantbot status
```

```bash
quantbot doctor
```

```bash
quantbot sync-data
```

```bash
quantbot reconcile
```

The durable kill switch starts **engaged** (`default fail-closed state`). Clearing it
requires every readiness gate to pass:

```bash
quantbot kill-switch clear --reason "all paper gates verified"
```

Then one cycle, or the scheduling daemon:

```bash
quantbot run-once
```

```bash
quantbot daemon
```

`daemon` sleeps until the broker's own next close + 5 minutes, runs one cycle, and repeats.
It holds a single-writer lock and stops cleanly on SIGINT/SIGTERM. Never run two writers.

## Cycle anatomy

`RunOnceCycle` (`src/quantbot/operations/cycle.py`) runs strictly sequentially:

1. **Ledger sync + recovery** (`LedgerSyncingRecovery`) — ingests broker fills and open
   orders, then derives open positions from the durable fill ledger alone and requires them
   to equal the broker's positions. Disagreement yields `POSITION_LEDGER_MISMATCH` and
   blocks new orders. Only on agreement is an account snapshot written, which is what the
   next reconciliation compares against.
2. **Kill-switch check** — durable state, checked after recovery.
3. **Data sync** (`MarketDataSync`) — fetches the warmup window of daily bars for the whole
   universe, aligns them to authoritative XNYS session closes from the broker calendar,
   validates for gaps/duplicates/future bars, and caches only validated bars.
4. **Staleness gate** — halts on future, stale, or incomplete data.
5. **Strategy** (`AdaptiveMomentumRunner`) — rebuilds the monthly roster deterministically
   from cached bars, evaluates every universe symbol at the last completed close for action
   at the next open, sizes entries through the real risk policy with in-cycle reservations,
   and records one signal row per symbol per run.
6. **Submission** (`ExecutionCoordinator`) — persists intent before network I/O, never
   replaces a client order identity, and recovers ambiguous submissions by lookup only.

Any hard failure engages the durable kill switch and halts. That is intended: it requires a
human to look before trading resumes.

## Position state

Broker positions carry no stop, so the trailing stop is reconstructed durably:

- `entered_at` is derived from the fill ledger (first fill of the currently open run).
- `initial_stop` and the ratcheted `active_stop` are persisted in each cycle's signal
  payload under `position_state` and read back on the next cycle.
- A position with no prior `position_state` (e.g. adopted from the broker) bootstraps its
  stop from `average_entry_price - initial_stop_atr * ATR` and records that bootstrap.

## Duplicate-order protection

`client_order_id` is a pure function of (strategy version, symbol, signal date, side,
sequence). Re-running the same trading day mints the identical identity, and the strategy
runner skips any symbol whose intent is already durable
(`DUPLICATE_INTENT_ALREADY_DURABLE`). Covered by
`tests/integration/test_runtime_wiring.py::test_rerunning_the_same_trading_day_never_submits_a_duplicate_order`.

## Reports

```bash
quantbot report-weekly --iso-year 2026 --iso-week 33
```

Writes `reports/weekly/YYYY-WW.md`. With no arguments it reports the last completed ISO
week. Every field is `NOT_YET_OBSERVED` unless the durable ledger contains real forward
evidence — backtests never fill these in.

```bash
quantbot backtest --variant FULL_STRATEGY
```

Runs against the durable bar cache, so `sync-data` must have run first. Omit `--variant` to
run all six required comparison variants.

## Research

Two databases, deliberately separate. `quantbot.db` is the operational ledger and its bar
cache feeds the `bar_set_hash` recorded against live signals. `research/bars.db` holds long
history and must never be merged into it, or live audit provenance changes retroactively.

```bash
QUANTBOT_MARKET_DATA_FEED=sip uv run python scripts/fetch_research_bars.py 2016-01-01 research/bars.db
```

```bash
QUANTBOT_MARKET_DATA_FEED=sip uv run python -u scripts/run_research_experiment.py research/bars.db 100 5
```

The `sip` feed reaches 2016-01-04 for all 23 symbols; `iex` is only usable from 2020-07-27.
Live trading stays on `iex` (measured divergence in daily OHLC on these ETFs is 0.003–0.04%
median, so it does not affect signals). Use `-u`: the harness buffers output otherwise and
a long run appears to hang.

Findings are recorded in `reports/research/`, with each study pinned by an immutable
`ExperimentManifest` hashing the config, the bar set and the git commit.

**Promotion blocker to know about before designing a research cycle:** `ComponentSwitches`
exists only in the backtest engine. The live `evaluate_symbol` hardcodes every filter — the
Donchian entry gate, the trend filter, the regime filter and the trailing stop are always
applied. The backtester can therefore simulate designs the live system cannot execute.
Promoting any ablation-derived design requires threading component switches into
`StrategyConfig` and `evaluate_symbol` first, as a new strategy version. Deliberately not
built yet: it is only worth doing once a design actually passes its holdout.

## Known gaps / next work

1. **No credentials, so zero elapsed paper observation.** This is the only thing standing
   between the current state and starting the 30/60-day qualification window. It cannot be
   substituted with backtests.
2. **The trade stream is not held open between cycles.** `AlpacaPaperTradeStream` and its
   event handling are implemented and tested but unused by the daily cycle, which ingests
   broker-confirmed fills over REST instead. Adequate for a daily post-close strategy; wire
   the stream if intraday order events are ever needed.
3. **Half-day sessions** are handled correctly (the close time comes from the broker
   calendar), but no live half-day has been observed yet.
4. **`paper-smoke`** submits exactly one 1-share benchmark order after every gate passes and
   the operator supplies the exact acknowledgement string. It is an operator action; it has
   never been run.

## Rules that must not be relaxed

- Never change strategy parameters to make a losing period look better. A losing week is
  not authorization to change anything.
- Never synthesize elapsed trading days, fills, or performance. `NOT_YET_OBSERVED` is a
  valid answer and the reporting code enforces it.
- Never enable live trading. Never add live credentials.
- Never raise risk to recover losses; never average down outside the researched policy.
- Zero valid signals for a week is a legitimate outcome — record `NO_VALID_SIGNALS`.
