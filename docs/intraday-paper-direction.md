# Current direction: the PAPER trading and learning core

> **Runtime handover, October 15 at 08:13 Eastern:** both stores are now schema 36; upgraded dashboard, inherited protection and deadline services are running. The bounded deployment task waits for the original IWM exit before commissioning guardian/outcomes and enabling trader after fresh protection checks. Trading is not active at this capture. See the [measured runtime handover](evidence/paper-core/runtime-handover-2026-10-15/README.md).

The implemented foundation as of October 14, 2026 is the smaller [PAPER core](paper-core.md).
PR289 is merged to `main` at `7889f576`; functional rollout and natural
broker-PAPER acceptance remain incomplete. The [handover](evidence/paper-core/handover.md)
and [Omen deployment package](evidence/paper-core/omen-deployment/README.md) record the
actual prepared release, operating-state evidence and next prerequisites. Dated reports,
frozen objectives and earlier experiment records retain their original meaning.

## Goal and present capability

The [approved successor](superpowers/specs/2026-10-14-daily-paper-exploration-design.md)
makes daily PAPER experience collection explicit: use qualifying signals first, otherwise
make exploratory entries across stocks, and learn from both wins and losses. It is not
merged to main or commissioned. The successor is implemented and locally tested, including the combined trading-to-learning witness. The measured staged release remains
the older XLE configuration, not a verified successor deployment.

Evaluate real opportunities throughout each session, protect PAPER trades, reconcile
their outcomes and use compatible experience in later decisions. Broker/market failures
remain observable and retryable; fills and learning labels cannot be fabricated. Daily
learning consideration cannot promise a valid parameter change, improved performance or
a profitable day. The role, accounting and evidence contracts below describe the merged
foundation and locally implemented successor contracts.

Three independent processes share one authoritative operational SQLite store:

- **Trader** evaluates fresh five-minute boundaries and records BUY or WAIT decisions
  against one immutable active model. It does no fitting or optional research.
- **Guardian** recovers uncertain submissions, maintains certified lot ownership and
  owns risk-reducing exits for new-core lots. Its scheduling and restart are independent.
- **Outcomes** resolves fills, terminal orders, owned flatness and costs, then considers
  bounded learning. Durable outcome processing takes priority over fitting and continues
  during closed sessions and entry waits.

Routine provider, acquisition or clock failures produce scoped waits/retries, followed
by recovery from fresh acceptable evidence. They do not automatically latch a global
kill switch. Explicit manual entry stops and genuine integrity/ownership faults remain
enforced; an entry wait cannot disable independent protection or trustworthy outcomes.
The dashboard/exporter, news, Kronos and general research workers are optional views or
research facilities, not dependencies of this three-role path.

## Signal-first selection and daily collection

`SIGNAL_OR_EXPLORE_V1` uses AAPL, MSFT, AMZN, GOOGL, META, NVDA, JPM, JNJ, XOM and PG as
candidate stocks, with SPY as benchmark only. At each completed five-minute boundary,
acquire a bounded causal cohort; unavailable/inherited-held/ineligible members are recorded
without suppressing healthy members. Any valid activated prediction strictly above 10 bps
takes priority over exploration. Signals rank by current-session reservation count ascending,
prediction descending, prior-session count ascending and configured order. A used stock
with a qualifying signal beats unused stocks with only weak signals.

If no eligible signal qualifies, exploration ranks by current-session count, prior-session
count and configured order, retaining the actual zero/negative prediction and recording
`EXPLORATION`. COLLECT explores; invalid model authority does not become an exploratory
fallback. Claim and final send replay the captured cohort/selection evidence.

Six durable reservations are allowed per verified exchange session, with one concurrent
core lot. Completed and uncertain possible-send reservations count; only definitive
pre-send proof releases one. Semantic boundary identity does not change with acquisition
time, selected stock, model or release. Guardian marks and health follow the actual selected
or owned symbol, even after it leaves the candidate universe. Protection/outcomes continue
after the cap, and six reservations do not guarantee six fills. `MODEL_ONLY_V1` remains
available with its original contract.

