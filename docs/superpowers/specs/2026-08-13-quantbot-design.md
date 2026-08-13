# QuantBot Design

**Status:** Approved by the user's autonomous continuation directive

**Date:** 2026-08-13

## Purpose

QuantBot is a deterministic, restart-safe algorithmic trading application that researches and backtests an Adaptive Momentum strategy locally, then forward-tests the same versioned strategy through an established broker's paper environment. Alpaca Paper Trading is the first execution target. The broker remains authoritative for cash, buying power, equity, positions, open orders, and fills.

The project is not complete when code builds or a backtest passes. Engineering completion is `PAPER-QUALIFIED`, which additionally requires broker integration evidence, safe continuous operation, at least 30 observed trading days, a meaningful trade sample, weekly reports, benchmark comparisons, and no unresolved high-severity reliability defects. Elapsed days, fills, and performance are never fabricated.

## Approach Decision

Three approaches were considered:

1. **Modular monolith (selected).** One Python service and CLI contain small domain modules behind broker, market-data, persistence, and clock interfaces. This minimizes operational burden while retaining strict boundaries and testability.
2. **Multiple worker processes.** Separate data, signal, execution, and reporting services improve failure isolation, but add coordination and deployment complexity before the strategy is qualified.
3. **Distributed event-sourced platform.** A durable event bus and independent services offer the strongest scaling path, but are disproportionate to one paper account and obscure the initial safety proof.

The modular monolith persists all decisions and broker observations in SQLite/WAL. Its interfaces and event records permit future process separation without strategy changes.

## Safety Invariants

- Forward PAPER and LIVE orders go only through a third-party brokerage adapter. Local execution simulation exists only inside historical backtests.
- Strategy and risk code never import a broker SDK or refer to broker authentication.
- The broker is authoritative during forward operation. Local records are an audit trail and cache.
- New orders are blocked unless broker health, market-data freshness, account verification, reconciliation, risk health, and the kill switch all pass.
- LIVE is disabled by default and cannot be activated by credentials alone. It additionally requires `TRADING_MODE=LIVE`, `BROKER_ENVIRONMENT=LIVE`, `LIVE_TRADING_ACKNOWLEDGED=true`, an expected account match, healthy dependencies, successful reconciliation, and kill switch off. No automation will set these values.
- Paper and live credentials are separate environment-variable sets. Secrets are never logged, serialized into state, returned by health output, or committed.
- Every intent is persisted before submission and receives a deterministic client order ID. An ambiguous submission is looked up by client ID before any retry.
- HTTP success is only acknowledgement. Holdings change only from broker-confirmed fills or broker reconciliation.
- Partial fills update only the executed quantity. Cancel acknowledgement is not terminal cancellation, and later fill events remain valid.
- Extended-hours trading is disabled by default. Broker clock/calendar state, not local wall-clock assumptions, controls eligibility.
- Strategy inputs use completed market bars only. A signal from session T may first execute during session T+1.
- The deployed strategy configuration is immutable and identified by strategy ID, semantic version, Git commit, canonical configuration hash, and deployment timestamp.

## Architecture

```text
Alpaca Market Data ---> MarketDataProvider ---> validated bars / freshness
                                                 |
                                                 v
Strategy ---> Portfolio Target ---> Risk Engine ---> Order Intent
                                                    |
SQLite audit/state <--- Execution Coordinator <-----+
       ^                         |
       |                         v
Reconciler <-------------- BrokerAdapter ---> Alpaca Paper Trading
       ^                         |
       +--------- order/fill stream + REST snapshots

Metrics / logs / weekly reports consume the same durable state.
```

### Domain models

All money, prices, quantities, and risk calculations use `Decimal`. Timestamps are timezone-aware UTC. Core immutable records include bars, signals, account snapshots, positions, order intents, broker orders, fill events, reconciliation results, equity snapshots, incidents, and strategy deployments.

The normalized order state machine is:

```text
RISK_APPROVED -> ORDER_CREATED -> SUBMITTING -> BROKER_ACCEPTED
                                              -> PARTIALLY_FILLED -> FILLED
                                              -> REJECTED
                                              -> CANCEL_PENDING -> CANCELLED
                                              -> EXPIRED
                                              -> ERROR / SUBMISSION_UNKNOWN
```

