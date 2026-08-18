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
| `scripts/supervisor.py --watch` | Watchdog. Restarts a *dead* daemon. **Never** clears the kill switch or resolves a reconciliation failure. Launched by the startup entry, which is version-controlled at `scripts/quantbot-watchdog.cmd` and installed to the Windows Startup folder. |

Verify both are alive with `scripts/supervisor.py`. `logs/watchdog-stderr.log` should stay
**empty** — anything in it means the watchdog itself is crashing.

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
3. **Incidents could never be closed.** The schema had `resolved_at` but nothing wrote it, so
   every incident stayed unresolved forever and the supervisor reported a permanent warning.
   `resolve_incident` closes that.

### The kill switch fired, and that was correct
The 403 halted the system with `UNHANDLED_OPERATIONAL_EXCEPTION`, and the supervisor
reported it without clearing it — exactly the designed behaviour, now proven in production.
It was cleared only after the root cause was fixed, against six empirically verified
readiness checks (paper mode, account id, broker health, data freshness, risk, reconciliation).
**Never clear it by asserting the evidence flags — verify each one.**

## 6. How to check state

```bash
uv run python scripts/supervisor.py          # one pass: daemon, kill switch, reconciliation
```
```bash
uv run python scripts/dashboard.py           # equity, positions, watchdog health
```

Set `QUANTBOT_CONFIG` to `config/strategy-v1-2.yaml` for any manual command, or you will
silently operate the wrong strategy identity.

## 6b. Cycle 11 research (2026-08-18) — four survivors, none of them alpha

Full detail: `reports/research/cycle11-results.md`. Pre-registered before measurement.

**The core finding is unchanged — nothing beats SPY buy-and-hold on return.** What survived is
about risk, cost and mechanics, which is what is left once an exhausted holdout rules out
signal discovery:

| # | Survived | Effect |
|---|---|---|
| S1 | **Timing luck** | 2.31 CAGR points decided by which day of the month the rotation runs. Removable by tranching. The deployed strategy currently holds this bet. |
| S2 | **Vol targeting as drawdown control** | maxDD reduced on 16 of 18 assets, p=0.0007. Costs ~1.83 CAGR pts/yr. **Not alpha.** |
| S3 | **Real spread is ~1bp, not 5bps** | SPY 0.26bps measured. The deployed cost model is miscalibrated. |
| S4 | **Halt resumption delay** | The halt is cheap; the delay costs 8-20% of terminal wealth. |

Killed: vol targeting as alpha (z=1.01, p=0.31), the overnight effect, turn-of-month.

**Read `REFUTED.md` before proposing anything** — it now lists 18 refuted hypotheses.

## 6c. Cycle 13 + v1.3.0 built (2026-08-18)

**Volatility-targeted sizing is implemented, tested, and DISABLED by default.** Deployed
identities are unchanged and a regression test pins them:

| config | identity | status |
|---|---|---|
| strategy-v1-2.yaml | `...309894d8d8a5296e` | **DEPLOYED, unchanged** |
| strategy-v1-3.yaml | `...f785a11dc906265f` | built, **not deployed** |

Why v1.3.0 exists: the configured thresholds are `[500, 1000, 1500, 2000]` bps, so **entries
halt at 15% and LIQUIDATION is required at 20%**. V1.2.0's historical max drawdown is **26.4%**
— the live strategy would have been *liquidated* historically, not merely halted. Vol targeting
at 100bps brings it to **14.4%, inside both thresholds**.

Correction worth knowing: an earlier version of this section said the halt was at 20% and that
150bps (17.1%) was inside it. Both were wrong — 17.1% still trips the 15% entry halt. The
drawdown halt is also **stateless**, recomputed each cycle, so it self-clears with no resumption
delay; cycle 11's 8-20% delay cost applies to the **kill switch**, which does need a human.

Established: vol targeting reduces drawdown (16/18 assets, p=0.0007).
**Not established: that it makes more money** — bootstrap CI [−$86.76, +$76.79], loses in 36.8%
of resamples. Do not justify v1.3.0 by returns.

Two traps found while building it, both recorded in REFUTED.md: a 15% target is a **silent
no-op** against a 10% weight cap, and cycle 12's portfolio-level result does **not** transfer to
per-position sizing.

**Deploying v1.3.0 restarts the 30-day qualification window (currently day 1) and is an
operator decision, not an agent one.**

## 7. Open items

- [ ] Accumulate paper observations toward the 30-day qualification window (day 1 of 30)
- [ ] Crypto sleeve blocked on a second Alpaca paper account (**operator action**)
- [ ] Exposure normalisation for asset-class sleeves — research, **not yet pre-registered**
- [ ] Fills are ingested from the REST ledger once per cycle; the push trade stream is not held
      open between cycles
- [ ] **Tranche the rebalance date** — $61.24 of terminal-wealth spread per $100 decided by the
      calendar alone, the largest free effect found in 13 cycles. Design settled in
      `docs/tranching-spec.md`: **5 tranches** (88% of the benefit, $2.00 minimum trade against
      Alpaca's $1 floor). The cheap weight-ramp shortcut is measured and refuted, so the roster
      machinery genuinely has to change.
      **Scheduling and weighting are now built and tested** in `src/quantbot/strategy/tranches.py`
      (11 tests, including the guarantee that 1 tranche reproduces month-end exactly, and that
      the tranche count is derived from equity so it widens as the account grows).
      Remaining: roster identity, durable state, and config wiring — see the spec.
- [ ] **Decide whether to deploy v1.3.0** — see 6c; restarts the qualification window
- [ ] **Automate halt resumption** (cycle 11 S4) — worth more than any signal tested
- [ ] **Recalibrate the cost model** from 5bps to the measured ~1bp (cycle 11 S3)

## 8. If you are about to change the strategy

Stop. Open `docs/agent-runbook.md` and follow the controlled research cycle: pre-register the
hypothesis, run it, apply multiple-testing correction, and record the outcome in `REFUTED.md`
whether it passed or failed. An uncorrected backtest improvement is not evidence.
