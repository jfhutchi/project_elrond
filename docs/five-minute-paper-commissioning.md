# Five-minute PAPER adapter candidate

This is local source, not an activated strategy. It consumes existing frozen DIA
observations and preserves the original models, trial charges, zero-selection
validation result, cost threshold and observation/outcome files. Positive-score
integration fixtures are explicitly synthetic, with hand-authored parameters;
they are not new research fits or real trading results.

## Implemented path

1. `load_five_minute_cohort` verifies the existing launch configuration's frozen
   pair and original canonical charges through `bound_models`. Model choice is
   explicit. It requires a verified clock, a unique current cohort and the exact
   original provider-snapshot bytes, then reconstructs the five-minute features
   and forecast. It performs no capture, fit, trial registration or writes.
2. `FiveMinutePaperRunner` validates the distinct feature contract and immutable
   source/model/code/risk binding. A one-minute artifact, substituted score,
   foreign account, live broker, missing baseline or wider pilot limit refuses.
   Broker health, account restrictions and post-response freshness are checked.
3. The established runner measures all six portfolio vetoes using durable daily
   risk history and actual holdings. It retains aggregate exposure, drawdown,
   cash reserve, missing-stop refusal and one-new-position ownership rules.
   The true 300-second feed cadence does not relax the 120-second entry limit.
   Missing risk history remains unavailable, not fabricated.
4. The inherited-position lifecycle records immutable baseline/retirement/thesis
   provenance without learner exit ownership. New entry checks revalidate clock,
   protected window, reconciliation, kill switch, independent deadline heartbeat,
   and fresh account-matched protection covering the observed position count.
5. The existing deadline handoff is durable before a submission request is returned.
   The existing promotion/deadline-gated coordinator handles PAPER submission.
   The independent supervisor handles stops/horizons without predictor permission;
   it continues after entry permission is withdrawn and records fills/closed outcomes.
6. `build_five_minute_paper_services` composes this path into an already configured
   PAPER runtime. It never opens, migrates, deploys, promotes or starts that runtime.
   It does not start another deadline supervisor or stop position protection.

`dia_exposure_classification` retains every original mandate/factor assignment and
adds only DIA's issuer-backed US_EQUITY mandate. This separately hashed definition
is effective prospectively on 2026-10-01; it does not change concentrations or
claim a technology/constituent mapping. Source: [State Street DIA fund objective](https://www.ssga.com/us/en/intermediary/etfs/state-street-spdr-dow-jones-industrial-average-etf-trust-dia).

## Smallest future commissioning scope

- Pin and validate this candidate; preserve the observation producer at its existing
  release and read its immutable root. No operational schema migration is required
  by these adapter changes.
- Choose exactly one already frozen model, record its exact identity/binding at
  PAPER_OBSERVATION through the supported lifecycle, and explicitly withdraw the
  old one-minute learner's entry permission. Exclusive entry rejects two active
  learner contracts. This is an execution-contract/model replacement, not a claim
  of economic qualification or approval to trade live.
- Configure an entry worker to call the frozen loader and factory with the existing
  PAPER profile, verified-clock callback and canonical protected-window guard.
  Native launcher/config integration is still required: the current deployed
  `execution_worker` recognizes one-minute model/cohort files. This library is not
  automatically consumed by that task. Do not deploy it as though it were.
- Preserve the existing inherited baseline and all tighter controls: one new learner
  position, maximum 5% value, 0.25% entry risk, at most 1% stop and 30-minute horizon.
  No legacy sales/reset, paid feed, new credentials, new access or protection outage
  is part of this scope. IEX remains SINGLE_VENUE; no consolidated-volume anomaly
  capability is invented.
- Prepare entry-only config/task backup and rollback; retain the original protection
  and single deadline supervisor. Before enabling entry, verify current broker state,
  complete daily risk inputs, exact recorded policy/classification, baseline continuity,
  and fresh protection/reconciliation/deadline receipts. Observe the first eligible
  prospective entry, broker fill, protective/horizon exit and durable account outcome.
  A rejection is recorded honestly; never force a trade to satisfy acceptance.

The latest previously verified real diagnostic (2026-10-01 19:21 UTC) found 18
forecasts, all below the unchanged cost floor, and no eligible intent. Commissioning
does not turn those observations into orders. Forecast labels are instrument-return
evidence, not daily account P&L or profitability proof.

Validation: 131 focused tests across the adapter, five-minute observation/launch,
dry-run, shared one-minute runner, deadline, inherited ownership and existing worker
tests passed; Ruff and mypy passed. The mock cycle includes synthetic buy/sell fills,
independent exit after entry demotion, and a durable closed event without duplicate
exit. Full-project and hosted CI checks were not run; no branch protections are waived.
No GitHub publication or runtime activation occurred.