Transitions are validated. Duplicate and out-of-order broker events are idempotent. `SUBMISSION_UNKNOWN` blocks automatic resubmission until lookup/reconciliation resolves the outcome.

### Broker boundary

`BrokerAdapter` exposes connection/health, account, cash/buying-power, positions, orders, client-ID lookup, fills, cancel, clock/calendar, capabilities, and order-event streaming. `AlpacaBrokerAdapter` is direct async HTTP/WebSocket integration against the paper host by default. It maps V1 MARKET, LIMIT, STOP, and STOP_LIMIT orders explicitly and rejects unsupported combinations rather than translating them.

Alpaca details follow the current official API: paper REST at `https://paper-api.alpaca.markets`, live REST at `https://api.alpaca.markets`, account/position/order resources under `/v2`, client-ID lookup at `/v2/orders:by_client_order_id`, and paper trade updates at `wss://paper-api.alpaca.markets/stream`. Reconnect performs reauthentication, resubscription, and REST reconciliation because the stream has no documented resume cursor.

Tradier and IBKR modules initially expose explicit unavailable/configuration-aware extension points. Tradier later uses its bearer-token production/sandbox REST environments. IBKR is modeled as a session-owning adapter rather than an API-key adapter; the preferred personal deployment path is TWS API through IB Gateway, with its operator authentication/restart constraints. Neither stub pretends to execute.

### Market-data boundary

`MarketDataProvider` is independent from execution and provides historical bars, latest completed bars, market calendar, health/freshness, and optional streaming. The Alpaca provider paginates all historical responses and explicitly selects the configured feed (`iex` by default for basic accounts). Daily adjusted data is stored by symbol/date with provider and adjustment metadata.

The system rejects duplicate or non-monotonic bars, nonpositive OHLC/volume values, high below low, timestamps outside the expected session, and stale data. Corporate-action handling is explicit: research uses adjusted bars; execution decisions record exactly which bar set and adjustment mode produced them.

## Deterministic V1 Strategy

The formulas below are proposed V1 defaults because the user specified the strategy components but not exact periods, universe, or scheduling. They are configuration, not claims of optimality.

### Universe and schedule

- Static diversified ETF universe: `SPY, QQQ, IWM, MDY, EFA, EEM, VNQ, XLB, XLE, XLF, XLI, XLK, XLP, XLU, XLV, XLY, DBC, GLD, TLT, IEF, TIP, LQD, HYG`.
- `SPY` is the regime instrument and benchmark. A static version-pinned ETF universe avoids retrospective constituent changes in V1, but is explicitly not represented as a survivorship-bias-free institutional universe.
- Signals are evaluated after a completed regular-session daily bar is available and fresh.
- Exits are evaluated each session. On each month's final broker-calendar trading session, positive-momentum symbols that pass the long-term trend filter are ranked and the top 10 form the roster effective next session. There is no mid-month backfill.
- Orders generated from session T data become eligible during the next broker-confirmed regular session. Extended hours remains false.

### Indicators

- **12-1 momentum:** `close[t-21] / close[t-252] - 1`, requiring 253 completed observations. Skipping the most recent 21 sessions limits short-term reversal exposure.
- **Long-term trend:** instrument `close[t] > SMA200[t]`.
- **Market regime:** `SPY close[t] > SPY SMA200[t]`. If false, no new long positions; existing positions follow exit rules.
- **Donchian entry:** `close[t] > max(high[t-55:t-1])`. The current bar is excluded from the channel.
- **Donchian exit:** `close[t] < min(low[t-20:t-1])`, also excluding the current bar.
- **ATR:** Wilder ATR(20), seeded with the arithmetic mean of the first 20 true ranges. True range is the maximum of high-low, absolute high-previous-close, and absolute low-previous-close.
- **Initial stop distance:** `2 * ATR20` at the signal close.
- **Trailing exit:** the greater of the prior active stop and `highest_high_since_entry - 3 * current_ATR20`; the stop never loosens, and a next-session exit intent is generated when the completed close is below it.

