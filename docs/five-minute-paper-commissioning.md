# Five-minute PAPER adapter candidate

> **Current PAPER runtime, October 15:** The approved affordable-stock/whole-share entry policy is active on the Omen. Fractions remain preferred; an exact one-share fallback keeps the existing loss budget. The first AAPL round trip is verified; fees/net remain unknown and no learner fit is claimed. See the [current handover](evidence/paper-core/whole-share-2026-10-15/README.md).

> **Legacy DIA adapter commissioning.** Current architecture and status are [PAPER core](paper-core.md) and [handover](evidence/paper-core/handover.md). The [approved hybrid/V3/current-funds successor](superpowers/specs/2026-10-14-daily-paper-exploration-design.md) requires its own prepared configuration and target/funds-specific commission; it is merged in PR #290 and commissioned locally with the submission readiness repair. The [Omen package](evidence/paper-core/omen-deployment/README.md) records the older XLE baseline. These legacy procedures neither activate the successor nor provide a compatible schema-29 service restart after schema-36 migration. The no-forced-acceptance and known-fee rules below retain their earlier adapter scope; they do not prohibit properly commissioned hybrid exploratory entries or V3 gross learning.

> **Earlier guidance — 2026-10-06:** These commissioning controls applied to the retained adapter; historical setup records did not activate new shadow artifacts or earn fresh outcomes.

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
7. `PaperWorkerConfig.contract` explicitly dispatches one-minute versus five-minute
   entry. The default remains one-minute; five-minute requires the observer config's
   exact SHA256, an explicit frozen model and full expected execution-binding hash.
   The separate entry state cannot overlap observation, frozen, clock or canonical
   stores. Exact model consent, canonical lineage/feature holdouts, clock health and
   recorded deployment/PAPER gate are required before entering the existing cycle.
   Waiting for a cohort cannot create or claim a missing deployment or permission.
8. `quantbot.learner.five_minute_entry` is the native entry launcher. The ordinary
   worker's five-minute branch consumes existing observations directly and never
   invokes one-minute capture, challenger training or automatic model choice. It
   retains the existing worker lock, receipts, progress alerts and ownership audit.
   Receipt validation binds the original artifact, code, consent, config and current
   durable gate. `--check` constructs no broker or runtime and performs no writes;
   its message explicitly says runtime eligibility was not checked.

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
- Configure the implemented native entry launcher with the existing PAPER profile
  and a separate existing `state/learner.db`; it creates no guard store or migration.
  `paper-execution.json` must include `contract: five-minute-price-alpha-v1`, the
  existing observer-config absolute path and exact SHA256, the explicitly chosen
  frozen `model_id`, actual operational database, inherited baseline/exclusive-entry
  policy and `expected_binding_hash = content_id(five_minute_paper_binding(...))`.
  Compute that binding from actual preserved base risk settings, recorded veto policy,
  actual market-data config and prospective DIA classification, never from assumed
  defaults. Set no model, consent, promotion or deployment record automatically.
- The test-only native command is `python -m quantbot.learner.five_minute_entry
  --directory <entry-root> --env-file <existing-paper-profile> --commit <exact-pin>
  --check`. Only omit `--check` after the separate commissioning approval and all
  required readiness checks. `task_xml` returns a plan only: existing interactive
  user, least privilege, hidden host, no wake, every ten minutes at 09:31:35 Eastern.
  This is 30 seconds after the observer's scheduled start, keeping the nominal
  five-minute cutoff within the independent 120-second entry limit. A slow/missing
  capture waits or rejects; catch-up never manufactures a timely entry. No task was
  registered or existing scheduler changed by this source work.
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

Read-only end-of-session acceptance at 2026-10-01 20:15 UTC verified 20 original
forecasts and 20 resolved labels, no pending labels, ten distinct resolved windows
and four nonoverlapping windows (not a statistical independence claim). These totals
include the two seed forecasts/labels; 18 additional forecasts/labels accumulated in
the existing observational installation. All 20 forecasts remain below the fixed
cost floor. The last natural tick at 20:11 reported OUTSIDE_FORECAST_SESSION with
zero orders/fits/authority. All prior evidence bytes and original charges verified.
At 20:23 UTC the dashboard reader verified one Oct 1 close baseline (captured 20:01
UTC), zero finalized account-P&L days, and today's honest BASELINE_MISSING status.
No previous close or profit was invented. Forecast labels are instrument-return
evidence, not daily account P&L or profitability proof.

Validation receipts are task-local. The final relevant aggregate passed 735 tests
in 139.65 seconds across all learner/risk unit tests, execution coordinator/runtime
integration, monitor/session and learner-panel tests. Ruff and mypy passed (six
changed source files). Native dispatch tests add mock entry, repeated-tick dedupe, missing consent/clock,
protected windows, exact config/binding refusal, native read-only preflight and
unchanged one-minute worker behavior. The mock cycle includes synthetic buy/sell
fills, independent exit after entry demotion, and a durable closed event without
duplicate exit. A date-boundary regression verifies March 31 lineage does not
include April 1, while lineage actually ending April 1 remains protected.
Full-project/hosted CI remain unrun;
no branch protections are waived. No GitHub publication, runtime activation, real
broker submission, model swap or refit occurred.