## Versioned current-funds authority

The approved `BROKER_PAPER_BALANCE_V1` policy permits
hybrid PAPER sizing from fresh stable broker cash/buying-power evidence while historical
fill-net cash remains incomplete. Immutable typed source evidence, current open-order
coverage and quantity certification must share account/store/owner/config/commission and
ledger-generation bindings. Stable capture is within five seconds, age at most 120 seconds;
validate again at claim, execution gate, locked send and recovery/resume. A changed ledger,
source or balance requires fresh authority, without network work inside the send transaction.

Retain `expected_cash=None`, `cash_complete=False`, unknown fees and null net P&L. Do not
invent cash activity, balancing residuals or a `RECONCILED` status. Known discrepancies,
disputed sources, unknown sends, manual stops, competing/unexplained open orders and risk/
guardian/time failures still block. Legacy model-only mode keeps exact-cash requirements;
independent protection and outcomes do not require entry-funds permission.

## Learning from owned outcomes

The successor keeps ten causal features and fixed ridge but adds
`daily-paper-exploration-walk-forward-v3`, targeting `owned-executed-paper-gross-return-v1`:
actual executed gross P&L divided by entry notional. A closed owned reconciled outcome may
train with `COSTS_PENDING` only when `FEE_UNAVAILABLE` is its sole incompleteness and gross
P&L is established. Fees and net remain unknown. V2 retains `owned-executed-net-return-v1`
with strict cost completeness. Both wins and losses contribute; untraded observations
never gain fills or labels. Old market-return models remain separate evidence.

After **ten completed exchange sessions, including quiet sessions**, consider learning
after every completed exchange session. Use all eligible compatible retained history,
with raw weight 2 for the latest ten completed sessions and 1 for older sessions,
normalized to mean 1. There is no 60-session history cutoff. Pending corrections,
quarantine, invalid ownership, incompatible contracts and protected data remain excluded.

Five expanding chronological validation folds use only earlier available revisions and
strict train/validation chronology with a 30-minute purge/embargo. V3 keeps overlapping
feature windows within training/history; V2's earlier overlap rule remains. At least 30 training episodes across
five earlier sessions and ten validation episodes across five later sessions remain
required. At most one candidate procedure per completed session is charged before
fitting, with all folds and final refit sharing a 30-second child budget. Passing the
fixed validation gates permits an all-eligible-history refit; that final artifact has
procedure-validation evidence, not an untouched held-out score.

Insufficient, unchanged or unreliable evidence produces an immutable `NO_CHANGE` receipt
that retains the incumbent. Activation requires the exact commissioned authority envelope;
otherwise a candidate remains `AWAITING_ACTIVATION`. V3 uses `DAILY_BOUNDED_V3`; V2 retains
`DAILY_BOUNDED_V2`, with `REVIEW_EACH_CANDIDATE` for explicit approval. Initial successor
commissioning uses target-neutral COLLECT; never reinterpret a net model as gross. Keep
holding/exit `collect-owned-30m-stop100-v1` and original selection provenance while versioning
selection/procedure/target separately. Failed/rejected candidates do not stop exploration.
A later real decision must acknowledge the receipt/model; a model file alone is insufficient.

## What the old “12 trades a year” wording meant

The exact legacy wording is the comment **“12 round trips a year”** immediately
above **max_annual_turnover: 12** in
[economic-objective-v1.yaml](../config/economic-objective-v1.yaml). It was marked
PROPOSED and explicitly described as an agent's choice, not an operator rule.

The field is validated as a nonnegative integer and included in the frozen
economic-objective identity in
[research/mandate.py](../src/quantbot/research/mandate.py). Current source does
**not** read it as an execution limit, count twelve trades, or refuse a thirteenth
trade. There is no implemented annual twelve-trade ceiling. The comment also
does not provide a code-defined conversion between turnover and trade count:
traded notional, position size, one-way legs and round trips are different units.