### Selection and exits

Positive-momentum symbols are ranked descending by 12-1 momentum, then symbol ascending as a deterministic tie-break. A new long candidate must be in the active top-10 monthly roster, pass instrument trend, market regime, and Donchian entry, and fit all risk limits. At most 20 positions are allowed globally; the V1 roster caps this strategy at 10.

An exit is generated when any of these is true: Donchian exit, instrument trend failure, trailing/initial stop breach on completed close, the instrument leaves the newly effective monthly roster, or it leaves the configured universe in a new strategy version. Regime failure prevents entries but does not force immediate liquidation; this distinction is fixed for V1 and testable through ablation.

### Position sizing and portfolio risk

For a candidate, whole-share quantity is the nonnegative floor of the minimum of:

```text
(equity * effective_risk_fraction) / stop_distance
(remaining allowed portfolio open risk) / stop_distance
(equity * 10%) / reference_price
remaining_gross_exposure / reference_price
available_buying_power / reference_price
```

Base risk per trade is 0.50%; maximum aggregate open risk is 5%; maximum position market value is 10% of equity; maximum gross exposure is 100%; maximum positions is 20. Open risk uses broker-confirmed quantity times the positive difference between current/reference price and active stop.

Drawdown is measured from the high-water equity snapshot. New-risk multipliers are 1.00 below 5%, 0.75 from 5% through under 10%, 0.50 from 10% through under 15%, and zero at 15% or more. At 20% or more, the global trading halt activates. Risk never increases to recover losses.

## Execution and Recovery

The coordinator performs the following transaction-like sequence:

1. Verify all readiness gates and broker market clock.
2. Build and persist the deterministic intent and client order ID from strategy version, symbol, signal date, side, and sequence.
3. Re-run risk checks against a fresh broker account/position snapshot.
4. Mark `SUBMITTING` and call the broker once.
5. Persist the acknowledgement as `BROKER_ACCEPTED`, or persist rejection.
6. On timeout/disconnect, mark `SUBMISSION_UNKNOWN`, query Alpaca by client ID, and adopt the broker order if found. Retry with the same ID only after a definitive not-found result and bounded policy approval.
7. Consume trade updates, applying event execution quantity exactly once. Periodic REST reconciliation corrects observation gaps while retaining an audit incident.

Startup always connects, verifies the expected account ID, collects broker snapshots, and reconciles before enabling order creation. Any material mismatch produces `RECONCILIATION_REQUIRED`; QuantBot never changes broker positions to make them resemble local state. Restart recovery reconstructs pending intents, resolves unknown submissions, refreshes orders/fills/positions, and remains halted until consistent.

## Persistence, Observability, and Operations

SQLite in WAL mode is the initial durable store. Schema migrations are explicit. Every run gets a run ID. Structured JSON logs redact configured secret values and authentication headers. Metrics cover broker/API health, request latency and errors, market-data age, stream reconnects, reconciliation outcome, order transitions/rejections, duplicate-prevention decisions, equity/drawdown, exposure, process restarts, stale data, and unexpected exceptions.

`quantbot run-once` performs one safe deterministic cycle. `quantbot daemon` schedules cycles around the broker calendar and runs monitoring/reconciliation continuously. A single-instance lock prevents two writers. Graceful shutdown stops new submissions, persists state, and closes streams. The initial operator uses an external process supervisor; the repository supplies documented Windows Task Scheduler and systemd examples rather than hiding process management inside trading logic.

The kill switch is durable, defaults on until initialization succeeds, is operator-controllable through CLI, and is also set automatically by hard safety failures. Clearing it never bypasses account, reconciliation, data, or risk gates.

`PROJECT_STATUS.md`, `docs/agent-runbook.md`, `logs/`, and `reports/` make progress and operation resumable by later sessions. Status fields distinguish verified evidence from pending qualification.

## Backtesting and Research

The backtest reuses production indicators, signal rules, portfolio targets, and risk policy, but has a separately named historical execution model. It fills next-session orders using configurable conservative assumptions, supports commissions and slippage, handles splits/dividends through adjusted data, and never imports the broker adapter.

