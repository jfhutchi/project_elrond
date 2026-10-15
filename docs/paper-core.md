# PAPER core: baseline, approved successor and acceptance

> **Runtime handover, October 15 at 08:13 Eastern:** both stores are now schema 36; upgraded dashboard, inherited protection and deadline services are running. The bounded deployment task waits for the original IWM exit before commissioning guardian/outcomes and enabling trader after fresh protection checks. Trading is not active at this capture. See the [measured runtime handover](evidence/paper-core/runtime-handover-2026-10-15/README.md).

The merged baseline replaces the operational path incrementally with three independent roles:
**trader**, **guardian**, and **outcomes with daily learning**. It retains the existing broker,
execution, SQLite, research-memory, and protected-data foundations. This overview summarizes the
[baseline specification](superpowers/specs/2026-10-13-paper-core-design.md) and the implementation
in [`src/quantbot/paper_core/`](../src/quantbot/paper_core/). Detailed deployment facts belong in
the [handover](evidence/paper-core/handover.md) and
[current Omen evidence](evidence/paper-core/local-deployment-2026-10-15/README.md).

The [approved successor](superpowers/specs/2026-10-14-daily-paper-exploration-design.md)
collects daily PAPER experience across stocks: qualifying signals take priority, otherwise
it selects an exploratory entry. V3 learns from actual gross outcomes, including wins and
losses, while preserving unknown fees and net P&L. A separate versioned current-funds policy
allows bounded entries without claiming that incomplete historical cash has been reconciled.

**The successor is merged in PR #290 and installed locally, but not commissioned.**
Its actual Windows task-identity verifier passed. The original IWM exit must reconcile before
commissioning; fresh guardian certification and inherited protection must then pass before
starting the trader. Both stores are now schema 36 and compatible retained services are running.
The integrated witness covers ten sessions of mixed gross outcomes,
real V3 fitting/activation and later signal/exploration decisions. The dated prepared baseline
remains XLE-bound. Use the [handover](evidence/paper-core/handover.md) for rollout status.
Daily attempts cannot guarantee fills or improved profit.
The [successor evidence package](evidence/paper-core/daily-exploration/README.md) retains task
reviews, candidate-preparation status and limitations. It remains separate from operational
acceptance and the sealed baseline release.

## Runtime and authority

The trader evaluates fresh five-minute bar opportunities, records both decisions and refusals,
and reads one active immutable model. It does no fitting. A durable intent and protection plan
precede submission; the final entry-authority check shares the transaction that records a
possible send. Recovery retains original client IDs and never treats a timeout or broker 404 as
proof that an order was not sent.

The guardian runs independently, imports order/fill evidence, reconciles certified ownership,
and owns stops and deadline exits for new-core lots. It neither opens exposure nor adopts
unexplained holdings. Every incremental fill remains protected. Inherited positions retain
their original owner until a separately verified handover; a position reset is not migration.
For the successor, health and price refresh follow the selected or actually owned symbol,
including owned lots whose symbols have left the candidate universe.

The outcomes worker continues processing durable evidence when markets are closed or entries
are paused. It assembles owned round trips, revises costs, and considers learning. A bounded fit
subprocess receives immutable inputs without broker credentials or an operational database
handle; outcome processing takes priority. Learning failure cannot disable the guardian.

All three use one authoritative operational SQLite store. An optional dashboard reads their
health and evidence; it grants no execution authority. The Omen is the sole permitted PAPER
coordinator. The Pi is retired.

The endpoint is fixed to `https://paper-api.alpaca.markets`. Runtime authority binds the exact
account, store, release, policy, model, and role. Unknown configuration or a mismatched binding
is refused. Code release identity is distinct from economic policy/model identity, so reviewed
compatible maintenance does not discard useful history or silently change trading semantics.
Real-money trading remains disabled and unauthorized.

## Daily signals and exploration

The successor's `SIGNAL_OR_EXPLORE_V1` preserves `MODEL_ONLY_V1` as the legacy selection
mode. Its candidate universe is AAPL, MSFT, AMZN, GOOGL, META, NVDA, JPM, JNJ, XOM and PG;
SPY supplies benchmark features and is not an entry member. Broker eligibility is measured,
not inferred from this list. Each completed five-minute boundary acquires a causal cohort;
unavailable, inherited-held or otherwise ineligible members have explicit reasons while
healthy members remain eligible.

Any valid activated prediction **strictly above 10 bps** takes priority over exploration.
Signals rank by current-session reservation count ascending, prediction descending,
prior-session count ascending and configured order. Thus a qualifying used stock beats
unused stocks with weak signals. If no eligible signal qualifies, exploration ranks by
current-session count, prior-session count and configured order. It keeps the true zero or
negative prediction and records `EXPLORATION`. COLLECT supplies exploration; invalid model
authority is a refusal, not permission to fabricate a weak-signal fallback.

