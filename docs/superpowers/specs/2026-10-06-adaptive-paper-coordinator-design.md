# Adaptive PAPER coordinator on the Omen

Status: written specification for human review. Direction A was approved on
October 6, 2026: rebuild the adaptive PAPER coordinator while retaining tested
execution and safety infrastructure. This document does not authorize product
changes, new fits, an expanded deployed universe, model activation, or rollout.
Written-spec review precedes the implementation plan and execution-method review.

## 1. Outcome and scope

Elrond must research a bounded hypothesis, make naturally eligible PAPER trades,
protect and monitor those positions, attribute their outcomes, register controlled
learner updates, and evaluate subsequent decisions on fresh evidence. The Omen is
the execution host. Profitability is an empirical research outcome, not promised
software behavior. The user's 70–80% profitable-day aspiration is neither an
entry prerequisite nor an automatic route to live money.

This design covers one complete experiment coordinator and its necessary data,
ownership, evaluation, and dashboard interfaces. It does not rebuild the broker,
replace protection, add paid data, expand credentials, introduce an LLM trading
agent, or repair hardware. Existing strategies and grandfathered holdings remain
separately owned. No forced trades or profit-based manipulation of the sample.

## 2. Assessment and selected approach

The existing DIA pilot predicts a DIA-minus-SPY market return while buying DIA
outright. Its new raw-return shadows address target alignment but are observation
artifacts, not activated execution models. The tracking launcher journals matured
labels and attributable fills; it does not refit or replace the active model.
Automatic challenger fitting exists in a separate original minute-data workflow
with unavailable coverage. News research is shadow advice, disconnected from
learner decisions. Protective-owner exits cannot currently be fully attributed.
These are workflow and contract gaps, not a missing profitability switch.

The generic three-year benchmark-growth objective and turnover constraint also
do not describe the requested intraday PAPER experiment. Retain that mandate for
its original strategies; create an explicitly separate experiment contract rather
than silently rewriting the project objective or weakening portfolio controls.

Selected approach A rebuilds the small coordinator around the retained services.
Alternative B completes the current narrow DIA workflow with less engineering,
but leaves its opportunity and target mismatch unresolved. Alternative C starts
with a rules-only strategy and shadow learning; it is explainable but delays the
requested adaptive loop. A provides clear ownership without claiming an edge.

Historical counts and operational receipts are evidence of the assessment, not
acceptance of this design. In particular, market labels, simulated outcomes, and
broker trades must never share an unlabeled progress count.

## 3. Experiment contract frozen before fitting

Use the existing authorized IEX price and Alpaca news access. SINGLE_VENUE volume
cannot support consolidated-volume anomaly claims. SEC filings remain unavailable
unless an already approved identifying contact and legitimate company mapping
exist; their absence does not prevent the price/news shadow stages. No ETF-to-
constituent inference or invented company identity.

Proposed research universe: six to ten instruments selected from official
instrument metadata and operationally available coverage. Freeze the exact roster
before opening performance comparisons or fitting. Qualify on identity, tradable
PAPER metadata, timestamp completeness, freshness, calendar, and sufficiently
complete price panels, not attractive historical returns. Record every screened
instrument and rejection. Company instruments need exact provider/company identity;
ETFs keep fund-level news coverage labels. The roster is a research proposal until
the exact deployment manifest is approved; current active DIA authority is unchanged.

Use genuine same-session five-minute bars and the existing ten price features,
adapted only by versioned instrument identity. Seven completed bars supply the
30-minute lookback; a 35-minute warmup follows each actual exchange open. Required
bars must exist, the newest completed panel must be within the existing two-minute
freshness bound, and a full 30-minute exit horizon must remain before the actual
session close, including early closes. Missing bars are missing; do not forward
fill, substitute venues, or slide backward to manufacture eligibility.

Primary predictive target: the instrument's raw 30-minute return. Benchmark-relative
return is a separately labeled evaluation diagnostic. Compare a frozen constant
baseline with one regularized ridge model, alpha 1 and seed 0. No parameter search,
quadratic expansion, or news alpha is included in the first implementation.
Preserve the existing ten-basis-point round-trip cost floor and all stricter entry
gates. An economic contract must name spread, fees, and slippage assumptions; actual
execution observations can inform a separately registered successor contract,
never retrospectively improve an earlier report.

Check canonical protected-date declarations and trial burden before reading any
archive. April–September protected history remains protected after its calendar
end. Use only permitted history and matured prospective evidence. Require at least
20 valid training sessions and five chronological validation sessions with boundary
purging and no overlapping target windows across splits. If this is unavailable,
wait or terminate the experiment; do not relax the requirement to fit sooner.
Previously inspected validation is diagnostic, not untouched forward evidence.

