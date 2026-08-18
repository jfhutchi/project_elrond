# Tranching the rebalance date — implementation spec

Cycle 11 S1 measured the effect; cycle 13 measured it on the real rotation and settled the
design questions. This is the largest free improvement found in 13 research cycles and it is
**not yet implemented**. This spec exists so it gets built correctly rather than quickly.

## What is being fixed

The deployed strategy rebalances its roster on one fixed date. Running the identical strategy
on a different day of the month produces terminal wealth anywhere from **$246.38 to $307.61**
per $100 — a **$61.24 spread decided by nothing but the calendar**. The system currently holds
one arbitrary draw from that distribution.

Tranching does not raise the expected return. It removes the gamble.

## Settled by measurement

| Question | Answer | Evidence |
|---|---|---|
| How many tranches? | **5** | Removes 88% of dispersion. 10 removes 93% but needs a $1.00 minimum trade, exactly Alpaca's floor. 5 needs $2.00, giving 2x headroom. |
| Is daily partial rebalancing equivalent? | **Yes**, but infeasible | Correlation 0.9987, $1.11 apart on $290. Needs $0.48 trades at $100 — below the $1 minimum. |
| Can weight-ramping substitute? | **No** | Tested and refuted: ramping over 5 or 10 sessions changed dispersion by −9% and −6%, i.e. slightly worse. Dispersion comes from *which date the ranking is evaluated on*, not from transition speed. A cheap substitute does not exist. |

That last row is the important one. It rules out the implementation that would have required no
changes to the roster machinery, so the invasive version is the only one that works.

## Built so far

`src/quantbot/strategy/tranches.py`, pure and covered by 18 tests:

* `tranche_schedule` — which session each tranche rebalances on. **A single tranche returns
  exactly the final session of each month**, so today's behaviour is the degenerate case rather
  than a separate code path.
* `next_rebalance_index` — replaces month-end expiry. A roster expires when its own tranche next
  rebalances; for one tranche that *is* the month boundary.
* `active_tranche_rosters` — which ranking each tranche is currently trading, returning `None`
  for tranches that have not had a first rebalance rather than pretending they hold nothing.
* `tranche_weights` — fractional membership: held by 3 of 5 tranches means three fifths of a
  position.
* `max_supportable_tranches` — derived from equity, not hardcoded, so tranching widens itself as
  the account grows past the broker minimum.

`rebalance_tranches` is wired into `StrategyConfig`, omitted from the canonical configuration at
its default so all four deployed identities stay byte-identical (pinned by test).

## What remains

`build_monthly_roster` still enforces `evaluation_at` being the final XNYS session of its month
(`adaptive_momentum.py:250`). Tranching needs **5 independent rosters evaluated on 5 staggered
sessions**. In dependency order:

1. **Roster identity.** `MonthlyRoster` gains a tranche index, and `build_monthly_roster` stops
   rejecting non-month-end evaluation dates (`adaptive_momentum.py:250`), accepting instead any
   session the schedule assigns to that tranche. `roster_expiry_after` delegates to
   `next_rebalance_index`.
2. **Durable state.** Roster state must record which tranche opened what, or exits cannot be
   attributed. **This is the part most likely to produce a reconciliation defect** and deserves
   the most care — a mis-attributed exit shows up as a position diff that halts trading.
3. **Runner and sizing.** Build one roster per tranche, blend with `tranche_weights`, and scale
   the position-value cap by the resulting fraction.

## Constraints to respect

* **$1 minimum notional.** At $100 with a 10% cap, a 1/5 slice is $2.00. Do not raise the
  tranche count without re-checking this; it is why 21 tranches is unavailable.
* **The constraint relaxes with capital.** Above roughly $210 even 21 tranches clears the
  minimum. The tranche count should be derived from equity, not hardcoded.
* **Deploying this restarts the 30-day qualification window.** It changes the strategy identity.

## Do not

* Do not implement the weight-ramp shortcut. It is measured and refuted above.
* Do not claim this raises returns. It removes dispersion; the median outcome is unchanged.