Captured cohort/selection evidence is replayed at claim and final send. There are at most
**six durable reservations per verified exchange session** and one concurrent core lot.
Completed trades and uncertain possible sends still consume reservations; only definitive
pre-send proof releases one. A semantic boundary is independent of acquisition time, symbol,
model or executable release, preventing duplicate claims after restart. Protection and
outcomes continue after the cap. Six reservations are not a promise of six broker fills.

The envelope retains 0.5% equity position value, 0.25% entry risk, a 1% stop, a 30-minute
horizon, at least a 120-second close buffer and the existing buying-power reserve. A failed
or rejected fit does not stop exploration. Genuine market, data, authority or broker blockers
remain visible and retryable; inherited positions are not liquidated to create capacity.

## Scoped waits, stops, and recovery

| Condition | Required behavior |
|---|---|
| Closed session, stale dependency, uncertain decision timing, or insufficient buying power | Record a scoped wait; retry when the relevant event/evidence can change |
| Timeout, rate limit, provider error, incomplete acquisition, or temporary store contention | Use bounded retries and visible health; recover automatically after fresh complete evidence |
| Malformed or untrusted provider data | Quarantine the affected input and block affected entries; do not accept it or invent an accounting contradiction |
| Explicit manual entry stop | Preserve it until an explicit audited resume passes fresh readiness |
| Wrong account/store, conflicting fill or client identity, unexplained ownership, or proven accounting contradiction | Quarantine affected authority; expand to account scope when the fault cannot safely be bounded |

Ordinary clock/data/provider interruptions do **not** create a persistent global kill switch.
Recovery requires current quantity certification and the commissioned funds authority, fresh
timing and decision dependencies, guardian acknowledgement, and no in-scope manual stop or
unresolved integrity fault. Legacy exact-cash mode retains its complete-cash requirement. It appends
recovery evidence instead of deleting failure history. Elapsed time or repeated raw observations
cannot clear a genuine contradiction or an unrelated stop.

Protection, exits, and trustworthy outcome/learning work continue independently wherever their
own evidence and authority remain valid. Ambiguous ownership never authorizes blind selling.
Entry readiness is not a prerequisite for accounting over already durable evidence.

The decision-specific `PAPER_TIME_V2` contract retains the bounded ±2-second provider-relative
interval and uses monotonic time for elapsed durations. Freshness and session boundaries remain
strict; future bars and labels are not admitted by widening causality checks. Restart reacquires
proofs. Provider-relative timing is not independent certification of absolute UTC accuracy.

## Current funds without invented cash accounting

The approved `BROKER_PAPER_BALANCE_V1` policy is specific to hybrid PAPER entries and is
implemented on the successor branch. It uses immutable, source-bound evidence from stable broker account
and position reads with current open-order coverage to authorize sizing from actual current
cash and buying power. It preserves quantity certification and exact account/store/owner/
config/commission binding. Legacy `MODEL_ONLY_V1` keeps its strict exact-cash requirement.

The quantity proof and typed funds evidence must persist together. Validate a stable capture
within five seconds, age at most 120 seconds, current ledger generation, account restrictions
and no competing or unexplained open order at claim, execution gate, locked send and recovery/
resume. A changed ledger, source or balance needs fresh authority; no network call occurs in
the send transaction. Broker-PAPER authority requires the actual commissioned PAPER adapter;
synthetic fixture evidence cannot authorize it.

This policy retains `expected_cash=None`, `cash_complete=False`, unknown fees and null net
P&L when exact historical cash remains incomplete. It creates no independent cash activity,
balancing residual or invented `RECONCILED` status. Known cash/position discrepancies,
disputed sources, unknown sends, manual stops and other genuine integrity faults still block;
a balance receipt cannot revive a revoked exact-cash certificate. Guardian/outcomes do not
depend on entry-funds permission.

## Outcomes and daily learning

A round trip joins exact owned entry and exit fills, terminal-order evidence, protection,
flat reconciliation for that lot, and cost provenance. Actual net P&L is available only when
all required cost components are broker-reported or supported by an applicable contractual-zero
record. Otherwise retain gross P&L and `COSTS_PENDING`. Modeled economics remain separate.
Late fees create revisions and learning reconsideration without another trade sample; pending
corrections are excluded until reprocessing covers their evidence.

The baseline V2 learner predicts `owned-executed-net-return-v1` and retains its strict cost
and overlap rules. The successor adds `daily-paper-exploration-walk-forward-v3`, targeting
`owned-executed-paper-gross-return-v1`: exact executed gross P&L divided by entry notional.
A closed, owned, reconciled outcome may train with `COSTS_PENDING` only when
`FEE_UNAVAILABLE` is its sole remaining incompleteness and gross P&L is established. Fees,
cost status and null net remain unchanged. Gross predictions are before fees, not evidence
of net profitability. Both wins and losses train; untraded observations never gain labels.

1. **Bootstrap:** wait for ten completed exchange sessions from the commissioned learning
   origin, counting quiet sessions too. Calendar sessions are not inferred from elapsed days or
   the number of sessions containing trades.