Proposed new fit budget: six engine calls maximum—two initial baseline/ridge fits
and at most four ridge successors. Charge each permanently before invocation,
including failed fits, and add them to the existing canonical aggregate burden.
No budget reset or free retry. A successor is eligible only after five additional
valid completed sessions and sufficient newly matured labels to pass the unchanged
data contract. Retraining uses a frozen chronological split recipe, not whichever
split produces a favorable result. Insufficient evidence does not consume a fit.

## 4. Components and interfaces

Use one coordinator process with a durable experiment store and leased scheduled
wakeups. It owns research workflow state, not broker protection. Reuse existing
modules behind typed adapters; avoid another independent scheduler per phase.

| Component | Responsibility and output | Authority |
| --- | --- | --- |
| Coverage/observation adapter | Official identity, required bars, source and availability timestamps, coverage manifest | Read existing providers; append observations |
| Evidence resolver | Mature market labels for every valid forecast, including vetoed entries | Append labels; no orders |
| Research registrar | Immutable hypothesis, dates, costs, budget, trials, artifacts and split reports | Fit only precharged approved research |
| Decision coordinator | Versioned prediction, eligibility, thesis, gate results, selected action | Submit only through the existing PAPER gateway |
| Execution adapter | Idempotent intent, fills, protection handoff, exit ownership | Existing bounded PAPER authority only |
| Independent protection/deadline service | Hard stops, time exits, reconciliation and emergency handling | Retained existing safety authority |
| Outcome/lot resolver | Attributed fills, fees, residual inventory and economic outcomes | Append revisions; never invent ownership |
| Independent evaluator/read model | Daily equity, strategy contribution, forward sample and dashboard state | Read only, except durable close capture |

Every envelope carries schema version, experiment/decision IDs, source hashes,
instrument identity, feature cutoff, actual available/recorded timestamps, model
and code/config hashes, and parent evidence IDs. Each lifecycle transition is
append-only and has a deterministic idempotency key. A transactional outbox owns
order-intent dispatch and acknowledgement; restart cannot create a second entry.
Use existing broker client-order identity and reconciliation before any retry.
Concurrent wakeups acquire the experiment lease, while independent protection
continues without that lease or the coordinator process.

Core states: REGISTERED, WAITING_FOR_COVERAGE, OBSERVING, FIT_ELIGIBLE,
FIT_REGISTERED, CANDIDATE_SHADOW, ACTIVATION_REQUIRED, ACTIVE_PAPER,
EVALUATING, COMPLETED, INSUFFICIENT_EVIDENCE, REJECTED, and FAULTED. State includes
owner and a machine-readable reason; consent recorded, activation preparation,
missing data, market closure, and genuine permission required are distinct.

## 5. Trade lifecycle, risk and ownership

Preserve one new learner position at a time, maximum 5% position value, 0.25%
entry risk, 1% stop, 30-minute holding horizon, and all tighter account exposure,
cash, cost, reconciliation, clock, and kill-switch controls. Grandfathered holdings
remain with their original exit owner; they count toward aggregate account risk.
Do not liquidate or reset them, claim their returns as learner alpha, or add a
second owner to a held instrument. An unavailable slot or cash budget is a valid
veto, not justification to weaken a limit.

Decision flow: verified current observation → versioned forecast and thesis →
all existing gates → idempotent PAPER intent → broker acknowledgement/fill →
durable independent protection handoff. A partial entry receives protection on
the actual filled quantity. Uncertain submission blocks further entries and
reconciles; it never retries blindly. The independent deadline/stop owner acts
even if research, news, or coordinator stops.

Define a durable learner lot ID at entry. All learner protection and timed exits
carry that lot and originating decision ID, including exits initiated outside the
coordinator. Existing adapters must preserve these IDs through partial fills,
cancel/replace and recovery. If protective broker orders cannot carry metadata,
persist an immutable local broker-order-to-lot ownership link before acknowledgement
is treated as complete. Do not infer an old exit's ownership from ticker and time.
Mixed legacy inventory without unambiguous quantity ownership remains ineligible.

Final execution outcome requires terminal entry/exit records, matched quantities,
actual fees, zero owned residual inventory, and reconciliation after the final
fill. Unknown costs or ownership produce INCOMPLETE, not zero or estimated profit.
Corrections append new economic revisions with the original evidence retained.
Hard stops and time exits cannot be vetoed by a model's expectation of rebound.

## 6. Research and adaptation semantics

Collect market-return labels for all valid predictions, not just profitable or
executed trades. These supervised labels are distinct from selection-biased broker
episodes. Broker results measure execution and policy behavior; they do not replace
market labels or make paper fills representative of live liquidity.

