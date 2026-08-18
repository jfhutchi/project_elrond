# QuantBot — Agent Orientation

**Read this first if you are an agent picking up this project.**
Last hand-updated: 2026-08-18. Branch: `claude/quantbot-trading-system-2bbc25`.

For *verified numbers* (equity, trade count, run ids) read **`PROJECT_STATUS.md`** instead —
it is regenerated from the durable ledger every cycle and is the only trustworthy source for
current figures. This file holds the things a generator cannot know: the goal, the rules, and
what is in flight.

---

## 1. The goal

Grow a **$100 Alpaca paper account** by systematic trading, compounding gains, toward doubling
capital. The operator's framing is "fortune favors the bold" — but see §2, because the way this
project fails is by chasing that framing into ruin.

## 2. The rules that override the goal

These come from the operator's persistent goal document. They are not suggestions and a losing
week is **not** authorization to break them.

| Rule | Why |
|---|---|
| **`LIVE_TRADING` stays DISABLED.** Only a human may enable it. Never insert live API credentials. | The system has not earned real money. |
| **Never optimize for short-term paper profit.** | Fitting to a week of noise is how this dies. |
| **Strategy is immutable once deployed.** Changes require a new version + `configuration_hash` + `git_commit`. | Without versioned identity, no result means anything. |
| **Never fabricate** elapsed trading days, broker fills, or profitability. | Report `NOT_YET_OBSERVED` when the ledger lacks evidence. |
| **A losing week is not a reason to change the strategy.** | Only a pre-registered research cycle is. |
| Risk limits: 0.5% risk/trade · 5% max open risk · 10% max position · 20% drawdown halt. | |

**Qualification window: 30–60 trading days AND 30+ completed trades before any conclusion.**
We are on **day 1**. Nothing observed so far is evidence of anything.

## 3. The single most important research finding

Across **10 research cycles and ~45 trials**, nothing built here beats **SPY buy-and-hold**
(15.36% CAGR, Sharpe 0.90, $454.70 from $100 over 10.6 years).

`REFUTED.md` holds 15 refuted hypotheses. **Read it before proposing an idea** — the odds are
high that it is already in there. The deployed config was selected off the *worst-ranked*
variant precisely to avoid selection bias.

Do not add a strategy component because a backtest liked it. Multiple-testing correction
(Deflated Sharpe / PBO, Bailey & López de Prado) is mandatory; `scripts/deflated_sharpe.py`
exists for this.

## 4. Current state

- **Deployed:** v1.2.0 — `adaptive-momentum-v1-309894d8d8a5296e`, config `config/strategy-v1-2.yaml`
  - Active components: momentum, asset_trend, roster_exit
  - Disabled: market_regime, donchian_entry, donchian_exit, trailing_stop
- **Positions:** IWM, MDY, XLE (filled 2026-08-17)
- **Queued for next open:** DBC, XLV, XLK, XLI — DAY orders from `run-20260818T005730Z`
- **Universe:** 23 symbols, roster of 10, ~10% of equity per name
- **Crypto sleeve (v2.0.0, `strategy-crypto-v2.yaml`)** is written and tested but **not deployed** —
  it needs a *second* Alpaca paper account. That is an operator action; do not create accounts.

### What runs unattended
| Process | Role |
|---|---|
| `quantbot daemon` | Broker-clock driven. Sleeps to each session close + 5 min, runs one cycle. Auto-starts via `%APPDATA%\...\Startup\quantbot-daemon.cmd`. |
| `scripts/supervisor.py --watch` | Watchdog. Restarts a *dead* daemon. **Never** clears the kill switch or resolves a reconciliation failure. |

Orders are evaluated at a **completed** session close and submitted as DAY orders for the
**next** open. That is deliberate — evaluating before the close would be look-ahead.

## 5. Fixed on 2026-08-18 (both were severity-1)

1. **The daemon could never run a cycle.** `DaemonRunner` and `RunOnceCycle` both took
   `SingleWriterLock` on the same path, and the lock is not reentrant — the daemon would have
   crashed on its first cycle. They are now two locks: `quantbot.daemon.lock` excludes a second
   *scheduler*; `quantbot.lock` excludes a second *writer* and is held only during a cycle.
   Side benefit: manual `run-once` and `reconcile` now work while the daemon is alive.
2. **Unfilled orders consumed no risk budget.** A submitted-but-unfilled order was neither a
   broker position nor an in-run reservation, so a re-run sized entries against a budget it had
   already spent. It deployed a further 20% of equity and took an Alpaca 403 mid-submission.
   `_pending_reservations` now seeds the budget from open broker orders, and the
   duplicate-intent check runs *before* sizing so the reported reason stays truthful.
   The two bug-produced orders (QQQ, EFA) were cancelled; buying power went $9.80 → $29.83.

## 6. How to check state

```bash
uv run python scripts/supervisor.py          # one pass: daemon, kill switch, reconciliation
```
```bash
uv run python scripts/dashboard.py           # equity, positions, watchdog health
```

Set `QUANTBOT_CONFIG` to `config/strategy-v1-2.yaml` for any manual command, or you will
silently operate the wrong strategy identity.

## 7. Open items

- [ ] Accumulate paper observations toward the 30-day qualification window (day 1 of 30)
- [ ] Crypto sleeve blocked on a second Alpaca paper account (**operator action**)
- [ ] Exposure normalisation for asset-class sleeves — research, **not yet pre-registered**
- [ ] Fills are ingested from the REST ledger once per cycle; the push trade stream is not held
      open between cycles

## 8. If you are about to change the strategy

Stop. Open `docs/agent-runbook.md` and follow the controlled research cycle: pre-register the
hypothesis, run it, apply multiple-testing correction, and record the outcome in `REFUTED.md`
whether it passed or failed. An uncorrected backtest improvement is not evidence.
