# Tranching the rebalance date — implementation spec

Cycle 11 S1 measured the effect; cycle 13 measured it on the real rotation and settled the
design questions. This is the largest free improvement found in 13 research cycles. This spec
exists so it gets built correctly rather than quickly.

> **Status, re-verified against `main` on 2026-09-02.** This document said "**not yet
> implemented**" and listed three steps as remaining. Two of them are built and the third is
> half-built, so the header was wrong in the direction that costs the most: a reader would have
> rebuilt work that exists. **Nothing is switched on** — `rebalance_tranches` defaults to 1 and
> no deployed config raises it — which is why the claim survived. See *What remains* below for
> what is actually left, which is one wire into the risk path plus the backtest engine.

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

Tranching needs **5 independent rosters evaluated on 5 staggered sessions**. The three steps
below were written in dependency order when none of them existed. Their status on `main` as of
2026-09-02:

1. **Roster identity — built.** `MonthlyRoster` carries `tranche` and `tranche_count`;
   `build_monthly_roster` takes a `tranche` argument and validates `evaluation_at` against
   `tranche_schedule_map` instead of insisting on month-end
   (`adaptive_momentum.py:290-303`). `XNYSSessionSequence.tranche_expiry_after` delegates to
   `next_rebalance_index`. The single-tranche case is pinned month-by-month against the calendar,
   so today's behaviour is still the degenerate case rather than a second code path.
2. **Durable state — built.** `RosterPosition.tranche` and `PositionContext.tranche` record which
   tranche opened what, and `_prior_position_state` reads `tranche` out of `position_state`
   defaulting to `0` when absent, so state written before tranching existed still loads — which
   is the shape the live account's positions are in right now. This was called out as the part
   most likely to produce a reconciliation defect, and a mis-attributed exit still shows up as a
   position diff that halts trading, so it stays the part to be careful with.
3. **Runner and sizing — half built, and the built half is unreachable.**
   `AdaptiveMomentumRunner._tranche_rosters` builds one roster per tranche, omitting tranches whose first
   rebalance has not happened rather than passing them as empty; `_target_fractions` blends the
   result with `tranche_weights`. **Neither is called by anything.** The live path is
   `runners.py:1312`, which calls `self._roster` — the single month-end roster — and `size_entry`
   still caps at the full `max_position_value_bps` regardless of how many tranches hold a name.

So what is genuinely left is:

* **Wire the blended fraction into position sizing.** That is a change to the risk path and the
  reason it was stopped short of: the two helpers above are dead code until it happens, and dead
  code that looks finished is exactly what made this document's old header wrong.
* **Call `_tranche_rosters` from the session path** instead of `_roster`, once sizing can accept
  a fractional target.
* **The backtest engine has no tranching at all.** `backtest/engine.py` contains no reference to
  a tranche; it rebuilds `active_roster` only at `is_month_end` (`engine.py:622`). Until that
  changes, tranching cannot be backtested — only run forward — which means the measurements this
  spec is built on cannot be reproduced through the production engine.

## Constraints to respect

* **$1 minimum notional.** At $100 with a 10% cap, a 1/5 slice is $2.00. Do not raise the
  tranche count without re-checking this; it is why 21 tranches is unavailable.
* **The constraint relaxes with capital.** Above roughly $210 even 21 tranches clears the
  minimum. The tranche count should be derived from equity, not hardcoded.
* **Deploying this restarts the 30-day qualification window.** It changes the strategy identity.

## Do not

* Do not implement the weight-ramp shortcut. It is measured and refuted above.
* Do not claim this raises returns. It removes dispersion; the median outcome is unchanged.