A matured batch triggers the registered successor-fit eligibility check. The
successor consumes only evidence available before its training cutoff and is
evaluated on later, untouched observations. Record dataset identities, fit charge,
artifact, diagnostic comparison, and the subsequent shadow decision. This proves
that evidence reaches learning; it does not prove a profitable strategy.

Research registration, candidate creation, PAPER activation, and live deployment
are separate authorities. Existing permission is not automatic activation of an
unidentified successor. The first candidate and each active-model replacement
require an exact-artifact approval unless a later, separately reviewed delegation
defines a bounded promotion policy. The coordinator must produce a concrete approval
package automatically and continue shadow work while pending. No silent pause or
claim that the user must select a GPT model. Live routing stays forbidden regardless
of performance. Full active adaptation acceptance includes an explicitly authorized
successor activation and subsequent active decision; shadow-only learning is reported
as a partial milestone.

News integration initially supplies position-specific shadow context and journaled
buy/hold/exit thesis evidence. Record provider, exact instrument/company mapping,
publication, first-seen and retrieval times, stale/unavailable/empty distinctions,
and coverage limits. Decisions cannot consume later-retrieved news as though it
was known earlier. ETF news is not comprehensive constituent analysis. Filings
with day-only publication timestamps are not intraday evidence unless availability
is independently established. News cannot override hard controls or alter active
entry/exit strategy in this release; a news-driven policy requires a separate
preregistered comparison and activation review.

## 7. Honest performance and user-visible health

Keep four clearly labeled result types: market forecast errors, reference-mark
simulations with assumed costs, attributed PAPER trade P&L, and total PAPER account
equity change. Show separately the learner contribution, legacy contribution,
unattributed residual, exposure and cash-flow adjustment. Do not imply that a
model's positive market label earned dollars.

Calendar daily P&L uses reconciled consecutive exchange-session close equity,
realized plus unrealized mark-to-market, with verified deposits/withdrawals and
account-epoch continuity. Unknown cash flows, resets or missing prior close invalidate
the daily difference. Capture durable close marks after actual close; finalize
only when reconciliation and baseline checks pass. Late evidence creates an audited
revision, not a fabricated timely close. Never backfill unavailable history as zero.

Month grid: signed USD, green positive, red negative, neutral zero, explicit pending,
missing, holidays and weekends. Sidebar: cumulative valid net P&L, profitable,
losing and flat day counts, drawdown, period, valid sample size and coverage. The
profitable-day denominator is all valid completed observed trading days; flat days
are nonprofitable. Missing/invalid days are excluded and counted separately. Any
win/loss-only percentage must be separately named. Include independent evaluation
against the constant strategy and no-trade baseline on matched forward windows.

Operational health is separate from economic eligibility. Report RUNNING_WAITING
with reasons CLOSED_SESSION, WARMUP, INCOMPLETE_COVERAGE, BELOW_COST_FLOOR,
RISK_VETO or ACTIVATION_REQUIRED; FAULTED requires actual operational evidence.
Schedule expectation comes from the actual exchange calendar and early close.
No fresh entry is expected overnight, but coordinator health, outstanding-label
resolution, applicable reconciliation, and protection of held positions still have
their own deadlines. A healthy monitor cannot conceal a dead worker. Existing
protection scheduling semantics must be retained and verified rather than invented.

Every alert names component, evidence age, expected next event, owner and predefined
action. Deduplicate incidents and record acknowledgement/recovery without deleting
history. Clock failure gates new entries and identifies the root cause; a known
closed session is not a stale-entry emergency. Unknown receipt/task liveness remains
an explicit fault. New alert catalog fields require the existing route review.

## 8. Staged executable acceptance

1. **Contract and safety:** freeze identity/coverage roster, permitted dates,
   hypothesis, costs, trial budget, ownership and exact config. Deterministic tests
   reject live endpoints, protected reads, future evidence, duplicate charges,
   missing bars and unauthorized activation. No engine call before charge.
2. **Integrated shadow:** real authorized provider receipts feed complete decisions,
   valid vetoes, all-forecast label resolution and idempotent journals. Synthetic
   fault tests verify partial fills, restart, lease expiry, repeated dispatch,
   uncertain broker response, protective-owner exits, clock failure, provider
   staleness and early-close boundaries. Simulated test fills stay synthetic.
3. **Real PAPER execution:** after exact initial-artifact activation approval,
   observe at least one naturally eligible prospective broker fill, actual independent
   stop/deadline coverage, protected exit, reconciled lot/fee attribution and final
   outcome. A veto or forecast-only success cannot satisfy this milestone. Existing
   strategy protection must remain healthy throughout. No minimum trade quota.
4. **Learning closes the loop:** matured market evidence and the real episode reach
   their separate journals; a precharged eligible successor fit records lineage,
   fresh evaluation and a subsequent shadow decision. After exact successor
   approval, verify its subsequent active decision. It may validly veto entry;
   decision execution readiness and profitability are separate acceptance claims.
