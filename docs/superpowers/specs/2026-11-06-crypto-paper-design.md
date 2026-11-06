# Crypto PAPER extension

The operator authorized connecting cryptocurrency trading to the existing Alpaca PAPER
engine, preserving U.S. equities and the authoritative ledger. International brokers, FX,
options, leverage, shorts and live trading are excluded. This authorization supersedes the
earlier research-only crypto roadmap. Strategy catalog research remains a separate deliverable.

## Measured starting point

- Source: `eed788b638e8554648325af1922defb79319da14`, `D:/Elrond/App`.
- Read-only account check on November 6: account ACTIVE, crypto ACTIVE, no account/trading
  block; 73 active tradable crypto assets. BTC/USD, ETH/USD and SOL/USD were returned with
  explicit minimum quantities and increments. Existing credentials work; none were exposed.
- Existing engine uses a stock-only discovery, IEX/SPY features, XNYS session checks and DAY
  orders. Offline crypto tests do not authorize or prove broker integration.
- Crypto fees can debit the credited asset and arrive after execution. Current equity cash
  rounding and raw buy-minus-sell ownership cannot simply be applied to crypto.
- Successful test-run scratch cleanup already exists and was verified: 17 tests passed,
  the synthetic root was removed, and the small report/log remained outside it.

## Approach and boundaries

Extend the existing broker, trader, Guardian and outcomes roles. No second engine, account,
database or scheduling service. An explicit entry-policy amendment binds crypto capability
and conservative-window defaults without changing historical core identities. Preserve old
serialized identities when new optional fields are absent.

Use broker-returned BTC/USD, ETH/USD and SOL/USD as a small initial spot universe; this is an
engineering choice based on verified tradability, not a user coin selection or an assertion of
profitability. Revalidate asset and account eligibility at acquisition and send. Round down to
broker increments, reject below-minimum orders, use supported crypto TIF, and never borrow.

Provider clock evidence remains independent of instrument sessions. Crypto has validated
24/7 market data and UTC market windows; equities retain their exchange calendar. All account
risk uses America/New_York dates. Shared decision boundaries must not drop one asset class.
Protective exits use actual owned/custodied quantities and continue outside entry windows.

Sleep mode is 22:00–06:00 America/New_York, including both DST transitions. Engineering defaults
must reduce per-entry size, available crypto slots and loss budget beneath existing ceilings.
They are PROJECT_DEFAULT values, never operator-selected or ratified. Exact values and tests
will be recorded with the implementation. A window transition never clears an entry stop,
forgives earlier losses, blocks protective exits or invents approval.

Actual asset/cash fee evidence and modeled costs remain separate. Missing fees never mean zero.
Do not allocate date-only account fees to a specific fill without evidence. Preserve gross
results, modeled cost-aware results and unknown actual net values separately. Existing equity
learner labels must not be relabelled; incompatible crypto observations need explicit provenance
and cannot silently train the equity model. No profitability or 60–70% win-rate claim is made.

## Release gates

Meaningful tests cover legacy identities/equities, account and asset refusal, session boundaries,
DST, quantity/minimum/TIF, freshness, duplicate/unknown sends, partial fills, fee denomination,
ownership, emergency stop and protective exits. Independent review precedes rollout. A bounded
PAPER integration check may prove plumbing but never proves profitability. Real broker evidence
must resolve custody/accounting uncertainties before unrestricted crypto activation.

Use a branch in the existing checkout, bounded agents and the existing D: test interpreter.
No production database test copies. Reuse established staged cutover/rollback and binding CAS;
verify only one authoritative writer and actual running release. No runtime changes occurred
during discovery. A dependency that cannot be verified remains an explicit release blocker.

## Added scope: contemporaneous external context

The operator subsequently added news, X and Reddit sentiment/context across existing equities,
funds and crypto. Reuse the existing news research and semantic pipelines. Audit configured
access and actual retained freshness before claiming a feed is connected; no paid commitments,
credential disclosure, posting, scraping around access controls or acceptance of source text as
instructions is authorized. Missing provider access must be explicit.

Persist decision-time context snapshots with source IDs/links, publication and first-observed
times, deduplication/entity mapping, transformation/prompt/model versions, coverage and missing
data. Distinguish information observed from inputs actually used by the decision. Existing
price-only exploratory trades remain price-only; do not invent news triggers retrospectively.
Link immutable decision provenance to subsequent cost-aware outcomes. Handle contradictory,
late, spam/manipulated and deleted material without backfilling unavailable future information.

Connect reproducible context features into a separate versioned candidate-learning path with
past-only joins, walk-forward/out-of-sample comparison against price-only baselines and realistic
costs. Existing model-promotion gates remain. Show authentic ingestion and candidate consumption,
not merely a dashboard toggle or synthetic demonstration. This is an additive deliverable;
crypto plumbing alone does not complete the expanded request.

The feedback path must also improve candidate pre-trade interpreters. Freeze fact/entity/event
and sentiment readings, expected direction/return, horizon and meaningful confidence with the
exact interpreter version. Attach later corrections and outcomes separately. Evaluate source
reliability, reading errors, predictive calibration, trading choices and execution costs as
different questions. A loss is not proof of a reading error, and a win does not validate it.
Allow inconclusive results. Eligible reviews must reach traceable interpreter candidate examples
and held-out/walk-forward evaluation, with promotion/rollback gates before active replacement.