The original v1 file remains unchanged, including its comment, because existing
evidence names that frozen objective. A future economic-objective change must
be a prospective successor version, not a revision of earlier results. This
clarification does not remove any actual cost, turnover, risk or promotion check
elsewhere in the system.

## Required real acceptance

1. Complete the reviewed operating migration and handover against fresh writer,
   account, order and ownership evidence. A prepared release or registered disabled
   task does not establish functional deployment.
2. Establish independent guardian/outcomes readiness before enabling the trader.
   Existing protection retains inherited lots until an explicit proven ownership
   transition; uncertain submissions are reconciled using their original IDs.
3. Observe a real signal or exploratory decision, exactly-once intent, attributable PAPER
   fills, independent protected exit and reconciled flatness for that owned lot.
4. Establish truthful gross/cost/net evidence. A V3 fee-only gap may permit gross learning
   while status stays `COSTS_PENDING` and broker net stays null. Actual net claims still
   require complete costs. Demonstrate successive entries with current-funds authority
   and no invented cash activity. Modeled economics remain separate; never double-deduct
   fill-price execution effects.
5. Record daily learning consideration and a later decision using the identified
   active model/receipt. A genuine `NO_CHANGE` is valid; synthetic parameter-change
   evidence remains a separate demonstration.

The core keeps at most one new position, **50 bps (0.5%) position value**, 25 bps
(0.25%) entry risk, a 100 bps (1%) stop, a 30-minute horizon, at least 120 seconds
before close and a 500 bps buying-power reserve, preserving any tighter control.
The 10 bps hurdle identifies qualifying signals in the hybrid policy; failing it selects
exploration when genuine entry gates permit. V3 predictions are before fees, not expected
verified net. The hurdle is not another actual-fee deduction or a calibrated profit probability.
Inherited holdings remain in aggregate exposure and retain their original exit owner.
There is no LIVE route, account reset, forced liquidation or silent manual-stop clearance.
The successor needs completed integrated verification, a distinct prepared configuration
and target/funds-specific commission; it is an economic change, not a code-only transition.

At the latest staged check (October 15, 01:44 UTC; October 14 Eastern), an existing
accepted IWM sell of 0.169669 shares remained unfilled during the closed market. It
blocks the no-open-order commissioning gate. No operating migration or commission
has occurred; new roles are disabled and legacy protection/deadline services continue.
These are baseline package facts. Use the handover for successor preparation status and
fresh evidence before any next action; old XLE pins do not commission hybrid/V3 authority.

## Performance and dashboard truth

The requested 70–80% profitable-day rate is aspirational, not a prerequisite for
every experimental PAPER observation and not a profitability promise. Evaluate
daily reconciled close-to-close equity change, including realized/unrealized
marks and broker costs, less verified external cash flow, within the same account
epoch. Missing closes and reset-distorted comparisons are excluded rather than
invented as zeros. Flat days count in the denominator of all valid completed
observed trading days; a win/loss-only ratio must be separately labeled.

Report net P&L/returns, sample size and period, flat/losing/profitable counts,
drawdown, expectancy and coverage alongside the percentage. The month grid uses
signed green +USD, red −USD, neutral zero and explicit pending/missing/invalid
states, with authoritative exchange days and early closes. Process liveness,
stored forecasts, market labels and real trading outcomes remain distinct.

Start with [PAPER core](paper-core.md), [implementation handover](evidence/paper-core/handover.md),
[current Omen staging](evidence/paper-core/omen-deployment/README.md) and
[safe dashboard access](dashboard-access.md). The [adaptive runbook](adaptive-paper-runbook.md)
and [acceptance history](adaptive-paper-acceptance.md) document the retained legacy path;
they are not the replacement role startup recipe. Verify pins and freshness through
actual runtime receipts; a merged checkout is not deployment evidence.
