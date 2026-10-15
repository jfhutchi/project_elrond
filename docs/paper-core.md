# PAPER core: implemented design and acceptance

The implemented design replaces the operational path incrementally with three independent roles:
**trader**, **guardian**, and **outcomes with daily learning**. It retains the existing broker,
execution, SQLite, research-memory, and protected-data foundations. This overview summarizes the
[accepted specification](superpowers/specs/2026-10-13-paper-core-design.md) and the implementation
in [`src/quantbot/paper_core/`](../src/quantbot/paper_core/). Detailed deployment facts belong in
the [handover](evidence/paper-core/handover.md) and
[current Omen evidence](evidence/paper-core/omen-deployment/README.md).

**Latest request, pending implementation:** daily bounded PAPER exploration across different
stocks, learning from eligible wins and losses even when model confidence or an ordinary
no-signal decision would otherwise cause abstention. The prepared baseline selects **XLE with
one new position** and does not force daily trades. It does not yet implement that new request.
An attempted trade cannot guarantee a fill; daily learning cannot promise a better model each day.

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

## Scoped waits, stops, and recovery

| Condition | Required behavior |
|---|---|
| Closed session, stale dependency, uncertain decision timing, or insufficient buying power | Record a scoped wait; retry when the relevant event/evidence can change |
| Timeout, rate limit, provider error, incomplete acquisition, or temporary store contention | Use bounded retries and visible health; recover automatically after fresh complete evidence |
| Malformed or untrusted provider data | Quarantine the affected input and block affected entries; do not accept it or invent an accounting contradiction |
| Explicit manual entry stop | Preserve it until an explicit audited resume passes fresh readiness |
| Wrong account/store, conflicting fill or client identity, unexplained ownership, or proven accounting contradiction | Quarantine affected authority; expand to account scope when the fault cannot safely be bounded |

Ordinary clock/data/provider interruptions do **not** create a persistent global kill switch.
Recovery requires a new complete reconciliation proof, fresh timing and decision dependencies,
guardian acknowledgement, and no in-scope manual stop or unresolved integrity fault. It appends
recovery evidence instead of deleting failure history. Elapsed time or repeated raw observations
cannot clear a genuine contradiction or an unrelated stop.

Protection, exits, and trustworthy outcome/learning work continue independently wherever their
own evidence and authority remain valid. Ambiguous ownership never authorizes blind selling.
Entry readiness is not a prerequisite for accounting over already durable evidence.

The decision-specific `PAPER_TIME_V2` contract retains the bounded ±2-second provider-relative
interval and uses monotonic time for elapsed durations. Freshness and session boundaries remain
strict; future bars and labels are not admitted by widening causality checks. Restart reacquires
proofs. Provider-relative timing is not independent certification of absolute UTC accuracy.

## Outcomes and daily learning

A round trip joins exact owned entry and exit fills, terminal-order evidence, protection,
flat reconciliation for that lot, and cost provenance. Actual net P&L is available only when
all required cost components are broker-reported or supported by an applicable contractual-zero
record. Otherwise retain gross P&L and `COSTS_PENDING`. Modeled economics remain separate.
Late fees create revisions and learning reconsideration without another trade sample; pending
corrections are excluded until reprocessing covers their evidence.

The current learner uses one fixed regularized outcome predictor for realized, owned executed
net return under the declared feature, selection, stop, and deadline contracts. Eligible wins
and losses both belong in its history. It does not invent executed labels for untraded
observations or mix incompatible cohorts.

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
4. **Bounded fit:** at most one candidate procedure per completed session, permanently charged
   before fitting, with all folds and final refit inside the existing worker budget. The fixed
   validation gates compare with training-only constants and the actual historical decision
   policy. No feature, algorithm, risk, horizon, or universe search is authorized by this baseline.
5. **All-history refit:** after procedure validation passes, refit on all current eligible
   history, including development-validation outcomes. The final model has procedure-validation
   evidence, not an independent held-out score after consuming those rows. Protected final
   holdouts remain excluded.
6. **Receipt and activation:** preserve parent/result model, input and excluded revisions,
   cutoff, validation, trial charge, reason, and activation reference. Insufficient, unchanged,
   or unreliable evidence records `NO_CHANGE`. Initial model use and unattended updates require
   the commissioned authority envelope. Activation is atomic; a later actual decision must
   acknowledge the exact active model and learning receipt.

Ten sessions do not guarantee enough eligible outcomes for a fit, and a candidate need not pass.
In this implemented baseline, COLLECT is available only when commissioned and still obeys
natural entry eligibility; persistent PREDICT abstention does not automatically reenable it.
The new daily exploration request changes that selection policy and must receive a distinct,
attributable contract. Existing positions retain their entry model and protection plan. No
position reset, invented fill, or fabricated learning improvement establishes progress.

## Acceptance and rollout state

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
Use the current deployment package for exact paths and freshness windows; old timestamps do not
authorize a later handover. Never restore an old trading database over newer broker activity.

Baseline operational acceptance requires a broker-PAPER lifecycle joining observation, decision,
intent, owned fills, independent protected exit, reconciliation, proven costs, learning
consideration, and a later model-attributed decision. Synthetic integration and real-data
simulation are separate evidence lanes. The synthetic parameter-change witness does not require
genuine observations to produce a fit; `NO_CHANGE` can be the correct result. The replacement
does not yet establish deployed learning, completed broker-PAPER acceptance, or profitability.
Acceptance of the new daily multi-stock exploration behavior remains additional work.