2. **Daily consideration:** after each completed exchange session, consider all eligible,
   compatible retained history. No 60-session cutoff applies to the current learning contract.
   The latest ten completed sessions receive raw weight 2 and older sessions weight 1,
   normalized to mean 1 in preprocessing and ridge fitting.
3. **Causal procedure validation:** use five expanding past-only folds, reconstructing revisions
   available at each cutoff, with a 30-minute embargo and canonical protected-data exclusions
   before materialization. Episode floors remain: at least 30 training episodes across five
   earlier sessions and ten validation episodes across five later sessions.
   V3 retains overlapping feature windows within training/history; strict train-versus-validation
   chronology, actual label availability and purge/embargo remain. Pending corrections,
   quarantine, invalid ownership and protected or otherwise noncausal rows remain excluded.
4. **Bounded fit:** at most one candidate procedure per completed session, permanently charged
   before fitting, with all folds and final refit inside the existing worker budget. The fixed
   validation gates compare with training-only constants and the actual historical decision
   policy. The fixed ridge procedure does not search features, algorithms, risk, horizon or universe.
5. **All-history refit:** after procedure validation passes, refit on all current eligible
   history, including development-validation outcomes. The final model has procedure-validation
   evidence, not an independent held-out score after consuming those rows. Protected final
   holdouts remain excluded.
6. **Receipt and activation:** preserve parent/result model, input and excluded revisions,
   cutoff, validation, trial charge, reason, and activation reference. Insufficient, unchanged,
   or unreliable evidence records `NO_CHANGE`. Initial model use and unattended updates require
   the target-specific commissioned authority envelope: `DAILY_BOUNDED_V3` for V3,
   `DAILY_BOUNDED_V2` for V2, or `REVIEW_EACH_CANDIDATE`. Activation is atomic; a later
   actual decision must acknowledge the exact active model and learning receipt.

Ten sessions do not guarantee enough eligible outcomes for a fit, and a candidate need not pass.
Daily consideration still records its result; exploration continues with the incumbent when a
fit fails or a candidate is rejected. Preserve holding/exit policy
`collect-owned-30m-stop100-v1`, compatible history and each decision's original selection
provenance; version selection, target and learning procedure separately. Initial successor
commissioning starts from target-neutral COLLECT. Never reinterpret a net predictive model as
gross or broaden a V2 commission to cover V3. The successor is an economic change, not a
compatible executable-only update. Existing positions keep their entry model and protection plan.

## Baseline staging evidence and successor rollout

Implementation and isolated preparation are accepted.
[PR #289](https://github.com/jfhutchi/project_elrond/pull/289) merged on **October 15, 2026 at
01:50 UTC (October 14 EDT)**. The task capture at **October 15, 02:00 UTC** and broker snapshot at **01:44 UTC** show
functional deployment waiting:

- Prepared release: `ec15cbd8dc28a1f90adcb03b26669552eb94b7fba4fa6e208c7cf5d342baf645`.
- Configuration: `aa6c93bccdf4f28a5c1cfda1d94efd1ace09bc1c7e5fd10da3e6298cb68679e2`; target schema 36.
- Mako is constrained to `>=1.4.2,<2` and locked to 1.4.3. Its affected regression gate passed
  169 tests with 26 PostgreSQL-only skips; this is not a full-suite claim.
- The exact release verifier passed under the actual limited task identity after the authorized
  CurrentUser `RemoteSigned` change. The replacement tasks are registered disabled, with
  submission/entry clamps and no triggers.
- Backup copies migrated from schema 29 to 36 while preserving all original values in 59 data
  tables. The operating database remains unmigrated.
- An existing broker-accepted **IWM SELL of 0.169669 shares**, unfilled while the market is closed,
  blocks the required no-open-order commission condition. Original protection/deadline services
  and inherited ownership remain in place.

These facts are a dated evidence snapshot. After the existing exit becomes terminal, reconcile
its original IDs and refresh ownership, writers, and backups before the reviewed migration and
fresh commissioning sequence. Guardian and outcomes readiness must precede trader enablement.
The hashes and verifier above belong to the earlier XLE package. The successor requires its
own prepared manifest/configuration, integrated verification and target/funds-specific initial
commission; an existing commissioned store cannot simply reopen with changed economic config.
Use the handover to identify that successor when available. Old timestamps do not authorize a
later handover. Never restore an old trading database over newer broker activity.

Successor operational acceptance requires a broker-PAPER lifecycle joining cohort/selection,
decision, intent, owned fills, independent protected exit, quantity reconciliation, truthful
gross/cost/net evidence, learning consideration and a later model-attributed decision. V3
learning need not wait for a fee-only gap; an actual net-profit claim still requires proven
costs. Successive entries must demonstrate current-funds continuity without invented cash
activity or fee values. Synthetic integration and real-data
simulation are separate evidence lanes. The synthetic parameter-change witness does not require
genuine observations to produce a fit; `NO_CHANGE` can be the correct result. The replacement
does not yet establish deployed learning, completed broker-PAPER acceptance, or profitability.
The combined offline witness has passed. Main integration and operational acceptance remain
open; exact review and candidate-preparation status belong in the successor evidence package.
