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

## What has to change

`build_monthly_roster` currently enforces `evaluation_at` being the final XNYS session of its
month (`adaptive_momentum.py:250`). Tranching needs **5 independent rosters evaluated on 5
staggered sessions**, so:

1. **Roster identity.** A roster gains a tranche index. Five live rosters at once, each with its
   own evaluation date and expiry.
2. **Scheduling.** Tranche *k* evaluates on session `round(k * sessions_in_month / 5)`. The
   month-end constraint becomes "the scheduled session for this tranche".
3. **Target weights.** A symbol's target is `max_position_value_bps * (tranches holding it) / 5`.
   Membership stops being binary.
4. **Durable state.** Roster state must record which tranche opened what, or exits cannot be
   attributed. This is the part most likely to produce a reconciliation defect.
5. **Config.** `rebalance_tranches: int = 1`, omitted from `canonical_configuration` at the
   default so deployed identities stay byte-identical — the same pattern
   `volatility_target_bps` uses.

## Constraints to respect

* **$1 minimum notional.** At $100 with a 10% cap, a 1/5 slice is $2.00. Do not raise the
  tranche count without re-checking this; it is why 21 tranches is unavailable.
* **The constraint relaxes with capital.** Above roughly $210 even 21 tranches clears the
  minimum. The tranche count should be derived from equity, not hardcoded.
* **Deploying this restarts the 30-day qualification window.** It changes the strategy identity.

## Do not

* Do not implement the weight-ramp shortcut. It is measured and refuted above.
* Do not claim this raises returns. It removes dispersion; the median outcome is unchanged.
