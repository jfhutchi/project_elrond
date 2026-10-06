# Five-minute intent dry-run

This source-only bridge diagnoses a frozen `five-minute-price-alpha-v1` DIA
observation. It does not construct a runtime, broker, execution coordinator,
domain order intent, deployment or promotion. It never fits a model or writes
to operational storage. Its hypothetical intent has no client order ID and
explicitly has no order or exit authority.

`evaluate` reconstructs the original forecast with its original model hash,
score, features and recording time. The caller must also reconstruct the
feature row from the original content-addressed provider snapshot. Missing
lineage, session, broker, ownership, sizing or veto inputs reject. The unchanged
fixed cost floor is 10 basis points, with a separate actual sizing/slippage/
commission check. The reference-age limit remains at most 120 seconds; the
five-minute bar interval does not relax it.

Existing `size_entry` and `evaluate_portfolio_vetoes` perform the arithmetic.
Every held position stays in aggregate risk. Entry risk and position value are
capped at the tighter of the original policy and 25/500 basis points, the
original aggregate position cap remains, and a stop wider than 100 basis points
rejects. Missing persisted stops are not bootstrapped. No legacy position gains
learner exit ownership; unknown ownership rejects, and one existing new learner
position rejects further entries.

`ReadOnlyStore` uses SQLite `mode=ro`, `query_only=ON` and read transactions. It
supports existing repository readers and the original deadline-supervisor gate.
`read_eligibility` reuses the original deployment/promotion check; it does not
require qualified/profitable paper evidence before PAPER_OBSERVATION, nor does
it create that observation-stage record. `read_sizing_inputs` uses existing
account, fill, equity and stop records. An old persisted snapshot is unsuitable
for an entry diagnosis; this does not reclassify a scheduled service as unhealthy.
Pending buys refuse rather than becoming fictitious zero-risk reservations.

The caller's separate journal records `ELIGIBILITY_DRY_RUN_NOT_TRADING_EVIDENCE`.
Intent keys bind the model, cutoff, symbol and unchanged proposed policy; repeating
that key rejects duplicate hypothetical entries. The observer's predictions,
outcomes and research declarations are never rewritten. A historical forecast
reviewed now retains its original timestamp and is correctly stale, rather than
being relabeled as a current decision.

This is not the executable five-minute order adapter. Broker restriction/clock
refresh, measured veto inputs and recorded policy matching, immutable inherited
ownership reconciliation, an identity-bound deadline handoff, and explicit
commissioning remain necessary before any PAPER submission integration. An
absent measurement remains unavailable, even when another gate already rejects.
Positive-score tests use synthetic data; they do not alter the real frozen models
or their original zero-selection validation result. Hosted validation/publication
and runtime activation are outside this source-only change.
