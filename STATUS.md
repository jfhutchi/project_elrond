# QuantBot — Agent Orientation

## START HERE

### ⚠ SEVERITY-1 (FIX BUILT, NOT DEPLOYED): the drawdown halt is a trap

Measured: the halt blocks entry on **70.8% of sessions** in backtest. Once equity is 15% below
its high-water mark, `entry_halted` stops new positions — but a strategy that cannot take
positions cannot earn back the drawdown. **The halt has no exit.**

`strategy-v1-2.yaml` (DEPLOYED) uses the same thresholds and has a 26.4% historical drawdown.
**If the live account reaches -15%, it stops trading and cannot recover by trading.** Only a
deposit or manual intervention releases it.

**Fixed in code, off by default, not yet deployed.** `drawdown_halt_floor_bps` trades at reduced
size instead of stopping. All four candidate fixes were measured in
`scripts/halt_policy_study.py`; a floor of 2500bps was chosen and verified through the real
engine, where it releases the trap (6 → 25 trades, 19.2% → 50.3% exposure).

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

**Cause pinned: it is time in market, not position size.** Adding `target_weight_sizing` to
remove the ATR risk cap entirely changed nothing — still 43% exposure, still 2.52% CAGR. This
engine is invested on 43% of sessions against the benchmark's 77%, using the *same* 200-day
condition. The difference is that it only acts on **monthly rebalance dates**: when SPY dips
below its average and recovers mid-month, the benchmark re-enters the next session while this
config waits until month end. Those missed re-entries are most of the return.

**Narrowed to one line.** `adaptive_momentum.py:273` applies the trend filter when the roster
is *built* (monthly). A symbol below its 200-day average at month end is locked out for the
whole following month, and a symbol absent from the roster cannot be entered at any size —
which is why every sizing knob looked inert. The trend gate is a hold-while-true predicate and
belongs in the per-session `evaluate_symbol` path, where it already exists.

Full spec: `docs/per-session-trend-spec.md`.

**CAUTION, added after the fact, now partly addressed:** `run_research_backtest.py` ran only
FIXED benchmark variants, so it ignored a config's component selection entirely and three config
changes produced identical output. **The 43%-vs-77% exposure diagnosis came from that runner and
is therefore still unattributed** — it has not been re-measured.

The harness defect itself is fixed. `switches_for_config()` maps `StrategyComponents` onto the
engine's `ComponentSwitches`, the runner now emits a `CONFIGURED_STRATEGY` row that actually
applies the config under test, and `tests/unit/backtest/test_configured_harness.py` asserts the
property whose absence allowed the original failure: **change the config, and the result has to
move.** `atr_risk` has no configuration counterpart and is named explicitly rather than left as
an invisible default.

**First attributed run, 2026-08-19** (`reports/research/attributed-harness-2026-08-19.md`,
classification `EXPLORATORY`): on 1,520 sessions of IEX data the deployed `strategy-v1-2.yaml`
scores **3.64% CAGR, Sharpe 0.61, 33.82% exposure** — against `FULL_STRATEGY`'s 0.30% / 0.11 /
18.66%, which is the row the old runner reported in its place. The harness is connected.

Two things that run does **not** establish, stated because the temptation is real. It is on
IEX 2020-2026, not SIP 2016-2026, so it is **not comparable** to any figure in this file or in
`REFUTED.md`, and it is **not** a correction of the 43%-vs-77% diagnosis in §6h. And the window
is consumed, so under §6i–6v the run is exploratory by construction: no registration, no power
gate, no probes. It is a diagnostic of the harness, not a measurement of an edge.

Re-measuring the cycle-16 finding needs the SIP database through the same runner. Still
outstanding, and now possible.

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

Branch `claude/roadmap-2-issue-5-185afc`. 766 tests; `ruff` and `mypy --strict` clean. Read this
section first if you are resuming.

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
| #3 Research director | `98f60b7` | lifecycle, computed priority, no route to the broker |

Schema is at V6 with revisions 0001–0006. Every revision declares its own tables.

### Not built

**#13 Discovery Engine, #11 External Workers, #15 Dashboard, #16 Promotion Ladder.**

Nothing calls an LLM anywhere in this system. `ModelRuntime` is wired and unused; the critic is
deterministic and names the five judgment dimensions it did not assess. No autonomous loop runs:
each gate is callable, none is driven.

### The next action

#13 in the stated order. Note before starting it: a discovery engine against an **exhausted**
holdout produces well-documented noise. `window_consumption("sip-us-equities-daily", ...)` will
answer `EXHAUSTED` for 2016–2026 US equities once seeded, and #14 will refuse the trials. That
is the system working, and it means #13's value depends on #17 delivering a genuinely new asset
class first — which needs a provider integration that could not be exercised offline here.

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

## 7. Open items

- [ ] Accumulate paper observations toward the 30-day qualification window (day 1 of 30)
- [ ] Crypto sleeve blocked on a second Alpaca paper account (**operator action**)
- [ ] Exposure normalisation for asset-class sleeves — research, **not yet pre-registered**
- [ ] Fills are ingested from the REST ledger once per cycle; the push trade stream is not held
      open between cycles
- [ ] **Move the trend gate out of roster construction** (`adaptive_momentum.py:273`) —
      spec in `docs/per-session-trend-spec.md`. Highest-value remaining work.
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