5. **Independent evaluation:** after 20 valid completed forward sessions, report
   net account and learner dollars, profitable/losing/flat days, drawdown, expectancy,
   turnover, sample dependence, baseline comparison and missing coverage. Overlapping
   instruments/windows are not independent trials. Confidence claims require an
   appropriate dependence-aware statistical protocol, not an arbitrary win count.

Review zero eligibility after five valid sessions; identify coverage, forecast,
cost, slot or risk cause. Do not force an order or loosen gates. If unresolved,
stop this registered experiment as insufficient opportunity and propose a new
charged hypothesis. Maximum evaluation window is 30 scheduled exchange sessions:
if fewer than 20 valid days or insufficient real trades remain, report INSUFFICIENT
EVIDENCE and stop new research work for this experiment, while continuing all
position protection. Safety faults immediately halt new entries under existing
controls. Positive short-run returns do not establish durable profitability.

Counts have separate definitions: a coverage-valid research session meets the
frozen observation contract, even if every entry is vetoed; a completed performance
day additionally has the reconciled, cash-flow-verified consecutive close marks.
Five-session eligibility/refit checks use the former, daily profitability uses the
latter. Count both and disclose missing days. Start the 30-session window at the
registered prospective evaluation start, not at the first profitable trade; fitting
or approval delays cannot silently reset it. A new hypothesis requires a new dated
registration with the previous stop reason and cumulative trial burden preserved.

## 9. Delivery phases, migration and rollback

Phase A implements the experiment store, typed adapters, immutable registration,
coverage contract, state machine and shadow evaluator. Phase B adds complete lot/
exit attribution and the transactional intent interface against existing PAPER
services. Phase C integrates bounded fitting and approval packages. Phase D deploys
the exact approved experiment, verifies the real loop and dashboard, and completes
the independent forward evaluation. These are manageable slices of one design;
each needs tests and evidence, not additional unrelated frameworks.

Prefer a separate additive experiment SQLite store and isolated release directory;
reuse the existing operational schema and typed interfaces. If durable exit linkage
cannot be expressed safely without an operational schema migration, stop and present
the exact reviewed migration, compatible writer/reader pins and maintenance request.
This specification does not approve an unbounded schema or protection outage.

Before runtime changes: consistent database backup/checkpoint with integrity test,
source/config hashes, task XML and active ownership inventory; exclude credential
files. Rehearse restoration and old/new reader compatibility on isolated copies.
Run new coordinator shadow-only first; then transfer exclusive entry ownership
with no open ambiguous submissions. Independent protection remains running. If a
pause becomes necessary, obtain bounded scope; do not exceed previously agreed
protection interruption merely because a deployment has begun.

Rollback disables new entries/coordinator activation, retains running protection,
restores compatible task/code/config pins and reads/reconciles current broker truth.
Never restore a pre-fill database over real later fills or delete journals/trial
charges. New-store rollback may leave additive evidence intact. Resolve in-flight
intents and owned positions before restoring an entry owner; fail closed on unknown
ownership. A rollback is complete only with fresh reconciliation/protection evidence.

## 10. Resource limits and remaining decisions

Omen CPU only: one fitting job at a time, six-call budget, bounded batches, no GPU
or per-tick paid AI. Proposed limits: two coordinator CPU workers maximum; 15-minute
wall limit per fit, timeout permanently charged; no overlapping fit/large backfill
jobs. Observe actual light-workload memory use before setting a hard cap in the
implementation plan; reject oversized panels rather than swapping indefinitely.
No stress tests on the unstable Omen. Provider reads use existing rate-limit/backoff
adapters; cache immutable receipts and request only the frozen roster/range. No
subscription, new credentials or billable hosted CI. Required repository protections
must still be respected; local validation cannot silently waive them.

Decisions remaining: human review of this specification; written implementation
plan and execution method; the exact coverage-qualified roster and fit registration;
then exact initial and successor activation/rollout packages. A materially necessary
schema migration, paid provider, SEC contact transmission, security mechanism or risk
change requires its own explicit scope. None is implied here. The approved direction
does not promise a profitable candidate or authorize a live-money switch.

## Source grounding

Repository contracts: `docs/five-minute-paper-experiment-20261001.md`,
`docs/five-minute-observation-producer.md`, `docs/trade-learning-shadow.md`,
`docs/paper-pilot-close-contract.md`, `docs/research-architecture.md`,
`config/economic-objective-v1.yaml`; implementation boundaries in
`learner/adaptive_research.py`, `learner/tracking_launch.py`,
`learner/trade_learning.py`, `learner/paper_runner.py`, and the existing news/
filing research adapters. Runtime observations informed the assessment but private
account values, holdings, logs and credential paths are intentionally absent.
