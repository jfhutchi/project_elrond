# QuantBot — Agent Orientation

## START HERE

### ⚠ FIXED 2026-08-22: a spent data window read as fresh capacity

**Corrected claim.** An earlier version of this block said the v0.2 luck bar was too low. **That
was wrong**, and the error is instructive: `PRIOR_TRIALS = 68` was already seeded in
`HypothesisRegistry`, so an empty registry reports 68 cumulative trials and a **2.905** bar,
matching cycle 17. Verified by running it, after publishing the wrong version.

**What was actually broken.** `window_consumption` counted only `hypothesis_data_windows` rows,
and there were none — so the equity window cycles 2-10 spent reported **`UNTOUCHED`**. The bar
was right and the capacity was wrong: a new hypothesis would have been judged against the
correct threshold while being told a spent window was fresh.

Section 6z of this file said the window would answer `EXHAUSTED` *"once seeded"*. Nothing seeded
it, and that sentence sat here through eight subsequent issues.

**Fixed:** `PRIOR_WINDOW_CONSUMPTION` records the pre-registry consumption as a constant — the
counterpart `PRIOR_TRIALS` always had. SIP US equities 2016-2026 now reports `EXHAUSTED` at 68
trials on an empty registry, and a vendor rename does not reset it (#40).

Recorded as a constant rather than as synthetic registrations: the trials were really spent, so
recording them is bookkeeping, while inventing hypothesis rows for experiments this schema never
saw would be fabricated provenance a later agent would read as real.

**Also fixed:** research memory was genuinely empty, so the novelty gate had nothing to recall
and a question answered as REFUTED #22 (t=0.05) passed it. The 25 settled findings are now
seeded (`quantbot.research.prior`) and loaded by `research-cycle`.

**Not deployed:** the Pi runs an older checkout.

### ⚠ SEVERITY-1 (FIX BUILT, NOT DEPLOYED): the drawdown halt is a trap

**The design defect is real and unchanged.** Once equity is 15% below its high-water mark,
`entry_halted` stops new positions — but a strategy that cannot take positions cannot earn back
the drawdown, so equity is frozen, so the drawdown is frozen. **The halt has no exit.** If the
live account reaches −15%, only a deposit or manual intervention releases it.

**Two figures previously in this block did not describe the deployed config, corrected
2026-08-19 by measurement through the production engine on SIP 2016-2026:**

| claim as written | measured, deployed `strategy-v1-2.yaml` |
|---|---|
| "the halt blocks entry on **70.8% of sessions**" | **0 sessions.** That 70.8% is `strategy-trend-v4`'s number (`docs/per-session-trend-spec.md`), not this config's |
| "has a **26.4%** historical drawdown" | **9.09%** max drawdown. Two independent computations agree; 26.4% does not reproduce on any window tried (SIP 9.09%, IEX 10.87%) |

The deployed config reaches reduced sizing on 371 sessions (13.9%) and **never reaches the 15%
halt threshold** over the measured history. That is why setting `drawdown_halt_floor_bps` to
1000, 2500 or 5000 produces byte-identical results: there is no trap to release on this window.

**What this changes and what it does not.** The trap remains a genuine severity-1 *design*
defect and the fix remains justified — a backtest maximum is not a bound on future drawdown, and
if the account does reach −15% it is stuck. What changes is urgency: the deployed strategy is
not sitting near the threshold, so this is a latent hazard rather than an active one. Deploying
the floor restarts the qualification window, and that trade-off now has honest numbers on both
sides.

**Fixed in code, off by default, not yet deployed.** `drawdown_halt_floor_bps` trades at reduced
size instead of stopping, and it *is* wired into the engine (`engine.py:642` →
`risk/drawdown.py:39`). The four candidate fixes were measured in `scripts/halt_policy_study.py`,
which is a **standalone simulation that never calls `BacktestEngine`** — under #8's rules that is
a `RESEARCH_SCRIPT` result, the same class as the `f77ea08` study that was reproducible and wrong
about this system. The 6 → 25 trades and 19.2% → 50.3% exposure figures come from
`strategy-trend-v4`, not the deployed config.

`scripts/halt_floor_through_engine.py` measures the switch through the production engine on the
deployed config. Result: no effect on this window, for the reason above.

**It does not improve returns, and that is the honest result.** On `strategy-trend-v4` the
released strategy scores Sharpe 0.21 against the halted 0.36, with drawdown 32.7% against 15.6%.
The hard halt's flattering Sharpe was achieved by not trading. Releasing a strategy with no edge
simply exposes more capital to it. The justification for the fix is capital safety — without it a
15% drawdown locks the live account out permanently — **not** performance.

**Hysteresis is refuted, structurally.** A halted account holds nothing, so equity is frozen, so
drawdown is frozen, so no drawdown-based release can ever fire. Measured byte-identical to the
broken policy at every resume threshold.

Deploying it to the live config is an operator decision: it restarts the qualification window.


Run these two first. They take a minute and tell you whether anything is on fire:

```
uv run python scripts/supervisor.py
QUANTBOT_CONFIG=config/strategy-trend-v4.yaml QUANTBOT_MARKET_DATA_FEED=sip uv run python scripts/backtest_config.py
```

**Momentum fails in every universe tested** — US ETFs (t=0.77), US single stocks (t=-2.08, a
price artefact), and global country indices (t=-0.78). Do not propose a new universe; three
structurally different ones have now come back null.

**The single most useful fact in this file:** the deployed momentum strategy is measurably
worthless — beta 0.71, alpha 0.10%/yr at t=0.05, and the ranking does not separate winners from
losers (t=0.77 on ETFs, confirmed on 595 single stocks). It is a fractional SPY position with
extra steps. Do not spend a cycle improving it; that has been done sixteen times.

**The one thing measured to work:** hold SPY while it is above its 200-day average. 10.34%
CAGR, Sharpe 0.91, the highest measured here. The engine cannot yet run it.

**Next concrete task:** `strategy-trend-v4.yaml` makes only 6 trades in 10.6 years against the
benchmark's 25, so it is barely entering the market. `evaluate_symbol` records a reason code on
every signal — read those to find which gate rejects entry. That is a lookup, not a hypothesis.

**Before trusting any number here:** nine analysis errors were caught in one session and every
one of them flattered the result. Three came from a harness that was not connected to the code
under test. If a change produces byte-identical output, suspect the instrument before
concluding the hypothesis failed. `REFUTED.md` has the full list.

---


**Read this first if you are an agent picking up this project.**
Branch: `main`. Sections 1–6ll were hand-written between 2026-08-15 and 2026-08-22 and cover
work up to about issue #75. **Everything merged after that — #82 the console and its control
plane, #136 worker dispatch, #137 environment identity, #157/#171/#175 position protection,
#187–#189 componentised hypothesis lineage, component evidence memory and evidence-aware
recombination — is not described here.** Read the git log and the issue threads for those; this
file will not tell you they exist.

Numbers about the software (test counts, file counts, schema version) were re-verified against
`main` on **2026-09-02** and are corrected in place where they had drifted. Numbers about the
*market* are dated where they were measured and are not re-run.

For *verified numbers* (equity, trade count, run ids) read **`PROJECT_STATUS.md`** instead —
it is regenerated from the durable ledger every cycle and is the only trustworthy source for
current figures. Note that the copy committed here was generated on 2026-08-18 and the ledger
itself is not in version control, so in a fresh clone it is a stale artifact rather than a live
reading. This file holds the things a generator cannot know: the goal, the rules, and what is
in flight.

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

**Deploying v1.3.0 restarts the 30-day qualification window (currently day 1).**

> **⚠ SUPERSEDED 2026-08-30 by #72.** This block used to end "and is an operator decision, not an
> agent one". Paper deployment moves no real capital, so under #72 it is an agent decision made
> against a versioned project default and labelled as one. Restarting the qualification window is
> a research consequence to weigh, not a human gate to wait at. The real-capital boundary is
> unchanged and lives elsewhere.

## 6d. Cycle 14 — what the money should actually be in

`scripts/cycle14_what_to_hold.py`. Sized so max drawdown respects the **15% entry halt**
(entries stop at 15%, liquidation at 20% — both thresholds are real and were mis-stated as a
single 20% rule earlier in this session):

| option | max scale | $100 becomes |
|---|---|---|
| SPY + vol target | 0.625x | **$288.33** |
| rotation + vol target | 1.000x | $203.92 |
| SPY buy-and-hold | 0.375x | $178.06 |
| **DEPLOYED: 10-name rotation** | 0.500x | **$175.99** ← worst of the four |

**+63.8% is available** relative to what is deployed. Caveat that matters: the bootstrap 95% CI
on that gap is **[−$25.08, +$341.56]** — it spans zero, though the alternative loses in only
5.3% of resamples. Suggestive, not established.

Unconstrained the deployed rotation returns $293.91 against SPY's $422.52, **−30.4%** — but
both breach liquidation unlevered, so that comparison is not the decision.

**The frame that matters more than the ranking:** the best option compounds at 10.51%/yr, which
takes $100 to $100,000 in **69 years**. The worst takes 130. The choice between these is worth a
few percent a year; the choice of how much capital enters is worth multiples.

## 6e. Three configs now exist. Only one is deployed.

| config | identity | what it is |
|---|---|---|
| `strategy-v1-2.yaml` | `...309894d8d8a5296e` | **DEPLOYED**. 10-name momentum rotation. Worst of the measured options under the risk rules; 26.4% historical drawdown breaches the 20% liquidation threshold. |
| `strategy-v1-3.yaml` | `...358c3bd691239575` | v1.2 plus vol targeting at 100bps. Drawdown 14.4%, inside both thresholds. |
| `strategy-index-v3.yaml` | `...5e14d03a647f4e6b` | Hold SPY, size by volatility. **DEAD END, do not deploy.** Runs at ~9% invested against the 62.5% designed; raising the risk parameter to 190 and 300 only reached 26% and 30% and still returned 0.98% CAGR. The engine's plain `SPY_SMA200` benchmark beats it tenfold with none of the machinery. |

The third is the real strategic question and it is the operator's, not an agent's: fourteen
cycles found no configuration of the momentum signal that beats holding the index, so that
config stops paying for a prediction and pays only for exposure management. The evidence is
suggestive (loses in 5.3% of resamples) but the bootstrap CI spans zero, which is why it is
built and not deployed.

Any switch restarts the 30-day qualification window.

## 6f. Cycle 15 — the finding that subsumes the rest

**The momentum ranking carries no measurable information.** Tested three ways:

| test | result | luck bar |
|---|---|---|
| alpha against SPY | 0.10%/yr, **t=0.05** | 2.87 |
| top 10 vs all 23 (does selecting beat not selecting?) | +1.43%/yr, **t=0.84** | 2.87 |
| top 10 vs bottom 10 (does the ranking sort at all?) | +2.60%/yr, **t=0.77** | 2.87 |

The third is the damning one: if momentum worked, the best-ranked names should beat the
worst-ranked decisively. They do not.

The deployed rotation is **beta 0.71 with zero alpha**. SPY held at 0.71x reproduces it to
within **0.05 CAGR points** — with no ranking, no rotation, no roster exits and no turnover.

**This explains every null result in cycles 1-10: they were tuning the parameters of a signal
that carries no information.** It should have been the first test run, not the fifteenth.

## 6g. Cycle 16 — momentum fails on single stocks too

Cycle 15 left one defence open: 23 correlated ETFs may have nothing to sort. Cycle 16 tested
595 survivorship-free single names (260 delisted), where the literature says momentum lives.

The ranking sorts **backwards** — bottom decile 36.04% CAGR against top decile 4.46% — and that
is an artefact, not a finding:

| price floor | top | bottom | t |
|---|---|---|---|
| $5 | 4.46% | 36.04% | −2.08 |
| $10 | 3.13% | 20.52% | −1.32 |
| **$20** | **8.07%** | **8.10%** | **0.00** |

It vanishes *exactly* above $20 and is negative everywhere at 200bps, which is realistic for
distressed small caps. Data cached at `research/stocks.db` so no future cycle re-pays for it.

## 6h. The architectural finding (cycle 16)

**This engine cannot express the one rule measured to beat the market.**

| | CAGR | Sharpe | exposure |
|---|---|---|---|
| `SPY_SMA200` benchmark (hold SPY above its 200d average) | **10.34%** | **0.91** | 77% |
| `strategy-index-v3.yaml` | 0.98% | 0.21 | 30% |
| `strategy-trend-v4.yaml` (stops effectively disabled) | 2.52% | 0.30 | 43% |

**That diagnosis was backwards, corrected 2026-08-19 by measurement**
(`reports/research/exposure-diagnosis-2026-08-19.md`). `exposure_fraction` is **capital**
exposure — the mean of `gross_exposure / equity`, `metrics.py:163` — not the fraction of sessions
holding a position. The finding read the first as the second.

Measured separately, `universe: [SPY]` with the trend gate only:

| | time in market | avg weight when held | capital exposure |
|---|---:|---:|---:|
| `SPY_SMA200` benchmark | 77.37% | 100.00% | 77.37% |
| engine, SPY + trend only | **72.50%** | **11.67%** | 8.46% |

**The engine expresses the timing almost exactly** — 72.50% against 77.37%, a 4.87 point gap
rather than 34. The difference is *size*: `max_position_value_bps: 1000` caps any position at
10% of equity, so a single-name config can never deploy more than a tenth.

`target_weight_sizing` changed nothing because it removes the **ATR** cap, not this one. The knob
turned was not the constraint.

Raising the cap moves exposure roughly fivefold (8.46% → 43.50% at a 100% cap) and then
plateaus. **That plateau was diagnosed the same day, and the "second constraint on position
weight" hypothesised here was wrong** — at a 100% cap the engine holds an average weight of
**100.00%** (1161/1161 held sessions ≥95%). What collapses is *time in market*, 72.50% → 43.50%.

The cause is the **drawdown risk ladder**. At a 10% cap the position is too small to draw the
account down 15%, so the ladder never fires; at a 100% cap it halts entry on **928 of 2669
sessions**, first firing 2020-06-11. Widening the ladder restores 72.50% exposure and all 25
trades. Setting `drawdown_halt_floor_bps` does *not* fix it — it releases the halt into the 20%
**liquidation** tier (868 sessions), for +0.79 exposure points and *worse* CAGR.

**Resolved later the same day** (`reports/research/spy-sma200-resolved-2026-08-19.md`): with the
cap, the ladder, and **one corrupt SPY bar** all controlled for, the engine reproduces `SPY_SMA200`
to 0.07 CAGR points. The corrupt bar — 2026-02-02, low 68.64 against a close of 691.70 — tripped a
stop at a price no trade occurred at and cost 20.1% of equity in one session, worth 2.67 CAGR
points, more than the trend gate itself. Now rejected at ingestion (`88ad02b`). **And the target
turned out not to be worth reaching**: `SPY_SMA200` loses to buy-and-hold by 4.75 CAGR points, and
its +0.026 Sharpe edge is negative in paired point estimate and insignificant (t = −1.24).

**This is not a recommendation to widen the ladder** — those runs are diagnostic instruments. They
buy 6.68% CAGR with a 24.46% drawdown, worse risk-adjusted than the benchmark. The defensible
conclusion is the opposite: **a concentrated single-name config is not deployable under this risk
ladder**, and the ladder is the correct component of the two. `SPY_SMA200` draws down 19.50% —
above the 15% halt — so the engine cannot express it at full size without accepting a drawdown the
risk engine exists to refuse. A real limitation, but a *risk-budget* one, not a sizing bug.

**Narrowed to one line.** `adaptive_momentum.py:329-336` applies the trend filter when the roster
is *built* (monthly) — `below_trend`, suppressed when `trend_gate_per_session` is on. (Cited as
`:273` when this was written; re-anchored 2026-09-02.) A symbol below its 200-day average at month end is locked out for the
whole following month, and a symbol absent from the roster cannot be entered at any size —
which is why every sizing knob looked inert. The trend gate is a hold-while-true predicate and
belongs in the per-session `evaluate_symbol` path, where it already exists.

Full spec: `docs/per-session-trend-spec.md`.

**RESOLVED 2026-08-19.** `run_research_backtest.py` ran only FIXED benchmark variants, so it
ignored a config's component selection and three config changes produced identical output. The
harness is fixed (`4644c3a`) and **the diagnosis has been re-measured attributably on the same
SIP window: 41.95% exposure against SPY_SMA200's 77.37%.** The finding holds; the "unattributed"
caveat is lifted.

The re-measurement reproduces every published benchmark figure exactly — SPY 15.37% CAGR /
Sharpe 0.90 / 33.79% DD, SPY_SMA200 10.34% / 0.91 / 19.50% / 77.37%, full strategy 0.50% / 0.15 —
which is why the one row it could not previously produce is trustworthy. See
`reports/research/attributed-harness-2026-08-19.md` (classification `EXPLORATORY`).

The harness defect itself is fixed. `switches_for_config()` maps `StrategyComponents` onto the
engine's `ComponentSwitches`, the runner now emits a `CONFIGURED_STRATEGY` row that actually
applies the config under test, and `tests/unit/backtest/test_configured_harness.py` asserts the
property whose absence allowed the original failure: **change the config, and the result has to
move.** `atr_risk` has no configuration counterpart and is named explicitly rather than left as
an invisible default.

**Re-measured 2026-08-19 on SIP, 2,669 sessions** (see
`reports/research/attributed-harness-2026-08-19.md`, classification `EXPLORATORY`):

| | CAGR | Sharpe | exposure |
|---|---:|---:|---:|
| SPY buy-and-hold | 15.37% | 0.90 | 100% |
| SPY_SMA200 | 10.34% | **0.91** | **77.37%** |
| **deployed `strategy-v1-2.yaml`** | **4.06%** | **0.69** | **41.95%** |
| `FULL_STRATEGY` (what the old runner reported instead) | 0.50% | 0.15 | 24.06% |

The deployed config performs about **eight times better** than the row that stood in for it, and
still loses to buy-and-hold by a wide margin. §6h's exposure diagnosis is confirmed at 41.95%
against 77.37%.

Sharpe 0.69 is **not** an edge: the minimum detectable Sharpe over this window at the current
trial count is ~0.89, so a measured 0.69 sits inside the noise band and would register as
`UNDERPOWERED`. That is the honest reading. Nothing here is confirmatory — the window is
consumed and the run carries no registration, power gate or probes.

## 6i. Hypotheses are now frozen before measurement (#5)

`quantbot.research.HypothesisRegistry` replaces the hand-written pre-registration markdown in
`reports/research/`. A registration is hashed at insert, so a prediction cannot be restated
after the number arrives — which is the only reason cycle 12's "growth-optimal leverage is
below 2x" counts as a miss against the 3.25x it measured.

Three things it refuses, mechanically:

| refusal | rule |
|---|---|
| `CONTAMINATED_WINDOW` | a `PROTECTED_EVALUATION` range may not overlap any range an earlier registration recorded on that dataset, nor its own discovery/validation ranges |
| `UNDERPOWERED` | detecting an annualised Sharpe of `SR` at bar `z` needs `(z/SR)^2` years; ~93 for 0.30, ~8.4 for 1.00. Not the same verdict as `REFUTED` |
| `TAMPERED` | the stored document is rehashed before every confirmatory run |

`FORWARD_PAPER` windows are deliberately exempt from the overlap block and carried by the trial
count instead. Blocking them would retire the paper account after one hypothesis, and it is the
only uncontaminated data this project will ever get.

The multiple-testing burden is counted, never declared: seeded at **68**, what cycles 1-17
already spent. `verify_for_execution` recomputes it before a run, because every registration
since raises the bar — a hypothesis adequately powered when frozen can be underpowered by the
time it executes.

Experiment manifests now carry `mode`, defaulting to `EXPLORATORY`. Only a run naming a
registration hash is `CONFIRMATORY`; anything else is not evidence. `quantbot hypotheses` lists
what is registered.

Deliberately left to its own issue: refutation memory and the structured `REFUTED.md`
migration (#6).

## 6j. The power gate refuses questions the data cannot answer (#19)

`quantbot.research.power`. Two arithmetic questions, asked before any compute is spent and
answerable without touching the data.

**Can this sample detect this effect?** `t = d*sqrt(n)`, so `n = (z/d)^2`. Five estimands over
one core: Sharpe is a standardised mean difference with `d = SR/sqrt(252)`, so it cannot drift
from `MEAN_DIFFERENCE` or `CROSS_SECTIONAL_SPREAD`. `HIT_RATE` uses the proportion form and
`INFORMATION_COEFFICIENT` uses Fisher's transform.

**Would it survive its own costs?** A minimum practical effect that cannot pay its annual
trading drag is `UNECONOMIC` -- a different fact from `UNDERPOWERED`. This is `REFUTED.md` #8
made mechanical: 1-day reversal had the strongest raw edge ever measured here and netted
-28.8% at 5bps.

**Variance inflation is mandatory, and it is the term that matters most here.** 2,669 daily
observations of a 252-day forward return are not 2,669 independent draws, they are closer to
ten. Three declared sources are applied and recorded with every power number: AR(1)
`(1+rho)/(1-rho)`, clustering `1+(m-1)*ICC`, and horizon overlap `h`. An unpaired comparison
costs 2x a paired one -- cycle 11 used the wrong one and the overnight premium looked
significant when it is not.

| refusal | rule |
|---|---|
| `UNDERPOWERED` | the requested sample cannot resolve the claimed effect at the current bar |
| `UNECONOMIC` | the smallest effect worth acting on cannot pay its own annual trading cost |
| `OVERRIDDEN` | underpowered, and an operator authorised it. Never becomes `POWERED` |

Every decision is written to `power_assessments` at registration and again before each
confirmatory run. A refusal travels out on the exception so the caller can record it after the
rollback: `UNDERPOWERED` must survive as its own outcome, because recording an untestable
hypothesis as refuted teaches a later agent that a mechanism was tested and failed when it was
never tested at all.

An override is audited, carries into execution (the operator accepted the shortfall, not one
sample count), and travels into the result bundle beside the minimum detectable effect so a
null result reads "no effect larger than X". It cannot rescue `UNECONOMIC`.

**No Monte Carlo, deliberately.** The gate runs before the data is read. At that point only
declared parameters exist, so simulating a null from them converges to the closed form with
noise added; the version that would add information needs the window the gate exists to
protect. Deferred until an estimand with no analytic form is actually registered.

## 6k. Result bundles carry what produced them, and get attacked (#18)

`quantbot.research.manifest` and `quantbot.research.reproducibility`. The manifest moved out of
`quantbot.backtest.experiment` (which is now a re-export shim, so scripts keep working) because
the registry needs its canonical JSON and the provenance tooling needs its types.

A **confirmatory** bundle now cannot be built without code provenance (commit *and* dirty flag,
config hash, execution path), an environment fingerprint (interpreter, platform, dependency-lock
hash, seed), dataset snapshots with vintages and roles, a statistical plan, and resource usage.
Exploratory bundles stay unencumbered — they make no evidential claim.

**Faithful reproduction of a wrong number is not the goal.** The statistical plan records the
test *and* the dependence structure of the data, and `check_invariants` refuses the combination
when they disagree. That is cycle 11's actual error made mechanical: a one-sample test on paired
data put the overnight premium at t=2.74 where paired gives 0.63, and an independent-samples
comparison of two Sharpes of the same asset gave 1.09 v 0.92 where Jobson-Korkie-Memmel gives
z=1.01.

Invariants, each encoding a defect that already happened here:

| invariant | the defect it encodes |
|---|---|
| `statistical-test-matches-dependence` | cycle 11, twice |
| `costs-never-improve-returns` | a margin path that appeared to create wealth |
| `forced-liquidation-never-creates-wealth` | cycle 12, monthly-vs-daily rebalance mismatch |
| `significance-clears-the-frozen-bar` | a verdict function declaring success with no test |

`InvariantReport` names what it **could not** check as well as what failed. A check that
silently does nothing because the result did not report the figure it needs looks exactly like a
check that passed, and this project has been burned by that shape of false assurance.

`compare()` answers both directions. The one that matters most is the reverse: three of this
project's recorded analysis errors came from a harness that was not connected to the code under
test, and the tell was byte-identical output across runs that should have differed. A bundle
whose results match another's while `inputs_hash` differs is reported as
"identical despite different inputs -- the run may not exercise what changed".

**Secrets are refused, not merely redacted.** The manifest scans every string it would serialise
against credential shapes and raises. A redaction bug now fails loudly instead of publishing a
key.

## 6l. Research memory: what we know, as records not recollection (#6)

`quantbot.research.memory`. `REFUTED.md` made queryable, without rewriting what any of it meant.

`import_refuted_markdown` loads the real file — 24 refuted findings, the survivor table, the
structural limits, and the defect log — carrying every statement **verbatim**. Nothing is
paraphrased or re-judged, and `IMPOSSIBLE` is not flattened into `REFUTED` because they are
different facts. Each section is read in the shape it is actually written in (tables as rows,
limits as bullets, the defect log as paragraphs), because a parser handling only one shape would
silently drop the defect log — the section that is the calibration prior on every number here.

Four things are enforced rather than encouraged:

| rule | why |
|---|---|
| `UNDERPOWERED` cannot be filed as `REFUTED` | recording an untestable hypothesis as refuted teaches a later agent the mechanism failed when it was never tested |
| a `STRUCTURAL_LIMIT` must be curated | a generated one becomes "momentum did not work" and loses the part that changes decisions |
| a `SUMMARY` carries no verdict | it is derived; deleting it destroys nothing because the evidence is separate rows |
| a conflicting rewrite of a record is refused | an exact repeat is idempotent, a changed one is an error |

**Novelty is now a registration gate.** A candidate overlapping a prior hypothesis by ≥0.80
Jaccard on universe + features is refused as `DUPLICATE` unless the registrant writes down what
is materially different. Momentum returned null on three structurally independent universes
because each new one had a plausible excuse; writing the excuse down is cheap when the
difference is real and impossible to fill in honestly when it is not.

**Window consumption is queryable.** `window_consumption(dataset, start, end)` returns
`UNTOUCHED`, `PARTIALLY_CONSUMED`, or `EXHAUSTED` with the trial count and the hypotheses that
spent it. That is the constraint `REFUTED.md` states in prose — "cycles 2-10 consumed every
out-of-sample window and SIP data begins 2016-01-04" — made machine-checkable. Consumption is
per range, not per dataset, so one run cannot retire a whole data source.

`recall(topic)` answers "what have we learned about momentum" with evidence-linked records.
Relational and keyword-based on purpose: at tens-to-hundreds of hypotheses a wrong semantic
neighbour is worse than a missed one when the answer decides whether to spend a holdout.
Embeddings and a graph database stay out until evidence demands them.

## 6m. Point-in-time data, and universes that include what died (#17)

`quantbot.market_data.pointintime` and `quantbot.market_data.instruments`.

**Availability is two timestamps, never one.** `observed_at` is what a value describes;
`available_at` is when it could first be known. `knowable(series, as_of)` filters to what a
decision may actually use — the complement is look-ahead by definition. Every result in
`REFUTED.md` rested on this being upheld by hand, and two of the three cycle 11-12 errors were
timing errors of exactly this family.

**A feature is gated by its slowest input, not its newest observation.** A backfilled or revised
value published after the newest bar delays the feature that uses it. `FeatureSpec.available_at`
composes lookback + input availability + publication lag so this is computed once rather than
reasoned about per experiment. The transformation version is part of the feature's identity: a
feature recomputed with changed code is a different feature.

**A universe is a function of time.** `members(as_of)` includes names that later stopped
trading; `survivors(as_of)` is the live-only set; `survivorship_bias(as_of)` is the difference,
so it is measurable rather than assumed. Cycle 10 was only interpretable because delisted
history existed — 511 names, 241 of which terminated — and whole-market momentum produced more
return with *less* Sharpe. A live-only universe would have shown a winner.

**A listing is not an exposure.** `quote_currency` and `exposure_currency` are separate fields,
and `unhedged()` names the instruments carrying FX the hypothesis never declared. Cycle 17's
global test ran through US-listed country ETFs, and `REFUTED.md` #12 found FX itself unusable at
Sharpe -0.14, which makes an undeclared currency exposure a confound rather than a detail.

**Missing capabilities raise.** `UnsupportedCapability` rather than an empty list, because
silence is how a survivorship-free study quietly becomes a live-only one. Alpaca serves bars,
quotes, corporate actions and delistings; `MACRO_SERIES` and `OPTIONS_DERIVED` are declared and
unimplemented, so asking for them fails loudly instead of returning nothing.

**Lineage travels into the bundle.** A confirmatory `DatasetSnapshot` now cannot be built
without provider, retrieval time, earliest availability, and transformation version. Without the
availability timestamp a bundle can only assert it avoided look-ahead, never show it.

The one change both #17 reviews asked for — the data layer exposing which windows have already
been used for confirmatory testing — landed in #6 as `window_consumption`.

## 6n. The critic: mechanical first, judgment second (#7)

`quantbot.research.critic`. A registration can no longer be frozen without a critic verdict on
that exact version, and the critique is frozen *inside* the registration — so a critic cannot
rewrite a review after the result arrives, for the same reason the prediction cannot be
restated.

The design brief is this project's own error rate: three analysis errors in two cycles, every
one flattering the result, and **none caught by the code looking wrong**. Errors of that shape
are invisible to a reviewer reading code and equally invisible to an LLM asked "is there
look-ahead here?". So everything mechanically decidable is decided mechanically.

| check | the recorded failure it catches |
|---|---|
| `hidden_beta` | cycle 15: rotation is beta 0.71 with alpha t=0.05; 0.71x SPY reproduces it with no trading |
| `cost_ladder` | refuted #8: +427.9% gross, **−28.8% net at 5bps** |
| `regime_split` | cycle 11: vol targeting won three eras of four and lost the most recent |
| `cross_asset_generality` | the same: 6 of 12 assets is a coin flip |
| `shifted_feature_probe` | shift every feature a bar; an unchanged result read the future |
| survivorship | cycle 10 needed 241 delisted names to stay interpretable |

Every fixture uses the real recorded numbers. The cost ladder reproduces −28.8% at 5bps from
9,134 legs, which is the leg count those two published figures imply rather than a round number
chosen to make the arithmetic land.

**Consensus cannot be configured into evidence.** `consensus()` returns the *most severe*
verdict, never the majority. Three PROCEEDs against one REJECT is REJECT, structurally.

**Confidence is advisory; reasons are mandatory.** A `Critique` without reasons cannot be
constructed, and a blocking objection cannot be returned as `PROCEED` — the gate is the
objection, not the verdict a critic feels like writing.

**A check that never ran is not a check that passed.** `DeterministicCritic` records
`unassessed` — the five judgment dimensions it did not assess — so silence on economic mechanism
or capacity is never read as approval. The LLM critic is an interface here; #9 owns model
runtime, and pretending to have assessed judgment would be worse than leaving it open.

## 6o. The experiment builder: compiled, probed, and about this system (#8)

`quantbot.research.builder`. Cycles 11-12 hand-wrote each hypothesis into a script, and that is
where two of the three analysis errors entered. Three things close that seam.

**The statistic comes from the declared dependence structure, never a default.** Compiling
"compare the Sharpe of A and B" into an independent-samples test is a faithful compilation and a
wrong experiment when A and B are the same asset — that measured z=1.01 where it looked like a
clear win. A pair the mapping cannot resolve is **refused**, because defaulting is what produced
the wrong test in the first place. Path-dependent quantities get a stationary bootstrap; cycle 12
is why.

| estimand + structure | test |
|---|---|
| Sharpe, paired | Jobson-Korkie-Memmel |
| Sharpe / mean difference / spread, unpaired | Welch |
| mean difference / spread, paired | paired t |
| hit rate, single sample | binomial |
| information coefficient, single sample | Spearman |
| anything path-dependent | stationary bootstrap |
| hit rate or IC, paired/unpaired | **refused** |

**Adversarial probes are compiled in, not added after a good result.** `shifted-feature-probe`,
`cost-ladder`, `regime-split`, `hidden-beta` always, plus `cross-asset-generality` when the
universe has more than one instrument. An outcome that did not run them **cannot be constructed
at all** — including a refutation, because a null from an unprobed run is not a null.

**A confirmatory result must come from the production path.** Commit `f77ea08` is the case: a
standalone study concluded a 2500bps exposure floor turned $100 into $252.43 against $126.80 —
deterministic, reproducible, and not about Elrond, because it measured the halt against
SPY-200-day returns while the engine runs the momentum rotation. Through the real engine the
same change scored Sharpe 0.21 against 0.36. A `RESEARCH_SCRIPT` outcome can say what it found;
it cannot be a `SURVIVED`, and `citable` is False.

A survivor must also clear the bar frozen for its own trial count, have no failing probe, and
report the statistic it cleared with. `FAILED` and `UNDERPOWERED` are recordable outcomes:
a failure is not an absence, and #6 needs to tell them apart.

## 6p. The budget governor, and the boundary around the daemon (#14)

`quantbot.research.budget`. Wall time, CPU, tokens and dollars refill. **Untouched data does
not**, and it is the only budget in this project with permanent consequences:

| cumulative trials | luck bar | minimum Sharpe detectable over 10.6y |
|---|---|---|
| 24 | 2.52 | 0.77 |
| 100 | 3.03 | 0.93 |
| 1,000 | 3.72 | 1.14 |

SPY scores 0.90 on that window. Spend enough trials and the project can no longer demonstrate
anything, and no amount of compute buys the budget back. So `TRIALS` gets the same machinery as
tokens — caps, admission control, audited overrides — and a different attitude. `RENEWABLE`
names the distinction and a test asserts it rather than leaving it to the docstring.

**Statistical budget is keyed per dataset and per family**, never as one global number: a single
figure hides the difference between having used up US equities and having used up everything.

**Estimated before admission, not counted after.** An external miner that evaluates 1,000
candidates and returns one has spent 1,000 trials; discovering that afterwards is discovering it
too late. `worth_spending()` is the Research Director's deferral — a family near its cap declines
a low-value question even when compute is free.

Fail closed throughout: a resource nobody capped is a resource nobody may spend. Spend is
append-only, so a budget cannot be quietly rewound. `LoopBound` is a hard stop on debate and
retry rounds, because a loop that can always try once more does not terminate.

### The boundary, tested rather than assumed

Both reviews asked that research starvation never reach the trading daemon. The paper account is
the only genuinely uncontaminated evidence this project will ever get.
`test_research_budgets_cannot_starve_the_trading_daemon` walks the AST of every module in
`runtime`, `cli`, `operations`, `execution`, `brokers` and `risk` and fails on any **top-level**
import of `quantbot.research`.

It found one on its first run. `cli.py` imported the registry at module scope for the
`hypotheses` command — so a research import failure would have broken the kill switch, which
lives in the same file. That import is now function-local, matching how `cli.py` already defers
`runtime`. The property enforced is precise: research is not a load-time dependency of the
trading path.

## 6q. Model runtime: replaceable, except for one rule (#9)

`quantbot.research.models`. Mostly ordinary provider-neutrality — a role names a model, a chain
names its fallbacks, and swapping either is configuration. Two things are not ordinary.

**A critic may not share a model identity with the generator it reviews.** Enforced in
`RoleRouting`, not left to whoever writes the config, and checked across the whole chain rather
than the primary. Not because another vendor is smarter: the same model asked to critique its
own proposal tends to find it sound. #7 already refuses to let agreement outvote an objection;
this refuses to let the agreement be an artefact of asking one model twice. It can be waived,
but only by setting `require_distinct_critic=False` explicitly — forgetting cannot waive it.

**Provenance is produced, not reconstructed.** `ModelResponse.provenance()` returns exactly the
`ModelProvenance` #18's manifest requires: provider, model@version, prompt-template hash,
parameter hash. A conclusion whose reasoning came from a since-updated model is not
reproducible, and without the hash there is no way to know that happened.

Also: fail-closed by default (a silent downgrade to a weaker critic is the failure nobody
notices), a circuit breaker with cooldown, and a prompt carrying a credential is **refused**
rather than redacted — a prompt leaves this machine, and stripping the key hides whatever
assembled it.

Transport is injected the way `market_data.transports` does it, so an OpenAI-compatible
endpoint — Ollama, LM Studio, vLLM, llama.cpp — is exercised end to end in tests with no network
and no vendor SDK.

## 6r. Source scout: external evidence that cannot become measurement (#4)

`quantbot.research.sources`. Three rules, each hardened past metadata because each guards a way
to manufacture a false edge.

**A derived artifact may never be cited in place of its source.** `cite()` raises on anything
marked `derived` — an LLM summary, an extraction, a translation. A summary that reads
authoritatively is worse than no artifact because it stops the search: this project believed a
Corwin-Schultz estimate of **34bps for SPY against a real 0.26bps** until someone pulled real
quotes.

**Event time and retrieval time are separate, and ordered.** A source retrieved before it was
published is refused outright. Look-ahead is the most reliable way to manufacture a fake edge
and it is invisible in results — a leaky backtest looks like a discovery, not a bug. Storing
both timestamps is what makes the #7 check possible at all.

**Agreement decides whether to test, never what the answer is.** `worth_testing()` returns a
boolean, and twenty supporting papers return exactly what one returns. There is deliberately no
function here converting source count into a prior, weight or confidence: published quant
findings replicate poorly, and the bias runs toward positive results — the direction that costs
money.

Only `MEASURED` gates promotion, and an `EvidenceBasis` **can never be** `MEASURED`: that is what
an experiment finds, not what a question starts with. `REFUTED.md` #13 is the standing case —
social-sentiment strategies were rejected on published evidence, which was defensible and is a
different fact from the 23 entries with a measurement behind them.

Every hypothesis now carries a `basis` with no default: citations, or an explicit
`DATA_DRIVEN_NO_EXTERNAL_SOURCE`. "We did not look" and "we looked and there is nothing" are
different facts, and a default would have made the first the common answer.

### The connector that fills it

`quantbot.research.scout`. The provenance layer above had nothing acquiring anything, so every
hypothesis declared `DATA_DRIVEN_NO_EXTERNAL_SOURCE` whether or not literature existed. arXiv is
the first place that gets looked: documented public API, no credentials, and q-fin is where this
project's subject matter lives.

**A failed fetch is never an empty result.** Every failure path raises `ScoutError` —
`SourceUnavailable` for a timeout or a non-200, `SourceProtocolError` for a body that is not the
documented schema. There is no code path turning an exception into an empty search, because a
connector that returns `[]` on a timeout records a transient outage as "no literature exists",
which is a research finding and a false one.

**arXiv reports a bad query as a result row.** A `<entry>` titled "Error", with `totalResults`
of 1 and an abstract. A parser that does not know this stores a paper called Error and cites it.
The captured response is `tests/fixtures/arxiv_error_entry.xml`, and the reported reason reaches
the caller rather than being flattened into a generic parse failure.

**"We looked and found nothing" is a record.** `search()` returns a `LiteratureSearch`, never a
bare tuple, so a zero-result search still carries the query, the connector and the time it ran.
`query` and `searched_at` have no defaults, so a search that never happened has no
representation at all — which is what keeps the two facts distinguishable.

**The connector cannot raise epistemic status.** It yields `Source` objects; `cite()` is still
the only step that says `LITERATURE_SUPPORTED`, at a call site that has to name the claim. Once
something is fetching, source count is the cheapest thing in this system to manufacture.

One request every three seconds, enforced by the transport rather than left to callers, with a
User-Agent that says who is calling and where to complain. Every test runs offline against real
responses captured from `export.arxiv.org`; the transport is injected the way
`market_data.transports` does it.

Still missing for #4: no second source type (SEC, macro releases, news), no orchestration path
that invokes the scout — `director` has a `SCOUTING` state and nothing drives it — and no
persistence for a `LiteratureSearch`, so a search record lives only as long as its caller.

## 6s. Research director: the lifecycle, and what it cannot reach (#3)

`quantbot.research.director`. Eleven states with an explicit transition table, mirroring the
order lifecycle in `domain/lifecycle.py`. A task runs `PROPOSED → SCOUTING → CRITIQUE →
REGISTERED → EXPERIMENTING → REVIEW → SURVIVED → PROMOTABLE` and survives a restart, because
state and its event log are durable.

**`UNDERPOWERED` is terminal and has no transition to `REFUTED`.** Not a policy — the entry in
the table is an empty frozenset. Research memory already blocks that conversion; the lifecycle
must not offer a way round it, so the route does not exist. The error message says why.

**Priority is computed, never narrated.** `expected_information_gain()` returns **zero** for an
underpowered or uneconomic assessment, zero when the trial budget cannot cover it, and zero for
an exact repeat of prior work — whatever the narrative interest. There is deliberately no
parameter for a model's opinion of promisingness: it would dominate every other term and it is
the only input with no measurement behind it.

Every transition is an append-only event carrying actor, reason, timestamp and the evidence and
budget state at the time. A state column cannot answer "who moved this and why", and an
autonomous loop that cannot answer that is not auditable.

### The boundary, from the research side

`test_the_director_has_no_route_to_the_broker_or_the_kill_switch` walks the AST of every module
in `quantbot/research` and fails on any import of `brokers`, `execution`, `operations` or
`runtime`. A research agent cannot place an order, enable live trading, or clear the kill
switch, and that holds because there is no import path — asserted, not assumed. #14 asserts the
same boundary from the trading side.

## 6z. Where v0.2 stands, and what to do next

Branch `main` (renamed from `elrond-v0.2` on 2026-08-22, when v0.2 became the default and
`codex/quantbot` was retired).

**Counts re-measured on 2026-09-02: 2,320 tests collected — 2,302 pass, 18 skip for an absent
WSL distribution and an unconfigured model endpoint — and `ruff` plus `mypy --strict` are clean
on both `--platform linux` and `--platform win32` across 210 source files.** The figures this
paragraph carried (1,332 tests, 146 source files) were true when written on 2026-08-22 and
described roughly half the tree; they are corrected rather than deleted because a stale count
that reads as current is how a reader concludes nothing has landed since.

Read this section first if you are resuming, and read the header's list of what it does not
cover before trusting it as an inventory.

**Updated 2026-08-22.** The table below was written when four issues were outstanding; all four
shipped, and so did #36, #37 and #38's enforcement. See `## 6aa` for what changed.

### Shipped

| issue | commit | what it enforces |
|---|---|---|
| #12 Sandbox | `25d6a64` | generated code cannot import `quantbot` or reach a broker |
| storage fix | `14c1932` | a database can be migrated instead of rejected — prerequisite for all six revisions below |
| #5 Registry | `8473085` | frozen predictions, contaminated-window refusal |
| #19 Power gate | `d4a909e` | five estimands, variance inflation, cost floor, `UNDERPOWERED` |
| #18 Reproducibility | `7570b5f` | statistical plan in the bundle, four invariants, secret refusal |
| #6 Research memory | `9249f53` | `REFUTED.md` imported verbatim, novelty gate, window consumption |
| #17 Data platform | `00bdeea` | point-in-time availability, survivorship, exposure vs listing |
| #7 Critic | `144f0a3` | six mechanical checks, severity-max consensus |
| #8 Experiment builder | `545e9f9` | test from dependence structure, mandatory probes, production path |
| #14 Budget governor | `b26d5f3` | trials as a non-renewable budget, daemon isolation |
| #9 Model runtime | `7161a08` | critic ≠ generator identity, provenance produced not reconstructed |
| #4 Source scout | `a2c226a` | a summary can never be cited for its source |
| #4 arXiv connector | this branch | an outage raises; it never reads as "no literature exists" |
| #3 Research director | `98f60b7` | lifecycle, computed priority, no route to the broker |

Schema was at V6 with revisions 0001–0006 when this table was written. **It is now at V24 with
revisions 0001–0024** (`storage/schema.py`, `SCHEMA_VERSION = 24`), and the eighteen added since
are, in order: reservation and search origin, the promotion ladder, search runs, literature
searches, promotion-claim binding, rejected candidates, rejection evidence, market anomaly
events, capacity assessments, market regime snapshots, counterfactual outcomes, instruments,
veto policy versions, catalyst assessments, session observations, position protection, position
protection actions, and artifact evidence. The forecasting ledger is a separate store on its own
revision 0001. Every revision declares its own tables.

### Not built

~~**#13 Discovery Engine, #11 External Workers, #15 Dashboard, #16 Promotion Ladder.**~~
All four shipped; this section is kept for the reasoning, not the status.

~~Nothing calls an LLM anywhere in this system.~~ Superseded: #9 was exercised against a live
Ollama endpoint and the semantic worker (#37) ran against `mistral:7b`. `ModelRuntime` is wired and unused; the critic is
deterministic and names the five judgment dimensions it did not assess.

~~No autonomous loop runs: each gate is callable, none is driven.~~ **Corrected 2026-09-02: a
driver exists.** `quantbot research-cycle --max-steps N --commit` calls `cycle.drain`, which
advances queued tasks through the stages that can be decided mechanically — `capacity_stage` then
`novelty_stage` — and seeds prior findings first so the novelty gate reads what the project
already knows. It is not a continuously running loop and it is not on by default: `--max-steps`
is required with no default and `--commit` is opt-in, both because each stage a run advances can
permanently raise the luck bar. Stages needing a model or a measurement have no handler and stay
*visibly stuck* rather than being advanced by something that did not run them, which is what
keeps "a driver exists" from meaning "the loop is closed".

`research.transports.HttpxModelTransport` supplies the wire the runtime was missing — an
OpenAI-compatible `/chat/completions` client covering Ollama, LM Studio, llama.cpp, vLLM and the
hosted APIs. It is exercised through `httpx.MockTransport`, which drives the production request
path with no network.

~~**No endpoint has been contacted and no composition root constructs one.**~~ **Half of that is
now wrong.** `research.composition.build_model_runtime` *is* a composition root: it reads
`QUANTBOT_MODEL_*` from the environment, refuses with `ResearchNotConfigured` rather than
defaulting to something permissive, and assembles `ModelRuntime` over
`OpenAICompatibleTransport(HttpxModelTransport(...))`. **Nothing in `src/` calls it** — the only
callers are `tests/unit/research/test_composition.py` and the live integration suite
`tests/integration/test_model_runtime_live.py`, which skips unless an endpoint is configured (it
accounts for four of the 18 skips). `research-status`, `preflight` and the console call
`model_configuration()`, which reports whether a runtime *could* be built without building one or
contacting anything. So the runtime remains infrastructure waiting for something to route — but
the thing that would construct it now exists, and a reader looking for it will find it.

### The next action

**Superseded — see `## 6aa`.** The note below was right about the mechanism and wrong about the
state: it said `window_consumption` "will answer `EXHAUSTED` ... once seeded", and nobody
seeded it. That sentence is the earliest written trace of the severity-1 at the top of this
file, and it sat here unactioned through eight subsequent issues.

Worth raising with the operator rather than deciding unilaterally.

### Defects found this session, all by guards rather than review

Recorded in `REFUTED.md` beside the nine that came before: three Alembic revisions built from
live metadata; a backfill `server_default` that diverged the schema; a permissive point-in-time
test; the kill switch's accidental dependency on research code. None was caught by the code
looking wrong.

## 6t. Discovery engine: propose without spending the evidence (#13)

`quantbot.research.discovery`. The generator is the component most able to destroy this
project's remaining statistical capacity, so it is hemmed in hardest. Four rules, all mechanical.

**A generator that read protected data has already spent it.** A `Candidate` declares
`inspected_roles`, and one naming `PROTECTED_EVALUATION` or `FORWARD_PAPER` **cannot be
constructed**. There is no confirmatory test left to run: the answer was seen before the
question was frozen. `ContaminatedGenerator` is deliberately not a `ValueError`, so it survives
pydantic's wrapping and a caller can tell contamination from a malformed field.

**A search reports what it examined, not what it returned.** A miner evaluating 900 and
proposing 3 spent 900, and #14 charges for all of them. A mode claiming a search without naming
the data it searched is refused.

**A generated story is worth nothing as evidence, in every mode.** `plausibility_credit()`
returns zero for cross-domain analogy and for everything else. It exists as a function so that
changing it has to be deliberate and reads as what it would be.

**A mode is judged on what reaches registration, never on volume.** 100 candidates with 1
registration is throttled; 10 with 6 is not. Optimising for candidate count against a fixed
dataset is how a research programme destroys its own dataset.

### The constraint that shapes what this can do

`exploratory_only()` returns True whenever any requested window is `EXHAUSTED`. For 2016–2026 US
equities that is already the answer — cycles 2–10 consumed every out-of-sample window. A
generated idea against that data stays exploratory however good it looks, unless there is new
protected data or **authentic forward evidence**.

That is #13's own last acceptance criterion, and it means the discovery engine's value is
currently bounded by #17 delivering a genuinely new asset class. Building the generator does not
create capacity to test what it generates.

## 6u. External workers: delegate research, not authority over evidence (#11)

`quantbot.research.workers`. An external miner is a fast way to spend a dataset — RD-Agent or
Qlib can evaluate thousands of candidates against a fixed window and hand back the best, and the
best of a thousand skill-free candidates looks excellent. The contract is built around that.

**A worker that will not disclose its search cardinality is `EXPLORATORY_ONLY`** — mechanically
unable to produce confirmatory evidence, because its result cannot be deflated against a search
it will not name. There is no configuration that changes this.

**An undisclosed search is charged the whole budget it was given**, not zero. A worker that will
not say what it spent must not be cheaper than one that will.

**A worker is never handed `PROTECTED_EVALUATION` or `FORWARD_PAPER` data.** A miner over
protected data spends it, whatever it reports afterwards.

**Unsupported semantics raise.** A worker that cannot express the test a registration requires
says so rather than running something adjacent and reporting it as the same thing — and a run
with anything unsupported is exploratory however good its number is, because the number is not
answering the registered question.

### The most useful thing a miner can do here

Not find a factor. `empirical_luck_threshold()` runs the same search procedure over shuffled or
synthetic **null** data and takes a high quantile of what it finds. That is a bar the procedure
earned rather than one assumed from `sqrt(2 ln N)`, and it is the only honest way to judge a
search whose true cardinality nobody fully specified. `null_calibration_summary()` records the
shape of what the search finds in noise.

## 6v. Promotion ladder: earning trust, with the last step reserved (#16)

`quantbot.research.promotion`. Seven stages, and **there is no `LIVE` stage in the enum**.
`LIVE_REVIEW_ELIGIBLE` is terminal, its transition set is empty, and no sequence of moves
reaches live trading — implemented by the absence of a destination rather than by a permission
check that could be misconfigured. Only the operator, outside this system, decides what happens
next.

**`RESEARCH_SURVIVOR` had to be strengthened**, and this is the change the review asked for.
Passing a pre-registered test is not enough: cycle 11 produced two candidates that did exactly
that and were both wrong, because the flaw was in the *statistic*, not the protocol.

| candidate | looked like | died on |
|---|---|---|
| vol targeting as alpha | Sharpe 0.92→1.09, all 12 parameter sets, break-even 113x spread | Jobson-Korkie z=1.01; 6 of 12 assets |
| overnight effect | IWM t=3.47, GLD t=3.82 | paired t: SPY 2.74→0.63; 1 of 18 assets |

`survivor_objections()` therefore requires the #7 probes to have run and passed, the production
evaluation path, the frozen luck bar to be cleared, **and the test to be valid for the
dependence structure of its data**.

**Paper qualification counts authentic forward evidence and nothing else.** A
`ForwardObservation` whose role is not `FORWARD_PAPER` cannot be constructed — a backtest, a
literature claim, an exploratory worker result and an underpowered outcome all increment the
counter by zero. The account holds **1 forward day against a 30-day, 30-trade window**, and
`forward_progress()` reports that distance honestly.

**A material change restarts the window by exclusion, not by resetting a counter.** Observations
of version 1.3.0 are not observations of 1.4.0, so `count_forward_days` filters them out and the
stage drops to `RESEARCH_SURVIVOR`.

Demotion is deliberately not gated. A system slow to stop trusting something is worse than one
occasionally too quick.

**Superseded in part by 6bb.** The counting described above is intact; what has changed is that
clearing it is no longer sufficient, and that the claim behind a strategy is no longer something
a caller supplies.

## 6w. Research dashboard: what changed, what is blocked, what is left (#15)

`quantbot.research.dashboard` — the data behind the view, not the view. `scripts/dashboard.py`
remains the single truth source for the operations panels; this adds the research panels beside
it rather than a second place to look.

**Forward evidence always carries its denominator.** `1 of 30 days, 0 of 30 trades`, never
`1 day`. Forward evidence and research volume live in separate fields with separate evidence
states, and there is deliberately no property combining them. Seventeen cycles is a bigger
number than one day; the panel makes that impossible to read as progress.

**Statistical budget shows what spending it would cost, before it is spent.** Trials, the bar
now, and the bar *if the budget is fully spent* — at 1,000 trials that is t=3.72, above SPY's own
Sharpe on this window. Renewable spend is shown beside it so the difference is visible rather
than implied.

**A generated number can never be displayed as a measurement.** `Provenance.MODEL_NARRATIVE`
cannot carry `OBSERVED` or `BACKTEST`, and `OBSERVED` accepts only the durable ledger — forward
evidence has exactly one source. Both are validators, not review habits.

**The default view is what is blocked, not what ran.** `attention()` returns halts, failed
reconciliations, exhausted budgets, blocked and stuck tasks, and survivors. Routine activity is
**dropped entirely** rather than ranked lower, because a research loop generates a large volume
of uninteresting activity by design and including it is how the signal gets lost. A quiet system
returns no alerts at all.

## 6x. The gates compose, and there is a source of new statistical capacity (#13/#17)

Two things I had wrongly written off as blocked.

### The pipeline is tested end to end

`tests/integration/test_research_pipeline.py` runs one candidate through every gate in order —
admission → budget → critic → registration → compile → outcome → memory → promotion — against
one database. Each gate had unit tests proving it refuses; none proved they *compose*. A system
of individually correct parts that do not fit together refuses everything, which looks exactly
like a system that works.

Both directions are asserted: a candidate on untouched data reaches `RESEARCH_SURVIVOR`, and the
same candidate on the window cycles 2-10 spent is stopped as `CONTAMINATED_WINDOW`. The second
test **seeds** that history, so `EXHAUSTED` is now a measured state rather than a claim I kept
making.

### FRED, and a look-ahead bug it caught in my own code

`quantbot.market_data.fred`. Free, no credentials, decades of history where equities gave ten
years — and it is the source that makes the #17 vintage machinery earn its keep, because macro
series are **revised after publication**.

| rule | why |
|---|---|
| a confirmatory run on a revised series must use **ALFRED** | `fredgraph.csv` serves today's revised values, which is what a naive study uses by mistake |
| a real-time pull carrying values published after the `as_of` **raises** | if the endpoint ignored the parameters the data is not point-in-time, and a backtest fed revised macro data simply looks prescient |
| an undeclared series is refused | a publication lag guessed at the call site is a look-ahead bug waiting to happen |

**The bug the tests caught.** FRED stamps a monthly series with its period *start*: March
payrolls are `2026-03-01`. My first version added the publication lag to that stamp, making
March data available on **6 March** — five days before the month it describes had finished. The
lag runs from period *end*; March payrolls publish 5 April. Pinned by
`test_the_publication_lag_runs_from_the_period_end_not_the_period_start`.

### And FRED is stronger than the equity window, not weaker

`scripts/power_survey.py`. I first wrote that low-frequency macro data would be weak — the power
gate refusing most effects on ~420 monthly rows. **That is backwards**, and `REFUTED.md` already
had the right unit: the structural limit is stated in *years*, because for an annualised Sharpe
the frequency cancels.

| dataset | years | minimum detectable Sharpe |
|---|---:|---:|
| SIP equities daily (EXHAUSTED) | 10.6 | **0.94** |
| FRED monthly 35y | 35 | 0.67 |
| FRED daily 50y | 50 | 0.56 |
| FRED monthly 75y (CPI from 1947) | 75 | **0.46** |

Charged with realistic autocorrelation (ρ=0.30 for macro, 0.05 for equities), so the advantage
is the honest one rather than the iid one. Free macro history is roughly **twice the
resolution** of the window this project has already spent.

## 6aa. External workers, measured (2026-08-22) — #36, #37, #38, #18

### Kronos runs, and does not work

First real inference on this machine, through the production provider and CLI, on 6,969 Alpaca
IEX daily bars. Three defects found only by running it:

- **`.resolve()` on the interpreter path** followed the venv symlink to the base interpreter, so
  every forecast failed the environment attestation. `python -m venv` symlinks by default on
  POSIX and `uv venv` always does; Windows venvs copy, which is why it was never seen.
- **OHLC validation discarded 61% of real forecasts.** Kronos samples O/H/L/C independently and
  4.35% of candles could not be real bars. One bad candle voided the whole artifact, and the
  loss correlated with model uncertainty — a selection effect on the dispersion statistic.
  Now counted into `ForecastFeatures.inconsistent_candles` rather than enforced.
- **Unscoreable forecasts waited forever.** A horizon spanning a holiday registers a session
  that never trades; scoring returned `None`, the same value it returns for "not mature yet".
  Now `UnscoreableForecast`, naming the missing targets.

**The result: REFUTED.md #25.** 42 non-overlapping 5-day windows, 30 scored, 688 forecasts.
Direction **0.501**. On absolute error it **loses to predicting zero** (paired t = −2.53, worse
in 21 of 30 windows, against a 1.79 luck bar). The unknown training cutoff means the window may
be in-sample, which would flatter it; it lost anyway. Scope: zero-shot, unfine-tuned, daily
bars, horizon 5, ETFs, one configuration.

**12 of 42 windows were unscoreable**, all holidays — roughly a quarter of windows are lost
without an authoritative exchange calendar.

### What else shipped

| area | what it enforces |
|---|---|
| #18 provenance | `WorkerProvenance` carries checkpoint, prompt, input hash and a `fingerprint`; a revised source cannot claim equivalence to the original artifact. **#18 closed.** |
| #23 burden | `measured_kronos_provenance` and `measured_semantic_cardinality` count rows; a prompt sweep costs what it spent |
| #37 boundary | `SemanticResearchWorker` satisfies `ResearchWorker`; an import test pins that it cannot reach the broker or kill switch |
| #38 placement | `quantbot.placement` refuses a job the host cannot carry, measured not declared; `KronosSignalProvider` is gated at construction |
| #6 memory | the 25 settled findings are seeded and loaded by `research-cycle` |

### Three tests of mine that measured nothing

Recorded because the pattern matters more than the instances, and all three passed before the
mutation exposed them:

1. **An import-isolation test** cleared `sys.modules` and re-imported the worker — which was
   already cached, so nothing re-executed. It passed with `KillSwitchController` imported into
   the module under test. Now runs in a fresh interpreter.
2. **A fingerprint test** never pinned `input_hash`, because both adapters already encode the
   input in the artifact id. Redundancy in the happy path is not coverage.
3. **A seeding idempotency test** pinned its own clock while the CLI passed `datetime.now`, so
   every re-seed conflicted and the *second* `research-cycle` of the day crashed. Found by
   running the CLI twice, not by the suite.

### Hardware, measured

`omen-windows` amd64 / 32 cores / 32,486 MB / 16,376 MB VRAM. `omen-wsl2` sees 15,847 MB — a
`.wslconfig` default, not a limit. The Omen has its own 16 GB GPU, so Kronos does not depend on
the RTX desktop being powered on. The Pi is ~434 MB usable, which does not meet the control
plane's 512 MB floor and does meet the watchdog's. `deploy/omen/` holds units that are written
and **not installed**.

## 6bb. What a deployment is allowed to claim, and who gets to define success (#46, #47, #53, #54)

**2026-08-22.** `PAPER_QUALIFIED` used to be reachable on 30 authentic forward days and 30
trades. Those are *authenticity* facts — real sessions happened, nobody invented them — and they
say nothing about money. A strategy could lose on all thirty trades and satisfy the rule. The
stage name was a stronger claim than the mechanism behind it, which matters because a dashboard,
a later agent and an operator all read `PAPER_QUALIFIED` as "the paper evidence supports this".

Six defects, five of them found by GPT-5.6 Sol reviewing the half-finished branch. All are the
same shape the project keeps producing: **a gate reading its input from the thing it constrains.**

| Defect | Was | Now |
|---|---|---|
| The claim behind a deployment | `hypothesis_id` supplied at qualification | Bound at `REGISTERED`, read from `promotion_state`, refused as an argument anywhere else |
| Evidence identity | version + config checked, `strategy_id` not | Exact strategy identity required; a fork sharing a config no longer transfers |
| Qualification provenance | The stage, and a free-text reason | `objective_identity` + `evidence_identity` on the state **and** the append-only event |
| Who defines success | Ratification required only at `LIVE_REVIEW_ELIGIBLE` | Required at `PAPER_QUALIFIED` too; an agent may not clear a bar it wrote — **superseded by #72, see below** |
| A later mandate | Inherited the earlier qualification | Identity must match; a policy edit forces reassessment |
| Session pairing | Consecutive *qualified* dates | Adjacent *exchange* sessions; a gap is dropped and counted, not annualised |

> **⚠ SUPERSEDED 2026-08-30 by #72.** The "Who defines success" row above is history. A
> `PROJECT_DEFAULT` mandate is now *usable* at `PAPER_QUALIFIED`: `ratification_objections()`
> returns nothing for one, and every artifact measured against it reports `authority` so a reader
> can tell an agent-authored bar from an operator-authored one. The concern the row records --
> an agent clearing a bar it wrote -- is answered by visible provenance rather than by waiting.
> `RATIFIED` and `ratified_by` remain unreachable from code.

**The claim is now part of the strategy, not part of the question.** Before, a strategy whose
mechanism was refuted could be offered a different, unrefuted registration, and the gate would
faithfully report finding no refutation of the thing it had been handed. `promotion_state` carries
`hypothesis_id`/`hypothesis_version`, written only by the `REGISTERED` transition; re-binding is
refused, and the only honest route to a different claim is back down to `CANDIDATE`, which costs
everything above it.

**An agent cannot ratify its own mandate.** `config/economic-objective-v1.yaml` stays
`PROVISIONAL`, and every value is now marked `[TRANSCRIBED]` or `[PROPOSED]`. Sol was right that
the earlier "every value is transcribed" claim was too strong: the intended capital range, the
100bps improvement threshold, the 12x turnover cap, the 3-year horizon and the choice of
`BENCHMARK_RELATIVE_GROWTH` over a drawdown-constrained objective are proposals an agent wrote.
A `RATIFIED` mandate naming an automated `ratified_by` is refused at load. **Ratifying is an
operator action and it is the thing standing between this system and a `PAPER_QUALIFIED` label.**

Ratification does **not** change the objective's content hash — the identity covers the policy
values and excludes `status`, `ratified_by`, `ratified_at` and authorship. Otherwise the
operator's signature would invalidate every assessment made against the values they were signing,
at the exact moment those values became usable. Editing any policy value does change it.

**`DeploymentRole` is on the operator's screen.** `OPERATIONAL_BASELINE` vs `EDGE_CANDIDATE` is
derived from durable state and now renders in the promotion panel. The deployed momentum rotation
has no frozen registration behind it, so it reads `OPERATIONAL_BASELINE`: it keeps producing real
fills, slippage and reconciliation evidence, and accrues nothing toward a claim about edge however
long it runs. `demote_on_refuted_claims` runs the derivation rather than waiting for the next
qualification attempt to notice — a derived answer nobody derives is a stale answer.

**A legacy refutation with no lineage is not read as a clean record.** `refutations_of` queries
`hypothesis_id`, which is right for anything registered under this schema and blind to everything
imported from `REFUTED.md`. Those findings predate hypothesis ids, so no exact lookup will ever
match one, and reading that silence as "never refuted" is the half of the defect that promotes
something. Unlinked refuting findings sharing a claim's family now land in `unassessed`, which
blocks support without asserting a refutation — what is true is narrower: nobody has adjudicated
whether they bear on this claim.

**Twelve protections, each mutation-checked.** Every one was deleted in turn and the test named
after it went red; a test that passes with the mechanism removed is measuring something else.

**What this does not do.** It does not make any strategy closer to qualifying. It makes the label
mean what it says, which in practice moves `PAPER_QUALIFIED` further away: it now needs a ratified
mandate, a frozen non-refuted claim, an adequately powered window, and a benchmark-relative
beta-adjusted result clearing the 2.905 luck bar. On thirty sessions that requires an annualised
excess Sharpe past 12. **No strategy here has one, and saying so is the point.**

## 6cc. What is actually left to test on (#50)

**2026-08-22.** `quantbot research-capacity`, and a statistical-capacity panel on the dashboard.

The question it answers is the one nothing answered: **what observations remain unused for
confirmatory testing, and which claims can legitimately use them.** `window_consumption` could
already say whether a range someone named was spent; that is a different question from the one an
operator has before deciding whether a search is worth running, and #50 was opened because the
gap let "the data platform is finished" be read as "there is fresh statistical budget".

On an empty database today:

| observations | status | note |
|---|---|---|
| `us-equities-daily` (SIP + Alpaca) | **EXHAUSTED**, 68 trials | unsearched sessions from 2026-08-22 |
| `crypto-daily` | AVAILABLE | crypto and cross-asset questions only |
| `us-macro-monthly` (FRED + ALFRED) | AVAILABLE | real capacity, with vintages |
| `sec-edgar-filings` | AVAILABLE | cycles 2-10 never looked at filings |
| `options-implied-daily` | **UNSUPPORTED** | declared in two enums, served by nothing |

Four things it refuses to do, each of which would overstate what is left: count two vendors of the
same dates as two budgets, report a capability with no provider as capacity, let crypto sessions
read as equity evidence, or let a source with no vintages answer a backtest.

**The only new equity capacity is time — and it is counted in sessions, not days.** The first
version of this derived new capacity from the calendar: the day after the last consumed date.
GPT-5.6 Sol's review predicted the defect and it was real — on 2026-08-22 the equity row reported
"unsearched sessions from 2026-08-22", a **Saturday**. A weekend is not capacity, and neither is
a holiday, an outage, or a gap the vendor never served. Capacity is now the set of sessions the
database actually holds bars for, attributed by provider prefix; a source with no local
observation store claims none. Nine protections, mutation-checked.

**The Research Director now consumes it.** `discovery.admissible` existed and was reachable only
from `bridge.draft_from_candidate`, which no production path imports — a rule rather than a
mechanism. `capacity_stage` wraps the PROPOSED handler and blocks before the memory query when the
capacity a question needs is already spent.

**Per-claim admission landed on 2026-08-27**, which was Sol's condition for closing #50.
`ResearchTask.required_datasets` declares the sources a confirmatory question needs, as
structured data — never inferred from the question text, because a wrong permissive match hands
out significance that was already spent. Every declared source must still have capacity, an
unrecognised name blocks rather than meaning "no constraint", and a task that declares nothing
still gets the weaker project-wide check. The field rides in `document_json`, so no migration.

The difference is not cosmetic: where US equities are exhausted and macro is untouched, the
project-wide gate advances an equity question on macro capacity and the declared path refuses it.
Five protections, mutation-checked.

**What this does not do.** It does not create capacity, and the honest headline is that there is
almost none for a US equity claim. That is the finding, not a limitation of the tool.

## 6dd. Engineering progress is not profitability progress (#52)

**2026-08-22.** `quantbot.research.milestone`, reported by `quantbot research-status` and by an
`quantbot research-status`.

**A correction, since three documents claimed otherwise.** README, `docs/research-architecture.md`
and this file all described an "edge-progress panel on the dashboard". There is no such panel, and
there is no dashboard renderer for it to live in — `research.dashboard` exports evidence and budget
panel *models* and nothing draws them. #52's criterion that dashboard/README/status separate
engineering from profitability progress is satisfied by `research-status`, which reports both
ladders side by side; the dashboard half is open, and a document asserting a view that does not
exist is the reporting version of the defect #52 exists to prevent.

Every number this project has reported about itself moves when code ships: issues closed, gates
built, tests passing, frameworks integrated. None of them move when the market says something.
That asymmetry is a project-management failure mode with a name — issue closure becomes a
surrogate objective — and #52 was opened because a long list of completed architecture issues
visually overwhelms the fact that the economic thesis is unproven.

Six levels, derived from durable state, so a milestone cannot be reached by writing about it:

| | reached when | today |
|---|---|---|
| M0 | the platform has honestly refuted things | **yes**, once research memory is seeded |
| M1 | a frozen candidate cleared the survivor gate, unrefuted | no |
| M2 | that claim spans several instruments and survived robustness | no |
| M3 | authentic forward sessions accrue against it | no |
| M4 | `PAPER_QUALIFIED` under #47, capital economics under #51 | no, and blocked |
| M5 | every gate clear, a human may look | no |

**The project is at M0.** That is not a setback and it is not new; it is the first time anything
reported it as a number rather than as a paragraph somebody has to read.

**M4 cannot clear at all yet, and says so.** The paper account holds $100, the mandate's range
reaches $10,000, and no amount of forward evidence at the smaller size distinguishes an effect
that survives at scale from one that does not. An unassessed check blocks, so M4 will not pass
until that is measured.

**#51 built the measurement, and did not thereby open the gate.** `assess_capital_capacity`
evaluates a strategy across a ladder derived from the mandate's own capital range and reports the
capital at which benchmark-relative edge crosses zero. M4 reads *that assessment* rather than the
issue's status, so closing #51 clears nothing; a verdict of `SURVIVES_MANDATE` does. A computed
assessment that says the edge dies moves from `unassessed` to `blocking`, because "somebody
looked and the answer was no" is a different state from "nobody looked".

**Two defects in the first version, found by Sol's review of #62 after it merged.** Both were
false-comfort shapes rather than arithmetic slips, which is why they are recorded here.

`commission_per_order` was a configurable field that `drag_bps` never referenced — a control a
reader could set while nothing changed. Worse than an absent one, and worst exactly where this
issue points: a fixed per-order fee is a large fraction of a $100 order and a rounding error on a
$10,000 one, so omitting it flattered the smallest rungs specifically. It is now charged as
annualised bps of capital, from the order count the round trips imply.

Participation was `capital / session_volume_usd` — the whole account treated as one order into
one book. That is not a conservative stand-in for a portfolio; it understates participation for a
concentrated sleeve and overstates it for a diversified one, and calling it measured portfolio
capacity would have been the claim this module exists to refuse. The ladder now takes
`InstrumentCapacity` per symbol — the share of capital it carries and the volume behind it —
computes participation from the notional each instrument is actually traded at, weights the drag
by that share, and reports the **worst** instrument's participation, because that is the one that
binds and an average hides the leg that cannot be filled.

No production path computes an assessment yet, and the reason is worth naming rather than leaving
implicit: annual turnover is not a durable per-strategy field, and neither is a durable
per-instrument allocation with point-in-time dollar volume. The mandate carries
`max_annual_turnover`, and substituting that declared ceiling for a measurement is a judgement
call about conservatism rather than a default to take quietly — so the assessment blocks on the
missing inputs instead. #51 stays open.

**Three rules make the ladder hard to flatter.** It is contiguous, so forward evidence with no
surviving candidate behind it reports M0 rather than M3. `unassessed` blocks. And a reached level
must name an artifact — "nothing blocks it" is not "something shows it". Eight protections,
mutation-checked.

**Before a new integration**, `docs/research-architecture.md` now asks three questions: which
milestone does this unblock, what cheaper thing answers the same question, and what measurable
result would cause us to reject it. A review convention rather than a machine check, written down
so a reviewer has something specific to hold a proposal against.

**`NO_EDGE_ESTABLISHED` is a destination.** The report says so when the project sits at M0 with no
clean confirmatory capacity left. That is a research result, and having a name for it is what
stops the alternative: adding another framework because nothing else can be said.

## 6ee. The bundle named a command that did not exist (#48)

**2026-08-27.** Found while scoping #48, and it is the same defect class as everything else on
this branch: **every reproducible manifest recorded `quantbot reproduce-experiment --experiment
<id>` in `reproduction_command`, and no such command existed.** An instruction inside the
evidence record, pointing at nothing.

Worse, the bundle recorded *what to run* and nothing about *what it would need*. A reader seeing
the `.inputs` directory beside a manifest had no way to tell whether it was complete, whether a
file had been edited since, or whether it was even the right directory.

**Fixed both halves.** `ExperimentManifest.reproduction_inputs` records each input's name, byte
count and content hash, taken from the content in memory at run time rather than from disk
afterwards. The two fields are one claim: a command with no recorded inputs and inputs with no
command are both refused at construction, so the old bundles' shape is unrepresentable.

`quantbot reproduce-experiment` now exists and reports **readiness**, failing closed three ways:
a missing or altered input blocks; a dirty original tree with no preserved patch blocks
permanently (`git_commit=<sha>, dirty=true` does not describe code nobody committed); a
dependency-lock difference is drift and never silent.

**It refuses to claim a reproduction.** `performed` is a field on the report, always false, and
the five capabilities #48 still needs are listed by name. A clean readiness report must not read
like a completed rerun to somebody skimming.

**#48 stays open, and its own first criterion says why.** It asks that one real historical
confirmatory experiment be rerun from its manifest. **There is no such manifest** — nothing
outside an integration test has ever written one, which is exactly consistent with the project
standing at M0. A reproducer built now could only ever be exercised against a synthetic fixture,
and this repository's rule is that an unexercised feature is an unclaimable one.

Eight protections, mutation-checked.

## 6ff. Rules that reject rather than resize (#58)

**2026-08-27.** `quantbot.risk.vetoes`, wired into `evaluate_order_gate` and evaluated where the
reference price lives.

**Most of #58 already existed**, which is worth saying before the new part. Per-trade risk,
position value, gross exposure, buying power, stop-distance-aware sizing, `max_positions` as a
hard rejection, and fail-closed behaviour on missing drawdown state or risk approval are all in
`risk/sizing.py` and `risk/gates.py` today. Re-listing them as new work is how engineering volume
becomes apparent progress, which #52 exists to prevent.

**What is new is the distinction between a cap and a veto.** A cap that binds downsizes the order;
a veto refuses it. Downsizing past a concentration limit still adds the exposure the limit exists
to bound, so these are a separate type from `SizingCapName` — and the separation is the
protection.

Four implemented: `NET_EXPOSURE` (gross was capped, net was not), `STALE_MARKET_DATA`,
`DAILY_LOSS` (keyed on the **Eastern** session, the same off-by-one that already cost this project
correct fill attribution), and `LIQUIDITY` (participation in average daily volume).

**Which are enforced is operator policy, and the default is deliberately partial.** `DAILY_LOSS`
needs an equity mark from the current session; `LIQUIDITY` needs twenty sessions of volume. On a
fresh ledger neither exists, so both correctly refuse — and enabling them by default would halt
the entries this project needs in order to accumulate forward evidence at all. The default enables
the two a thin ledger can always answer; the other two are implemented, tested, and **off until
the operator turns them on**. Same shape as the economic mandate: an agent may propose the values,
the operator decides.

**Sector and correlation are declared and unimplemented, and the policy refuses to enable them.**
There is no sector classification anywhere in this repository, and classifying a historical
position by a symbol's sector as of today is look-ahead inside the risk layer — #57 would then be
measuring the counterfactual value of a rule that could never have existed. They wait on #59.

**A defect of mine, found by the integration suite.** The staleness ceiling shipped at 900 seconds,
which encodes an intraday assumption into a system that decides once per session on daily bars.
The newest observation is always the previous close, so it refused every entry and the wired-cycle
test caught it immediately. Now four days — a long weekend passes, a feed genuinely behind does
not. Calendar seconds are still a coarse proxy for "sessions behind".

**Every rejection carries `gate_id`, `gate_version` and `rule_parameters_hash`** so #57 can
attribute an outcome to a rule at particular settings, and every veto speaks even when an earlier
one refused — a reason that never reached the decision cannot be evaluated. Fourteen protections,
mutation-checked.

**This creates no alpha and the milestone ladder does not move.** If #52's ladder ever advances
because risk gates were added, that is a bug in the ladder.

## 6gg. What a concentration limit has to be able to see (#59, slice 1)

**2026-08-27.** `config/exposure-classification-v1.yaml` and `quantbot.risk.exposure`, which turn
`SECTOR_CONCENTRATION` from a declared-and-unimplemented name into `EXPOSURE_CONCENTRATION` with an
evaluator behind it.

**The rename is the finding, not the paperwork.** #58 recorded that the sector veto was blocked on
point-in-time classification, because classifying a historical position by today's sector is
look-ahead. That reasoning is right for single stocks and does not bind here: all 23 instruments in
the universe are ETFs, and an ETF's exposure is its published mandate, not a vendor's estimate of a
company's business. Nothing to license, nothing to revise.

But a *sector* veto would not have caught this portfolio anyway. The roster holds 10 of 23, and the
dangerous state is not "too much technology" — SPY + QQQ + XLK + IWM + MDY share no sector at all
and are one bet on US equity beta wearing five tickers. TLT + IEF + TIP is duration. A sector table
would rate the first of those as perfectly diversified, which is a limit reporting itself as
enforced while the risk it exists to bound walks straight through it.

So the classification is by **exposure**: asset class, region and sector together, and a symbol
counts in every group its mandate implies. That can only make the veto more likely to refuse.

**What survives from the original look-ahead objection is that mandates change.** Constituents left
XLK when Communication Services was created in 2018. The table therefore carries an
`effective_from` and refuses to classify any decision before it — and that date is a *verification*
date, not a researched inception date. The table declines to speak about history nobody checked.
The cost is that the veto cannot be backtested; the cost is correct, because the veto could not
have run over that history.

**Unknown blocks, which is the whole asymmetry.** An unclassified instrument belongs to no group,
so it would add to no group's concentration and pass every limit by not being recognised. An
unclassifiable *held* position blocks too, because it makes every group total an understatement,
and an understated numerator is the permissive direction. Thirteen protections, mutation-checked.

**One global ceiling cannot serve this universe.** It is mostly US equity, so a roster can sit
near 100% `US_EQUITY` while being exactly what the strategy is designed to hold -- too loose for
the dominant group, and far too tight for the narrow ones if set for it.

> **⚠ SUPERSEDED 2026-08-30 by #75 and #72.** This block used to say the veto "ships disabled" and
> that per-group ceilings "are an operator decision about risk appetite". Both stopped being true:
> every implemented veto ships enabled under #75, and `group_exposure_ceilings_bps` now carries
> per-group values chosen as versioned project defaults under #72. Nothing here is waiting on a
> preference.

**Mandate and stress behaviour are now separate claims (Sol's #59 follow-up).** The first version
of the table mixed them: `HYG: [CREDIT, US_EQUITY]` and `LQD: [CREDIT, TREASURY_DURATION]` recorded
an empirical stress proxy with the same provenance as a prospectus reading. HYG's equity beta in a
drawdown is real and is exactly why a concentration limit should see it — and it is not a claim
that HYG holds equities.

`mandate` is what a fund holds by its published objective. `factor_model` is what it behaves like
under stress, with its own method, as-of date and version, hashed separately and folded into the
classification identity so a change to either half makes two studies two studies.

**Provenance belongs to the assignment, not the group.** `US_EQUITY` is definitional for SPY and
inferred for HYG — the same exposure, different evidence — so it is recorded per edge and a
rejection reads `US_EQUITY (FACTOR) would reach …`. #57 compares refusals, and two refusals under
different mechanisms are not one dataset. Where both halves claim the same exposure the mandate
label wins, because reporting a definitional exposure as inferred understates what is known.

An instrument cannot be classified on stress behaviour alone: a factor row with no mandate row is
refused at load, since it would rest everything about that symbol on the weaker claim.

**`CORRELATION_CONCENTRATION` is untouched** and still refuses to be enabled. It needs a frozen
covariance, which is a different kind of object from a table read off a prospectus.

## 6hh. Opening a database is not permission to rewrite it (#66)

**2026-08-28.** `Database.create` / `Database.migrate`, `MigrationRequiredError`, and a
`quantbot migrate-database` command that reports before it acts.

**#65 was containment, not the fix, and Sol was right to say so.** The autouse pytest isolation
stopped the *test suite* pointing at the operator's store. It did not remove the capability that
made the incident possible: `Database.__init__` ran Alembic to head, so **anything** that
obtained a connection could upgrade whatever it opened — a diagnostic, a read-only CLI command,
an experimental checkout carrying an unmerged migration. Acquiring read access granted mutation
authority as a side effect, which is the governing rule this project applies everywhere else.

Now:

- **Opening an existing store never upgrades it.** A behind-head file raises
  `MigrationRequiredError`, and the raise happens *before* the engine is created, so nothing has
  touched the file beyond a read-only preflight.
- **A file that is absent is still created at head**, because creating a store where none existed
  cannot damage operator state. The hazard is upgrading one that already holds someone's data.
- **`Database.migrate` is the only path that may rewrite a store**, and a caller has to name it.
- **`Database.create` refuses an existing file**, so it cannot become a migration by another name;
  `Database.migrate` refuses an absent one, so a typo'd path cannot silently produce an empty
  store and report a successful migration.
- **A store ahead of this build stays fail-closed and untouched.** There is deliberately no
  downgrade: rewriting a store to suit an older checkout is the destructive direction.
- **The CLI reports before it acts.** `quantbot migrate-database` says what it would do and stops;
  only `--confirm` carries the authority. It is dispatched before any context is built, because
  building one opens a database — and the store the command exists for is precisely the one that
  cannot be opened.

The tests assert against the **bytes on disk** rather than the returned object, because "it did
not migrate" is a claim about the file. Six protections, mutation-checked, including the incident
itself: a `status` command pointed at a behind-head store now fails and leaves the file byte-identical.

## 6ii. A default is not an approval, and a missing preference is not a blocker (#72)

**2026-08-29.** `MandateStatus.PROJECT_DEFAULT`, `EconomicObjective.authority`, and a rewritten
stop policy in `CLAUDE.md`.

**The deadlock was mine.** #54 established that an agent must not choose a success criterion after
seeing results. That concern is real. What I built to enforce it was a human signature, and those
are not the same thing — what defeats metric shopping is freezing and versioning the bar
*beforehand*, which an agent-authored objective does exactly as well as a signed one. The
signature requirement then spread: paper qualification waited on it, capacity assessment inherited
it, and implemented risk controls shipped disabled pending threshold selection.

**The circular part, which Sol caught and I had no answer to.** The requirement also gated
`LIVE_REVIEW_ELIGIBLE` — the stage whose entire meaning is "a human should look at this". Reading
the package is the event most likely to *produce* a ratification, so requiring ratification to
enter the queue meant nothing could ever arrive to prompt one. Human review had become a
prerequisite for its own eligibility.

**What replaced the gate is a label, and it tells the reader more.** `PROJECT_DEFAULT` is frozen,
versioned and honest about its authorship, and every artifact measured against an objective
reports `authority` — so a qualification claim states plainly whether its criteria were chosen by
the system or accepted by the operator. A gate only ever said "somebody touched this once".

What did **not** move:

- `RATIFIED` is unreachable from code. Promotion requires naming a human and a date, and an
  identity reading as automated is refused — so the workaround of signing as the tool is closed.
- A `PROVISIONAL` draft still certifies nothing. A criterion still being edited is not a criterion.
- `LIVE_REVIEW_ELIGIBLE` is terminal and grants no broker authority. That assertion is what makes
  opening the stage safe: reaching it cannot lead anywhere on its own.
- Switching `TRADING_MODE` to live does not clear the boundary; the acknowledgement is separate.

`tests/unit/test_autonomy_boundary.py` pins all of it against **reachability rather than wording**
— a check that grepped for "operator" would flag the comments explaining why a boundary exists and
train the next change to delete the explanation instead of the gate. Four protections,
mutation-checked.

## 6jj. The vetoes are on, and the cost of that is stated (#75)

**2026-08-29.** All five implemented vetoes enabled by default, per-group exposure ceilings, and
`PolicyAuthority` so a threshold says who chose it.

**They were off for a reason that had stopped being true.** #58 enabled only the two a thin ledger
can always answer, because `DAILY_LOSS` needs a session equity mark and `LIQUIDITY` needs twenty
sessions of volume, and on a fresh ledger both refuse. Sound reasoning, wrong conclusion: it left
tested controls doing nothing while waiting for a preference nobody had been asked for.

**A single ceiling could not work, which is why enabling it needed more than a flag.** Measured on
the deployed roster, `US_EQUITY` sits at ~5972bps of equity — a limit tight enough to bind there
refuses nearly every order the strategy exists to place. So `US_EQUITY` gets the net-exposure
ceiling and everything else gets 4000bps, which is four of ten equal-weight holdings in one
exposure: a duration book of TLT + IEF + TIP + LQD passes, a fifth does not.

**The veto was also unwired, and enabling it alone would have halted trading.** Neither production
call site passed a classification, and an absent table rejects every order. Both now load it.

**A defect this surfaced.** `effective_from` was the date the table was written (2026-08-27), a
deliberate conservatism to avoid researching per-fund history. Once the veto was enabled that
refused *every* decision dated earlier — every replay, every backtest, and the wired-cycle
integration test, which submitted zero orders. It is now 2018-10-01, the month after Communication
Services split out of the S&P sector scheme, which is the last event that materially changed what
these funds hold. One documented reshuffle is a smaller claim than 23 inception dates and buys
back the whole usable period.

**The cost is real and is asserted rather than hoped away**: a thin ledger is now refused, and a
test says so. A false refusal costs a paper entry; a false permit costs the thing the control
exists to prevent. #57 records every refusal so the trade can be revisited on evidence.

`CORRELATION_CONCENTRATION` still refuses to be enabled. Four protections, mutation-checked.

## 6kk. Freshness is a question about sessions, not seconds (#74)

**2026-08-29.** `quantbot.risk.freshness`: a versioned `MarketFreshnessPolicy`, an expectation read
from the exchange calendar, and four distinct outcomes.

**The wall clock was wrong twice, in both directions.** It first shipped at 900 seconds, which
encoded an intraday assumption into a daily-bar system and refused every entry. That was corrected
to four days, which cleared a long weekend — and the code recorded, honestly, that calendar seconds
were a coarse proxy for "sessions behind".

Elapsed time was never the measurement. A Friday close is perfectly current on Sunday; a Thursday
close is stale on Friday evening at 25 hours old. The second is *newer* and the four-day allowance
waved it through. No single number separates them, and the calendar separates them without one.

- **Daily**: the newest observation must belong to the most recent *completed* session. Weekends,
  holidays and half-days need no allowance — a half-day is simply a session whose `close_at` is
  earlier, and the rule never learns that half-days exist.
- **Intraday**: cadence-derived, and only while the market is open. The same 20-minute-old
  observation is fresh under the daily rule and `FEED_STALE` under a minute cadence, which is what
  stops #56's engine inheriting a four-day allowance.
- **`MARKET_CLOSED` is not `FEED_STALE`.** An intraday feed is supposed to be silent overnight.
- **Everything fails closed**: no calendar, no observation, a future stamp, or an intraday policy
  that cannot name its cadence.

The rejection carries `freshness_policy_identity`, expected session, observed session and market
state as **structured fields**, because #57 compares refusals across rule versions and cannot group
on prose. Eight protections, mutation-checked; two of them only became load-bearing after the
assertions were sharpened to isolate the mechanism from a second guard.

## 6ll. What moves together, and the clustering that would have got it wrong (#58, #59 slice 2)

**2026-08-29.** `quantbot.risk.dependence`: a frozen point-in-time `DependenceSnapshot`, and
`CORRELATION_CONCENTRATION` implemented against it. `UNIMPLEMENTED` is now empty.

**The obvious implementation was wrong, and measuring first is what caught it.** "Cluster symbols
above a correlation threshold" reads as connected components of the qualifying graph. Measured
over 120 sessions of daily log returns on the real stored bars, that puts **thirteen of
twenty-three symbols in one cluster at 0.80**, containing `TLT`, `IEF` and `TIP` alongside `SPY`,
`QQQ` and `XLK`.

Nothing links Treasuries to equities directly — `corr(TLT, SPY) = +0.445`. They arrive together
through credit: `HYG–SPY` at 0.82 on one side, `LQD–TLT` at 0.90 on the other. Single linkage
needs one qualifying edge per join, so a chain suffices.

A limit built on that would report a bond position as adding to equity concentration. It is not
conservative — it is a wrong answer that happens to refuse, which is the hardest kind to notice.

**Complete linkage**, requiring every pair inside a cluster to clear the threshold, gives
`{IEF, LQD, TLT}` and `{QQQ, SPY}` and `{IWM, MDY}`: duration, large-cap growth, mid/small caps.

**The threshold is measured, not picked.** At 0.90 the clusters are identical at 40, 90 and 120
sessions. 60 sessions disagrees — it produces `{EEM, QQQ, XLK}` instead — which is a real caution
that the rule is window-sensitive, and exactly why the window is part of the snapshot identity
rather than a constant somewhere.

**A control that needs data forces the data layer to fetch it.** The sync window was
`warmup_sessions(config)` — what the *signal* needs. A correlation veto measuring over 120
sessions against a store holding 52 is permanently unmeasurable, refusing every order correctly
and uselessly. The sync now fetches whichever consumer needs more.

**`REQUIRE_COMPLETE`, with no pairwise-complete option**, because pairwise-complete computes each
coefficient on whatever sample that pair happens to share and then presents them as comparable.

Eleven protections, mutation-checked. Three needed isolated tests written before they were
load-bearing: the existing thin-ledger test asserts a *superset* of refusals, so it stayed green
whether or not the correlation guards fired.

## 7. Open items

- [x] **Enable the implemented portfolio vetoes — done autonomously under #72/#75.** All five
      now ship on, with per-group exposure ceilings, and the classification is wired into both
      production call sites. Both former operator items are closed; the thresholds are labelled
      `PROJECT_DEFAULT`, not approved.
- [ ] **#48 reproducibility is blocked on there being a confirmatory experiment to reproduce.**
      The readiness half is built and exercised (§6ee); the reproducer needs a real manifest,
      which needs a confirmatory run, which needs M1. Not blocked on engineering.
- [ ] Accumulate paper observations toward the 30-day qualification window (day 1 of 30).
      Note that the window is now an authenticity floor rather than the qualification: see 6bb.
- [ ] **~~Ratify `config/economic-objective-v1.yaml`; it blocks `PAPER_QUALIFIED` entirely.~~
      It does not, and has not since #72.** The file's `status` is `PROJECT_DEFAULT`, and
      `MandateStatus` accepts that alongside `RATIFIED` (`research/mandate.py`) — a frozen,
      versioned bar an agent wrote still stops metric shopping, because what stops it is
      freezing the criterion before the results and not signing it. The old design gated
      `LIVE_REVIEW_ELIGIBLE` on ratification too, which made human review a prerequisite for its
      own eligibility; that deadlock is what #72 removed.
      **What remains is a label, not a gate.** Five values are still marked `[PROPOSED]` because
      an agent chose them, and every artifact measured against this objective reports its
      `authority` so a qualification claim says in plain words who set the bar. Ratifying is
      still worth doing — `intended_capital_range_usd` and `minimum_meaningful_improvement_bps`
      in particular are guesses at operator intent — and it is an operator action:
      `RATIFIED`/`ratified_by` are unreachable from code, and a value that reads as an automated
      identity is refused at load. Ratifying does not change the identity hash, so evidence
      already measured stays valid; editing a value does, and forces reassessment.
- [ ] **Deploy the seeded findings to the Pi.** Its store is still empty; it runs an older
      checkout and this session had no working SSH credential for it.
- [ ] **Move the ledger to the coordinator and start the daemon there** — **operator action**,
      sequence in `deploy/omen/README.md`. Deliberately not scripted.
- [x] **~~Kronos criterion 9 (#36)~~ — CLOSED 2026-08-22.** H-2026-026 pre-registered before
      measurement, critic `PROCEED`, and the power gate refused it `UNDERPOWERED`: an IC of 0.15
      needs 4,485 observations against 688 available (57.3 independent after 12x inflation).
      The refusal cost nothing — trials still 68, window still `UNTOUCHED`. **#36 is closed**;
      Sol's unanswered question on OHLC ordering is recorded disagreement in the thread, not an
      unmet criterion. The aligned correlation stays uncomputed: below the 0.375 detectable
      effect it is uninterpretable, and a number under that ceiling gets quoted without it.
- [ ] **TradingAgents wrapper: implementation exists, acceptance criteria not yet satisfied
      (#37).** `quantbot.research.agent_debate` runs pinned `a33fd4c0` out of process and returns
      the bull/bear debate as `derived` + `UNVERIFIABLE` sources; verified end to end on a real
      SPY run. **Sol's review then found three evidence-integrity defects, all mine, all now
      fixed:** debate backdated to the session it discussed (direct look-ahead — `known_by` gates
      on `published_at`), upstream still owning source acquisition (now refused for historical
      sessions; forward-only), and a self-asserted framework revision (now read from the
      installed checkout). Read "tests pass" as tests pass, **not** as criteria met — the
      downstream-value evaluation has not run.
- [ ] **TradingAgents criterion 9 (#37):** whether it improves downstream hypothesis quality.
      Needs the scheduled loop and hypotheses on both sides.
- [ ] Crypto sleeve blocked on a second Alpaca paper account (**operator action**)
- [ ] Exposure normalisation for asset-class sleeves — research, **not yet pre-registered**
- [ ] Fills are ingested from the REST ledger once per cycle; the push trade stream is not held
      open between cycles
- [x] **~~Move the trend gate out of roster construction~~ — CLOSED 2026-08-19.**
      `trend_gate_per_session` was already implemented and gated off; what was missing was a valid
      measurement. With the position cap, the risk ladder, and one corrupt bar all controlled for,
      the engine reproduces `SPY_SMA200` to **0.07 CAGR points** (10.56% vs 10.63%, Sharpe 0.92 vs
      0.93, exposure 77.29% vs 77.44%). The gate itself is worth 1.70 CAGR points.
      `reports/research/spy-sma200-resolved-2026-08-19.md`.
- [ ] **Decide whether `SPY_SMA200` was ever worth reproducing — evidence says no.** It loses to
      SPY buy-and-hold by **4.75 CAGR points** ($291 vs $455 per $100). Its whole case is a +0.026
      Sharpe edge, and paired against buy-and-hold that edge is **negative in point estimate and
      insignificant** (−2.005 bps/session, t = −1.24, 27 years needed). Operator call on whether the
      halved drawdown (19.50% vs 33.79%) justifies it; it is not an alpha result.
- [ ] **Apply the power gate to engineering targets, not just hypotheses.** A benchmark treated as
      a goal is a hypothesis about what is worth building. Six cycles pursued a target that a
      one-backtest check would have disqualified before any code was written.
- [x] **~~Identify the second constraint on position weight~~ — resolved 2026-08-19: there is
      none.** Weight is 100.00% at a 100% cap. The plateau is the drawdown ladder halting entry on
      928 sessions. See §6h.
- [ ] **Tranche the rebalance date** — $61.24 of terminal-wealth spread per $100 decided by the
      calendar alone, the largest free effect found in 13 cycles. Design settled in
      `docs/tranching-spec.md`: **5 tranches** (88% of the benefit, $2.00 minimum trade against
      Alpaca's $1 floor). The cheap weight-ramp shortcut is measured and refuted, so the roster
      machinery genuinely has to change.
      **Steps 1 and 2 are now built.** Scheduling and weighting in
      `src/quantbot/strategy/tranches.py`; roster identity and tranche-aware expiry on
      `MonthlyRoster`/`XNYSSessionSequence`; durable tranche attribution on `PositionContext`
      and in `position_state`. The single-tranche equivalence to month-end is pinned
      month-by-month against the calendar, and state written before tranching existed still
      loads as tranche 0 — the live account is holding positions in exactly that shape.
      **Step 3 is half done.** `build_monthly_roster` takes a tranche and validates against the
      schedule instead of insisting on month-end; `_tranche_rosters` builds one roster per
      tranche; `_target_fractions` blends them. Unstarted tranches are omitted rather than
      passed as empty rosters — an empty roster and an unstarted one differ, and conflating
      them would divide every weight by the full tranche count and under-invest the account
      through the ramp-up month.
      **Remaining: wire the blended fraction into position sizing** — `size_entry` still caps
      at the full `max_position_value_bps` regardless of how many tranches hold the name. That
      is a change to the risk path and deserves a fresh session rather than a tired one.
      Nothing is switched on; `rebalance_tranches` is still at its default of 1.
- [ ] **Decide whether to deploy v1.3.0** — see 6c; restarts the qualification window
- [ ] **Automate halt resumption** (cycle 11 S4) — worth more than any signal tested
- [ ] **Recalibrate the cost model** from 5bps — deliberately NOT done yet. Two independent
      estimates say the assumption is 5-20x too high (quoted NBBO 0.26-3.31bps; realised fills
      -0.3bps against the opening auction), and there is a structural reason to believe them:
      DAY orders submitted after the close execute in the opening auction, where a single
      clearing price means no spread to cross. But n=3. Lowering an assumed cost improves every
      historical result at once, so it needs a real sample.
      `scripts/slippage_report.py` accumulates the evidence and refuses to endorse a change
      below n=30. Re-run it as fills accrue; when it clears, do the recalibration as a
      pre-registered change with a new strategy version, not by editing the constant.

## 8. If you are about to change the strategy

Stop. Open `docs/agent-runbook.md` and follow the controlled research cycle: pre-register the
hypothesis, run it, apply multiple-testing correction, and record the outcome in `REFUTED.md`
whether it passed or failed. An uncorrected backtest improvement is not evidence.
