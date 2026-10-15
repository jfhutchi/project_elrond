# Current direction: the PAPER trading and learning core

The implemented foundation as of October 14, 2026 is the smaller [PAPER core](paper-core.md).
PR289 is merged to `main` at `7889f576`; functional rollout and natural
broker-PAPER acceptance remain incomplete. The [handover](evidence/paper-core/handover.md)
and [Omen deployment package](evidence/paper-core/omen-deployment/README.md) record the
actual prepared release, operating-state evidence and next prerequisites. Dated reports,
frozen objectives and earlier experiment records retain their original meaning.

## Goal and present capability

The latest user requirement is daily PAPER experience collection across different
stocks, including bounded exploration when a model has no confidence or positive signal,
followed by learning from both winning and losing outcomes. This requested behavior is
follow-on work: the prepared release binds **XLE** and its benchmark dependencies and
does not yet implement daily multi-stock collection. Its current wait/selection behavior
must not be presented as satisfying that requirement.

Evaluate real opportunities throughout each session, protect PAPER trades, reconcile
their outcomes and use compatible experience in later decisions. Broker/market failures
remain observable and retryable; fills and learning labels cannot be fabricated. Daily
learning consideration cannot promise a valid parameter change, improved performance or
a profitable day. The role, accounting and evidence contracts below describe the merged
foundation that the requested collection policy will extend.

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

## Learning from owned outcomes

The fixed ridge predictor uses ten causal features and the
`owned-executed-net-return-v1` target. Eligible wins and losses both contribute;
untraded observations do not acquire invented fills or executed labels. Previous
five-session and fixed-30-minute market-return models remain separate evidence.

After **ten completed exchange sessions, including quiet sessions**, consider learning
after every completed exchange session. Use all eligible compatible retained history,
with raw weight 2 for the latest ten completed sessions and 1 for older sessions,
normalized to mean 1. There is no 60-session history cutoff. Pending corrections,
unknown actual costs, incompatible contracts and protected data remain excluded.

Five expanding chronological validation folds use only earlier available revisions,
nonoverlapping windows and a 30-minute embargo. At least 30 training episodes across
five earlier sessions and ten validation episodes across five later sessions remain
required. At most one candidate procedure per completed session is charged before
fitting, with all folds and final refit sharing a 30-second child budget. Passing the
fixed validation gates permits an all-eligible-history refit; that final artifact has
procedure-validation evidence, not an untouched held-out score.

Insufficient, unchanged or unreliable evidence produces an immutable `NO_CHANGE` receipt
that retains the incumbent. Activation requires the exact commissioned authority envelope;
otherwise a candidate remains `AWAITING_ACTIVATION`. A later real decision must acknowledge
the learning receipt and active model. A model file alone does not complete the loop.

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
3. Observe a natural real-data decision, exactly-once intent, attributable PAPER
   fills, independent protected exit and reconciled flatness for that owned lot.
4. Establish actual cost completeness. Until then report gross P&L and
   `COSTS_PENDING`; broker net remains unknown. Modeled economics are separate and
   fill-price execution effects are never deducted twice.
5. Record daily learning consideration and a later decision using the identified
   active model/receipt. A genuine `NO_CHANGE` is valid; synthetic parameter-change
   evidence remains a separate demonstration.

The core keeps at most one new position, **50 bps (0.5%) position value**, 25 bps
(0.25%) entry risk, a 100 bps (1%) stop, a 30-minute horizon, at least 120 seconds
before close and a 500 bps buying-power reserve, preserving any tighter control.
PREDICT entry requires positive conservative expected net after a separately declared
incremental 10 bps stress reserve. That reserve is not another actual-fee deduction.
Inherited holdings remain in aggregate exposure and retain their original exit owner.
There is no LIVE route, account reset, forced liquidation or silent manual-stop clearance.
The requested daily exploration policy needs its own explicit implementation and
commissioned configuration; the current prepared envelope is not widened by this guide.

At the latest staged check (October 15, 01:44 UTC; October 14 Eastern), an existing
accepted IWM sell of 0.169669 shares remained unfilled during the closed market. It
blocks the no-open-order commissioning gate. No operating migration or commission
has occurred; new roles are disabled and legacy protection/deadline services continue.
Use the deployment package for exact pins and refreshed evidence before any next action.

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