Validation includes synthetic fixtures with known indicator outputs, invariance to future-data mutation, next-bar execution assertions, warm-up enforcement, missing-session handling, cash/position accounting, partial allocation, and benchmark parity tests. Research runs compare SPY buy-and-hold, SMA200, Donchian-only, pure 12-1 momentum, momentum+trend, and the full strategy. They record data/version hashes, parameters, costs, train/validation/holdout ranges, walk-forward splits, parameter-neighborhood robustness, and ablations. A candidate never mutates the deployed paper version without human review and a new identity.

## Reporting and Qualification

Daily equity and incident records feed weekly Markdown reports at `reports/weekly/YYYY-WW.md`, containing every field required by the project objective. Performance includes realized/unrealized/cumulative P&L, return, CAGR-equivalent, Sharpe, Sortino, drawdown, profit factor, expectancy, win rate, average winner/loser, exposure, turnover, costs/slippage, profitable periods, and trades per week.

Qualification is evidence-based. A durable qualification record counts actual trading sessions observed and completed broker-confirmed round trips. It targets at least 30 trading days and 30 completed trades, preferably 60 days and 50-100 trades, without increasing frequency. At the end of a valid window, results are classified as positive evidence, inconclusive, or negative evidence separately from engineering reliability.

## Testing Layers

- Unit tests for models, configuration guards, indicators, deterministic IDs, state transitions, risk sizing, drawdown controls, reconciliation diffs, accounting, reports, and redaction.
- Contract tests run each adapter against scripted HTTP/WebSocket transports, including pagination, rate limits, malformed payloads, partial fills, cancel races, and timeout lookup-before-retry.
- Integration tests use temporary SQLite databases and a deterministic broker double that cannot be selected in PAPER/LIVE configuration.
- Backtest validation uses frozen, synthetic, and holdout fixtures with explicit anti-look-ahead checks.
- Credential-gated Alpaca Paper smoke tests are manual/CI-secret opt-in and create only controlled orders after explicit operator acknowledgement. They are never part of default tests and never target live.
- Restart, kill-switch, stale-stream, and reconciliation fault-injection tests are required before the operational daemon is considered safe.

## Delivery Phases

1. **Foundation:** package, domain types, configuration/live guards, durable schema, logging, status/runbook.
2. **Strategy and research:** data validation, indicators, deterministic V1, risk engine, backtest and benchmark reports.
3. **Alpaca Paper vertical slice:** broker and market-data clients, lifecycle, idempotency, fills, reconciliation, streaming, controlled smoke command.
4. **Operations:** scheduler, kill switch, restart recovery, metrics, health, incident and weekly reporting.
5. **Qualification:** credentialed paper deployment, real observations, defect repair, controlled research, and evidence tracking.
6. **Additional adapters:** Tradier sandbox, then IBKR session-aware implementation, each behind capability and contract tests.

Phases 1-4 can be completed without credentials. Phase 5 cannot be declared complete without external paper credentials and real elapsed market time; the system must be left safely runnable with explicit continuation instructions when those are the only remaining dependencies.

## Official Integration References

- Alpaca Authentication: https://docs.alpaca.markets/us/v1.1/docs/authentication-1
- Alpaca Orders: https://docs.alpaca.markets/us/docs/orders-at-alpaca
- Alpaca Trading WebSocket: https://docs.alpaca.markets/us/docs/websocket-streaming
- Alpaca Market Data: https://docs.alpaca.markets/us/docs/about-market-data-api
- Tradier Endpoints: https://docs.tradier.com/docs/endpoints
- Tradier Authentication: https://docs.tradier.com/docs/authentication
- Tradier Streaming: https://docs.tradier.com/docs/streaming-data
- IBKR API overview: https://www.interactivebrokers.com/campus/ibkr-api-page/ibkr-api-home/
- IBKR TWS connectivity: https://www.interactivebrokers.com/docs/tws-api/doc/connectivity/establishing-an-api-connection
- IBKR Client Portal API: https://www.interactivebrokers.com/campus/ibkr-api-page/cpapi-v1/
